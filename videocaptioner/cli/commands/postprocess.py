"""postprocess command — run the standalone subtitle postprocessing module."""

from __future__ import annotations

from argparse import Namespace
from dataclasses import replace
from pathlib import Path

from videocaptioner.cli import exit_codes as EXIT
from videocaptioner.cli import output
from videocaptioner.cli.config import get

_LAYOUT_MODES = {
    "auto": "auto",
    "source-above": "original_on_top",
    "target-above": "translate_on_top",
    "source-only": "original_only",
    "target-only": "translate_only",
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


def _write_sidecar(result, *, verbose: bool) -> None:
    """保存可复用的对齐时间轴 sidecar（共享守卫，见 core/postprocess/sidecar.py）。"""

    from videocaptioner.core.postprocess.sidecar import write_timing_sidecar_if_applied

    output_path = result.task.postprocessed_subtitle_path or result.task.active_subtitle_path
    sidecar_path = write_timing_sidecar_if_applied(result, output_path or "")
    if verbose and sidecar_path is not None:
        output.info(f"Timing evidence -> {sidecar_path}")


def _report_locations(result) -> None:
    """展示过程报告与状态的位置（票 08，D21/D28）。

    过程报告 / 状态已由核心任务入口写入 ``videocaptioner-workspace``；
    这里只展示位置，不再向普通输出目录复制过程文件。
    位置行是普通 info（非仅 verbose）：验收 7 的用户可见展示。
    """

    from videocaptioner.core.postprocess.report_locations import describe_persisted_outputs

    for label, path in describe_persisted_outputs(result.task):
        output.info(f"{label} -> {path}")


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
    resolved = replace(resolved, **overrides)
    # Compress re-translation and semantic repair resolve their model from the
    # profile store (utility binding first, then derived from main). Only a
    # config that actually issues utility LLM requests needs a profile, and it
    # fails fast with guidance when none can be resolved — matching the GUI.
    if resolved.needs_utility_llm() and resolved.utility_llm_profile is None:
        from videocaptioner.cli.config import resolve_cli_utility_profile

        try:
            resolved = replace(
                resolved, utility_llm_profile=resolve_cli_utility_profile(config)
            )
        except ValueError as exc:
            output.error(str(exc))
            return EXIT.USAGE_ERROR

    if media_missing:
        # Drop the unusable path so the core takes the clean 对齐降级
        # ("degraded_no_media") branch instead of aligning a missing file.
        if getattr(resolved, "precise_timing", False) and not getattr(args, "quiet", False):
            output.warn(f"关联媒体不存在，媒体增强对齐已降级（对齐降级）: {media_value}")
        media_value = None

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
    # 翻译执行快照（票 06，D15）：process 管线把字幕阶段冻结的任务快照
    # 传入；独立调用无快照，由 runner 从验证过的过程资产重建或明确提示。
    task.bind_translation_snapshot(getattr(args, "translation_execution_snapshot", None))
    # 上游过程资产（票 08，D21）：完整 workflow 把字幕阶段产物显式传入；
    # 独立调用可显式补充资产，其余由资产发现按 manifest 验证。
    explicit_assets = getattr(args, "explicit_assets", None)
    if isinstance(explicit_assets, dict):
        task.explicit_assets = {
            str(kind): str(path) for kind, path in explicit_assets.items()
        }

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
            gateway=getattr(args, "gateway", None),
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

    # 过程报告 / 状态位置由核心写入过程目录（票 08，D21/D28）：
    # 这里只保存对齐 sidecar 并展示报告位置，不再向普通输出目录复制过程文件。
    _write_sidecar(result, verbose=verbose and not quiet)
    if not quiet:
        _report_locations(result)
    args.result_data = result.output_data
    # 下游接缝字段（票 08，D14）：process 管线按 continue_downstream 门控
    # 下游、按 active_subtitle_path 取活动字幕（成功 = 后处理字幕；
    # 回退 / 分析模式 = 初版）。
    args.continue_downstream = result.continue_downstream
    args.active_subtitle_path = result.task.active_subtitle_path or str(input_path)
    # 显式交付导出（票 08，D28）：模块成功后按 manifest 复制过程资产；
    # 运行中 / 取消 / 模块级失败不提供。导出失败只报导出失败。
    export_dir = getattr(args, "export_assets", None)
    if export_dir:
        from videocaptioner.core.postprocess.workspace import (
            ProcessAssetExportError,
            export_process_assets,
        )

        try:
            copied = export_process_assets(task, export_dir)
        except ProcessAssetExportError as exc:
            output.error(f"Process asset export failed: {exc}")
        else:
            if not quiet:
                output.info(f"Exported {len(copied)} process asset(s) -> {export_dir}")
    active_path = result.task.active_subtitle_path or str(input_path)
    if not quiet:
        from videocaptioner.core.postprocess.summary import build_postprocess_stage_summary

        output.stage(build_postprocess_stage_summary(result))
    if progress:
        output.success(f"Done -> {active_path}")
    if quiet:
        print(active_path)
    return EXIT.SUCCESS


__all__ = ["run"]
