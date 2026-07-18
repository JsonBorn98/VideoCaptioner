"""postprocess command — run the standalone subtitle postprocessing module."""

from __future__ import annotations

import os
from argparse import Namespace
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from videocaptioner.cli import exit_codes as EXIT
from videocaptioner.cli import output
from videocaptioner.cli.config import get

if TYPE_CHECKING:
    from videocaptioner.core.postprocess import PostprocessResult
    from videocaptioner.core.utils.stage_summary import StageSummary

_LAYOUT_MODES = {
    "auto": "auto",
    "source-above": "original_on_top",
    "target-above": "translate_on_top",
    "source-only": "original_only",
    "target-only": "translate_only",
}

# 媒体增强对齐 / 对齐时间轴 的结果徽标（见 CONTEXT.md / docs/adr/0009）。键为
# PostprocessResult.precise_timing_outcome 的取值，值为摘要中常驻显示的短标签。
_PRECISE_TIMING_BADGES = {
    "applied": "applied",
    "degraded_no_media": "degraded-no-media",
    "degraded_failed": "degraded-failed",
}

_CONFIG_OVERRIDE_FIELDS = {
    "remove_placeholders": "remove_placeholders",
    "normalize_quotes": "normalize_quotes",
    "trim_trailing_punct": "trim_trailing_punct",
    "qa_report": "qa_report",
    "speed_optimize": "speed_optimize",
    "mode": "speed_mode",
    "speed_profile_file": "speed_profile_file",
    "primary_side": "speed_primary",
    "precise_timing": "precise_timing",
    "save_timing_sidecar": "save_timing_sidecar",
    "reference_audit": "speed_reference_audit",
    "semantic_repair": "speed_semantic_repair",
    "semantic_window": "speed_semantic_window",
    "llm_uncertain_review": "speed_llm_uncertain_review",
}


def _timing_resolver(task, data, _layout):
    """Resolve optional ForcedAligner evidence without making media mandatory."""

    if not task.media_path:
        return ()
    from langdetect import LangDetectException, detect

    from videocaptioner.core.speed.alignment import load_or_align_timing
    from videocaptioner.core.speed.models import CueSnapshot

    snapshots = tuple(
        CueSnapshot.from_input(
            index=index,
            start_ms=segment.start_time,
            end_ms=segment.end_time,
            text=segment.text,
            translated_text=segment.translated_text,
        )
        for index, segment in enumerate(data.segments)
    )
    sample = "\n".join(segment.text for segment in data.segments)[:5000]
    try:
        language = detect(sample) if sample.strip() else ""
    except LangDetectException:
        language = ""
    bundle, issues, _cache_hit = load_or_align_timing(
        task.source_subtitle_path,
        task.media_path,
        snapshots,
        language,
    )
    task.warnings.extend(issues)
    task.timing_bundle = bundle
    return bundle.windows if bundle is not None else ()


def _write_reports(result, *, verbose: bool, base_path: str | None = None) -> None:
    config = result.task.config_snapshot
    report = result.report
    output_path = result.task.postprocessed_subtitle_path or base_path
    if config is None or not output_path:
        return
    if config.qa_report:
        from videocaptioner.core.postprocess import build_qa_report

        report.source_path = result.task.source_subtitle_path
        report.output_path = output_path
        report.segment_count = len(result.output_data.segments)
        qa_path = Path(output_path).with_suffix(".qa.md")
        qa_path.write_text(build_qa_report(report), encoding="utf-8")
        if verbose:
            output.info(f"QA report -> {qa_path}")
    if report.speed is not None:
        from videocaptioner.core.speed.report import write_changes

        changes_path = Path(output_path).with_suffix(".speed-changes.json")
        write_changes(changes_path, report.speed)
        if verbose:
            output.info(f"Changes -> {changes_path}")
    timing_bundle = result.task.timing_bundle
    if config.save_timing_sidecar and timing_bundle is not None:
        from videocaptioner.core.speed.timing_archive import (
            timing_sidecar_path,
            write_timing_archive,
        )

        sidecar_path = timing_sidecar_path(output_path)
        write_timing_archive(sidecar_path, timing_bundle)
        if verbose:
            output.info(f"Timing evidence -> {sidecar_path}")


def _build_stage_summary(result: "PostprocessResult") -> "StageSummary":
    """Render a truthful, level-independent postprocess summary from the report."""
    from videocaptioner.core.postprocess.report import _STAGE_LABELS
    from videocaptioner.core.utils.stage_summary import StageSummary

    report = result.report
    counts: list[tuple[str, int]] = [("段", len(result.output_data.segments))]
    for key, stage_report in report.stages.items():
        if stage_report.changed > 0:
            counts.append((_STAGE_LABELS.get(key, key), stage_report.changed))
    if report.compress_failures:
        counts.append(("压缩失败", len(report.compress_failures)))
    if report.placeholder_review:
        counts.append(("占位符复查", len(report.placeholder_review)))
    audit = report.audit
    if audit is not None:
        audit_counts = audit.counts()
        if audit_counts["hard"]:
            counts.append(("硬超速", audit_counts["hard"]))
    # 媒体增强对齐 / 对齐时间轴 的结果常驻显示（见 docs/adr/0009）：已应用时补充每档
    # 证据窗计数，降级时通过 status 徽标可见。
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


def run(args: Namespace, config: dict) -> int:
    input_path = Path(args.input)
    if not input_path.exists():
        output.error(f"Input file not found: {input_path}")
        return EXIT.FILE_NOT_FOUND

    from videocaptioner.cli.validators import validate_subtitle_input

    error_code = validate_subtitle_input(input_path)
    if error_code is not None:
        return error_code

    media_value = getattr(args, "media", None) or getattr(args, "speed_media", None)
    media_value = media_value or get(config, "postprocess.media", "") or None
    # 关联媒体缺失或路径不存在都不再硬失败：媒体增强对齐 / 对齐时间轴 会在核心内
    # 自然降级（对齐降级），任务继续并以 0 退出。缺失的输入字幕仍返回 FILE_NOT_FOUND。
    media_missing = bool(media_value) and not Path(media_value).exists()

    from videocaptioner.core.postprocess import (
        PostprocessProfileStore,
        PostprocessTask,
        run_postprocess_task,
    )
    from videocaptioner.core.postprocess.profiles import PostprocessProfileError

    profile_id = (
        getattr(args, "profile", None)
        or getattr(args, "speed_profile", None)
        or get(config, "postprocess.profile", "balanced")
    )
    store = PostprocessProfileStore()
    try:
        resolved = store.resolve_config(profile_id)
    except (PostprocessProfileError, KeyError) as exc:
        output.error(f"Postprocessing profile is unavailable: {exc}")
        return EXIT.USAGE_ERROR

    section = config.get("postprocess", {})
    overrides = {
        field_name: section[key]
        for key, field_name in _CONFIG_OVERRIDE_FIELDS.items()
        if key in section
    }
    llm_model = get(config, "llm.model", "") or None
    resolved = replace(resolved, llm_model=llm_model, **overrides)

    if media_missing:
        # Drop the unusable path so the core takes the clean 对齐降级
        # ("degraded_no_media") branch instead of aligning a missing file.
        if getattr(resolved, "precise_timing", False) and not getattr(args, "quiet", False):
            output.warn(f"关联媒体不存在，媒体增强对齐已降级（对齐降级）: {media_value}")
        media_value = None

    api_key = get(config, "llm.api_key", "")
    api_base = get(config, "llm.api_base", "")
    if api_key:
        os.environ["OPENAI_API_KEY"] = api_key
    if api_base:
        os.environ["OPENAI_BASE_URL"] = api_base

    layout_value = getattr(args, "layout", "auto") or "auto"
    requested_output = getattr(args, "output", None)
    canonical_output = None
    if requested_output:
        canonical_output = str(Path(requested_output).with_suffix(".srt"))
    task = PostprocessTask(
        source_subtitle_path=str(input_path),
        initial_subtitle_path=str(input_path),
        postprocessed_subtitle_path=canonical_output,
        profile_id=profile_id,
        layout_mode=_LAYOUT_MODES[layout_value],
        media_path=str(media_value) if media_value else None,
        config_snapshot=resolved,
    )
    task.input_data = getattr(args, "input_data", None)

    quiet = getattr(args, "quiet", False)
    verbose = getattr(args, "verbose", False)
    if requested_output and requested_output != canonical_output and not quiet:
        output.warn(
            f"Postprocess stages persist canonical SRT; output changed to {canonical_output}"
        )
    progress = None if quiet else output.ProgressLine("Postprocessing subtitles").start()
    try:
        result = run_postprocess_task(
            task,
            profile_store=store,
            timing_resolver=_timing_resolver,
        )
    except Exception as exc:  # invalid initial subtitles cannot safely fall back
        if progress:
            progress.fail(output.clean_error(str(exc)))
        else:
            output.error(output.clean_error(str(exc)))
        return EXIT.RUNTIME_ERROR

    if progress:
        progress.finish()  # stop spinner before the clean warning/summary/Done lines

    for warning in result.warnings:
        if not quiet:
            output.warn(warning)
    if not result.succeeded:
        output.error("Postprocessing failed; input subtitle was preserved")
        return EXIT.RUNTIME_ERROR

    report_base = canonical_output or task.default_output_path()
    _write_reports(result, verbose=verbose and not quiet, base_path=report_base)
    args.result_data = result.output_data
    active_path = result.task.active_subtitle_path or str(input_path)
    if not quiet:
        output.stage(_build_stage_summary(result))
    if progress:
        output.success(f"Done -> {active_path}")
    if quiet:
        print(active_path)
    return EXIT.SUCCESS


__all__ = ["run"]
