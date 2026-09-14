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

主修复受控并发（票 04，ADR-0018）：同轮各批载荷只读 ``round_snapshot``
（ADR-0021），批间无数据依赖 → 有界滑动窗口重叠发出；响应到达后按
``reversed(plan.batches)`` 固定序游标归并，任意完成顺序产生相同字幕、
验收与报告顺序。窗口 = 修复角色 ``clamped_concurrency``（任务并发请求数
经每 profile 显式保护上限钳制）；全部请求走同一网关的 per-profile
信号量，不在批内嵌套第二套限流把上限相乘。主修复到其高级校对保持
先后依赖：复校在游标推进时发出（批量化属票 05）。
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
)

import json_repair

from ..asr.asr_data import ASRData, ASRDataSeg
from ..llm import LLMGateway, LLMMessage, LLMRequest
from ..llm.gateway import request_deadline_hint
from ..llm.utility import DEFAULT_GATEWAY_CONCURRENCY, borrow_utility_gateway
from ..prompts import get_prompt
from ..subtitle.io import clone_subtitle_data
from ..utils.logger import setup_logger
from ..utils.text_utils import is_mainly_cjk
from .config import SINGLE_LINE, PostprocessConfig
from .diagnostics import (
    WaitRefresher,
    batch_event,
    retry_event,
    round_event,
    terminal_event,
)
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
# 并发请求数权威默认（ADR-0018）：与 GUI/CLI ``thread_num`` 默认一致；
# 任务入口从冻结配置传入实际值，缺省不悄悄回退网关隐藏默认。
# 单一来源（标准轴修复）：与自建网关闸默认同值，改名处 import 不再各留一份。
DEFAULT_THREAD_NUM = DEFAULT_GATEWAY_CONCURRENCY

# 问题稳定身份：working 段序会因拆分漂移，跨轮计数一律用
# (初版段序, 显示侧, 问题类别)；problem_id 只在一次请求内对模型显式绑定。
ProblemIdentity = Tuple[int, str, str]

# 受控并发取消轮询窗口（秒）：wait(FIRST_COMPLETED) 的超时粒度；
# 不依赖该间隔判断正确性，取消正确性由请求内/请求前检查保证。
CONCURRENT_CANCEL_POLL_SECONDS = 0.05


def request_window_seconds(profile: "LLMModelProfile") -> float:
    """本次修复请求的单次尝试网络窗口（等待期限展示口径，票 07）。

    复用网关 ``request_deadline_hint`` 的同一缩放口径（票 07 审查
    修复：不再各自硬编码 baseline，协议变化不静默漂移）；这是展示
    估计值，不是承诺的完成时限。公开命名对齐 ``request_deadline_hint``
    （标准轴：同一「单次网络窗口」概念不再三个名字）。
    """
    from ..llm.models import LLMRequest

    return request_deadline_hint(
        LLMRequest(messages=(), max_output_tokens=profile.max_output_tokens)
    )


def _start_refresher(
    emit: Callable[[Dict[str, Any]], None],
    on_event: Optional[Callable[[Dict[str, Any]], None]],
    snapshot: Callable[[], Dict[str, int]],
    *,
    round_index: Callable[[], int],
    role: str,
    request_window_s: float,
) -> Optional[WaitRefresher]:
    """启动一次在途请求的等待刷新器（票 07 审查修复：两处同型收编）。

    ``on_event`` 缺省时返回 None（不启动刷新器、零观测开销）；
    ``round_index`` / percent 由闭包按发射时点取值（轮次推进后
    仍读最新值，等待事件不携带过期轮次口径）。
    """
    if on_event is None:
        return None
    return WaitRefresher(
        emit,
        snapshot,
        round_index=round_index(),
        role=role,
        request_window_s=request_window_s,
        percent_provider=lambda: min(90, 55 + round_index() * 4),
    ).start()


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
    # 批量校对观测（票 05，spec 第 7 条）：校对请求数 / 主体覆盖与容量
    # 口径进入报告与状态载荷——不声称真实模型吞吐证据（受控响应下的
    # 组批行为观察，主体计数与片段计数分列）。
    review_requests: int = 0
    """实际发出的批量校对逻辑请求数（含传输失败；缓存命中不减）。"""
    review_planned_requests: int = 0
    """规划校对请求数（含收缩后重规划的轮次内累加）。"""
    review_planned_subjects: int = 0
    """规划覆盖的校对主体数（已拼接主体计数，与片段数分列）。"""
    review_planned_input_tokens: int = 0
    """规划校对请求输入估计之和（完整请求口径，票 05）。"""
    review_planned_output_reserve_tokens: int = 0
    """规划校对请求输出预留之和（reviews 数组 + 协议开销）。"""
    review_unplannable_subjects: int = 0
    """零上下文单主体仍超出校对预算的主体数（保留主候选并告警）。"""
    review_shrunk_subject_groups: int = 0
    """校对组主体数因容量收缩的组数（先减主体，D26 第一级）。"""
    review_shrunk_context_groups: int = 0
    """校对组上下文半径因容量收缩的组数（单主体后减上下文，第二级）。"""
    # 受控并发观测（票 04，spec 第 7 条：报告有效并发与最大在途，
    # 不声称真实模型吞吐证据——冷缓存下的窗口行为观察）。
    thread_num: int = 0
    """任务开始时冻结的并发请求数（0 = 旧调用方未提供，未启用观测）。"""
    concurrency_gate: int = 0
    """主修复窗口实际生效的并发闸（thread_num 经 profile 保护上限钳制）。"""
    effective_concurrency: int = 0
    """本轮窗口并发上限（批数不足窗口时收缩到批数）。"""
    max_inflight: int = 0
    """发出层观测的最大同时在途主修复请求数（含瞬时缓存命中；真实
    adapter 在途由传输探针口径覆盖——spec：缓存命中不冒充模型吞吐）。"""
    concurrent_rounds: int = 0
    """窗口并发 > 1 的轮次数（供配置贯通核对）。"""


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
    spans = [max(bound, int(math.floor(duration * load / total))) for load in loads]
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
        side for side in sides_for_layout(layout) if cfg.display_mode_for(side) == SINGLE_LINE
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
    subject_input = estimate_tokens(json.dumps(payload["repair_subjects"], ensure_ascii=False))
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


def _review_bindings(
    region_start: int,
    accepted_entries: Dict[int, List[Dict[str, Any]]],
    replacements: List[List[ASRDataSeg]],
    segment_problems: Dict[int, List[PlanProblem]],
) -> Tuple[
    Dict[int, Dict[str, Any]],
    Dict[Tuple[str, int], Dict[str, Any]],
]:
    """主体内偏移 → proposals 与复校绑定表的构造（单主体载荷段共用）。

    ``accepted_entries`` 按主体内偏移携带主翻译已接受候选的显式绑定；
    ``replacements`` 提供主体内各输入段的片段数（复校定位用）。
    ``region_position`` 是主体拼接后区域内的片段位置（应用段定位坐标）。
    """
    # 主体内各输入段在拼接后区域中的起始偏移（含透传段的 1 片）。
    prefix: List[int] = []
    cursor = 0
    for fragments in replacements:
        prefix.append(cursor)
        cursor += len(fragments)
    subjects: Dict[int, Dict[str, Any]] = {}
    bindings: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for offset in sorted(accepted_entries):
        input_index = region_start + offset
        problems = segment_problems.get(input_index, [])
        if not problems:
            continue
        problem_id = problems[0].problem_id
        proposals: List[Dict[str, Any]] = []
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
        subjects[offset] = {
            "id": input_index,
            "problem_ids": [problem.problem_id for problem in problems],
            "proposals": proposals,
        }
    return subjects, bindings


@dataclass
class _ReviewSubjectEntry:
    """一个已拼接主体进入批量复校的载荷条目（票 05）。

    ``segments`` 是请求内可见载荷（prompt 契约：每个 review subject
    携带 segments 的 id / problem_ids / proposals）；``candidate_view``
    是主体区间叠加本次候选后的上下文视图（ADR-0021：键 = 轮次快照段序）；
    ``bindings`` 是 (problem_id, output_index) → 提案 的应用段定位表
    （``region_position`` 为主体拼接后区域内的片段位置），不进请求。
    ``state_refs`` 是拼接后 state 内的主体片段对象引用（应用段直接定位，
    不受同轮后续 splice 的段序漂移影响）。
    """

    region_start: int
    region_span: int
    segments: List[Dict[str, Any]]
    candidate_view: Dict[int, Dict[str, Any]]
    bindings: Dict[Tuple[str, int], Dict[str, Any]]
    state_refs: List[ASRDataSeg] = field(default_factory=list)


def _review_subject_entry(
    *,
    region_start: int,
    accepted_entries: Dict[int, List[Dict[str, Any]]],
    replacements: List[List[ASRDataSeg]],
    segment_problems: Dict[int, List[PlanProblem]],
    state_refs: Optional[List[ASRDataSeg]] = None,
) -> Optional[_ReviewSubjectEntry]:
    """构造一个已拼接主体的复校载荷条目（票 05 多主体同批共用）。

    主体载荷携带独立身份（输入段 id / 问题 ids / proposals 的
    output_index + original + translated）。上下文视图（ADR-0021）以
    ``round_snapshot``（修复轮次快照）为基：主体区间叠加本次合法候选，
    主体外一律是轮次快照内容——邻批（或同批其他主体）刚归并的拆分
    结果不进入本主体复校上下文（完成顺序无关）。窗口以主体输入段数
    （``len(replacements)``）为界，不随候选拆分片段数扩张。
    ``state_refs`` 是拼接后 state 内该区域的片段对象引用（应用段直接
    定位，不受同轮后续 splice 的段序漂移影响）；缺省为空（应用段退回
    段序定位）。返回 None 表示主体无可复校候选（不进入任何校对请求）。
    """
    subjects, bindings = _review_bindings(
        region_start, accepted_entries, replacements, segment_problems
    )
    if not bindings:
        return None

    # 复校上下文视图（轮次快照语义，ADR-0021）：主体区间叠加本次
    # 已接受候选（拼接后的片段文本 / 译文）；主体外保持轮次快照内容。
    candidate_view: Dict[int, Dict[str, Any]] = {}
    for offset in sorted(accepted_entries):
        fragments = replacements[offset]
        candidate_view[region_start + offset] = {
            "id": region_start + offset,
            "text": " ".join(fragment.text for fragment in fragments),
            "translated": " ".join(fragment.translated_text for fragment in fragments),
        }

    return _ReviewSubjectEntry(
        region_start=region_start,
        region_span=len(replacements),
        segments=[subjects[offset] for offset in sorted(subjects)],
        candidate_view=candidate_view,
        bindings=bindings,
        state_refs=list(state_refs) if state_refs is not None else [],
    )


def _review_context_window(
    round_snapshot: ASRData,
    entry: "_ReviewSubjectEntry",
    radius: int,
) -> List[Dict[str, Any]]:
    """一个复校主体的边界上下文窗口（发送时按 ``radius`` 取）。

    窗口在轮次快照坐标上：上文 [start-radius, start)，下文
    [start+span, start+span+radius)；主体区间取 ``candidate_view``
    （叠加候选），主体外取轮次快照（ADR-0021 完成顺序无关）。
    """

    def _context_view(index: int) -> Dict[str, Any]:
        view = entry.candidate_view.get(index)
        if view is not None:
            return view
        return _context_entry(index, round_snapshot.segments[index])

    context: List[Dict[str, Any]] = []
    for index in range(
        max(0, entry.region_start - radius),
        entry.region_start + entry.region_span + radius,
    ):
        if index >= len(round_snapshot.segments):
            break
        context.append(_context_view(index))
    return context


def _build_review_payload(
    round_snapshot: ASRData,
    cfg: PostprocessConfig,
    entries: Sequence["_ReviewSubjectEntry"],
    radius: int,
) -> Dict[str, Any]:
    """构造一次批量复校请求载荷（票 05：多主体共同进入同一物理请求）。

    主体与输出片段身份独立保留（``_parse_review_response`` 按显式绑定
    归还）；分组来自固定归并顺序的主修复批（稳定输入分组），不由网络
    完成顺序决定（spec「稳定输入分组避免网络完成顺序改变批次语义」）。
    边界上下文是各主体窗口的稳定拼接；主体外一律轮次快照。
    """
    return {
        "limits": {
            "absolute_cjk": cfg.single_line_absolute_cjk,
            "absolute_latin": cfg.single_line_absolute_latin,
        },
        "boundary_context": [
            item
            for entry in entries
            for item in _review_context_window(round_snapshot, entry, radius)
        ],
        "review_subjects": [{"segments": entry.segments} for entry in entries],
        "feedback": [],
    }


def _apply_review_corrections(
    state: "_WorkingState",
    cfg: PostprocessConfig,
    layout: "SubtitleLayoutEnum",
    entries: Sequence["_ReviewSubjectEntry"],
    corrections: Dict[Tuple[str, int], str],
) -> Tuple[int, int, List[str]]:
    """把一批复校校订按显式绑定应用到拼接后字幕（票 05 应用段）。

    逐项验收：非空性守恒 / 单行换行 / 有效绝对上限与主翻译验收同一
    约束；优先经 ``state_refs``（拼接时捕获的对象引用）定位，缺省退回
    ``region_start + region_position``。非法单项只拒绝该处修正（警告），
    不影响同批其他合法项（逐项验收，spec「同批其他合法校对项独立验收」）。
    """
    active_sides = _active_single_line_sides(cfg, layout)
    warnings: List[str] = []
    corrections_applied = 0
    missing = 0
    for entry in entries:
        for key, proposal in entry.bindings.items():
            translated = corrections.get(key)
            if translated is None:
                # 缺项可观察（票 05 验收 5）：响应遗漏的提案逐项计数并
                # 告警——不静默跳过（截断 / 部分恢复同样落到这条路径，
                # 该主体提案保留主翻译候选）。
                missing += 1
                continue
            position = entry.region_start + proposal["region_position"]
            if entry.state_refs:
                # 拼接时捕获的对象引用：不受同轮后续 splice 的段序漂移影响。
                offset_in_region = position - entry.region_start
                ref = (
                    entry.state_refs[offset_in_region]
                    if 0 <= offset_in_region < len(entry.state_refs)
                    else None
                )
            else:
                ref = state.segments[position] if position < len(state.segments) else None
            if ref is None:
                continue
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
            if translated.strip() and translated != ref.translated_text:
                ref.translated_text = translated
                corrections_applied += 1
    if missing:
        warnings.append(f"高级校对复校响应缺少 {missing} 个提案的校订（保留主翻译候选）")
    return corrections_applied, missing, warnings


# 复校输出比例（票 05）：复校只改译文侧，输出 ≈ 校订后的 reviews
# 数组（每提案一条）；比例沿用主修复的 1.5（保守覆盖译文改写），
# 协议开销按主体计（每主体一段 JSON 对象开销）。
REVIEW_OUTPUT_INPUT_RATIO = 1.5
REVIEW_OUTPUT_PROTOCOL_OVERHEAD_TOKENS = 256


def _review_group_input_tokens(
    round_snapshot: ASRData,
    cfg: PostprocessConfig,
    entries: Sequence["_ReviewSubjectEntry"],
    radius: int,
    guidance: str,
) -> int:
    """估算一组复校请求的完整输入 token（票 05 容量口径，镜像票 03）。

    直接复用 ``_review_request_messages`` 构造与实际请求逐字一致的
    消息（系统提示词 + guidance + ``_build_review_payload`` 载荷：
    limits / segments / boundary_context / feedback 协议开销），
    不只估主体裸文本；估算与实际请求共用同一构造（单一来源）。
    """
    from ..translate.enhanced.token_planner import estimate_tokens

    payload = _build_review_payload(round_snapshot, cfg, entries, radius)
    messages = _review_request_messages(payload, guidance)
    return estimate_tokens("\n".join(message.content for message in messages))


def _review_group_output_reserve(
    entries: Sequence["_ReviewSubjectEntry"],
    *,
    work_context_tokens: Optional[int] = None,
    max_output_tokens: Optional[int] = None,
) -> int:
    """估算一组复校请求的输出预留（票 05，一对多输出 + 协议开销）。

    比例式预留（ADR-0019）：主体输入载荷 × 输出比 + 协议开销，
    受工作上下文与用户请求输出上限钳制（不自动抬升用户配置）。
    """
    from ..translate.enhanced.token_planner import estimate_tokens

    subject_input = estimate_tokens(
        json.dumps([{"segments": entry.segments} for entry in entries], ensure_ascii=False)
    )
    reserve = int(subject_input * REVIEW_OUTPUT_INPUT_RATIO) + (
        REVIEW_OUTPUT_PROTOCOL_OVERHEAD_TOKENS * max(1, len(entries))
    )
    if work_context_tokens is not None:
        reserve = min(reserve, work_context_tokens - 1)
    if max_output_tokens is not None:
        reserve = min(reserve, max_output_tokens)
    return max(reserve, 1)


@dataclass
class _ReviewGroup:
    """一次批量复校请求的规划组（票 05）。

    ``subjects`` 按固定主体顺序分组（稳定输入分组）；``radius`` 是
    本组实际使用的上下文半径（容量缩上下文时小于请求值）；
    ``input_tokens`` / ``output_reserve_tokens`` 是规划估算（供观测）。
    """

    subjects: List["_ReviewSubjectEntry"]
    radius: int
    input_tokens: int
    output_reserve_tokens: int


@dataclass
class _ReviewPlan:
    """一轮批量复校的规划结果（票 05）。"""

    groups: List["_ReviewGroup"] = field(default_factory=list)
    unplannable_subjects: int = 0
    """零上下文单主体仍超出预算的主体数（保留主候选并告警）。"""
    shrunk_subject_groups: int = 0
    """主体数量因容量收缩的组数（先减主体，D26 第一级）。"""
    shrunk_context_groups: int = 0
    """上下文半径因容量收缩的组数（单主体后减上下文，第二级）。"""


def _plan_review_groups(
    round_snapshot: ASRData,
    cfg: PostprocessConfig,
    entries: List["_ReviewSubjectEntry"],
    *,
    radius: int,
    guidance: str,
    token_budget: Optional[int],
    work_context_tokens: Optional[int],
    max_output_tokens: Optional[int],
    max_subjects_per_group: int = 10,
) -> "_ReviewPlan":
    """把同轮已拼接主体按复校容量规划为批量请求组（票 05）。

    收缩顺序镜像主修复（D26 / 票 03）：先减少组内主体数（保留完整
    radius 上下文），单主体仍放不下时逐步缩减上下文半径（radius → 0）；
    零上下文单主体仍超出预算时该主体不进入任何请求——保留主修复
    候选并告警（spec：零上下文仍不足明确报告、不截断内容、不为凑满
    批无限等待；容量允许时允许大批，不足时允许小批）。分组按固定
    主体顺序（稳定输入分组），完成顺序不改变分组语义。
    """
    plan = _ReviewPlan()
    if not entries:
        return plan

    def _fits(group: List["_ReviewSubjectEntry"], group_radius: int) -> Optional["_ReviewGroup"]:
        estimated = _review_group_input_tokens(round_snapshot, cfg, group, group_radius, guidance)
        reserve = _review_group_output_reserve(
            group,
            work_context_tokens=work_context_tokens,
            max_output_tokens=max_output_tokens,
        )
        if token_budget is not None and estimated + reserve > token_budget:
            return None
        return _ReviewGroup(
            subjects=group,
            radius=group_radius,
            input_tokens=estimated,
            output_reserve_tokens=reserve,
        )

    cursor = 0
    while cursor < len(entries):
        # 收缩第一级：先减少组内主体数（保留完整上下文）。
        max_take = min(max_subjects_per_group, len(entries) - cursor)
        placed: Optional["_ReviewGroup"] = None
        for take in range(max_take, 0, -1):
            placed = _fits(entries[cursor : cursor + take], radius)
            if placed is not None:
                if take < max_take:
                    plan.shrunk_subject_groups += 1
                break
        # 收缩第二级：单主体仍放不下时逐步减上下文（radius → 0）。
        if placed is None:
            for group_radius in range(radius - 1, -1, -1):
                placed = _fits([entries[cursor]], group_radius)
                if placed is not None:
                    plan.shrunk_context_groups += 1
                    break
        if placed is None:
            # 零上下文单主体仍超出预算：明确报告容量不足（不截断、
            # 不静默丢弃校对覆盖——主体保留主修复候选）。
            plan.unplannable_subjects += 1
            cursor += 1
            continue
        plan.groups.append(placed)
        cursor += len(placed.subjects)
    return plan


def _dispatch_ordered(
    batch_count: int,
    send: Callable[[int], "_BatchOutcome"],
    *,
    window: int,
    cancelled: Optional[Callable[[], bool]] = None,
    on_merged: Optional[Callable[[int, "_BatchOutcome"], None]] = None,
) -> List["_BatchOutcome"]:
    """有界滑动窗口发出全部主修复请求，按固定批序归并消费（票 04/05）。

    批间无数据依赖（ADR-0021 同轮快照）→ 全部请求立即进入窗口，无固定
    首批串行预热；窗口有界（``window`` = 并发闸与批数的较小值），不按
    整片问题数创建队列。取消：worker 入口先查一次（请求发送前的最后
    一次取消检查）；在途请求按在途处理（由网关 ``cancelled`` 通道抢占，
    票 06 范围），此处轮询只让等待侧尽快退出。返回值按批序排列。

    ``on_merged``（票 05 依赖调度）按固定批序（order 0, 1, …）在归并
    游标推进时回调：响应到达先入 ready 表，游标只在队首批就绪时推进
    ——归并顺序保持固定（02 确定性语义：完成顺序不改变归并输入与
    working 坐标），每批归并完成立即派生其校对工作，不等待整轮全部
    完成（无全轮屏障）；回调异常按整体失败传播（取消全部在途）。
    """
    if window < 1:
        raise ValueError("window must be a positive integer")
    if window == 1 or batch_count <= 1:
        outcomes = [send(order) for order in range(batch_count)]
        if on_merged is not None:
            for order, outcome in enumerate(outcomes):
                on_merged(order, outcome)
        return outcomes

    results: List[Optional["_BatchOutcome"]] = [None] * batch_count
    ready: Dict[int, "_BatchOutcome"] = {}
    merge_cursor = 0
    with ThreadPoolExecutor(max_workers=window) as executor:
        pending: Dict["Future[_BatchOutcome]", int] = {
            executor.submit(send, order): order for order in range(batch_count)
        }
        try:
            while pending or merge_cursor < batch_count:
                if cancelled is not None and cancelled():
                    for future in pending:
                        future.cancel()
                    raise InterruptedError("LLM request cancelled")
                if pending:
                    completed, _ = wait(
                        tuple(pending),
                        timeout=CONCURRENT_CANCEL_POLL_SECONDS,
                        return_when=FIRST_COMPLETED,
                    )
                    for future in completed:
                        order = pending.pop(future)
                        results[order] = future.result()
                        ready[order] = results[order]  # type: ignore[arg-type]
                # 固定序游标：只消费队首就绪批（归并顺序 = 完成语义无关）。
                while merge_cursor < batch_count and merge_cursor in ready:
                    outcome = ready.pop(merge_cursor)
                    if on_merged is not None:
                        on_merged(merge_cursor, outcome)
                    merge_cursor += 1
        except BaseException:
            for future in pending:
                future.cancel()
            raise
    if len([outcome for outcome in results if outcome is not None]) != batch_count:
        raise RuntimeError("dispatch lost outcomes; batch order must stay dense")
    return [outcome for outcome in results if outcome is not None]


class _BatchOutcome(NamedTuple):
    """一个主修复请求的落账载荷（票 04 并发执行）。

    执行（网络等待）与归并（写共享状态）分离：worker 只产生结果与
    错误，不触碰共享字幕 / 重试簿记（ADR-0021「worker 只产生候选」）。
    性能观测（在途窗口）由协调侧 ``inflight_state`` 单独持有，缓存
    命中不区分——真实 adapter 在途由传输探针口径覆盖（spec：缓存
    命中不冒充模型吞吐）。
    """

    batch_order: int
    """请求按 ``reversed(plan.batches)`` 固定序的序号（归并顺序）。"""
    response: Optional[Any]
    """成功响应（LLMResult）；传输级失败为 None。"""
    error: Optional[BaseException] = None
    """传输级失败原因（整体请求失败；业务级拒绝在响应解析后落账）。"""


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
    input_translated_cjk = is_mainly_cjk(seg.translated_text) if seg.translated_text else False
    # 阅读速度验收（D27）：不得重新引入输入段未违反的硬速度约束。
    input_speed_over = _over_hard_cps(seg.text, input_cjk, duration_ms, cfg) or _over_hard_cps(
        seg.translated_text, input_translated_cjk, duration_ms, cfg
    )

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
            or _over_hard_cps(entry["translated"], translated_cjk, frag_end - frag_start, cfg)
        ) and not input_speed_over:
            return "片段阅读速度超出硬限（输入段未违反）", [], ""
        fragments.append(ASRDataSeg(entry["original"], frag_start, frag_end, entry["translated"]))
    fingerprint = _fingerprint(
        [[entry["output_index"], entry["original"], entry["translated"]] for entry in entries]
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
    thread_num: Optional[int] = None,
    progress: Optional[Callable[[int, str], None]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
    on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    task_id: str = "",
) -> Tuple[ASRData, QualityReport]:
    """执行批量观看问题修复循环（票 05/06 核心入口，供任务入口调用）。

    修复方式从任务开始时冻结的翻译执行快照中选择（票 06，D07/D15）：
    增强型翻译跟随主翻译 + 高级校对两段流程；普通 LLM 翻译只复用主翻译，
    不自动增加高级校对；非 LLM 翻译与缺失快照不静默发起 LLM 请求，只执行
    确定性处理并仅报告。``profile`` 是无快照时的显式备用翻译角色。
    每轮：扫描 → 规划（主体 + 边界上下文）→ 受控并发批量请求（票 04）→
    定序归并 + 显式绑定验收 →（增强流程）高级校对复校 → 拼回完整字幕 →
    重新验收进入下一轮；耗尽 / 重复 / 容量不足的区域局部回退到修复入口
    快照并标记未解决（D13/D14），不阻断下游。``boundary_context_radius``
    缺省取快照中的上游设置（D18），显式传入时覆盖（独立任务用当前任务
    配置）。
    ``thread_num`` 是任务冻结的并发请求数（票 04，ADR-0018）：缺省用
    权威默认 10；每 profile 显式保护上限经 ``clamped_concurrency`` 钳制，
    同 profile 主修复 / 校对共享网关 per-profile 信号量（嵌套调度不相乘）。
    ``on_event``（票 07，spec「进度与诊断」）是可选结构化事件回调：
    轮次分母 / 归并验收通过 / 在途等待刷新 / 重试原因 / 终态事件，
    与 ``progress`` 简单百分比消费者并列（既有回调保持可用）；回调在
    多线程发射，必须线程安全。``task_id`` 进入请求 ``metadata``，
    内容日志关闭时请求记录仍可按任务 / 轮次 / 批次关联。
    """
    flow = select_repair_flow(snapshot, profile, profile_resolver)
    summary = RepairSummary()
    summary.translation_method = snapshot.method if snapshot is not None else ""
    summary.flow_mode = flow.mode
    summary.main_role = role_label(flow.main_profile, None)
    summary.review_role = role_label(flow.review_profile, None)

    def _emit(event: Dict[str, Any]) -> None:
        if on_event is not None:
            try:
                on_event(event)
            except Exception:  # noqa: BLE001 —— 观测回调失败不阻断修复
                logger.debug("postprocess progress event callback failed", exc_info=True)

    summary.boundary_context_radius = (
        boundary_context_radius
        if boundary_context_radius is not None
        else flow.boundary_context_radius
    )
    # 并发闸冻结（票 04，ADR-0018）：调用方（任务入口）从冻结配置传入；
    # 无配置值时用权威默认（与 CLI/GUI 默认一致），不悄悄回退网关隐藏值。
    if thread_num is None or type(thread_num) is not int or thread_num < 1:
        thread_num = DEFAULT_THREAD_NUM
    summary.thread_num = thread_num
    report.viewing_repair = summary
    if flow.mode == "report_only":
        # 非 LLM / 缺失快照：不静默发起 LLM 请求（D15），仅确定性处理与报告。
        summary.warnings.append(f"观看问题模型修复未执行（{flow.reason}）")
        logger.info("观看问题模型修复按仅报告处理：%s", flow.reason)
        _emit(terminal_event(status="report_only", counts={"rounds": 0}))
        return working, report

    repair_profile = flow.main_profile
    assert repair_profile is not None  # mode main / main_review 保证非空
    radius = summary.boundary_context_radius
    # 并发闸在任务开始时冻结并进入报告（票 04）：窗口值随修复角色
    # ``clamped_concurrency`` 钳制（显式 profile 保护上限生效，ADR-0018）。
    concurrency_gate = repair_profile.clamped_concurrency(thread_num)
    summary.concurrency_gate = concurrency_gate
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
            summary.warnings.append(f"观看问题修复回退区域（初版段 {list(indices)}）：{reason}")
        else:
            summary.warnings.append(f"观看问题修复回退失败（初版段 {list(indices)}）：{reason}")
        closed_regions.update(indices)
        for identity in [i for i in accepted if i[0] in set(indices)]:
            accepted.discard(identity)

    def _raise_if_cancelled() -> None:
        if cancelled is not None and cancelled():
            raise InterruptedError("LLM request cancelled")

    with borrow_utility_gateway(gateway) as runtime:
        try:
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
                    batch_input_estimator=lambda subjects_in_batch,
                    context: _estimate_request_input(
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
                # 批量校对调度（票 05）：主修复响应到达即归并该批，归并完成
                # 立即按容量规划派生该校对请求（与仍在途的其他主修复请求
                # 重叠执行，不等整轮主修复完成——spec「不等待整轮主修复
                # 全部完成才启动任何校对」）。校对窗口 = 校对角色钳制值与
                # 主修复闸的较小值（同 profile 共享闸，ADR-0018 不相乘）；
                # 应用段写 state 片段对象引用，互不重叠主体无锁竞争，
                # summary 聚合由轮末按提交序执行（可交换，完成顺序无关）。
                review_window = min(
                    (
                        flow.review_profile.clamped_concurrency(thread_num)
                        if flow.mode == "main_review" and flow.review_profile is not None
                        else concurrency_gate
                    ),
                    concurrency_gate,
                )
                review_executor = (
                    ThreadPoolExecutor(max_workers=max(1, review_window))
                    if flow.mode == "main_review" and flow.review_profile is not None
                    else None
                )
                # 复校观测锁（票 05，对齐票 04 inflight_lock 先例）：review
                # 窗口多线程并发自增计数必须加锁——``+=`` 非原子可丢计数。
                review_lock = threading.Lock()
                review_futures: "List[Future[Tuple[int, List[str]]]]" = []
                # 校对发送前的取消检查闸（票 06）：停止被接受后窗口线程不再
                # 发新校对请求（请求发送前的最后一次取消检查，spec 第 6 条）。
                review_stop_gate = threading.Event()
                # 校对在途观测（票 07）：等待刷新事件的 review 快照口径。
                review_inflight = {"active": 0}
                # 修复轮次快照（ADR-0021）：本轮全部请求的主体与边界上下文
                # 共享 ``round_snapshot``（轮初 state 的冻结副本）；同轮邻批
                # 已归并的拆分 / 校对结果不进入本批载荷。下一轮才读取归并后
                # 的字幕状态（轮首重建快照）。
                round_snapshot = state.as_data()
                # 轮次事件（票 07）：分母逐轮显式——重规划 / 拆分 / 回退的
                # 分母变化不藏在伪单调百分比里（spec「批次和问题计数口径」）。
                _emit(
                    round_event(
                        round_index=summary.rounds,
                        open_problems=len(open_problems),
                        batches=len(plan.batches),
                        window=min(concurrency_gate, max(1, len(plan.batches))),
                        # P5 修复（spec 决策 4「显示实际生效值」）：冻结并发
                        # 经角色钳制后的本轮实际闸进轮次事件，前端不再只从
                        # 状态载荷 / 日志读（用户故事 28 等待期限同理已带）。
                        concurrency_gate=concurrency_gate,
                        # 与简单百分比消费者同一事实口径（票 07）：事件
                        # 通道无条件携带，前端不再各自推导。
                        percent=min(90, 55 + summary.rounds * 4),
                    )
                )
                if progress is not None:
                    progress(
                        min(90, 55 + summary.rounds * 4),
                        f"正在修复观看问题（第 {summary.rounds} 轮，{len(open_problems)} 个未解决）",
                    )
                # 阶段 1（受控并发发出，票 04）+ 阶段 2（定序归并）：worker 只
                # 发请求并带回响应 / 传输错误与观测，不触碰共享字幕与重试簿记
                # （ADR-0021「worker 只产生候选」）；归并按 ``reversed(plan.batches)``
                # 固定序在协调线程执行，任意完成顺序产生相同字幕、验收与报告。
                # 窗口 = 并发闸与批数的较小值：少量批次收缩到批数，不因无收益
                # 的固定首批预热强制串行（spec「有界并发与入口配置」）。
                ordered_batches = list(reversed(plan.batches))
                window = min(concurrency_gate, len(ordered_batches))
                # 有效并发取各轮最大值：尾轮批数收缩不抹掉此前轮次的真实窗口。
                summary.effective_concurrency = max(summary.effective_concurrency, window)
                if window > 1:
                    summary.concurrent_rounds += 1
                inflight_state = {"active": 0, "max": 0}
                inflight_lock = threading.Lock()

                def _send(order: int) -> "_BatchOutcome":
                    batch = ordered_batches[order]
                    _raise_if_cancelled()
                    with inflight_lock:
                        # 发出层计数（票 04）：通过发送前取消检查、真正进入
                        # 网关调用的请求才计数；取消拦下的未发请求不计。
                        summary.requests += 1
                    payload = _build_payload(round_snapshot, cfg, batch, segment_problems, feedback)
                    with inflight_lock:
                        inflight_state["active"] += 1
                        inflight_state["max"] = max(inflight_state["max"], inflight_state["active"])
                    # 等待刷新（票 07）：请求在途期间以节流间隔持续更新等待
                    # 时长 / 在途 / 排队（01 冻结门槛 ≤0.50s 的 2.5 倍余量）；
                    # 不依赖 token streaming，请求返回即停（下个请求重新起算）。
                    refresher = _start_refresher(
                        _emit,
                        on_event,
                        lambda: {
                            "window": window,
                            "inflight": inflight_state["active"],
                            "queued": max(0, len(ordered_batches) - summary.requests),
                        },
                        round_index=lambda: summary.rounds,
                        role="main",
                        request_window_s=request_window_seconds(repair_profile),
                    )
                    try:
                        response = runtime.complete(
                            repair_profile,
                            LLMRequest(
                                messages=tuple(_request_messages(payload, main_guidance)),
                                max_output_tokens=repair_profile.max_output_tokens,
                                metadata={
                                    "stage": "viewing_repair",
                                    "role": "utility",
                                    "task_id": task_id,
                                    "round": str(summary.rounds),
                                    "batch": str(order),
                                },
                            ),
                            cancelled=cancelled,
                        )
                    except InterruptedError:
                        raise
                    except Exception as exc:  # noqa: BLE001 —— 传输级失败不消耗业务重试
                        logger.warning("观看问题修复请求失败（传输级）: %s", exc)
                        _emit(
                            retry_event(
                                round_index=summary.rounds,
                                batch_index=order,
                                category="transport",
                                reason=str(exc),
                            )
                        )
                        return _BatchOutcome(order, None, error=exc)
                    finally:
                        if refresher is not None:
                            refresher.stop()
                        with inflight_lock:
                            inflight_state["active"] -= 1
                    return _BatchOutcome(order, response)

                def _merge_batch(order: int, outcome: "_BatchOutcome") -> None:
                    """归并一个主修复批并派生其批量校对（完成回调线程，票 05）。

                    由 ``_dispatch_ordered`` 按完成顺序逐批调用：主修复响应
                    一到达即归并（共享 state 写只在本函数串行，无并发写），
                    随后立即按容量规划把该批已拼接主体派生为批量校对请求
                    ——不等整轮主修复完成（spec「不等待整轮主修复全部完成
                    才启动任何校对」）。校对发送在 review 窗口线程执行
                    （网络等待不阻塞归并循环）。
                    """
                    nonlocal round_accepted, round_transport_failed
                    _raise_if_cancelled()
                    batch = ordered_batches[order]
                    # 本批已拼接主体的校对分组（批内固定段序收集）。
                    batch_review_entries: List[_ReviewSubjectEntry] = []
                    # 本批归并前的累计验收通过数（批事件的本批 / 累计分列）。
                    accepted_before_batch = len(accepted)
                    batch_problem_map = {
                        problem.problem_id: problem
                        for problem in open_problems
                        if any(
                            subject.start_index <= problem.segment_index < subject.end_index
                            for subject in batch.subjects
                        )
                    }
                    if outcome.error is not None:
                        # 传输级失败：不消耗业务重试（D09），同轮其他批次照常归并。
                        round_transport_failed = True
                        return
                    fatal, grouped = _parse_response(outcome.response.text, batch_problem_map)
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
                        # 业务级拒绝（票 07）：响应不可用是可观察的重试原因
                        # （计入反馈进入下一轮，不静默跳过）。P2 修复：带上
                        # 本批问题的业务请求累计口径（用户故事 28 重试次数可见）。
                        _emit(
                            retry_event(
                                round_index=summary.rounds,
                                batch_index=order,
                                category="business",
                                reason=fatal,
                                attempt_count=max(
                                    (
                                        attempts.get(_identity(problem), 0)
                                        for problem in batch_problem_map.values()
                                    ),
                                    default=0,
                                ),
                            )
                        )
                        return

                    # 阶段 2（批内）：固定段序降序归并本批候选。
                    for subject in reversed(batch.subjects):
                        region = state.initial_indices(subject.start_index, subject.end_index)
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
                        spliced = state.splice(subject.start_index, subject.end_index, replacements)
                        if not spliced:
                            continue
                        # 区域状态重复检测（D27）：拼回后状态与历史一致即回退。
                        indices = state.initial_indices(
                            subject.start_index, subject.start_index + spliced
                        )
                        region_fp = _fingerprint(state.region_state(indices))
                        if any(region_fp in state_fps.get(index, set()) for index in indices):
                            _rollback(indices, "区域状态重复（无改善）")
                            continue
                        for index in indices:
                            state_fps.setdefault(index, set()).add(region_fp)
                        summary.spliced_fragments += spliced
                        # 增强流程（票 05 批量化收集）：拼接成功后把已接受候选
                        # 收进本主修复批的校对分组（不发请求）；本批主体循环
                        # 结束后立即按容量规划分组进入批量复校请求（不等整轮
                        # 主修复完成）。复校上下文以轮次快照为底（ADR-0021）：
                        # 不读邻批刚完成的归并。
                        if review_executor is not None:
                            # 拼接时捕获对象引用：应用段定位不受同轮后续 splice
                            # 的段序漂移影响（state.splice 在原位替换本区域）。
                            state_refs = state.segments[
                                subject.start_index : subject.start_index + spliced
                            ]
                            entry = _review_subject_entry(
                                region_start=subject.start_index,
                                accepted_entries=accepted_entries,
                                replacements=replacements,
                                segment_problems=segment_problems,
                                state_refs=state_refs,
                            )
                            if entry is not None:
                                batch_review_entries.append(entry)
                        round_accepted = True

                    # 批归并验收事件（票 07）：模型返回不直接计为问题解决，
                    # 只有归并验收通过的计数进入「已通过」——本批 / 累计分列，
                    # 分母由 round 事件显式携带（不藏在伪单调百分比里）。
                    _emit(
                        batch_event(
                            round_index=summary.rounds,
                            batch_index=order,
                            accepted_in_batch=len(accepted) - accepted_before_batch,
                            accepted_total=len(accepted),
                            subjects=len(batch.subjects),
                        )
                    )

                    # 批末调度（票 05）：本主修复批已拼接主体 → 容量规划分组
                    # → review 窗口提交（与仍在途的其他主修复请求重叠执行，
                    # 不等整轮主修复完成）。失败语义：传输/解析失败保留主翻译
                    # 候选并告警（spec：校对失败不回退已合格主修复、不标记
                    # 成功）；同批其他组合法校对项独立验收。
                    if review_executor is not None and batch_review_entries:
                        # 同批主体共享批级候选视图（ADR-0021「快照叠加对应
                        # 主候选的明确视图」：批 = 稳定输入分组，本批全部主体
                        # 的候选对整批可见——与完成顺序无关，组内一致）。
                        batch_view: Dict[int, Dict[str, Any]] = {}
                        for item in batch_review_entries:
                            batch_view.update(item.candidate_view)
                        for item in batch_review_entries:
                            merged = dict(batch_view)
                            merged.update(item.candidate_view)
                            item.candidate_view = merged
                        review_plan = _plan_review_groups(
                            round_snapshot,
                            cfg,
                            batch_review_entries,
                            radius=radius,
                            guidance=review_guidance,
                            token_budget=(
                                flow.review_profile.work_context_tokens
                                if flow.review_profile is not None
                                else None
                            ),
                            work_context_tokens=(
                                flow.review_profile.work_context_tokens
                                if flow.review_profile is not None
                                else None
                            ),
                            max_output_tokens=(
                                flow.review_profile.max_output_tokens
                                if flow.review_profile is not None
                                else None
                            ),
                        )
                        summary.review_planned_requests += len(review_plan.groups)
                        summary.review_planned_subjects += sum(
                            len(group.subjects) for group in review_plan.groups
                        )
                        summary.review_unplannable_subjects += review_plan.unplannable_subjects
                        summary.review_shrunk_subject_groups += review_plan.shrunk_subject_groups
                        summary.review_shrunk_context_groups += review_plan.shrunk_context_groups
                        for group in review_plan.groups:
                            summary.review_planned_input_tokens += group.input_tokens
                            summary.review_planned_output_reserve_tokens += (
                                group.output_reserve_tokens
                            )
                        if review_plan.unplannable_subjects:
                            summary.warnings.append(
                                "观看问题修复高级校对容量不足（零上下文单主体仍超出 "
                                "token 预算）：保留主修复候选，跳过 "
                                f"{review_plan.unplannable_subjects} 个主体的复校"
                            )
                        # 闭包内类型收窄：批末调度段仅在 review_profile 非 None
                        # 时到达（review_executor 仅在 main_review 创建）。
                        review_profile = flow.review_profile
                        assert review_profile is not None
                        for group in review_plan.groups:
                            # review_index 由协调线程在 submit 前分配（票 07 审查
                            # 修复）：worker 内读 ``len(review_futures)`` 与协调线程
                            # 的 append 并发竞争，索引可能重复 / 错位——默认参数
                            # 捕获同 ``group=group`` 先例。
                            review_index = len(review_futures)

                            def _run_review(
                                group: "_ReviewGroup" = group,
                                review_index: int = review_index,
                            ) -> Tuple[int, List[str]]:
                                # 停止被接受后不再发新校对请求（票 06：请求发送前
                                # 的最后一次取消检查；竞态中已发出的按在途处理）。
                                if review_stop_gate.is_set():
                                    return 0, []
                                _raise_if_cancelled()
                                # 发出层计数（票 05）：进入网关调用前计数——传输
                                # 失败的请求已发生，照常计入（缓存命中不减）；
                                # review 窗口多线程并发，加锁（票 04 同型先例）。
                                with review_lock:
                                    summary.review_requests += 1
                                    review_inflight["active"] += 1
                                payload = _build_review_payload(
                                    round_snapshot, cfg, group.subjects, group.radius
                                )
                                review_refresher = _start_refresher(
                                    _emit,
                                    on_event,
                                    lambda: {
                                        "window": max(1, review_window),
                                        "inflight": review_inflight["active"],
                                        "queued": max(
                                            0,
                                            summary.review_planned_requests
                                            - summary.review_requests,
                                        ),
                                    },
                                    round_index=lambda: summary.rounds,
                                    role="review",
                                    request_window_s=request_window_seconds(review_profile),
                                )
                                try:
                                    response = runtime.complete(
                                        review_profile,
                                        LLMRequest(
                                            messages=tuple(
                                                _review_request_messages(payload, review_guidance)
                                            ),
                                            max_output_tokens=review_profile.max_output_tokens,
                                            metadata={
                                                "stage": "viewing_repair_review",
                                                "role": "utility",
                                                "task_id": task_id,
                                                "round": str(summary.rounds),
                                                "batch": str(review_index),
                                            },
                                        ),
                                        cancelled=cancelled,
                                    )
                                except InterruptedError:
                                    raise
                                except Exception as exc:  # noqa: BLE001 —— 复校传输失败保留主翻译候选
                                    logger.warning(
                                        "观看问题修复高级校对请求失败（保留主翻译候选）: %s",
                                        exc,
                                    )
                                    _emit(
                                        retry_event(
                                            round_index=summary.rounds,
                                            batch_index=review_index,
                                            category="transport",
                                            reason=str(exc),
                                        )
                                    )
                                    return 0, [f"高级校对复校请求失败，保留主翻译候选: {exc}"]
                                finally:
                                    if review_refresher is not None:
                                        review_refresher.stop()
                                    with review_lock:
                                        review_inflight["active"] -= 1
                                bindings: Dict[Tuple[str, int], Dict[str, Any]] = {}
                                for subject_entry in group.subjects:
                                    bindings.update(subject_entry.bindings)
                                fatal, corrections = _parse_review_response(response.text, bindings)
                                if fatal is not None:
                                    # P2 修复（用户故事 28）：业务拒绝带本组绑定问题的
                                    # 业务请求累计——``region_start`` 是主体 working 段序
                                    # （``segment_problems`` 同一坐标系），``_identity``
                                    # 再映射回初版段序（``attempts`` 键）；校对失败
                                    # 不消耗业务重试（保留主候选），计数是主修复口径。
                                    group_identities = [
                                        _identity(problem)
                                        for subject_entry in group.subjects
                                        for index in range(
                                            subject_entry.region_start,
                                            subject_entry.region_start
                                            + subject_entry.region_span,
                                        )
                                        for problem in segment_problems.get(index, [])
                                    ]
                                    _emit(
                                        retry_event(
                                            round_index=summary.rounds,
                                            batch_index=review_index,
                                            category="business",
                                            reason=fatal,
                                            attempt_count=max(
                                                (
                                                    attempts.get(identity, 0)
                                                    for identity in group_identities
                                                ),
                                                default=0,
                                            ),
                                        )
                                    )
                                    return 0, [f"高级校对复校响应被拒（保留主翻译候选）: {fatal}"]
                                # 应用前重确认取消（spec「停止全路径」：请求返回后、
                                # 校对应用前重新确认——取消竞态下迟到校订不得写回）。
                                _raise_if_cancelled()
                                applied, _missing, warnings = _apply_review_corrections(
                                    state, cfg, layout, group.subjects, corrections
                                )
                                return applied, warnings

                            review_futures.append(review_executor.submit(_run_review))

                # 归并搬到完成回调（票 05）：主修复响应一到达即归并该批并
                # 派生校对请求（重叠执行）；返回值固定批序仍供异常/观测路径。
                # review executor 在取消 / 异常路径也必须有界关闭（spec：
                # 残留回调不得继续调度或写回字幕）。取消路径置 stop gate：
                # 未发送的校对不再发出（gate 检查），在途校对由网关取消通道
                # 尽快解除；shutdown 等待的就是这些已发出请求的自然结束
                # （有界：max_workers 个在途 × 网关总预算上界）。
                try:
                    _dispatch_ordered(
                        len(ordered_batches),
                        _send,
                        window=window,
                        cancelled=cancelled,
                        on_merged=_merge_batch,
                    )
                    # 在途观测是发出层口径（含瞬时缓存命中）；真实 adapter 在途由
                    # 传输探针口径覆盖（spec：缓存命中不冒充模型吞吐）。
                    summary.max_inflight = max(summary.max_inflight, inflight_state["max"])
                    # 轮末聚合（票 05）：等待本轮全部批量校对请求（提交序聚合，
                    # 应用段可交换——对象引用定位，完成顺序不影响最终字幕）。
                    for future in review_futures:
                        applied, review_warnings = future.result()
                        summary.review_corrections += applied
                        summary.warnings.extend(
                            warning
                            for warning in review_warnings
                            if warning not in summary.warnings
                        )
                    review_futures.clear()
                except BaseException:
                    # 取消 / 异常路径（票 06）：停止被接受 → 未发校对不再发；
                    # 已提交的 future 逐个聚合（丢弃迟到校订写回的机会——
                    # _run_review 应用前重确认取消，迟到结果无副作用），再关闭。
                    review_stop_gate.set()
                    for future in review_futures:
                        try:
                            future.result()
                        except Exception:  # noqa: BLE001 —— 取消路径的迟到结果不写回（含 InterruptedError）
                            continue
                    raise
                finally:
                    if review_executor is not None:
                        review_executor.shutdown(wait=True)
                if round_accepted:
                    transport_streak = 0
                elif round_transport_failed:
                    transport_streak += 1
                    if transport_streak >= MAX_TRANSPORT_FAILURE_ROUNDS:
                        summary.warnings.append("观看问题修复连续传输失败，已停止修复循环")
                        break

        except InterruptedError:
            # 取消终态事件由调用方（runner）统一发射（票 07 审查修复）：
            # 修复层与任务层各发一次会重复「修复已停止」终态——spec
            # 「无重复完成」；这里只上抛，runner 的 except 恰发一次。
            raise
    summary.resolved_problem_count = len(accepted)
    repaired = state.as_data()
    # 终态以交付字幕上的重新扫描为准（D27）：回退区域保持未解决，不记为通过。
    report.viewing_problems = scan_viewing_lengths(repaired, cfg, layout)
    # 已接受但终态仍超限的问题同样不记为通过：以终态扫描为验收基准。
    report.segment_count = len(repaired.segments)
    if summary.rounds >= MAX_ROUNDS:
        summary.warnings.append("观看问题修复达到轮数上界，提前停止")
    # 终态事件（票 07）：成功 / 失败均有明确终态；取消路径在 except
    # 外层发（本函数不吞取消——InterruptedError 上抛由 runner 收尾）。
    _emit(
        terminal_event(
            status="completed",
            counts={
                "rounds": summary.rounds,
                "requests": summary.requests,
                "resolved": summary.resolved_problem_count,
                "unresolved": len(report.unresolved_viewing_problems()),
                "rollbacks": len(summary.rollbacks),
            },
        )
    )
    logger.info(
        "观看问题修复：%d 轮 / %d 次请求，解决 %d 个问题，回退 %d 区域，容量不足 %d 主体",
        summary.rounds,
        summary.requests,
        summary.resolved_problem_count,
        len(summary.rollbacks),
        summary.unplannable_subjects,
    )
    if summary.requests:
        # 受控并发观测（票 04，spec 第 7 条）：有效并发、最大在途与冻结闸
        # 进入日志；这是窗口行为观察，不是真实模型吞吐证据。
        logger.info(
            "观看问题修复并发：冻结 %d / 闸 %d / 最大在途 %d（窗口轮 %d）",
            summary.thread_num,
            summary.concurrency_gate,
            summary.max_inflight,
            summary.concurrent_rounds,
        )
    if summary.flow_mode == "main_review" and summary.review_requests:
        # 批量校对观测（票 05，spec 第 7 条）：请求数、主体覆盖与容量
        # 口径进入日志；受控组批观察，不是真实模型吞吐证据。
        logger.info(
            "观看问题修复批量校对：请求 %d（规划 %d / 覆盖主体 %d / 修正 %d 处，"
            "容量不足主体 %d / 缩主体组 %d / 缩上下文组 %d）",
            summary.review_requests,
            summary.review_planned_requests,
            summary.review_planned_subjects,
            summary.review_corrections,
            summary.review_unplannable_subjects,
            summary.review_shrunk_subject_groups,
            summary.review_shrunk_context_groups,
        )
    return repaired, report


__all__ = [
    "DEFAULT_BUSINESS_RETRIES",
    "DEFAULT_THREAD_NUM",
    "MAX_FRAGMENTS_CAP",
    "MAX_TRANSPORT_FAILURE_ROUNDS",
    "RegionRollback",
    "RepairSummary",
    "execute_viewing_repair",
    "select_repair_flow",
]
