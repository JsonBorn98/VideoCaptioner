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

    outcome = result.precise_timing_outcome
    if outcome == "applied":
        for grade_name, grade_count in result.precise_timing_grades or ():
            counts.append((grade_name, grade_count))

    status_parts: list[str] = []
    if result.used_fallback:
        status_parts.append("fallback")
    elif result.task.status == "skipped":
        status_parts.append("skipped")
    badge = _PRECISE_TIMING_BADGES.get(outcome or "")
    if badge:
        status_parts.append(f"对齐时间轴 {badge}")
    status = " · ".join(status_parts) or None
    return StageSummary("postprocess", counts, warnings=result.warnings, status=status)


__all__ = ["build_postprocess_stage_summary"]
