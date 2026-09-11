"""观看问题规划：问题身份、修复主体与边界上下文、批次容量收缩。

设计记录 D16/D18/D22/D26（见 docs/dev/subtitle-postprocessing-design-record.md）：
- 扫描汇集的问题（长度 / 速度 / 语义 / 结构）统一为 ``PlanProblem``，
  每个问题保留独立身份；修复以问题区域为主体，各主体向上下
  扩展 ``boundary_context_radius`` 个相邻段作为边界上下文。
- 边界上下文与主体分开表示，不作为默认修改目标（D16）；主体区间
  永远从上下文中剔除，同批独立主体之间的间隙段在 radius 覆盖内时
  成为共享上下文（D16/D22）。
- 主体重叠或相邻时合并为一个修复主体，同时保留原问题 ID、原因
  和区域级验收关系；仅上下文重叠而主体独立时保持主体独立（D22）。
- 上下文范围直接复用任务快照中的 ``boundary_context_radius``
  （``entities.py`` ``SubtitleConfig`` 默认 3；上游 token planner 的
  ``context_radius`` 同样默认 3），不产生第二套用户设置（D18）。
- 请求容量不足时先减少主体数量，再逐步减少上下文（沿用上游
  token planner 的收缩顺序，D26）；单个主体在零上下文下仍超出
  预算时明确报告容量不足，不截断字幕内容。

本模块只读消费字幕与问题，不修改字幕；供核心任务入口与
修复执行（票 05）复用，gateway 由调用方注入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, List, Literal, Optional, Sequence

from .viewing import Side, ViewingProblem

if TYPE_CHECKING:
    from ..asr.asr_data import ASRData

# 上游默认边界上下文半径（D18）：与 entities.py SubtitleConfig.boundary_context_radius
# 的默认值保持一致，后处理不新增第二套设置。
DEFAULT_BOUNDARY_CONTEXT_RADIUS = 3

ProblemKind = Literal["length", "speed", "semantic", "structure"]


@dataclass
class PlanProblem:
    """规划消费的统一问题记录：稳定身份 + 独立验收关系。

    长度问题由 ``ViewingProblem`` 适配而来；速度 / 语义 / 结构问题
    由调用方按同一身份契约构造。``problem_id`` 在一次任务内稳定，
    合并主体只改变归属批次，不改变问题身份。
    """

    problem_id: str
    kind: ProblemKind
    side: Side
    segment_index: int
    reason: str
    resolved: bool = False
    # 诊断载荷（长度问题的度量 / 速度问题的 CPS 等），随问题透传。
    detail: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.problem_id:
            raise ValueError("problem_id must not be empty")
        if self.segment_index < 0:
            raise ValueError("segment_index must not be negative")


def problems_from_viewing(viewing: Sequence[ViewingProblem]) -> List[PlanProblem]:
    """把单行限长扫描出的 ``ViewingProblem`` 适配为统一问题记录。

    行数问题归入结构问题（行结构违反单行显示）；长度问题保持
    长度类别。原问题 ID 原样保留，供批次响应显式绑定。
    """
    adapted: List[PlanProblem] = []
    for problem in viewing:
        kind: ProblemKind = "structure" if problem.problem_id.startswith("lines:") else "length"
        adapted.append(
            PlanProblem(
                problem_id=problem.problem_id,
                kind=kind,
                side=problem.side,
                segment_index=problem.segment_index,
                reason=problem.reason,
                resolved=problem.resolved,
                detail={
                    "weighted_length": problem.weighted_length,
                    "absolute_limit": problem.absolute_limit,
                    "target_limit": problem.target_limit,
                    "text": problem.text,
                },
            )
        )
    return adapted


@dataclass
class RepairSubject:
    """一个修复主体：合并后的问题区域，携带原问题身份。

    主体以字幕段区间表示；``problem_ids`` 保序去重，``reasons``
    与问题一一对应。区域级验收与回退（D13）以主体为单位。
    """

    start_index: int
    end_index: int  # 半开区间 [start, end)
    problem_ids: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.start_index < 0 or self.end_index <= self.start_index:
            raise ValueError("subject span must be a non-empty ascending range")

    @property
    def size(self) -> int:
        return self.end_index - self.start_index

    def covers(self, index: int) -> bool:
        return self.start_index <= index < self.end_index


@dataclass
class BoundaryContext:
    """一个批次的边界上下文：主体外相邻参考段，不是修改目标。

    ``spans`` 是互不相交的升序半开区间；主体区间永远不在其中。
    同批独立主体之间的间隙段在 radius 覆盖内时作为共享上下文
    出现（D16/D22：仅上下文重叠不合并主体）。
    """

    spans: tuple[tuple[int, int], ...] = ()

    def is_empty(self) -> bool:
        return not self.spans

    def segment_count(self) -> int:
        """上下文覆盖的段数（供预算与测试断言）。"""
        return sum(end - start for start, end in self.spans)


def _context_spans(
    subjects: Sequence[RepairSubject], radius: int, segment_count: int
) -> BoundaryContext:
    """各主体上下扩展 ``radius`` 个相邻段、剔除主体区间后的共享上下文。

    逐主体扩展再取并集（D16「向主体上下扩展」），主体段从上下文中
    剔除（D16 上下文不成为修改目标）；独立主体之间的间隙段因此
    可以同时出现在两个主体的上下文里（D22 共享上下文）。
    """
    if radius < 0:
        raise ValueError("boundary context radius must not be negative")
    covered: set[int] = set()
    context: set[int] = set()
    for subject in subjects:
        covered.update(range(subject.start_index, subject.end_index))
        context.update(range(max(0, subject.start_index - radius), subject.start_index))
        context.update(range(subject.end_index, min(segment_count, subject.end_index + radius)))
    context -= covered
    spans: List[tuple[int, int]] = []
    for index in sorted(context):
        if spans and index == spans[-1][1]:
            spans[-1] = (spans[-1][0], index + 1)
        else:
            spans.append((index, index + 1))
    return BoundaryContext(spans=tuple(spans))


def merge_subjects(
    problems: Sequence[PlanProblem], *, segment_count: int
) -> List[RepairSubject]:
    """按主体重叠或相邻合并问题区域，保留原问题 ID 与原因（D22）。

    主体按段区间排序后扫描：两个问题的主体区间重叠或直接相邻
    （中间无间隙段）时合并为一个主体。仅边界上下文会重叠而
    主体独立的输入自然保持独立（本函数只看主体区间，不扩展上下文）。
    """
    if segment_count < 0:
        raise ValueError("segment_count must not be negative")
    if not problems:
        return []
    if any(problem.segment_index >= segment_count for problem in problems):
        raise ValueError("problem segment_index exceeds segment_count")

    # 单段主体按段序稳定排序；同段多问题按 ID 保序，合并结果可复现。
    ordered = sorted(problems, key=lambda p: (p.segment_index, p.problem_id))
    subjects: List[RepairSubject] = []
    for problem in ordered:
        start, end = problem.segment_index, problem.segment_index + 1
        if subjects:
            last = subjects[-1]
            if start <= last.end_index:  # 重叠或直接相邻：并入当前主体。
                last.end_index = max(last.end_index, end)
                if problem.problem_id not in last.problem_ids:
                    last.problem_ids.append(problem.problem_id)
                    last.reasons.append(problem.reason)
                continue
        subjects.append(
            RepairSubject(
                start_index=start,
                end_index=end,
                problem_ids=[problem.problem_id],
                reasons=[problem.reason],
            )
        )
    return subjects


@dataclass
class RepairBatch:
    """一批修复请求：主体列表 + 分开表示的边界上下文。

    响应必须显式包含问题 ID 与输出段序号（票 05 消费）；
    ``estimated_tokens`` 是请求输入估算：默认路径只计主体/上下文
    载荷，调用方注入 ``batch_input_estimator``（票 03）时为完整
    序列化请求（系统提示词 + guidance + limits + feedback 协议开销）。
    ``output_reserve_tokens`` 是输出预留（票 03：一对多拆分 + 原译
    对应 + 协议开销）；``context_radius`` 是本批实际使用的上下文
    半径（收缩可观察，票 03 验收）。
    """

    subjects: List[RepairSubject]
    context: BoundaryContext
    estimated_tokens: int = 0
    output_reserve_tokens: int = 0
    context_radius: int = DEFAULT_BOUNDARY_CONTEXT_RADIUS


@dataclass
class RepairPlan:
    """一次问题修复轮次的规划结果（票 05 的输入）。"""

    batches: List[RepairBatch] = field(default_factory=list)
    # 容量不足的主体（零上下文下仍超出预算）：明确报告，不截断内容。
    unplannable: List[RepairSubject] = field(default_factory=list)

    @property
    def subject_count(self) -> int:
        return sum(len(batch.subjects) for batch in self.batches)

    def problem_ids(self) -> List[str]:
        ids: List[str] = []
        for batch in self.batches:
            for subject in batch.subjects:
                ids.extend(subject.problem_ids)
        return ids


def estimate_plan_tokens(
    asr_data: "ASRData",
    subjects: Sequence[RepairSubject],
    context: BoundaryContext,
) -> int:
    """保守估算一批请求的输入 token（不含固定 prompt 与输出预留）。

    计价直接复用上游 token planner 的 ``estimate_tokens``（单一来源，
    D26 沿用上游做法）；主体与上下文分开编码，边界上下文显式
    标注，不与主体混淆。
    """
    import json

    from ..translate.enhanced.token_planner import estimate_tokens

    def _segment_payload(index: int) -> dict:
        segment = asr_data.segments[index]
        return {
            "id": index,
            "text": segment.text,
            "translated": segment.translated_text,
        }

    payload = {
        "boundary_context": [
            _segment_payload(index) for start, end in context.spans for index in range(start, end)
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
            for subject in subjects
        ],
    }
    return estimate_tokens(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def plan_repair_batches(
    asr_data: "ASRData",
    problems: Sequence[PlanProblem],
    *,
    boundary_context_radius: int = DEFAULT_BOUNDARY_CONTEXT_RADIUS,
    token_budget: Optional[int] = None,
    max_subjects_per_batch: int = 10,
    batch_input_estimator: Optional[
        Callable[[Sequence[RepairSubject], BoundaryContext], int]
    ] = None,
    output_reserve_estimator: Optional[
        Callable[[Sequence[RepairSubject]], int]
    ] = None,
) -> RepairPlan:
    """把问题规划为批量修复请求（D16/D22/D26，票 03 接通容量）。

    - 主体先合并（重叠 / 相邻），每批主体各带 ``boundary_context_radius``
      个相邻段上下文；上下文与主体分开表示。
    - 容量口径（票 03，对齐上游 ``plan_translation_batches`` 的 estimator
      先例）：``batch_input_estimator`` 估算一批的完整请求输入（调用方
      覆盖真实 payload 形状：提示词、guidance、limits、feedback、主/译
      文本、问题、上下文与协议开销），``output_reserve_estimator``
      预留输出（一对多拆分 + 原译对应 + 协议开销）；缺省回退到
      ``estimate_plan_tokens`` 的主体/上下文载荷估算（不设输出预留）。
    - 请求超出 ``token_budget`` 时沿用上游收缩顺序：先减少单批主体
      数量，再逐步减少上下文；单个主体在零上下文下仍超出预算时
      列入 ``unplannable`` 明确报告，不截断字幕内容。
    - ``token_budget`` 为 None 表示不设输入预算（默认整批提交）；
      ``max_subjects_per_batch`` 对应上游 ``batch_size``（D16 沿用
      上游翻译的批次做法）。
    """
    if boundary_context_radius < 0:
        raise ValueError("boundary_context_radius must not be negative")
    if max_subjects_per_batch <= 0:
        raise ValueError("max_subjects_per_batch must be positive")

    def _input_estimate(
        subjects_in_batch: Sequence[RepairSubject], context: BoundaryContext
    ) -> int:
        if batch_input_estimator is not None:
            estimated = batch_input_estimator(subjects_in_batch, context)
            if estimated < 0:
                raise ValueError("batch_input_estimator must not return a negative value")
            return estimated
        return estimate_plan_tokens(asr_data, subjects_in_batch, context)

    def _output_reserve(subjects_in_batch: Sequence[RepairSubject]) -> int:
        if output_reserve_estimator is None:
            return 0
        estimated = output_reserve_estimator(subjects_in_batch)
        if estimated < 0:
            raise ValueError("output_reserve_estimator must not return a negative value")
        return estimated

    segment_count = len(asr_data.segments)
    # 已解决问题不进入修复请求（D27：只重试仍未解决问题）。
    open_problems = [problem for problem in problems if not problem.resolved]
    subjects = merge_subjects(open_problems, segment_count=segment_count)
    plan = RepairPlan()
    if not subjects:
        return plan

    def fits(
        subjects_in_batch: Sequence[RepairSubject], radius: int
    ) -> Optional[RepairBatch]:
        context = _context_spans(subjects_in_batch, radius, segment_count)
        estimated = _input_estimate(subjects_in_batch, context)
        reserve = _output_reserve(subjects_in_batch)
        if token_budget is not None and estimated + reserve > token_budget:
            return None
        return RepairBatch(
            subjects=list(subjects_in_batch),
            context=context,
            estimated_tokens=estimated,
            output_reserve_tokens=reserve,
            context_radius=radius,
        )

    cursor = 0
    while cursor < len(subjects):
        subject = subjects[cursor]
        batch: Optional[RepairBatch] = None

        if token_budget is None:
            # 无预算：按 max_subjects_per_batch 切批，主体保持合并后的连续顺序。
            window = subjects[cursor : cursor + max_subjects_per_batch]
            batch = fits(window, boundary_context_radius)
            if batch is not None:
                plan.batches.append(batch)
                cursor += len(window)
                continue
            # fits 在无预算下不可能失败；保底推进防死循环。
            raise RuntimeError("unreachable: fits without budget cannot fail")

        # 收缩顺序（D26）：先减少主体数量，保留完整上下文。
        max_take = min(max_subjects_per_batch, len(subjects) - cursor)
        for take in range(max_take, 0, -1):
            window = subjects[cursor : cursor + take]
            candidate = fits(window, boundary_context_radius)
            if candidate is not None:
                batch = candidate
                break

        # 单主体仍放不下：再逐步减少上下文（radius -> 0）。
        if batch is None:
            for radius in range(boundary_context_radius - 1, -1, -1):
                candidate = fits([subject], radius)
                if candidate is not None:
                    batch = candidate
                    break

        if batch is None:
            # 零上下文单主体仍超出预算：明确报告容量不足（D26），
            # 保留完整文本，不做截断；继续规划其余主体。
            plan.unplannable.append(subject)
            cursor += 1
            continue

        plan.batches.append(batch)
        cursor += len(batch.subjects)

    return plan


__all__ = [
    "DEFAULT_BOUNDARY_CONTEXT_RADIUS",
    "PlanProblem",
    "ProblemKind",
    "RepairSubject",
    "BoundaryContext",
    "RepairBatch",
    "RepairPlan",
    "problems_from_viewing",
    "merge_subjects",
    "plan_repair_batches",
    "estimate_plan_tokens",
]
