"""批量观看问题修复：显式绑定、一对多拆分、确定性时间与局部回退。

设计记录 D05/D08-D14/D22/D24/D27（见 docs/dev/subtitle-postprocessing-design-record.md）：
- 响应片段显式绑定 ``problem_id`` + ``output_index``，不能按数组位置推断归属；
  一个输入段可产生多个输出段，原文片段按 ``output_index`` 顺序拼接后与输入
  原文等价（空白重排允许，非空白字符不增删改，D05/D12）。
- 输出时间由确定性规则决定：按输出片段的阅读负荷比例分配原 cue 时长（D24）；
  片段数受最短显示时长约束，时长不足时该段候选整体拒绝并反馈。
- 每轮候选拼回完整字幕后重新验收（结构 / 原文等价 / 语义 / 绝对长度 /
  阅读速度）；只有仍未解决的问题进入下一轮（D09/D27）。
- 首次提交属于正常流程不计入重试；首次失败后最多 ``DEFAULT_BUSINESS_RETRIES``
  次业务修复重试（D09）；网络 / 限流由 gateway 传输重试独立兜底，传输失败
  轮不消耗业务重试，连续 ``MAX_TRANSPORT_FAILURE_ROUNDS`` 轮传输失败停止循环。
- 业务重试耗尽、重复候选或区域状态重复时，只回退该问题区域的初版未拆分
  快照（D13，区域 = 修复主体的初版段区间），其他成功区域保留；局部未解决
  不阻断下游（D14），模块级异常仍上抛由调用方整体回退。
- 回退快照取修复入口（确定性规则阶段后）的 working 字幕，不取后处理入口
  的初版字幕：确定性产物是已完整验收的最后状态（D10「不取某次未完整验收
  的中间结果」的意图），回退到更早状态会复活用户显式开启的确定性修正。

修复主体沿用 planning（票 04）：主体 + 边界上下文分开表示，上下文不作为
修改目标；``boundary_context_radius`` 沿用上游默认值（D18，完整接线见票 06）。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

import json_repair

from ..asr.asr_data import ASRData, ASRDataSeg
from ..llm import LLMGateway, LLMMessage, LLMRequest
from ..llm.utility import borrow_utility_gateway
from ..prompts import get_prompt
from ..subtitle.io import clone_subtitle_data
from ..utils.logger import setup_logger
from ..utils.text_utils import is_mainly_cjk
from .config import SINGLE_LINE, PostprocessConfig
from .planning import (
    DEFAULT_BOUNDARY_CONTEXT_RADIUS,
    BoundaryContext,
    PlanProblem,
    RepairBatch,
    RepairSubject,
    plan_repair_batches,
    problems_from_viewing,
)
from .report import QualityReport
from .translation import (
    RepairFlow,
    TranslationExecutionSnapshot,
    resolve_repair_flow,
    role_label,
)
from .viewing import (
    char_count,
    effective_length_limit,
    scan_viewing_lengths,
    sides_for_layout,
    weighted_length,
)

if TYPE_CHECKING:
    from ..entities import SubtitleLayoutEnum
    from ..llm import LLMModelProfile

logger = setup_logger("postprocess.repair")

# D09：首次提交不计入重试；首次失败后最多 4 次业务修复重试。
DEFAULT_BUSINESS_RETRIES = 4
# 单次请求内一个输入段允许的最大输出片段数上界（防病态拆分）。
MAX_FRAGMENTS_CAP = 8
# 连续传输失败轮数的停止阈值：网络持续不可用时停止循环并进入报告（D09）。
MAX_TRANSPORT_FAILURE_ROUNDS = 2
# 修复循环轮数安全上界：每个问题至多 1+4 次请求后封闭，超出即异常路径。
MAX_ROUNDS = 16

# 问题稳定身份：working 段序会因拆分漂移，跨轮计数一律用
# (初版段序, 显示侧, 问题类别)；problem_id 只在一次请求内对模型显式绑定。
ProblemIdentity = Tuple[int, str, str]


def select_repair_flow(
    snapshot: Optional["TranslationExecutionSnapshot"],
    profile: Optional["LLMModelProfile"],
    profile_resolver=None,
) -> RepairFlow:
    """把快照 / 显式备用角色解析为修复方式（票 06，D07/D15）。

    快照优先（完整 workflow 冻结的任务翻译方式）；无快照但有显式
    ``profile`` 时按普通 LLM 方式修复（显式备用输入，不静默升级）；
    两者皆无时仅报告。``report_only`` 的原因由调用方写入警告与报告。
    """
    if snapshot is not None:
        return resolve_repair_flow(snapshot, profile_resolver=profile_resolver)
    if profile is not None:
        # 显式备用翻译角色：普通 LLM 方式，不自动增加高级校对（D07）。
        return RepairFlow(
            "main",
            "无翻译执行快照，使用显式备用翻译角色按普通方式修复",
            main_profile=profile,
        )
    return RepairFlow("report_only", "缺少翻译执行快照，无法自动重译")


def _compact(text: str) -> str:
    """原文等价比较基：剔除全部空白后按字符序比较（D05 拆分边界空白重排允许）。"""
    return "".join(text.split())


def _clone_seg(seg: ASRDataSeg) -> ASRDataSeg:
    return ASRDataSeg(seg.text, seg.start_time, seg.end_time, seg.translated_text)


def _fingerprint(payload: Any) -> str:
    """候选 / 区域状态的稳定指纹（重复检测，D27）。"""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@dataclass
class RegionRollback:
    """一次区域级回退记录（D13）：恢复的初版段区间与原因。"""

    initial_indices: Tuple[int, ...]
    reason: str


@dataclass
class RepairSummary:
    """修复循环的可见结果状态（报告 / 警告 / 下游状态消费）。"""

    rounds: int = 0
    requests: int = 0
    spliced_fragments: int = 0
    resolved_problem_count: int = 0
    rollbacks: List[RegionRollback] = field(default_factory=list)
    unplannable_subjects: int = 0
    warnings: List[str] = field(default_factory=list)
    # 容量规划观测（票 03）：实际发出的批次容量口径，供报告 / 基准
    # 记录请求数、估计 token 与各收缩原因（spec 第 7 条：不声称真实
    # 模型吞吐证据——这里只有规划侧估计值）。
    planned_requests: int = 0
    """规划批次总数（含收缩后重规划的轮次内累加）。"""
    planned_input_tokens: int = 0
    """规划批次输入估计之和（完整请求口径，票 03）。"""
    planned_output_reserve_tokens: int = 0
    """规划批次输出预留之和（一对多输出 + 协议开销）。"""
    planned_subjects: int = 0
    """规划覆盖的修复主体数（主体计数与段数 / 问题数分列，票 03）。"""
    planned_problems: int = 0
    """规划覆盖的问题数（含同段多问题；与主体数分开口径）。"""
    shrunk_subject_batches: int = 0
    """主体数量因容量收缩的批次数（先减主体，D26 顺序第一级）。"""
    shrunk_context_batches: int = 0
    """边界上下文因容量收缩的批次数（单主体后减上下文，第二级）。"""
    # 修复方式选择（票 06）：翻译方式与角色身份进入报告与任务状态，
    # 便于核对实际行为（D15「翻译方式选择和资产身份进入报告」）。
    translation_method: str = ""
    """任务开始时冻结的翻译方式（single_llm / enhanced_llm / non_llm；空=未知）。"""
    flow_mode: str = ""
    """实际修复方式：main_review / main / report_only。"""
    main_role: str = ""
    """主翻译角色身份（profile_id / model；仅报告用，无连接机密）。"""
    review_role: str = ""
    """高级校对角色身份（仅 main_review 方式非空）。"""
    boundary_context_radius: int = DEFAULT_BOUNDARY_CONTEXT_RADIUS
    """本轮实际使用的边界上下文半径（默认取快照中的上游设置，D18）。"""
    review_corrections: int = 0
    """高级校对复校实际修正的译文处数（仅 main_review 方式计数）。"""


class _WorkingState:
    """working 字幕 + 初版来源追踪。

    ``origin[i]`` 是 working 第 i 段来自初版快照的段序；拆分产生的片段
    继承其输入段的 origin。区域回退以初版段为原子单位（D13）：恢复快照段
    并移除它的全部现役片段；快照对象只读，写回 working 一律克隆。
    """

    def __init__(self, snapshot: ASRData) -> None:
        self.snapshot = snapshot
        self.segments: List[ASRDataSeg] = [_clone_seg(seg) for seg in snapshot.segments]
        self.origin: List[int] = list(range(len(snapshot.segments)))

    def as_data(self) -> ASRData:
        return ASRData([_clone_seg(seg) for seg in self.segments])

    def initial_indices(self, start: int, end: int) -> Tuple[int, ...]:
        """working 区间 [start, end) 覆盖的初版段区间（区域身份，跨轮稳定）。"""
        return tuple(dict.fromkeys(self.origin[start:end]))

    def splice(self, start: int, end: int, replacements: List[List[ASRDataSeg]]) -> int:
        """把 working[start:end) 每段替换为其输出片段列表，返回新片段数。"""
        assert len(replacements) == end - start
        new_segments: List[ASRDataSeg] = []
        new_origin: List[int] = []
        added = 0
        for offset, fragments in enumerate(replacements):
            source_origin = self.origin[start + offset]
            for fragment in fragments:
                new_segments.append(_clone_seg(fragment))
                new_origin.append(source_origin)
                added += 1
        self.segments = self.segments[:start] + new_segments + self.segments[end:]
        self.origin = self.origin[:start] + new_origin + self.origin[end:]
        return added

    def rollback(self, initial_indices: Tuple[int, ...]) -> bool:
        """区域回退（D13）：恢复初版未拆分段，移除其全部现役片段。"""
        wanted = set(initial_indices)
        lo = next((i for i, origin in enumerate(self.origin) if origin in wanted), None)
        if lo is None:
            return False
        hi = lo
        while hi < len(self.origin) and self.origin[hi] in wanted:
            hi += 1
        if {self.origin[i] for i in range(lo, hi)} != wanted:
            return False  # 区域片段不连续或不完整：拒绝部分回退。
        restored = [_clone_seg(self.snapshot.segments[k]) for k in initial_indices]
        self.segments = self.segments[:lo] + restored + self.segments[hi:]
        self.origin = self.origin[:lo] + list(initial_indices) + self.origin[hi:]
        return True

    def region_state(self, initial_indices: Tuple[int, ...]) -> List[List[Any]]:
        """区域当前状态指纹载荷（重复状态检测，D27）。"""
        wanted = set(initial_indices)
        return [
            [origin, seg.start_time, seg.end_time, seg.text, seg.translated_text]
            for origin, seg in zip(self.origin, self.segments)
            if origin in wanted
        ]


def _max_fragments(cfg: PostprocessConfig, duration_ms: int) -> int:
    """单段允许的输出片段数：受最短显示时长约束（D24），封顶防病态拆分。"""
    if duration_ms <= 0:
        return 1
    return max(1, min(MAX_FRAGMENTS_CAP, duration_ms // max(1, cfg.min_duration_ms)))


def _allocate_times(
    seg_start: int, seg_end: int, loads: List[float], cfg: PostprocessConfig
) -> Optional[List[Tuple[int, int]]]:
    """按阅读负荷比例分配原 cue 时长（D24，确定性，模型不参与）。

    每片段下限 ``min(duration // count, min_duration_ms)``（调用方已用
    ``_max_fragments`` 保证可容纳）；份额按负荷比例取整后，差额一次性
    加到最大份额 / 从大于下限的份额自最大起扣，无法收敛时返回 None。
    """
    count = len(loads)
    if count <= 0:
        return None
    duration = seg_end - seg_start
    if count == 1 or duration <= 0:
        return [(seg_start, seg_end)] * count
    bound = max(1, min(cfg.min_duration_ms, duration // count))
    total = sum(loads)
    if total <= 0:
        loads = [1.0] * count
        total = float(count)
    spans = [
        max(bound, int(math.floor(duration * load / total))) for load in loads
    ]
    diff = duration - sum(spans)
    if diff > 0:
        spans[spans.index(max(spans))] += diff
    elif diff < 0:
        need = -diff
        for index in sorted(range(count), key=lambda i: -spans[i]):
            if need <= 0:
                break
            take = min(need, spans[index] - bound)
            spans[index] -= take
            need -= take
        if need > 0:
            return None
    if any(span < bound for span in spans) or sum(spans) != duration:
        return None
    times: List[Tuple[int, int]] = []
    cursor = seg_start
    for span in spans:
        times.append((cursor, cursor + span))
        cursor += span
    return times


def _active_single_line_sides(
    cfg: PostprocessConfig, layout: "SubtitleLayoutEnum"
) -> Tuple[str, ...]:
    """布局内仍是单行限长的显示侧（自动换行侧跳过长度与行数约束，D19）。"""
    return tuple(
        side
        for side in sides_for_layout(layout)
        if cfg.display_mode_for(side) == SINGLE_LINE
    )


def _context_entry(index: int, seg: ASRDataSeg) -> Dict[str, Any]:
    """轮次快照坐标上的一个边界上下文条目（主修复 / 复校载荷共用形状）。"""
    return {"id": index, "text": seg.text, "translated": seg.translated_text}


def _build_payload(
    round_snapshot: ASRData,
    cfg: PostprocessConfig,
    batch: "RepairBatch",
    segment_problems: Dict[int, List[PlanProblem]],
    feedback: List[str],
) -> Dict[str, Any]:
    """构造一次批量修复请求载荷（主体 / 上下文 / 问题显式分开表示）。

    载荷只读 ``round_snapshot``（修复轮次快照，ADR-0021）：同轮各批的
    主体与边界上下文共享同一版本，邻批已归并的拆分 / 校对结果不进入
    本批载荷。``id`` 是轮次快照内的段序。
    """

    segments = round_snapshot.segments

    def _segment_payload(index: int) -> Dict[str, Any]:
        seg = segments[index]
        problems = segment_problems.get(index, [])
        return {
            "id": index,
            "text": seg.text,
            "translated": seg.translated_text,
            "duration_ms": seg.end_time - seg.start_time,
            "max_fragments": _max_fragments(cfg, seg.end_time - seg.start_time),
            "problem_ids": [p.problem_id for p in problems],
        }

    return {
        "limits": {
            "absolute_cjk": cfg.single_line_absolute_cjk,
            "absolute_latin": cfg.single_line_absolute_latin,
            "target_cjk": cfg.single_line_target_cjk,
            "target_latin": cfg.single_line_target_latin,
        },
        "boundary_context": [
            _context_entry(index, segments[index])
            for start, end in batch.context.spans
            for index in range(start, end)
        ],
        "repair_subjects": [
            {
                "segments": [
                    _segment_payload(index)
                    for index in range(subject.start_index, subject.end_index)
                ],
                "problems": [
                    {"id": pid, "reason": reason}
                    for pid, reason in zip(subject.problem_ids, subject.reasons)
                ],
            }
            for subject in batch.subjects
        ],
        "feedback": feedback,
    }


# 主修复输出比例（票 03，对齐 ADR-0019 比例式输出预留）：一个输入段的
# 合法输出覆盖其原文 + 译文两侧（一对多拆分原译对应），再加 JSON 协议
# 开销。比例值保守取 1.5（高于上游翻译的 1.2/1.3：修复候选同时携带
# original / translated 双侧文本与绑定键）。
REPAIR_OUTPUT_INPUT_RATIO = 1.5
REPAIR_OUTPUT_PROTOCOL_OVERHEAD_TOKENS = 256


def _estimate_request_input(
    round_snapshot: ASRData,
    cfg: PostprocessConfig,
    subjects: Sequence["RepairSubject"],
    context: "BoundaryContext",
    segment_problems: Dict[int, List[PlanProblem]],
    feedback: List[str],
    guidance: str,
) -> int:
    """估算一批主修复请求的完整输入 token（票 03 容量口径）。

    覆盖完整序列化请求（spec「主修复与高级校对的容量规划」）：直接
    复用 ``_request_messages`` 构造与实际请求逐字一致的消息（系统
    提示词 + guidance 块 + ``_build_payload`` 载荷：limits / 主译文本 /
    问题 / 上下文 / feedback 协议开销），不只估主体裸文本。估算与
    实际请求共用同一构造（单一来源），协议变化不会静默漂移。
    """
    from ..translate.enhanced.token_planner import estimate_tokens

    batch = RepairBatch(
        subjects=list(subjects),
        context=context,
    )
    payload = _build_payload(round_snapshot, cfg, batch, segment_problems, feedback)
    messages = _request_messages(payload, guidance)
    return estimate_tokens("\n".join(message.content for message in messages))


def _estimate_output_reserve(
    round_snapshot: ASRData,
    cfg: PostprocessConfig,
    subjects: Sequence["RepairSubject"],
    segment_problems: Dict[int, List[PlanProblem]],
    *,
    work_context_tokens: Optional[int] = None,
    max_output_tokens: Optional[int] = None,
) -> int:
    """估算一批主修复请求的输出预留（票 03，一对多输出 + 协议开销）。

    比例式预留（ADR-0019）：主体输入载荷 × 输出比 + 协议开销，
    受工作上下文与用户请求输出上限钳制（不自动抬升用户配置）。
    主体输入按载荷编码（含文本 / 问题 / 协议），不只按裸文本。
    """
    from ..translate.enhanced.token_planner import estimate_tokens

    batch = RepairBatch(
        subjects=list(subjects),
        context=BoundaryContext(),
    )
    payload = _build_payload(round_snapshot, cfg, batch, segment_problems, [])
    subject_input = estimate_tokens(
        json.dumps(payload["repair_subjects"], ensure_ascii=False)
    )
    reserve = int(subject_input * REPAIR_OUTPUT_INPUT_RATIO) + (
        REPAIR_OUTPUT_PROTOCOL_OVERHEAD_TOKENS * max(1, len(subjects))
    )
    if work_context_tokens is not None:
        reserve = min(reserve, work_context_tokens - 1)
    if max_output_tokens is not None:
        reserve = min(reserve, max_output_tokens)
    return max(reserve, 1)


def guidance_block(guidance: str, label: str = "translation") -> str:
    """任务冻结的原翻译提示配置块（主修复 / 复校消息构造共用形状）。"""
    if guidance.strip():
        return f"Custom {label} guidance from the original task:\n" + guidance.strip() + "\n\n"
    return ""


def _request_messages(payload: Dict[str, Any], guidance: str = "") -> List[LLMMessage]:
    """构造一次请求消息；``guidance`` 是任务冻结的原翻译提示配置（可空）。"""
    system_prompt = get_prompt("optimize/viewing_repair")
    return [
        LLMMessage("system", system_prompt),
        LLMMessage(
            "user",
            guidance_block(guidance)
            + "Repair the following subtitle viewing problems:\n<input>"
            + json.dumps(payload, ensure_ascii=False)
            + "</input>",
        ),
    ]


def _review_request_messages(payload: Dict[str, Any], guidance: str = "") -> List[LLMMessage]:
    """构造高级校对复校请求消息（增强流程第二段，票 06）。"""
    system_prompt = get_prompt("optimize/viewing_repair_review")
    return [
        LLMMessage("system", system_prompt),
        LLMMessage(
            "user",
            guidance_block(guidance, label="review")
            + "Review the following proposed repair fragments:\n<input>"
            + json.dumps(payload, ensure_ascii=False)
            + "</input>",
        ),
    ]


def _parse_review_response(
    text: str, proposals: Dict[Tuple[str, int], Dict[str, Any]]
) -> Tuple[Optional[str], Dict[Tuple[str, int], str]]:
    """解析高级校对复校响应并显式绑定 (problem_id, output_index)。

    返回 (整体致命错误, 绑定键 → 校订译文)。未知绑定或协议违规整体拒绝，
    保留主翻译候选不变（保守默认，prompt 规则 7）。
    """
    try:
        parsed = json_repair.loads(text)
    except Exception as exc:  # noqa: BLE001 —— 模型输出垃圾按业务失败处理
        return f"复校响应不是有效 JSON: {exc}", {}
    if not isinstance(parsed, dict) or not isinstance(parsed.get("reviews"), list):
        return "复校响应缺少 reviews 数组", {}
    corrections: Dict[Tuple[str, int], str] = {}
    for entry in parsed["reviews"]:
        if not isinstance(entry, dict):
            return "reviews 含非对象条目", {}
        problem_id = entry.get("problem_id")
        output_index = entry.get("output_index")
        translated = entry.get("translated")
        if not isinstance(problem_id, str) or type(output_index) is not int:
            return "复校绑定必须是 problem_id 字符串 + output_index 整数", {}
        if output_index < 0:
            return "复校 output_index 必须是非负整数", {}
        if not isinstance(translated, str):
            return "复校 translated 必须是字符串", {}
        key = (problem_id, output_index)
        if key not in proposals:
            return f"未知复校绑定 problem_id={problem_id!r}", {}
        corrections[key] = translated
    return None, corrections


def _review_pass(
    runtime: LLMGateway,
    review_profile: "LLMModelProfile",
    state: "_WorkingState",
    round_snapshot: ASRData,
    cfg: PostprocessConfig,
    layout: "SubtitleLayoutEnum",
    *,
    region_start: int,
    region_length: int,
    accepted_entries: Dict[int, List[Dict[str, Any]]],
    replacements: List[List[ASRDataSeg]],
    segment_problems: Dict[int, List[PlanProblem]],
    radius: int,
    guidance: str = "",
    cancelled: Optional[Callable[[], bool]] = None,
) -> Tuple[int, List[str]]:
    """对一个已拼接主体执行高级校对复校（增强流程第二段，票 06）。

    ``accepted_entries`` 按主体内偏移携带主翻译已接受候选的显式绑定；
    ``replacements`` 提供主体内各输入段的片段数（复校定位用）。
    只复校译文侧；原文侧与时间由主翻译候选保证（D05/D24）。
    复校上下文（ADR-0021）以 ``round_snapshot``（修复轮次快照）为基：
    主体区间叠加本次合法候选，主体外一律是轮次快照内容——邻批
    （或同批其他主体）刚归并的拆分结果不进入本主体复校上下文。
    ``region_length`` 是拼接后片段数（复校定位坐标）；
    上下文窗口在轮次快照坐标上以 ``len(replacements)``（主体输入段数）
    为界，不随候选拆分片段数扩张。
    返回 (修正数, 警告列表)；复校失败不回退已接受候选（保守保留
    主翻译，prompt 规则 7）。
    """
    if not accepted_entries or region_length <= 0:
        return 0, []
    # 主体内各输入段在拼接后区域中的起始偏移（含透传段的 1 片）。
    prefix: List[int] = []
    cursor = 0
    for fragments in replacements:
        prefix.append(cursor)
        cursor += len(fragments)
    proposals: List[Dict[str, Any]] = []
    bindings: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for offset in sorted(accepted_entries):
        input_index = region_start + offset
        problems = segment_problems.get(input_index, [])
        if not problems:
            continue
        problem_id = problems[0].problem_id
        for position, entry in enumerate(accepted_entries[offset]):
            proposal = {
                "problem_id": problem_id,
                "output_index": entry["output_index"],
                "region_position": prefix[offset] + position,
                "original": entry["original"],
                "translated": entry["translated"],
            }
            proposals.append(proposal)
            bindings[(problem_id, entry["output_index"])] = proposal
    if not proposals:
        return 0, []

    # 复校上下文视图（轮次快照语义，ADR-0021）：以修复轮次快照为基，
    # 只在主体区间内叠加本次已接受候选（拼接后的片段文本 / 译文）。
    # 主体外相邻段保持轮次快照内容：同轮邻批 / 其他主体的归并
    # 结果不进入本主体的复校上下文（完成顺序无关）。
    candidate_view: Dict[int, Dict[str, Any]] = {}
    for offset in sorted(accepted_entries):
        fragments = replacements[offset]
        index = region_start + offset
        candidate_view[index] = {
            "id": index,
            "text": " ".join(fragment.text for fragment in fragments),
            "translated": " ".join(fragment.translated_text for fragment in fragments),
        }

    def _context_view(index: int) -> Dict[str, Any]:
        entry = candidate_view.get(index)
        if entry is not None:
            return entry
        return _context_entry(index, round_snapshot.segments[index])

    # 上下文窗口在轮次快照坐标上取主体输入段数（``len(replacements)``）
    # 为界：上文 [start-radius, start)，下文 [start+span, start+span+radius)。
    subject_span = len(replacements)
    context: List[Dict[str, Any]] = []
    for index in range(max(0, region_start - radius), region_start + subject_span + radius):
        if index >= len(round_snapshot.segments):
            break
        context.append(_context_view(index))
    payload = {
        "limits": {
            "absolute_cjk": cfg.single_line_absolute_cjk,
            "absolute_latin": cfg.single_line_absolute_latin,
        },
        "boundary_context": context,
        "review_subjects": [
            {
                "segments": [
                    {
                        "id": region_start + offset,
                        "problem_ids": [
                            problem.problem_id
                            for problem in segment_problems.get(
                                region_start + offset, []
                            )
                        ],
                        "proposals": [
                            {
                                "output_index": entry["output_index"],
                                "original": entry["original"],
                                "translated": entry["translated"],
                            }
                            for entry in accepted_entries[offset]
                        ],
                    }
                    for offset in sorted(accepted_entries)
                ]
            }
        ],
        "feedback": [],
    }
    if cancelled is not None and cancelled():
        raise InterruptedError("LLM request cancelled")
    try:
        response = runtime.complete(
            review_profile,
            LLMRequest(
                messages=tuple(_review_request_messages(payload, guidance)),
                max_output_tokens=review_profile.max_output_tokens,
                metadata={"stage": "viewing_repair_review", "role": "utility"},
            ),
            cancelled=cancelled,
        )
    except InterruptedError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 复校传输失败保留主翻译候选
        logger.warning("观看问题修复高级校对请求失败（保留主翻译候选）: %s", exc)
        return 0, [f"高级校对复校请求失败，保留主翻译候选: {exc}"]
    fatal, corrections = _parse_review_response(response.text, bindings)
    if fatal is not None:
        return 0, [f"高级校对复校响应被拒（保留主翻译候选）: {fatal}"]
    active_sides = _active_single_line_sides(cfg, layout)
    warnings: List[str] = []
    corrections_applied = 0
    for (problem_id, output_index), translated in corrections.items():
        proposal = bindings.get((problem_id, output_index))
        if proposal is None:
            continue
        position = region_start + proposal["region_position"]
        if position >= len(state.segments):
            continue
        seg = state.segments[position]
        # 非空性守恒（主翻译验收同一约束）：复校不得发明或丢失译文。
        if bool(proposal["translated"].strip()) != bool(translated.strip()):
            warnings.append("高级校对复校改动译文侧非空性，已拒绝该处修正")
            continue
        if "\n" in translated and "translated" in active_sides:
            warnings.append("高级校对复校引入换行（单行显示侧），已拒绝该处修正")
            continue
        if "translated" in active_sides and translated.strip():
            limit = effective_length_limit(
                translated,
                cjk_limit=cfg.single_line_absolute_cjk,
                latin_limit=cfg.single_line_absolute_latin,
            )
            if weighted_length(translated) > limit:
                warnings.append("高级校对复校超出有效绝对上限，已拒绝该处修正")
                continue
        if translated.strip() and translated != seg.translated_text:
            seg.translated_text = translated
            corrections_applied += 1
    return corrections_applied, warnings


def _parse_response(
    text: str, batch_problems: Dict[str, PlanProblem]
) -> Tuple[Optional[str], Dict[int, List[Dict[str, Any]]]]:
    """解析响应并按 problem_id 显式绑定到段（不按数组位置推断归属）。

    返回 (整体致命错误, 段序 → 片段载荷列表)。未知 problem_id（含跨批
    绑定）是协议级违规：整体拒绝，不猜测归属；其余绑定错误归到对应段。
    """
    try:
        parsed = json_repair.loads(text)
    except Exception as exc:  # noqa: BLE001 —— 模型输出垃圾按业务失败处理
        return f"响应不是有效 JSON: {exc}", {}
    if not isinstance(parsed, dict) or not isinstance(parsed.get("repairs"), list):
        return "响应缺少 repairs 数组", {}
    grouped: Dict[int, List[Dict[str, Any]]] = {}
    for entry in parsed["repairs"]:
        if not isinstance(entry, dict):
            return "repairs 含非对象条目", {}
        problem_id = entry.get("problem_id")
        output_index = entry.get("output_index")
        original = entry.get("original")
        translated = entry.get("translated")
        if not isinstance(problem_id, str) or problem_id not in batch_problems:
            return f"未知问题绑定 problem_id={problem_id!r}", {}
        if type(output_index) is not int or output_index < 0:
            return "output_index 必须是非负整数", {}
        if not isinstance(original, str) or not isinstance(translated, str):
            return "original / translated 必须是字符串", {}
        problem = batch_problems[problem_id]
        grouped.setdefault(problem.segment_index, []).append(
            {
                "problem_id": problem_id,
                "output_index": output_index,
                "original": original,
                "translated": translated,
            }
        )
    return None, grouped


def _over_hard_cps(value: str, cjk: bool, span_ms: int, cfg: PostprocessConfig) -> bool:
    if not value or not value.strip() or span_ms <= 0:
        return False
    limit = cfg.max_cps_cjk if cjk else cfg.max_cps_latin
    return char_count(value, cjk) / (span_ms / 1000) > limit


def _validate_segment_candidate(
    seg: ASRDataSeg,
    entries: List[Dict[str, Any]],
    cfg: PostprocessConfig,
    layout: "SubtitleLayoutEnum",
) -> Tuple[Optional[str], List[ASRDataSeg], str]:
    """验收一个输入段的候选片段组（结构 / 等价 / 语义 / 长度 / 速度 / 时间）。

    返回 (错误, 带时间的输出片段, 候选指纹)。错误非 None 时整段候选拒绝；
    指纹供重复候选检测（调用方在验收通过后才记录）。
    """
    entries = sorted(entries, key=lambda e: e["output_index"])
    indices = [entry["output_index"] for entry in entries]
    if not entries or indices != list(range(len(entries))):
        return "output_index 必须从 0 连续编号", [], ""

    duration_ms = seg.end_time - seg.start_time
    cap = _max_fragments(cfg, duration_ms)
    if len(entries) > cap:
        return f"片段数 {len(entries)} 超过时长允许上限 {cap}", [], ""

    # 原文等价（D05/D12）：非空白字符按序完全一致，空白重排允许。
    joined = "".join(entry["original"] for entry in entries)
    if _compact(joined) != _compact(seg.text):
        return "原文片段拼接后与输入原文不等价", [], ""

    active_sides = _active_single_line_sides(cfg, layout)
    input_cjk = is_mainly_cjk(seg.text) if seg.text else False
    input_translated_cjk = (
        is_mainly_cjk(seg.translated_text) if seg.translated_text else False
    )
    # 阅读速度验收（D27）：不得重新引入输入段未违反的硬速度约束。
    input_speed_over = _over_hard_cps(
        seg.text, input_cjk, duration_ms, cfg
    ) or _over_hard_cps(seg.translated_text, input_translated_cjk, duration_ms, cfg)

    loads: List[float] = []
    for entry in entries:
        original, translated = entry["original"], entry["translated"]
        if not original.strip():
            return "原文片段为空", [], ""
        if "\n" in original and "original" in active_sides:
            return "原文片段含换行（单行显示）", [], ""
        if "\n" in translated and "translated" in active_sides:
            return "译文片段含换行（单行显示侧）", [], ""
        # 语义最小门槛：译文侧非空性与输入一致（不发明、不丢失译文）。
        if bool(seg.translated_text.strip()) != bool(translated.strip()):
            return "译文侧非空性必须与输入一致", [], ""
        if "original" in active_sides:
            limit = effective_length_limit(
                original,
                cjk_limit=cfg.single_line_absolute_cjk,
                latin_limit=cfg.single_line_absolute_latin,
            )
            if weighted_length(original) > limit:
                return "原文片段超出有效绝对上限", [], ""
        if "translated" in active_sides and translated.strip():
            limit = effective_length_limit(
                translated,
                cjk_limit=cfg.single_line_absolute_cjk,
                latin_limit=cfg.single_line_absolute_latin,
            )
            if weighted_length(translated) > limit:
                return "译文片段超出有效绝对上限", [], ""
        loads.append(max(0.5, weighted_length(original) + weighted_length(translated)))

    times = _allocate_times(seg.start_time, seg.end_time, loads, cfg)
    if times is None or len(times) != len(entries):
        return "时间分配失败（时长不足最短显示时长）", [], ""

    fragments: List[ASRDataSeg] = []
    for entry, (frag_start, frag_end) in zip(entries, times):
        original_cjk = is_mainly_cjk(entry["original"])
        translated_cjk = is_mainly_cjk(entry["translated"]) if entry["translated"] else False
        if (
            _over_hard_cps(entry["original"], original_cjk, frag_end - frag_start, cfg)
            or _over_hard_cps(
                entry["translated"], translated_cjk, frag_end - frag_start, cfg
            )
        ) and not input_speed_over:
            return "片段阅读速度超出硬限（输入段未违反）", [], ""
        fragments.append(
            ASRDataSeg(entry["original"], frag_start, frag_end, entry["translated"])
        )
    fingerprint = _fingerprint(
        [
            [entry["output_index"], entry["original"], entry["translated"]]
            for entry in entries
        ]
    )
    return None, fragments, fingerprint


def execute_viewing_repair(
    working: ASRData,
    cfg: PostprocessConfig,
    report: QualityReport,
    layout: "SubtitleLayoutEnum",
    *,
    gateway: Optional[LLMGateway] = None,
    snapshot: Optional["TranslationExecutionSnapshot"] = None,
    profile: Optional["LLMModelProfile"] = None,
    profile_resolver=None,
    boundary_context_radius: Optional[int] = None,
    progress: Optional[Callable[[int, str], None]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
) -> Tuple[ASRData, QualityReport]:
    """执行批量观看问题修复循环（票 05/06 核心入口，供任务入口调用）。

    修复方式从任务开始时冻结的翻译执行快照中选择（票 06，D07/D15）：
    增强型翻译跟随主翻译 + 高级校对两段流程；普通 LLM 翻译只复用主翻译，
    不自动增加高级校对；非 LLM 翻译与缺失快照不静默发起 LLM 请求，只执行
    确定性处理并仅报告。``profile`` 是无快照时的显式备用翻译角色。
    每轮：扫描 → 规划（主体 + 边界上下文）→ 批量请求 → 显式绑定验收 →
    （增强流程）高级校对复校 → 拼回完整字幕 → 重新验收进入下一轮；
    耗尽 / 重复 / 容量不足的区域局部回退到修复入口快照并标记未解决
    （D13/D14），不阻断下游。``boundary_context_radius`` 缺省取快照中
    的上游设置（D18），显式传入时覆盖（独立任务用当前任务配置）。
    """
    flow = select_repair_flow(snapshot, profile, profile_resolver)
    summary = RepairSummary()
    summary.translation_method = snapshot.method if snapshot is not None else ""
    summary.flow_mode = flow.mode
    summary.main_role = role_label(flow.main_profile, None)
    summary.review_role = role_label(flow.review_profile, None)
    summary.boundary_context_radius = (
        boundary_context_radius
        if boundary_context_radius is not None
        else flow.boundary_context_radius
    )
    report.viewing_repair = summary
    if flow.mode == "report_only":
        # 非 LLM / 缺失快照：不静默发起 LLM 请求（D15），仅确定性处理与报告。
        summary.warnings.append(f"观看问题模型修复未执行（{flow.reason}）")
        logger.info("观看问题模型修复按仅报告处理：%s", flow.reason)
        return working, report

    repair_profile = flow.main_profile
    assert repair_profile is not None  # mode main / main_review 保证非空
    radius = summary.boundary_context_radius
    # 任务冻结的原翻译提示配置（D15）：只作为修复请求的补充指引，
    # 不替换修复系统提示词；非快照路径（显式备用角色）为空。
    main_guidance = snapshot.main_prompt if snapshot is not None else ""
    review_guidance = snapshot.review_prompt if snapshot is not None else ""
    state = _WorkingState(clone_subtitle_data(working))
    # 区域封闭表（初版段序）：回退 / 容量不足的区域不再进入后续请求。
    closed_regions: Set[int] = set()
    # 业务重试计数（D09）：每个问题身份至多 1 + DEFAULT_BUSINESS_RETRIES 次请求。
    attempts: Dict[ProblemIdentity, int] = {}
    last_subject: Dict[ProblemIdentity, Tuple[int, ...]] = {}
    last_error: Dict[ProblemIdentity, str] = {}
    accepted: Set[ProblemIdentity] = set()
    candidate_fps: Dict[int, Set[str]] = {}
    state_fps: Dict[int, Set[str]] = {}
    transport_streak = 0

    def _identity(problem: PlanProblem) -> ProblemIdentity:
        return (state.origin[problem.segment_index], problem.side, problem.kind)

    def _rollback(indices: Tuple[int, ...], reason: str) -> None:
        """区域回退（D13）：恢复快照、封闭区域、撤销该区域的已接受计数。"""
        if state.rollback(indices):
            summary.rollbacks.append(RegionRollback(initial_indices=indices, reason=reason))
            summary.warnings.append(
                f"观看问题修复回退区域（初版段 {list(indices)}）：{reason}"
            )
        else:
            summary.warnings.append(
                f"观看问题修复回退失败（初版段 {list(indices)}）：{reason}"
            )
        closed_regions.update(indices)
        for identity in [i for i in accepted if i[0] in set(indices)]:
            accepted.discard(identity)

    def _raise_if_cancelled() -> None:
        if cancelled is not None and cancelled():
            raise InterruptedError("LLM request cancelled")

    with borrow_utility_gateway(gateway) as runtime:
        while summary.rounds < MAX_ROUNDS:
            _raise_if_cancelled()
            summary.rounds += 1
            data = state.as_data()
            # 扫描结果统一适配为 PlanProblem（票 04 的身份契约），
            # 循环内不再混用 ViewingProblem 形状。
            scanned = problems_from_viewing(scan_viewing_lengths(data, cfg, layout))
            open_problems = [
                problem
                for problem in scanned
                if state.origin[problem.segment_index] not in closed_regions
            ]
            if not open_problems:
                break
            # 容量规划（票 03）：每批按真实请求口径估算输入与输出预留，
            # 预算取修复角色真实工作上下文（快照 / 角色方案的
            # ``work_context_tokens``），受用户请求输出上限钳制；
            # 超预算先减少主体数量，再缩减边界上下文，零上下文单主体
            # 仍不足时进入 ``unplannable`` 明确报告（不截断原文）。
            segment_problems: Dict[int, List[PlanProblem]] = {}
            for problem in open_problems:
                segment_problems.setdefault(problem.segment_index, []).append(problem)
            feedback = [
                f"{problem.problem_id}: {last_error[_identity(problem)]}"
                for problem in open_problems
                if _identity(problem) in last_error
            ]
            plan = plan_repair_batches(
                data,
                open_problems,
                boundary_context_radius=radius,
                token_budget=repair_profile.work_context_tokens,
                batch_input_estimator=lambda subjects_in_batch, context: _estimate_request_input(
                    data,
                    cfg,
                    subjects_in_batch,
                    context,
                    segment_problems,
                    feedback,
                    main_guidance,
                ),
                output_reserve_estimator=lambda subjects_in_batch: _estimate_output_reserve(
                    data,
                    cfg,
                    subjects_in_batch,
                    segment_problems,
                    work_context_tokens=repair_profile.work_context_tokens,
                    max_output_tokens=repair_profile.max_output_tokens,
                ),
            )
            for subject in plan.unplannable:
                # 容量不足（D26）：明确报告、封闭区域、不截断内容。
                indices = state.initial_indices(subject.start_index, subject.end_index)
                if closed_regions.isdisjoint(indices):
                    summary.unplannable_subjects += 1
                    summary.warnings.append(
                        "观看问题修复容量不足（零上下文单主体仍超出 token 预算）："
                        f"问题 {subject.problem_ids}"
                    )
                closed_regions.update(indices)
            # 容量收缩观测（票 03，spec 第 7 条）：收缩原因由规划器在
            # 批次上标记（``subjects_shrunk`` / ``context_shrunk``），
            # 执行侧只按批次聚合——主体计数用批次主体数（不用问题段数
            # 代替主体数），问题计数按显式绑定分列。
            for batch in plan.batches:
                summary.planned_requests += 1
                summary.planned_input_tokens += batch.estimated_tokens
                summary.planned_output_reserve_tokens += batch.output_reserve_tokens
                summary.planned_subjects += len(batch.subjects)
                summary.planned_problems += sum(
                    len(subject.problem_ids) for subject in batch.subjects
                )
                if batch.subjects_shrunk:
                    summary.shrunk_subject_batches += 1
                if batch.context_shrunk:
                    summary.shrunk_context_batches += 1
            if not plan.batches:
                break

            # 业务重试耗尽检查（D09/D13）：超过 1+4 次请求的问题按区域回退。
            exhausted_regions: Dict[Tuple[int, ...], List[ProblemIdentity]] = {}
            for problem in open_problems:
                identity = _identity(problem)
                if (
                    state.origin[problem.segment_index] not in closed_regions
                    and attempts.get(identity, 0) >= 1 + DEFAULT_BUSINESS_RETRIES
                ):
                    region = last_subject.get(identity, (identity[0],))
                    exhausted_regions.setdefault(region, []).append(identity)
            if exhausted_regions:
                for region, identities in exhausted_regions.items():
                    logger.info(
                        "观看问题修复重试耗尽（%d 个问题），回退区域 %s",
                        len(identities),
                        list(region),
                    )
                    _rollback(region, "业务修复重试耗尽")
                continue

            round_accepted = False
            round_transport_failed = False
            # 修复轮次快照（ADR-0021）：本轮全部请求的主体与边界上下文
            # 共享 ``round_snapshot``（轮初 state 的冻结副本）；同轮邻批
            # 已归并的拆分 / 校对结果不进入本批载荷。下一轮才读取归并后
            # 的字幕状态（轮首重建快照）。
            round_snapshot = state.as_data()
            if progress is not None:
                progress(
                    min(90, 55 + summary.rounds * 4),
                    f"正在修复观看问题（第 {summary.rounds} 轮，{len(open_problems)} 个未解决）",
                )
            # 阶段 1：载荷构建 + 候选计算（按固定批序发出请求，验收候选，
            # 不直接修改共享字幕）；阶段 2：统一归并（固定段序降序 splice、
            # 区域守卫与高级校对）。本票保持串行执行，分离只为确定性语义。
            for batch in reversed(plan.batches):
                _raise_if_cancelled()
                batch_problem_map = {
                    problem.problem_id: problem
                    for problem in open_problems
                    if any(
                        subject.start_index
                        <= problem.segment_index
                        < subject.end_index
                        for subject in batch.subjects
                    )
                }
                payload = _build_payload(round_snapshot, cfg, batch, segment_problems, feedback)
                summary.requests += 1
                try:
                    response = runtime.complete(
                        repair_profile,
                        LLMRequest(
                            messages=tuple(_request_messages(payload, main_guidance)),
                            max_output_tokens=repair_profile.max_output_tokens,
                            metadata={"stage": "viewing_repair", "role": "utility"},
                        ),
                        cancelled=cancelled,
                    )
                except InterruptedError:
                    raise
                except Exception as exc:  # noqa: BLE001 —— 传输级失败不消耗业务重试
                    logger.warning("观看问题修复请求失败（传输级）: %s", exc)
                    round_transport_failed = True
                    continue
                fatal, grouped = _parse_response(response.text, batch_problem_map)
                # 本批请求覆盖的问题身份：请求已发生，计入业务次数并记录区域。
                for subject in batch.subjects:
                    region = state.initial_indices(subject.start_index, subject.end_index)
                    for index in range(subject.start_index, subject.end_index):
                        for problem in segment_problems.get(index, []):
                            identity = _identity(problem)
                            attempts[identity] = attempts.get(identity, 0) + 1
                            last_subject[identity] = region
                if fatal is not None:
                    for problem in batch_problem_map.values():
                        last_error[_identity(problem)] = fatal
                    continue

                # 阶段 2（批内）：固定段序降序归并本批候选。
                for subject in reversed(batch.subjects):
                    region = state.initial_indices(
                        subject.start_index, subject.end_index
                    )
                    replacements: List[List[ASRDataSeg]] = []
                    # 已接受候选的显式绑定（主体内偏移 → 按 output_index 排序的条目），
                    # 供增强流程高级校对复校（票 06）。
                    accepted_entries: Dict[int, List[Dict[str, Any]]] = {}
                    splice_ok = True
                    any_accepted = False
                    for index in range(subject.start_index, subject.end_index):
                        seg = round_snapshot.segments[index]
                        entries = grouped.get(index, [])
                        problems = segment_problems.get(index, [])
                        identities = [_identity(problem) for problem in problems]
                        if not entries:
                            for identity in identities:
                                last_error[identity] = "响应缺少该段输出片段"
                            replacements.append([_clone_seg(seg)])
                            continue
                        error, fragments, fingerprint = _validate_segment_candidate(
                            seg, entries, cfg, layout
                        )
                        if error is not None:
                            for identity in identities:
                                last_error[identity] = error
                            replacements.append([_clone_seg(seg)])
                            continue
                        # 重复候选（D27）：立即停止该问题并回退报告。
                        initial_index = state.origin[index]
                        if fingerprint in candidate_fps.get(initial_index, set()):
                            _rollback(region, "重复修复候选")
                            splice_ok = False
                            break
                        candidate_fps.setdefault(initial_index, set()).add(fingerprint)
                        replacements.append(fragments)
                        accepted_entries[index - subject.start_index] = sorted(
                            entries, key=lambda entry: entry["output_index"]
                        )
                        any_accepted = True
                        accepted.update(identities)
                        for identity in identities:
                            last_error.pop(identity, None)
                    if not splice_ok:
                        continue
                    if not any_accepted:
                        # 整个主体无合格候选：状态不变，不落 splice、不记区域指纹
                        # （拒绝的候选不算一次改善，留给重试计数与耗尽回退处理）。
                        continue
                    spliced = state.splice(
                        subject.start_index, subject.end_index, replacements
                    )
                    if not spliced:
                        continue
                    # 区域状态重复检测（D27）：拼回后状态与历史一致即回退。
                    indices = state.initial_indices(
                        subject.start_index, subject.start_index + spliced
                    )
                    region_fp = _fingerprint(state.region_state(indices))
                    if any(
                        region_fp in state_fps.get(index, set()) for index in indices
                    ):
                        _rollback(indices, "区域状态重复（无改善）")
                        continue
                    for index in indices:
                        state_fps.setdefault(index, set()).add(region_fp)
                    summary.spliced_fragments += spliced
                    # 增强流程（票 06，D07）：拼接成功后对已接受候选执行
                    # 高级校对复校；失败保留主翻译候选，不回退已接受区域。
                    # 复校上下文以轮次快照为底（ADR-0021）：不读邻批刚完成的归并。
                    if flow.mode == "main_review" and flow.review_profile is not None:
                        applied, review_warnings = _review_pass(
                            runtime,
                            flow.review_profile,
                            state,
                            round_snapshot,
                            cfg,
                            layout,
                            region_start=subject.start_index,
                            region_length=spliced,
                            accepted_entries=accepted_entries,
                            replacements=replacements,
                            segment_problems=segment_problems,
                            radius=radius,
                            guidance=review_guidance,
                            cancelled=cancelled,
                        )
                        summary.review_corrections += applied
                        summary.warnings.extend(
                            warning for warning in review_warnings if warning not in summary.warnings
                        )
                    round_accepted = True

            if round_accepted:
                transport_streak = 0
            elif round_transport_failed:
                transport_streak += 1
                if transport_streak >= MAX_TRANSPORT_FAILURE_ROUNDS:
                    summary.warnings.append("观看问题修复连续传输失败，已停止修复循环")
                    break

    summary.resolved_problem_count = len(accepted)
    repaired = state.as_data()
    # 终态以交付字幕上的重新扫描为准（D27）：回退区域保持未解决，不记为通过。
    report.viewing_problems = scan_viewing_lengths(repaired, cfg, layout)
    # 已接受但终态仍超限的问题同样不记为通过：以终态扫描为验收基准。
    report.segment_count = len(repaired.segments)
    if summary.rounds >= MAX_ROUNDS:
        summary.warnings.append("观看问题修复达到轮数上界，提前停止")
    logger.info(
        "观看问题修复：%d 轮 / %d 次请求，解决 %d 个问题，回退 %d 区域，容量不足 %d 主体",
        summary.rounds,
        summary.requests,
        summary.resolved_problem_count,
        len(summary.rollbacks),
        summary.unplannable_subjects,
    )
    return repaired, report


__all__ = [
    "DEFAULT_BUSINESS_RETRIES",
    "MAX_FRAGMENTS_CAP",
    "MAX_TRANSPORT_FAILURE_ROUNDS",
    "RegionRollback",
    "RepairSummary",
    "execute_viewing_repair",
    "select_repair_flow",
]
