"""Shared structured summary builder for subtitle postprocessing."""

from ..utils.stage_summary import StageSummary
from .models import PostprocessResult
from .report import _STAGE_LABELS

_PRECISE_TIMING_BADGES = {
    "applied": "applied",
    "degraded_no_media": "degraded-no-media",
    "degraded_failed": "degraded-failed",
}


def build_postprocess_stage_summary(result: PostprocessResult) -> StageSummary:
    """Build the same truthful postprocess summary for CLI and GUI frontends."""

    report = result.report
    counts: list[tuple[str, int]] = [("段", len(result.output_data.segments))]
    for key, stage_report in report.stages.items():
        if stage_report.changed > 0:
            counts.append((_STAGE_LABELS.get(key, key), stage_report.changed))
    if report.compress_failures:
        counts.append(("压缩失败", len(report.compress_failures)))
    if report.placeholder_review:
        counts.append(("占位符复查", len(report.placeholder_review)))
    if report.audit is not None:
        audit_counts = report.audit.counts()
        if audit_counts["hard"]:
            counts.append(("硬超速", audit_counts["hard"]))
    repair = report.viewing_repair
    if repair is not None and repair.review_corrections:
        counts.append(("校对修正", repair.review_corrections))

    outcome = result.precise_timing_outcome
    if outcome == "applied":
        for grade_name, grade_count in result.precise_timing_grades or ():
            counts.append((grade_name, grade_count))

    status_parts: list[str] = []
    if result.used_fallback:
        status_parts.append("fallback")
    elif result.task.status == "skipped":
        status_parts.append("skipped")
    if repair is not None:
        # 修复方式选择进入任务状态摘要（票 06，D15）：
        # 翻译方式 -> 实际修复方式，便于核对实际行为。
        method_label = repair.translation_method or "未知"
        flow_label = {
            "main_review": "主翻译+高级校对",
            "main": "仅主翻译",
            "report_only": "仅报告",
        }.get(repair.flow_mode, repair.flow_mode or "仅报告")
        status_parts.append(f"修复 {method_label}->{flow_label}")
    badge = _PRECISE_TIMING_BADGES.get(outcome or "")
    if badge:
        status_parts.append(f"对齐时间轴 {badge}")
    status = " · ".join(status_parts) or None
    return StageSummary("postprocess", counts, warnings=result.warnings, status=status)


__all__ = ["build_postprocess_stage_summary"]
