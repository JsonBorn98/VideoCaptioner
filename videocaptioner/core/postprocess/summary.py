"""Shared structured summary builder for subtitle postprocessing."""

from ..utils.stage_summary import StageSummary
from .models import PostprocessResult
from .report import _STAGE_LABELS
from .translation import flow_mode_label

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
    # 未解决问题与回退区域进入共享摘要（票 08，D10/D14）：适配层
    # 不再各自从报告重复推导，CLI 与 GUI 展示同一事实。
    unresolved = report.unresolved_viewing_problems()
    if unresolved:
        counts.append(("未解决问题", len(unresolved)))
    if repair is not None and repair.rollbacks:
        counts.append(("回退区域", len(repair.rollbacks)))

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
        status_parts.append(f"修复 {method_label}->{flow_mode_label(repair.flow_mode)}")
    # 活动字幕状态（票 08）：下游消费哪一份字幕——后处理字幕或回退到初版。
    if result.continue_downstream:
        status_parts.append(
            "活动字幕=后处理字幕"
            if result.task.postprocessed_subtitle_path
            else "活动字幕=初版字幕"
        )
    else:
        status_parts.append("活动字幕=初版字幕")
    badge = _PRECISE_TIMING_BADGES.get(outcome or "")
    if badge:
        status_parts.append(f"对齐时间轴 {badge}")
    status = " · ".join(status_parts) or None
    return StageSummary("postprocess", counts, warnings=result.warnings, status=status)


__all__ = ["build_postprocess_stage_summary"]
