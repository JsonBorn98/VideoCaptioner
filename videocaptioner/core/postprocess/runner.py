"""Standalone subtitle postprocess runner with immutable-input fallback."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import nullcontext as _nullcontext
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Optional

from ..asr.asr_data import ASRData
from ..entities import SubtitleLayoutEnum
from ..llm.utility import borrow_utility_gateway
from ..subtitle.io import clone_subtitle_data, import_subtitle, save_canonical_srt
from ..utils.logger import setup_logger
from . import run_post_stage, run_pre_stage
from .config import PostprocessConfig, config_payload
from .models import (
    PostprocessAssetAdapter,
    PostprocessLayoutMode,
    PostprocessResult,
    PostprocessTask,
)
from .profiles import PostprocessProfileStore
from .repair import execute_viewing_repair
from .report import QualityReport
from .translation import load_translation_snapshot_file
from .workspace import FilesystemAssetStore, fingerprint_subtitle

if TYPE_CHECKING:
    from ..llm import LLMGateway
    from ..speed.timing_evidence import TimingEvidenceWindow

logger = setup_logger("postprocess.runner")

TimingResolver = Callable[
    [PostprocessTask, ASRData, SubtitleLayoutEnum], Iterable["TimingEvidenceWindow"]
]


def _load_and_classify(
    task: PostprocessTask,
) -> tuple[ASRData, SubtitleLayoutEnum, float, list[str]]:
    mode = PostprocessLayoutMode(task.layout_mode)
    warnings: list[str] = list(task.warnings)
    layout_hint = None
    if mode is PostprocessLayoutMode.ORIGINAL_ON_TOP:
        layout_hint = SubtitleLayoutEnum.ORIGINAL_ON_TOP
    elif mode is PostprocessLayoutMode.TRANSLATE_ON_TOP:
        layout_hint = SubtitleLayoutEnum.TRANSLATE_ON_TOP
    elif mode is PostprocessLayoutMode.ORIGINAL_ONLY:
        layout_hint = SubtitleLayoutEnum.ONLY_ORIGINAL
    elif mode is PostprocessLayoutMode.TRANSLATE_ONLY:
        layout_hint = SubtitleLayoutEnum.ONLY_TRANSLATE
    if task.input_data is not None:
        data = clone_subtitle_data(task.input_data)
        if layout_hint is not None:
            return data, layout_hint, 1.0, warnings
        bilingual = bool(data.segments) and all(
            bool(seg.text.strip() and seg.translated_text.strip()) for seg in data.segments
        )
        if bilingual:
            return data, SubtitleLayoutEnum.ORIGINAL_ON_TOP, 0.9, warnings
        warnings.append("字幕结构识别置信度不足，已按单语字幕处理")
        return data, SubtitleLayoutEnum.ONLY_ORIGINAL, 0.5, warnings

    imported = import_subtitle(task.source_subtitle_path, layout_hint=layout_hint)
    data = imported.data
    if mode is PostprocessLayoutMode.AUTO:
        warnings.extend(imported.warnings)
        return data, imported.layout, imported.confidence, warnings
    if mode in (
        PostprocessLayoutMode.SINGLE,
        PostprocessLayoutMode.ORIGINAL_ONLY,
        PostprocessLayoutMode.TRANSLATE_ONLY,
    ):
        if mode is PostprocessLayoutMode.TRANSLATE_ONLY:
            return data, SubtitleLayoutEnum.ONLY_TRANSLATE, 1.0, warnings
        return data, SubtitleLayoutEnum.ONLY_ORIGINAL, 1.0, warnings

    layout = (
        SubtitleLayoutEnum.ORIGINAL_ON_TOP
        if mode is PostprocessLayoutMode.ORIGINAL_ON_TOP
        else SubtitleLayoutEnum.TRANSLATE_ON_TOP
    )
    # Explicit user structure takes precedence over conservative parser inference.
    for segment in data.segments:
        if segment.translated_text.strip():
            continue
        lines = [line.strip() for line in segment.text.splitlines() if line.strip()]
        if len(lines) < 2:
            warnings.append("部分字幕段缺少可分离的双语行，已保留为单侧")
            continue
        first, second = lines[0], "\n".join(lines[1:])
        if layout is SubtitleLayoutEnum.TRANSLATE_ON_TOP:
            segment.text, segment.translated_text = second, first
        else:
            segment.text, segment.translated_text = first, second
    return data, layout, 1.0, warnings


def _validate_output(data: ASRData) -> None:
    if not data.segments:
        raise ValueError("postprocess produced an empty subtitle")
    previous_start = -1
    for segment in data.segments:
        if segment.start_time < 0 or segment.end_time <= segment.start_time:
            raise ValueError("postprocess produced an invalid time range")
        if segment.start_time < previous_start:
            raise ValueError("postprocess produced an unordered timeline")
        if not segment.text.strip() and not segment.translated_text.strip():
            raise ValueError("postprocess produced an empty cue")
        previous_start = segment.start_time


def _summarize_timing_grades(
    evidence: Iterable["TimingEvidenceWindow"],
) -> tuple[tuple[str, int], ...]:
    """Count precise-timing evidence windows by quality grade for the result.

    Returns ``(grade_name, count)`` pairs ordered HIGH -> MEDIUM -> LOW,
    including only grades with a non-zero count.  Grade names are the
    ``TimingQualityGrade`` enum member names ("HIGH"/"MEDIUM"/"LOW").
    """

    from ..speed.timing_evidence import TimingQualityGrade

    counts: dict[TimingQualityGrade, int] = {grade: 0 for grade in TimingQualityGrade}
    for window in evidence:
        counts[window.quality_grade] = counts.get(window.quality_grade, 0) + 1
    ordered = (TimingQualityGrade.HIGH, TimingQualityGrade.MEDIUM, TimingQualityGrade.LOW)
    return tuple((grade.name, counts[grade]) for grade in ordered if counts.get(grade))


def _has_aligned_timing_evidence(evidence: Iterable["TimingEvidenceWindow"]) -> bool:
    """Return whether at least one window contains media-derived timing evidence."""

    return any(window.quality_metrics.get("fallback") is not True for window in evidence)


def _snapshot_profile_resolver():
    """按快照角色身份解析连接的延迟解析器（票 06）；方案库惰性加载。"""

    from .translation import store_profile_resolver

    return store_profile_resolver(None)


def _blocked_result(
    task: PostprocessTask,
    original: ASRData,
    report: QualityReport,
    layout: SubtitleLayoutEnum,
    confidence: float,
    warnings: list[str],
    *,
    status: Literal["invalid_initial", "cancelled", "fallback"],
    used_fallback: bool = False,
    error: str | None = None,
    precise_timing_outcome: str | None = None,
    precise_timing_grades: tuple[tuple[str, int], ...] | None = None,
) -> PostprocessResult:
    """Publish a halted task that rolls back to the 初版快照 and blocks 下游继续."""

    task.status = status
    if error is not None:
        task.error = error
    task.warnings = warnings
    task.active_subtitle_path = task.initial_subtitle_path
    task.result_data = clone_subtitle_data(original)
    return PostprocessResult(
        task,
        original,
        original,
        report,
        layout,
        confidence,
        tuple(warnings),
        False,
        used_fallback,
        precise_timing_outcome,
        precise_timing_grades,
        continue_downstream=False,
    )


def _module_failure_result(
    task: PostprocessTask,
    original: ASRData,
    report: QualityReport,
    layout: SubtitleLayoutEnum,
    confidence: float,
    warnings: list[str],
    exc: BaseException,
    *,
    precise_timing_outcome: str | None = None,
    precise_timing_grades: tuple[tuple[str, int], ...] | None = None,
) -> PostprocessResult:
    warnings.append(f"字幕后处理失败，已回退到初版字幕: {exc}")
    logger.warning("字幕后处理失败，已回退到初版字幕: %s", exc)
    return _blocked_result(
        task,
        original,
        report,
        layout,
        confidence,
        warnings,
        status="fallback",
        used_fallback=True,
        error=str(exc),
        precise_timing_outcome=precise_timing_outcome,
        precise_timing_grades=precise_timing_grades,
    )


def run_postprocess_task(
    task: PostprocessTask,
    *,
    profile_store: PostprocessProfileStore | None = None,
    timing_windows: Iterable["TimingEvidenceWindow"] = (),
    timing_resolver: TimingResolver | None = None,
    gateway: Optional["LLMGateway"] = None,
    assets: PostprocessAssetAdapter | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> PostprocessResult:
    """Run one isolated stage and fall back to its immutable initial subtitle.

    A caller that owns ForcedAligner lifecycle may provide ``timing_resolver``.
    This keeps the runner independently callable without implicitly loading a
    heavyweight model.  Resolved evidence is consumed only when precise timing
    is enabled in the frozen profile config.
    """

    task.status = "running"
    input_data, layout, confidence, warnings = _load_and_classify(task)
    logger.info(
        "后处理任务开始：%d 段（layout=%s，置信度=%.2f）",
        len(input_data.segments),
        layout.name,
        confidence,
    )
    original = clone_subtitle_data(input_data)
    # An invalid initial hand-off is not a module-level processing failure and
    # cannot be a valid fallback.  Publish a distinct status and block downstream.
    try:
        _validate_output(original)
    except ValueError as exc:
        warnings.append(f"初版字幕无效，已阻断下游: {exc}")
        report = QualityReport(segment_count=len(original.segments))
        return _blocked_result(
            task,
            original,
            report,
            layout,
            confidence,
            warnings,
            status="invalid_initial",
            error=str(exc),
        )
    report = QualityReport(segment_count=len(input_data.segments))
    if cancelled is not None and cancelled():
        return _blocked_result(
            task, original, report, layout, confidence, warnings, status="cancelled"
        )
    if not task.enabled:
        task.status = "skipped"
        task.active_subtitle_path = task.initial_subtitle_path
        task.result_data = clone_subtitle_data(original)
        logger.info("后处理任务跳过（未启用），沿用初版字幕")
        return PostprocessResult(
            task, original, original, report, layout, confidence, tuple(warnings), True, False
        )

    config = task.config_snapshot
    if config is None:
        config = (profile_store or PostprocessProfileStore()).resolve_config(task.profile_id)
    # Freeze mutable config fields even when the caller supplied a live object, while
    # keeping the runtime-injected utility profile (an object reference, not payload).
    injected = config.utility_llm_profile
    config = PostprocessConfig(**config_payload(config))
    config.utility_llm_profile = injected
    task.config_snapshot = config
    task.subtitle_fingerprint = fingerprint_subtitle(original)
    adapter = assets if assets is not None else FilesystemAssetStore()
    try:
        adapter.discover(task)
    except InterruptedError:
        return _blocked_result(
            task, original, report, layout, confidence, warnings, status="cancelled"
        )
    except Exception as exc:  # noqa: BLE001
        return _module_failure_result(
            task, original, report, layout, confidence, warnings, exc
        )
    warnings.extend(item for item in task.warnings if item not in warnings)
    # 翻译执行快照（票 06，D15）：完整 workflow 由调用方在任务开始时冻结注入；
    # 独立任务从验证过的过程资产重建身份快照。缺失时的明确提示由修复循环
    # 的 report_only 结果写入报告与任务警告（不猜测配置、不静默升级）。
    if task.translation_snapshot is None and task.asset_discovery is not None:
        snapshot_path = task.asset_discovery.asset_path("translation_snapshot")
        rebuilt = load_translation_snapshot_file(snapshot_path) if snapshot_path else None
        if rebuilt is not None:
            task.translation_snapshot = rebuilt
            if not task.translation_method.strip():
                task.translation_method = rebuilt.method
    snapshot = task.translation_snapshot
    evidence = tuple(timing_windows) if config.precise_timing else ()
    # Visible outcome of 媒体增强对齐 / 对齐时间轴 (see CONTEXT.md).  None = not
    # requested; otherwise one of "applied" / "degraded_no_media" / "degraded_failed".
    precise_timing_outcome: str | None = None
    precise_timing_grades: tuple[tuple[str, int], ...] | None = None
    if config.precise_timing:
        if timing_resolver is not None and task.media_path:
            try:
                evidence = tuple(timing_resolver(task, original, layout))
                warnings.extend(item for item in task.warnings if item not in warnings)
            except InterruptedError:
                return _blocked_result(
                    task, original, report, layout, confidence, warnings, status="cancelled"
                )
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"对齐时间轴生成失败，已降级为字幕内部估算时间轴: {exc}")
                # Drop any caller-supplied windows so "degraded_failed" truly
                # means no media evidence was applied downstream.
                evidence = ()
                precise_timing_outcome = "degraded_failed"
            else:
                if evidence and _has_aligned_timing_evidence(evidence):
                    precise_timing_outcome = "applied"
                    precise_timing_grades = _summarize_timing_grades(evidence)
                else:
                    # Resolver ran but produced no usable evidence (e.g. preflight
                    # ineligible / empty alignment) — a well-defined 对齐降级.
                    precise_timing_outcome = "degraded_failed"
        elif not evidence:
            warnings.append("已开启对齐时间轴，但未提供媒体时间证据，已降级处理")
            precise_timing_outcome = "degraded_no_media"
        elif _has_aligned_timing_evidence(evidence):
            # Precise timing requested with caller-supplied evidence and no resolver.
            precise_timing_outcome = "applied"
            precise_timing_grades = _summarize_timing_grades(evidence)
        else:
            precise_timing_outcome = "degraded_failed"

    if config.speed_mode == "analyze":
        # Analyze is a stage-wide dry run.  Text cleanup and timing mutation
        # are deliberately disabled; speed/audit reports still inspect the
        # same immutable hand-off artifact.
        analysis_config = replace(
            config,
            remove_placeholders=False,
            normalize_quotes=False,
            trim_trailing_punct=False,
            fix_gaps=False,
            tail_compensation=False,
            compress_fast_subtitles=False,
        )
        _, report = run_post_stage(
            clone_subtitle_data(original),
            analysis_config,
            report,
            layout=layout,
            timing_windows=evidence,
            gateway=gateway,
        )
        task.status = "completed"
        task.postprocessed_subtitle_path = None
        task.active_subtitle_path = task.initial_subtitle_path
        task.result_data = clone_subtitle_data(original)
        warnings.append("分析模式仅生成报告，未写入后处理字幕")
        task.warnings = warnings
        logger.info("后处理分析模式完成：仅生成报告，未写入字幕")
        return PostprocessResult(
            task,
            original,
            original,
            report,
            layout,
            confidence,
            tuple(warnings),
            True,
            False,
            precise_timing_outcome,
            precise_timing_grades,
        )

    try:
        working, report = run_pre_stage(clone_subtitle_data(original), config, report)
        # 批量观看问题修复（票 05）：确定性阶段结束后执行；局部回退只影响
        # 对应区域（D13/D14），模块级异常仍走整体回退。分析模式已在上方提前返回。
        # apply 路径只借一次工具网关：compress 与修复循环共用同一实例。
        needs_repair = (
            config.utility_llm_profile is not None and config.any_viewing_single_line()
        )
        # 修复方式按任务冻结快照选择（票 06，D15）：快照角色连接缺失时按
        # 角色身份从方案库显式解析（可验证的资产身份才复用）。
        repair_resolver = _snapshot_profile_resolver()
        with borrow_utility_gateway(gateway) if needs_repair else _nullcontext(gateway) as runtime:
            working, report = run_post_stage(
                working,
                config,
                report,
                layout=layout,
                timing_windows=evidence,
                gateway=runtime,
            )
            if config.any_viewing_single_line():
                working, report = execute_viewing_repair(
                    working,
                    config,
                    report,
                    layout,
                    gateway=runtime,
                    snapshot=snapshot,
                    profile=config.utility_llm_profile,
                    profile_resolver=repair_resolver,
                )
                # 修复循环的警告（回退 / 传输失败 / 容量不足）并入任务警告（D10）。
                repair_summary = report.viewing_repair
                if repair_summary is not None:
                    warnings.extend(
                        item
                        for item in repair_summary.warnings
                        if item not in warnings
                    )
        _validate_output(working)
        output = Path(task.postprocessed_subtitle_path or task.default_output_path()).with_suffix(
            ".srt"
        )
        source = Path(task.source_subtitle_path).resolve()
        if output.resolve() == source:
            raise ValueError("postprocess output must not overwrite its input subtitle")
        output = save_canonical_srt(working, output, layout=layout)
    except InterruptedError:
        return _blocked_result(
            task,
            original,
            report,
            layout,
            confidence,
            warnings,
            status="cancelled",
            precise_timing_outcome=precise_timing_outcome,
            precise_timing_grades=precise_timing_grades,
        )
    except Exception as exc:  # noqa: BLE001
        return _module_failure_result(
            task,
            original,
            report,
            layout,
            confidence,
            warnings,
            exc,
            precise_timing_outcome=precise_timing_outcome,
            precise_timing_grades=precise_timing_grades,
        )

    task.status = "completed"
    task.postprocessed_subtitle_path = str(output)
    task.active_subtitle_path = str(output)
    task.result_data = clone_subtitle_data(working)
    task.warnings = warnings
    logger.info("后处理完成：%d 段 -> %s", len(working.segments), output.name)
    return PostprocessResult(
        task,
        original,
        working,
        report,
        layout,
        confidence,
        tuple(warnings),
        True,
        False,
        precise_timing_outcome,
        precise_timing_grades,
    )


__all__ = ["PostprocessAssetAdapter", "TimingResolver", "run_postprocess_task"]
