"""后处理恢复检查点：阶段末 / 轮末检查点的读写与恢复形状（票 06，ADR-0022）。

检查点身份 = 输入指纹 + 语言对（R05）。两类恢复边界（R04）：

- **阶段末**：确定性阶段、压缩重译、语义修复完成、进入观看问题修复前
  写入；内容 = 工作字幕全量 + 质量报告累积状态（阶段报告、压缩失败、
  速度结果、精确时间轴结论）。仅报告流程没有修复轮次，只写这一级。
- **轮末**：修复轮次归并验收后写入；内容 = 工作字幕全量 + 修复摘要
  （轮次号、请求计数、各问题身份尝试计数、已关闭区域、回退记录、
  下一轮反馈、候选与状态指纹）。

写入顺序固定（票 02 口径）：先原子替换检查点数据文件，manifest 才
登记该级完成。恢复只信 manifest；manifest 登记而文件缺失或不可读的
级别视为未完成并告警。修复循环经完成回调把轮末状态交给运行器落盘，
本模块只做纯数据形状与读写，ADR-0021 的冻结上下文与确定性归并不受
影响。精确时间轴证据不进检查点（既有时间轴缓存已覆盖）。检查点与
manifest 不含 API key、完整 prompt 文本和模型原始响应。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping, Optional

from ..recovery import (
    RECOVERY_MANIFEST_SCHEMA,
    RECOVERY_MANIFEST_VERSION,
    RecoverySummary,
    now_utc,
    software_version,
    write_recovery_manifest,
)
from ..recovery import (
    matching_recovery_manifest as shared_matching_recovery_manifest,
)
from .workspace import normalize_language, normalize_task_name

if TYPE_CHECKING:
    from pathlib import Path

    from ..asr.asr_data import ASRData
    from .repair import RegionRollback, RepairSummary
    from .report import QualityReport, SpeedWarning
    from .viewing import ViewingProblem

# 后处理恢复模块名：manifest ``module`` 字段与恢复摘要共用。
RECOVERY_MODULE = "subtitle_postprocess"
RECOVERY_MANIFEST_FILENAME = "recovery-manifest.json"
# 阶段末 / 轮末检查点数据文件（任务过程目录内稳定文件名，票 06 口径）。
PHASE_CHECKPOINT_FILENAME = "recovery-phase.json"
ROUND_CHECKPOINT_FILENAME = "recovery-round.json"

# 轮末修复循环状态的可序列化形状（repair.py 闭包内局部状态的字段子集）。
# 只含恢复口径需要的字段；规划观测类计数字段不进检查点（恢复后由
# 下一轮重新扫描 / 规划，不虚记完成）。


def _subtitle_payload(data: "ASRData") -> dict[str, Any]:
    """工作字幕全量的可往返载荷（``ASRData.to_json`` 同一形状）。"""

    return {key: dict(value) for key, value in data.to_json().items()}


def _subtitle_from_payload(payload: Any) -> "Optional[ASRData]":
    """从检查点载荷重建字幕；形状不符返回 ``None``（该级视为未完成）。"""

    from ..asr.asr_data import ASRData

    if not isinstance(payload, Mapping) or not payload:
        return None
    try:
        return ASRData.from_json(dict(payload))
    except (KeyError, TypeError, ValueError):
        return None


def _problems_payload(problems: "list[ViewingProblem]") -> list[dict[str, Any]]:
    """观看长度问题的可往返载荷（终态扫描重建 ``report.viewing_problems``）。"""

    return [
        {
            "problem_id": problem.problem_id,
            "side": problem.side,
            "segment_index": problem.segment_index,
            "text": problem.text,
            "weighted_length": problem.weighted_length,
            "absolute_limit": problem.absolute_limit,
            "target_limit": problem.target_limit,
            "reason": problem.reason,
            "resolved": problem.resolved,
        }
        for problem in problems
    ]


def _problems_from_payload(payload: Any) -> "list[ViewingProblem]":
    from .viewing import ViewingProblem

    if not isinstance(payload, list):
        return []
    problems: list[ViewingProblem] = []
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        side = item.get("side")
        if side not in ("original", "translated"):
            continue
        try:
            problems.append(
                ViewingProblem(
                    problem_id=str(item["problem_id"]),
                    side=side,  # type: ignore[arg-type]  # 上面已收窄到 Side
                    segment_index=int(item["segment_index"]),
                    text=str(item["text"]),
                    weighted_length=float(item["weighted_length"]),
                    absolute_limit=float(item["absolute_limit"]),
                    target_limit=float(item["target_limit"]),
                    reason=str(item["reason"]),
                    resolved=bool(item["resolved"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return problems


def _rollback_payload(rollbacks: "list[RegionRollback]") -> list[dict[str, Any]]:
    return [
        {"initial_indices": list(item.initial_indices), "reason": item.reason} for item in rollbacks
    ]


def _rollbacks_from_payload(payload: Any) -> "list[RegionRollback]":
    from .repair import RegionRollback

    if not isinstance(payload, list):
        return []
    rollbacks: list[RegionRollback] = []
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        indices = item.get("initial_indices")
        if not isinstance(indices, list) or not all(
            isinstance(value, int) and not isinstance(value, bool) for value in indices
        ):
            continue
        rollbacks.append(
            RegionRollback(
                initial_indices=tuple(int(value) for value in indices),
                reason=str(item.get("reason", "")),
            )
        )
    return rollbacks


# ---------------------------------------------------------------------------
# 阶段末检查点：工作字幕 + 质量报告累积状态。
# ---------------------------------------------------------------------------


def phase_checkpoint_payload(
    *,
    working: "ASRData",
    report: "QualityReport",
    precise_timing_outcome: Optional[str],
    precise_timing_grades: Optional[tuple[tuple[str, int], ...]],
) -> dict[str, Any]:
    """阶段末检查点载荷：工作字幕全量 + 质量报告累积状态。

    速度结果存 ``result_to_dict`` 载荷（恢复侧 ``result_from_dict``
    重建对象，QA 报告与不中断运行等价）；审计结构按字段逐项往返。
    速度结果不可序列化时存 ``None``（检查点不因单个字段失效）。
    """

    from ..speed.report import result_to_dict

    speed_payload: Any = None
    if report.speed is not None:
        try:
            speed_payload = result_to_dict(report.speed)
        except Exception:  # noqa: BLE001 —— 速度结果序列化失败不废整个检查点
            speed_payload = None

    stages = {
        key: {"name": stage.name, "changed": stage.changed, "samples": list(stage.samples)}
        for key, stage in report.stages.items()
    }
    audit: dict[str, Any] | None = None
    if report.audit is not None:
        source = report.audit

        def _warning(w: "SpeedWarning") -> dict[str, Any]:
            return {
                "index": w.index,
                "start": w.start,
                "end": w.end,
                "duration_s": w.duration_s,
                "is_cjk": w.is_cjk,
                "chars": w.chars,
                "cps": w.cps,
                "limit": w.limit,
                "over_by": w.over_by,
                "text": w.text,
                "translated": w.translated,
                "context": list(w.context),
            }

        audit = {
            "segment_count": source.segment_count,
            "hard": [_warning(w) for w in source.hard],
            "comfort": [_warning(w) for w in source.comfort],
            "long_duration": [
                {
                    "index": item.index,
                    "start": item.start,
                    "end": item.end,
                    "duration_s": item.duration_s,
                    "chars": item.chars,
                    "reason": item.reason,
                    "text": item.text,
                    "translated": item.translated,
                }
                for item in source.long_duration
            ],
            "overlaps": [
                {
                    "index": item.index,
                    "prev_index": item.prev_index,
                    "overlap_ms": item.overlap_ms,
                    "start": item.start,
                    "text": item.text,
                }
                for item in source.overlaps
            ],
        }
    return {
        "schema": "videocaptioner.postprocess_phase_checkpoint",
        "version": 1,
        "working": _subtitle_payload(working),
        "report": {
            "source_path": report.source_path,
            "output_path": report.output_path,
            "segment_count": report.segment_count,
            "stages": stages,
            "audit": audit,
            "placeholder_review": list(report.placeholder_review),
            "compress_failures": list(report.compress_failures),
            "speed": speed_payload,
            "viewing_problems": _problems_payload(report.viewing_problems),
        },
        "precise_timing_outcome": precise_timing_outcome,
        "precise_timing_grades": (
            [[name, count] for name, count in precise_timing_grades]
            if precise_timing_grades
            else None
        ),
    }


class PhaseResumeState:
    """阶段末检查点重建出的恢复状态（runner 装载形状，与轮末对齐）。

    ``working`` / ``report`` 跳过前处理与后处理阶段直接进入修复循环；
    ``precise_timing_outcome`` / ``precise_timing_grades`` 是截至该点的
    对齐时间轴结论（不重跑 timing resolver）。
    """

    def __init__(
        self,
        *,
        working: "ASRData",
        report: "QualityReport",
        precise_timing_outcome: Optional[str],
        precise_timing_grades: Optional[tuple[tuple[str, int], ...]],
    ) -> None:
        self.working = working
        self.report = report
        self.precise_timing_outcome = precise_timing_outcome
        self.precise_timing_grades = precise_timing_grades


def phase_checkpoint_from_payload(payload: Any) -> Optional["PhaseResumeState"]:
    """重建阶段末恢复状态；载荷不可用返回 ``None``。"""

    from ..speed.report import result_from_dict
    from .report import (
        AuditResult,
        DurationAnomaly,
        Overlap,
        QualityReport,
        SpeedWarning,
        StageReport,
    )

    if not isinstance(payload, Mapping):
        return None
    working = _subtitle_from_payload(payload.get("working"))
    if working is None:
        return None
    report_payload = payload.get("report")
    if not isinstance(report_payload, Mapping):
        return None
    report = QualityReport(
        source_path=str(report_payload.get("source_path", "")),
        output_path=str(report_payload.get("output_path", "")),
        segment_count=int(report_payload.get("segment_count", 0) or 0),
    )
    stages = report_payload.get("stages")
    if isinstance(stages, Mapping):
        for key, item in stages.items():
            if not isinstance(item, Mapping):
                continue
            samples = item.get("samples")
            report.stages[str(key)] = StageReport(
                name=str(item.get("name", key)),
                changed=int(item.get("changed", 0) or 0),
                samples=list(samples) if isinstance(samples, list) else [],
            )
    audit_payload = report_payload.get("audit")
    if isinstance(audit_payload, Mapping):

        def _warnings(raw: Any) -> list[SpeedWarning]:
            items: list[SpeedWarning] = []
            if not isinstance(raw, list):
                return items
            for item in raw:
                if not isinstance(item, Mapping):
                    continue
                context = item.get("context")
                items.append(
                    SpeedWarning(
                        index=int(item.get("index", 0)),
                        start=str(item.get("start", "")),
                        end=str(item.get("end", "")),
                        duration_s=float(item.get("duration_s", 0.0)),
                        is_cjk=bool(item.get("is_cjk", False)),
                        chars=int(item.get("chars", 0)),
                        cps=float(item.get("cps", 0.0)),
                        limit=float(item.get("limit", 0.0)),
                        over_by=float(item.get("over_by", 0.0)),
                        text=str(item.get("text", "")),
                        translated=str(item.get("translated", "")),
                        context=list(context) if isinstance(context, list) else [],
                    )
                )
            return items

        def _anomalies(raw: Any) -> list[DurationAnomaly]:
            items: list[DurationAnomaly] = []
            if not isinstance(raw, list):
                return items
            for item in raw:
                if not isinstance(item, Mapping):
                    continue
                items.append(
                    DurationAnomaly(
                        index=int(item.get("index", 0)),
                        start=str(item.get("start", "")),
                        end=str(item.get("end", "")),
                        duration_s=float(item.get("duration_s", 0.0)),
                        chars=int(item.get("chars", 0)),
                        reason=str(item.get("reason", "")),
                        text=str(item.get("text", "")),
                        translated=str(item.get("translated", "")),
                    )
                )
            return items

        def _overlaps(raw: Any) -> list[Overlap]:
            items: list[Overlap] = []
            if not isinstance(raw, list):
                return items
            for item in raw:
                if not isinstance(item, Mapping):
                    continue
                items.append(
                    Overlap(
                        index=int(item.get("index", 0)),
                        prev_index=int(item.get("prev_index", 0)),
                        overlap_ms=int(item.get("overlap_ms", 0)),
                        start=str(item.get("start", "")),
                        text=str(item.get("text", "")),
                    )
                )
            return items

        report.audit = AuditResult(
            segment_count=int(audit_payload.get("segment_count", 0) or 0),
            hard=_warnings(audit_payload.get("hard")),
            comfort=_warnings(audit_payload.get("comfort")),
            long_duration=_anomalies(audit_payload.get("long_duration")),
            overlaps=_overlaps(audit_payload.get("overlaps")),
        )
    failures = report_payload.get("compress_failures")
    if isinstance(failures, list):
        report.compress_failures = [str(item) for item in failures]
    reviews = report_payload.get("placeholder_review")
    if isinstance(reviews, list):
        report.placeholder_review = [str(item) for item in reviews]
    speed_payload = report_payload.get("speed")
    if isinstance(speed_payload, Mapping):
        try:
            report.speed = result_from_dict(dict(speed_payload))
        except (KeyError, TypeError, ValueError):
            report.speed = None
    report.viewing_problems = _problems_from_payload(report_payload.get("viewing_problems"))
    outcome = payload.get("precise_timing_outcome")
    grades_raw = payload.get("precise_timing_grades")
    grades: Optional[tuple[tuple[str, int], ...]] = None
    if isinstance(grades_raw, list):
        pairs = [
            (str(item[0]), int(item[1]))
            for item in grades_raw
            if isinstance(item, (list, tuple)) and len(item) == 2
        ]
        if pairs:
            grades = tuple(pairs)
    return PhaseResumeState(
        working=working,
        report=report,
        precise_timing_outcome=str(outcome) if isinstance(outcome, str) else None,
        precise_timing_grades=grades,
    )


# ---------------------------------------------------------------------------
# 轮末检查点：工作字幕 + 修复循环状态。
# ---------------------------------------------------------------------------


def round_checkpoint_payload(
    *,
    working: "ASRData",
    snapshot: "ASRData",
    origin: "list[int]",
    summary: "RepairSummary",
    closed_regions: "set[int]",
    attempts: "Mapping[tuple[int, str, str], int]",
    last_error: "Mapping[tuple[int, str, str], str]",
    last_subject: "Mapping[tuple[int, str, str], tuple[int, ...]]",
    accepted: "set[tuple[int, str, str]]",
    candidate_fps: "Mapping[int, set[str]]",
    state_fps: "Mapping[int, set[str]]",
    transport_streak: int,
    viewing_problems: "list[ViewingProblem]",
) -> dict[str, Any]:
    """轮末检查点载荷：``_WorkingState`` 全量 + 修复摘要。

    问题身份 ``ProblemIdentity`` 是 ``(初版段序, 显示侧, 问题类别)``；
    序列化为 ``origin|side|kind`` 字符串键。``snapshot`` + ``origin``
    与 working 全量一起存：恢复后 ``_WorkingState`` 完整重建，问题
    身份、区域回退（恢复到检查点基线段）与重复检测与不中断运行等价。
    下一轮反馈由恢复侧从 ``last_error`` 重建（与修复循环同式）。
    """

    def _identity_key(identity: tuple[int, str, str]) -> str:
        # JSON 对象键必须是字符串：``origin|side|kind``（side/kind 不含 |，
        # 见 viewing.py problem_id 构成）。
        return f"{identity[0]}|{identity[1]}|{identity[2]}"

    return {
        "schema": "videocaptioner.postprocess_round_checkpoint",
        "version": 1,
        "working": _subtitle_payload(working),
        "snapshot": _subtitle_payload(snapshot),
        "origin": list(origin),
        "summary": {
            "rounds": summary.rounds,
            "requests": summary.requests,
            "spliced_fragments": summary.spliced_fragments,
            "resolved_problem_count": summary.resolved_problem_count,
            "rollbacks": _rollback_payload(summary.rollbacks),
            "unplannable_subjects": summary.unplannable_subjects,
            "warnings": list(summary.warnings),
            "translation_method": summary.translation_method,
            "flow_mode": summary.flow_mode,
            "main_role": summary.main_role,
            "review_role": summary.review_role,
            "boundary_context_radius": summary.boundary_context_radius,
            "review_corrections": summary.review_corrections,
            "review_requests": summary.review_requests,
            # 批量校对观测（票 05）：全部口径进轮末检查点——恢复运行的
            # 后处理状态（postprocess-state.json）与不中断运行等价（票 06）。
            "review_planned_requests": summary.review_planned_requests,
            "review_planned_subjects": summary.review_planned_subjects,
            "review_planned_input_tokens": summary.review_planned_input_tokens,
            "review_planned_output_reserve_tokens": summary.review_planned_output_reserve_tokens,
            "review_unplannable_subjects": summary.review_unplannable_subjects,
            "review_shrunk_subject_groups": summary.review_shrunk_subject_groups,
            "review_shrunk_context_groups": summary.review_shrunk_context_groups,
            "thread_num": summary.thread_num,
            "concurrency_gate": summary.concurrency_gate,
            "effective_concurrency": summary.effective_concurrency,
            "max_inflight": summary.max_inflight,
            "concurrent_rounds": summary.concurrent_rounds,
        },
        "closed_regions": sorted(closed_regions),
        "attempts": {_identity_key(identity): value for identity, value in attempts.items()},
        "last_error": {_identity_key(identity): error for identity, error in last_error.items()},
        "last_subject": {
            _identity_key(identity): list(subject) for identity, subject in last_subject.items()
        },
        "accepted": sorted(_identity_key(identity) for identity in accepted),
        "candidate_fps": {
            str(index): sorted(fingerprints) for index, fingerprints in candidate_fps.items()
        },
        "state_fps": {
            str(index): sorted(fingerprints) for index, fingerprints in state_fps.items()
        },
        "transport_streak": transport_streak,
        "viewing_problems": _problems_payload(viewing_problems),
    }


def round_checkpoint_from_payload(
    payload: Any,
) -> Optional["RoundResumeState"]:
    """重建轮末恢复状态；载荷不可用返回 ``None``。"""

    from .repair import RepairSummary

    if not isinstance(payload, Mapping):
        return None
    working = _subtitle_from_payload(payload.get("working"))
    if working is None:
        return None
    snapshot = _subtitle_from_payload(payload.get("snapshot"))
    origin_raw = payload.get("origin")
    if (
        snapshot is None
        or not isinstance(origin_raw, list)
        or not all(isinstance(value, int) and not isinstance(value, bool) for value in origin_raw)
    ):
        # snapshot / origin 是等价性的最小完备集：缺任一即视为该级未完成。
        return None
    if len(origin_raw) != len(working.segments) or not origin_raw:
        return None
    if max(origin_raw) >= len(snapshot.segments) or min(origin_raw) < 0:
        return None
    origin = [int(value) for value in origin_raw]
    summary_payload = payload.get("summary")
    if not isinstance(summary_payload, Mapping):
        return None
    summary = RepairSummary(
        rounds=int(summary_payload.get("rounds", 0) or 0),
        requests=int(summary_payload.get("requests", 0) or 0),
        spliced_fragments=int(summary_payload.get("spliced_fragments", 0) or 0),
        resolved_problem_count=int(summary_payload.get("resolved_problem_count", 0) or 0),
        rollbacks=_rollbacks_from_payload(summary_payload.get("rollbacks")),
        unplannable_subjects=int(summary_payload.get("unplannable_subjects", 0) or 0),
        translation_method=str(summary_payload.get("translation_method", "")),
        flow_mode=str(summary_payload.get("flow_mode", "")),
        main_role=str(summary_payload.get("main_role", "")),
        review_role=str(summary_payload.get("review_role", "")),
        boundary_context_radius=int(summary_payload.get("boundary_context_radius", 3) or 3),
        review_corrections=int(summary_payload.get("review_corrections", 0) or 0),
        review_requests=int(summary_payload.get("review_requests", 0) or 0),
        review_planned_requests=int(summary_payload.get("review_planned_requests", 0) or 0),
        review_planned_subjects=int(summary_payload.get("review_planned_subjects", 0) or 0),
        review_planned_input_tokens=int(summary_payload.get("review_planned_input_tokens", 0) or 0),
        review_planned_output_reserve_tokens=int(
            summary_payload.get("review_planned_output_reserve_tokens", 0) or 0
        ),
        review_unplannable_subjects=int(summary_payload.get("review_unplannable_subjects", 0) or 0),
        review_shrunk_subject_groups=int(
            summary_payload.get("review_shrunk_subject_groups", 0) or 0
        ),
        review_shrunk_context_groups=int(
            summary_payload.get("review_shrunk_context_groups", 0) or 0
        ),
        thread_num=int(summary_payload.get("thread_num", 0) or 0),
        concurrency_gate=int(summary_payload.get("concurrency_gate", 0) or 0),
        effective_concurrency=int(summary_payload.get("effective_concurrency", 0) or 0),
        max_inflight=int(summary_payload.get("max_inflight", 0) or 0),
        concurrent_rounds=int(summary_payload.get("concurrent_rounds", 0) or 0),
    )
    warnings = summary_payload.get("warnings")
    if isinstance(warnings, list):
        summary.warnings = [str(item) for item in warnings]

    def _identity(raw: Any) -> Optional[tuple[int, str, str]]:
        if not isinstance(raw, str):
            return None
        parts = raw.split("|")
        if len(parts) != 3:
            return None
        origin, side, kind = parts
        try:
            origin_index = int(origin)
        except ValueError:
            return None
        return (origin_index, side, kind)

    attempts: dict[tuple[int, str, str], int] = {}
    raw_attempts = payload.get("attempts")
    if isinstance(raw_attempts, Mapping):
        for raw_key, value in raw_attempts.items():
            identity = _identity(raw_key)
            if identity is None or not isinstance(value, int) or isinstance(value, bool):
                continue
            attempts[identity] = value
    last_error: dict[tuple[int, str, str], str] = {}
    raw_errors = payload.get("last_error")
    if isinstance(raw_errors, Mapping):
        for raw_key, value in raw_errors.items():
            identity = _identity(raw_key)
            if identity is None or not isinstance(value, str):
                continue
            last_error[identity] = value
    last_subject: dict[tuple[int, str, str], tuple[int, ...]] = {}
    raw_subjects = payload.get("last_subject")
    if isinstance(raw_subjects, Mapping):
        for raw_key, value in raw_subjects.items():
            identity = _identity(raw_key)
            if identity is None or not isinstance(value, list):
                continue
            if not all(isinstance(item, int) and not isinstance(item, bool) for item in value):
                continue
            last_subject[identity] = tuple(int(item) for item in value)
    accepted: set[tuple[int, str, str]] = set()
    raw_accepted = payload.get("accepted")
    if isinstance(raw_accepted, list):
        for raw in raw_accepted:
            identity = _identity(raw)
            if identity is not None:
                accepted.add(identity)
    closed: set[int] = set()
    raw_closed = payload.get("closed_regions")
    if isinstance(raw_closed, list):
        closed = {
            int(value)
            for value in raw_closed
            if isinstance(value, int) and not isinstance(value, bool)
        }
    candidate_fps: dict[int, set[str]] = {}
    raw_candidate = payload.get("candidate_fps")
    if isinstance(raw_candidate, Mapping):
        for key, value in raw_candidate.items():
            try:
                index = int(key)
            except (TypeError, ValueError):
                continue
            if isinstance(value, list):
                candidate_fps[index] = {str(item) for item in value}
    state_fps: dict[int, set[str]] = {}
    raw_state = payload.get("state_fps")
    if isinstance(raw_state, Mapping):
        for key, value in raw_state.items():
            try:
                index = int(key)
            except (TypeError, ValueError):
                continue
            if isinstance(value, list):
                state_fps[index] = {str(item) for item in value}
    transport_raw = payload.get("transport_streak")
    transport_streak = (
        int(transport_raw)
        if isinstance(transport_raw, int) and not isinstance(transport_raw, bool)
        else 0
    )
    return RoundResumeState(
        working=working,
        snapshot=snapshot,
        origin=origin,
        summary=summary,
        closed_regions=closed,
        attempts=attempts,
        last_error=last_error,
        last_subject=last_subject,
        accepted=accepted,
        candidate_fps=candidate_fps,
        state_fps=state_fps,
        transport_streak=transport_streak,
        viewing_problems=_problems_from_payload(payload.get("viewing_problems")),
    )


class RoundResumeState:
    """轮末检查点重建出的修复循环恢复状态（repair 恢复入口的入参形状）。"""

    def __init__(
        self,
        *,
        working: "ASRData",
        snapshot: "ASRData",
        origin: "list[int]",
        summary: "RepairSummary",
        closed_regions: "set[int]",
        attempts: "dict[tuple[int, str, str], int]",
        last_error: "dict[tuple[int, str, str], str]",
        last_subject: "dict[tuple[int, str, str], tuple[int, ...]]",
        accepted: "set[tuple[int, str, str]]",
        candidate_fps: "dict[int, set[str]]",
        state_fps: "dict[int, set[str]]",
        transport_streak: int,
        viewing_problems: "list[ViewingProblem]",
    ) -> None:
        self.working = working
        self.snapshot = snapshot
        self.origin = origin
        self.summary = summary
        self.closed_regions = closed_regions
        self.attempts = attempts
        self.last_error = last_error
        self.last_subject = last_subject
        self.accepted = accepted
        self.candidate_fps = candidate_fps
        self.state_fps = state_fps
        self.transport_streak = transport_streak
        self.viewing_problems = viewing_problems


# ---------------------------------------------------------------------------
# 恢复 manifest：唯一进度真相。
# ---------------------------------------------------------------------------


def recovery_identity(
    *, fingerprint: str, source_language: str, target_language: str
) -> dict[str, str]:
    """检查点身份：输入指纹 + 语言对（R05；后处理无翻译方式维度）。"""

    return {
        "input_fingerprint": fingerprint,
        "source_language": normalize_language(source_language),
        "target_language": normalize_language(target_language),
    }


def new_recovery_manifest(
    *,
    identity: Mapping[str, str],
    task_name: str,
) -> dict[str, Any]:
    """新 manifest：全部未完成（「从头开始」重置也走这一形状）。"""

    timestamp = now_utc()
    return {
        "schema": RECOVERY_MANIFEST_SCHEMA,
        "version": RECOVERY_MANIFEST_VERSION,
        "module": RECOVERY_MODULE,
        "identity": dict(identity),
        "task_name": normalize_task_name(task_name),
        "created_at": timestamp,
        "updated_at": timestamp,
        "software_version": software_version(),
        # 冻结配置摘要（票 07 交付）：本轮先占位，恢复侧未记录即单漂移项。
        "config_fingerprint": None,
        "completed": {
            "phase": False,
            "rounds": 0,
        },
        "interruption": None,
    }


def matching_recovery_manifest(
    path: "Path",
    *,
    identity: Mapping[str, str],
) -> Optional[dict[str, Any]]:
    """读取并校验 manifest：schema / 版本 / 模块 / 身份四项全等才可用。

    共享校验（票 06 审查上提）：与翻译侧同一实现，
    见 ``core/recovery.py`` 的 ``matching_recovery_manifest``。
    """

    return shared_matching_recovery_manifest(path, module=RECOVERY_MODULE, identity=identity)


def mark_manifest_completed(
    manifest: dict[str, Any],
    *,
    path: "Path",
    phase: Optional[bool] = None,
    rounds: Optional[int] = None,
) -> None:
    """先原子替换数据文件、再由本函数登记该级完成（票 02 写入顺序）。

    ``rounds`` 是累计轮末检查点数（每轮 +1，恢复后延续不重置）。
    """

    completed = dict(manifest["completed"])
    if phase is not None:
        completed["phase"] = phase
    if rounds is not None:
        completed["rounds"] = int(rounds)
    manifest["completed"] = completed
    manifest["updated_at"] = now_utc()
    write_recovery_manifest(path, manifest)


def build_recovery_summary(
    *,
    manifest: Mapping[str, Any],
    identity: Mapping[str, str],
    rounds: int,
    phase_completed: bool,
    configuration_drift: "tuple[str, ...]" = (),
) -> RecoverySummary:
    """恢复摘要：模块、身份、已完成级别与计数、检查点时间、漂移清单。

    ``configuration_drift`` 是票 07 的接线点：本轮（票 06）无调用方
    传值、恒为空清单；票 07 接入 ``config_drift`` 比对后由 runner 传入。
    """

    return RecoverySummary(
        module=RECOVERY_MODULE,
        identity=dict(identity),
        completed={"phase": int(phase_completed), "rounds": int(rounds)},
        checkpoint_time=str(manifest.get("updated_at", "")),
        configuration_drift=configuration_drift,
    )


__all__ = [
    "RECOVERY_MANIFEST_FILENAME",
    "RECOVERY_MODULE",
    "PHASE_CHECKPOINT_FILENAME",
    "ROUND_CHECKPOINT_FILENAME",
    "PhaseResumeState",
    "RoundResumeState",
    "build_recovery_summary",
    "mark_manifest_completed",
    "matching_recovery_manifest",
    "new_recovery_manifest",
    "phase_checkpoint_from_payload",
    "phase_checkpoint_payload",
    "recovery_identity",
    "round_checkpoint_from_payload",
    "round_checkpoint_payload",
]
