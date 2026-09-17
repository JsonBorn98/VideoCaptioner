"""Standalone subtitle postprocess runner with immutable-input fallback."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from contextlib import nullcontext as _nullcontext
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Optional

from ..asr.asr_data import ASRData
from ..entities import SubtitleLayoutEnum
from ..llm.utility import borrow_utility_gateway
from ..recovery import (
    RecoveryDecision,
    RecoveryProvenance,
    RecoverySummary,
    atomic_write_json,
    drifted_keys,
    load_recovery_manifest,
    now_utc,
    write_recovery_manifest,
)
from ..subtitle.io import clone_subtitle_data, import_subtitle, save_canonical_srt
from ..utils.logger import setup_logger
from . import run_post_stage, run_pre_stage
from .checkpoint import (
    PHASE_CHECKPOINT_FILENAME,
    RECOVERY_MANIFEST_FILENAME,
    ROUND_CHECKPOINT_FILENAME,
    PhaseResumeState,
    RoundResumeState,
    build_recovery_summary,
    mark_manifest_completed,
    matching_recovery_manifest,
    new_recovery_manifest,
    phase_checkpoint_from_payload,
    phase_checkpoint_payload,
    postprocess_config_fingerprint,
    recovery_identity,
    round_checkpoint_from_payload,
    round_checkpoint_payload,
)
from .config import PostprocessConfig, config_payload
from .diagnostics import stage_event, terminal_event
from .models import (
    PostprocessAssetAdapter,
    PostprocessDeliveryContext,
    PostprocessLayoutMode,
    PostprocessResult,
    PostprocessTask,
)
from .profiles import PostprocessProfileStore
from .repair import RoundCheckpointState, execute_viewing_repair
from .report import QualityReport
from .translation import load_translation_snapshot_file, store_profile_resolver
from .workspace import (
    FilesystemAssetStore,
    fingerprint_subtitle,
    register_transient_recovery_assets,
)

if TYPE_CHECKING:
    from ..llm import LLMGateway
    from ..speed.timing_evidence import TimingEvidenceWindow

logger = setup_logger("postprocess.runner")

TimingResolver = Callable[
    [PostprocessTask, ASRData, SubtitleLayoutEnum], Iterable["TimingEvidenceWindow"]
]
ProgressCallback = Callable[[int, str], None]
EventCallback = Callable[[dict], None]


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


def _module_outputs(
    task: PostprocessTask,
    report: QualityReport,
    config: PostprocessConfig,
    delivery: PostprocessDeliveryContext,
) -> dict[str, bytes]:
    """组装模块成功后的下游产物载荷（票 08，D21/D28）。

    QA 报告按 ``config.qa_report``；速度变更记录与状态载荷无条件进入
    过程目录（速度结果是模块结果的一部分，状态是导出门控的依据）。
    """

    from .report import build_qa_report
    from .workspace import build_postprocess_state_payload

    outputs: dict[str, bytes] = {}
    if config.qa_report:
        report.source_path = task.source_subtitle_path
        report.output_path = task.postprocessed_subtitle_path or delivery.active_subtitle_path or ""
        # QA 报告恢复来源一节（票 07）：恢复运行才带（与 source_path 同一赋值模式）。
        report.recovery = delivery.recovery_provenance
        outputs["qa_report"] = build_qa_report(report).encode("utf-8")
    if report.speed is not None:
        from ..speed.models import canonical_json_bytes
        from ..speed.report import result_to_dict

        outputs["speed_changes"] = canonical_json_bytes(result_to_dict(report.speed)) + b"\n"
    outputs["postprocess_state"] = (
        json.dumps(
            build_postprocess_state_payload(
                task,
                report,
                active_subtitle_path=delivery.active_subtitle_path,
                precise_timing_outcome=delivery.precise_timing_outcome,
                precise_timing_grades=delivery.precise_timing_grades,
                recovery_provenance=delivery.recovery_provenance,
            ),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    return outputs


def _publish_module_outputs(
    task: PostprocessTask,
    report: QualityReport,
    config: PostprocessConfig,
    adapter: PostprocessAssetAdapter,
    delivery: PostprocessDeliveryContext,
    *,
    clear_recovery: bool = True,
) -> None:
    """把下游产物写入过程目录；失败只警告，不改变已完成的核心结果（D28）。

    ``clear_recovery=False`` 供分析模式：干跑不写检查点，也不删别的
    运行留下的检查点（票 06）。``delivery.recovery_provenance``（票 07）
    是恢复运行的溯源：QA 报告与状态载荷由 ``_module_outputs`` 组装，
    manifest 登记经 ``recovery_payload`` 传给适配层。
    """

    try:
        outputs = _module_outputs(task, report, config, delivery)
        adapter.publish_downstream_outputs(
            task,
            outputs,
            clear_recovery=clear_recovery,
            recovery_payload=(
                delivery.recovery_provenance.to_persisted()
                if delivery.recovery_provenance is not None
                else None
            ),
        )
    except InterruptedError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 过程产物落盘不得阻断交付
        task.warnings.append(f"过程产物写入失败: {exc}")
        logger.warning("过程产物写入失败，继续交付: %s", exc)


def _resolve_profile_identity(
    task: PostprocessTask, profile_store: PostprocessProfileStore | None
) -> tuple[str, str]:
    """方案名与来源模板（票 07 冻结摘要用）；解析失败或方案缺失时留空。

    统一走方案库（注入 store 优先，缺省默认库）：GUI / CLI / 恢复重跑
    都走同一解析路径，不因入口是否传 store 产生假漂移。不猜测配置
    （D15）：解析不到就空串——冻结摘要是可比对值，两侧同为空不产生
    假漂移；方案确已删除是真实漂移（下次恢复会列出），不静默吞掉。
    """

    try:
        store = profile_store if profile_store is not None else PostprocessProfileStore()
        profile = store.get(task.profile_id)
    except Exception:  # noqa: BLE001 —— 摘要比对不因方案库缺失而阻断任务
        return "", ""
    return str(profile.name), str(profile.base_template_id)


def run_postprocess_task(
    task: PostprocessTask,
    *,
    profile_store: PostprocessProfileStore | None = None,
    timing_windows: Iterable["TimingEvidenceWindow"] = (),
    timing_resolver: TimingResolver | None = None,
    gateway: Optional["LLMGateway"] = None,
    assets: PostprocessAssetAdapter | None = None,
    cancelled: Callable[[], bool] | None = None,
    progress: ProgressCallback | None = None,
    on_event: EventCallback | None = None,
    recovery_decision: Callable[[RecoverySummary], RecoveryDecision | str] | None = None,
) -> PostprocessResult:
    """Run one isolated stage and fall back to its immutable initial subtitle.

    A caller that owns ForcedAligner lifecycle may provide ``timing_resolver``.
    This keeps the runner independently callable without implicitly loading a
    heavyweight model.  Resolved evidence is consumed only when precise timing
    is enabled in the frozen profile config.
    """

    task.status = "running"
    # 任务墙钟（票 07 spec「运行耗时」）：从任务入口起算，终态事件携带。
    task_started = time.perf_counter()

    def report_progress(value: int, message: str) -> None:
        if progress is not None:
            progress(value, message)

    def _emit_event(event: dict) -> None:
        if on_event is not None:
            try:
                on_event(event)
            except Exception:  # noqa: BLE001 —— 观测回调失败不阻断任务
                logger.debug("postprocess event callback failed", exc_info=True)

    input_data, layout, confidence, warnings = _load_and_classify(task)
    logger.info(
        "后处理任务开始：%d 段（layout=%s，置信度=%.2f）",
        len(input_data.segments),
        layout.name,
        confidence,
    )
    report_progress(10, "已读取初版字幕")
    _emit_event(stage_event(stage="read", message="已读取初版字幕", percent=10))
    original = clone_subtitle_data(input_data)
    # An invalid initial hand-off is not a module-level processing failure and
    # cannot be a valid fallback.  Publish a distinct status and block downstream.
    try:
        _validate_output(original)
    except ValueError as exc:
        warnings.append(f"初版字幕无效，已阻断下游: {exc}")
        report = QualityReport(segment_count=len(original.segments))
        _emit_event(terminal_event(status="failed", counts={"初版无效": 1}))
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
        # 早期取消（修复尚未开始）同样有终态事件（spec「停止均有终态」）。
        _emit_event(terminal_event(status="cancelled", counts={"rounds": 0, "requests": 0}))
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
        report_progress(18, "正在发现过程资产")
        _emit_event(stage_event(stage="discover_assets", message="正在发现过程资产", percent=18))
    except InterruptedError:
        return _blocked_result(
            task, original, report, layout, confidence, warnings, status="cancelled"
        )
    except Exception as exc:  # noqa: BLE001
        # 模块级失败终态（票 07 审查修复）：可读原因，不渲染 error 1。
        _emit_event(terminal_event(status="failed", counts={"资产发现": 1}))
        return _module_failure_result(task, original, report, layout, confidence, warnings, exc)
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
    # ---------------------------------------------------------------------------
    # 恢复检查点（票 06，ADR-0022）：过程资产发现之后、进入处理之前。
    # 分析模式不读写检查点；发现只在可读 manifest 验证通过时提示。
    # ---------------------------------------------------------------------------
    identity = recovery_identity(
        fingerprint=task.subtitle_fingerprint,
        source_language=task.source_language,
        target_language=task.target_language,
    )
    recovery_dir = task.asset_discovery.task_dir if task.asset_discovery is not None else None
    recovery_manifest_path = (
        recovery_dir / RECOVERY_MANIFEST_FILENAME if recovery_dir is not None else None
    )
    recovery_manifest: dict | None = None
    if recovery_manifest_path is not None and config.speed_mode != "analyze":
        recovery_manifest = (
            matching_recovery_manifest(recovery_manifest_path, identity=identity)
            if recovery_manifest_path.is_file()
            else None
        )
        if recovery_manifest is None and recovery_manifest_path.is_file():
            # manifest 存在但不匹配 / 损坏：告警并从头（R06：验不过不提示）。
            warnings.append("恢复 manifest 不可用（身份不符或损坏），已从头开始")
    # 冻结配置摘要（票 07）：后处理配置方案冻结值哈希 + 翻译执行快照侧五项。
    # 方案名与来源模板按方案库解析（同一解析路径，无入口差异假漂移）。
    profile_name, base_template = _resolve_profile_identity(task, profile_store)
    current_fingerprint = postprocess_config_fingerprint(
        config,
        profile_id=task.profile_id,
        profile_name=profile_name,
        base_template=base_template,
        snapshot=snapshot,
    )
    resume_phase: PhaseResumeState | None = None
    round_resume: RoundResumeState | None = None
    recovery_summary: RecoverySummary | None = None
    recovery_provenance: RecoveryProvenance | None = None
    if config.speed_mode != "analyze" and recovery_manifest is not None:
        completed = recovery_manifest.get("completed", {})
        phase_completed = completed.get("phase") is True
        completed_rounds = completed.get("rounds", 0)
        if not isinstance(completed_rounds, int) or isinstance(completed_rounds, bool):
            completed_rounds = 0
        if phase_completed or completed_rounds > 0:
            # 恢复只信 manifest；登记而文件缺失 / 不可读的级别视为未完成并告警。
            resume_warnings: list[str] = []
            if phase_completed and recovery_dir is not None:
                phase_path = recovery_dir / PHASE_CHECKPOINT_FILENAME
                restored = None
                if phase_path.is_file():
                    loaded = load_recovery_manifest(phase_path)
                    restored = phase_checkpoint_from_payload(loaded) if loaded is not None else None
                if restored is None:
                    resume_warnings.append("阶段末检查点不可用，已重跑后处理阶段")
                else:
                    resume_phase = restored
            if completed_rounds > 0 and recovery_dir is not None and resume_phase is not None:
                # 轮末检查点以阶段末可用为前提：没有工作字幕就没有修复循环入口。
                round_path = recovery_dir / ROUND_CHECKPOINT_FILENAME
                round_state = None
                if round_path.is_file():
                    loaded = round_path.read_text(encoding="utf-8")
                    try:
                        round_state = round_checkpoint_from_payload(json.loads(loaded))
                    except (OSError, UnicodeError, json.JSONDecodeError):
                        round_state = None
                if round_state is None:
                    resume_warnings.append("轮末检查点不可用，修复循环从阶段末重跑")
                else:
                    round_resume = round_state
            if round_resume is not None or resume_phase is not None:
                # 恢复摘要（票 07）：逐项比对冻结摘要生成漂移清单。
                recovery_summary = build_recovery_summary(
                    manifest=recovery_manifest,
                    identity=identity,
                    rounds=completed_rounds,
                    phase_completed=resume_phase is not None,
                    current_fingerprint=current_fingerprint,
                )
                decision = (
                    RecoveryDecision.CONTINUE
                    if recovery_decision is None
                    else RecoveryDecision(recovery_decision(recovery_summary))
                )
                if decision is RecoveryDecision.START_FRESH:
                    # 「从头开始」：重写 manifest 为全部未完成，不预删数据文件；
                    # 内存引用同步换成新 manifest（后续完成标记写新进度）。
                    if recovery_manifest_path is not None:
                        recovery_manifest = new_recovery_manifest(
                            identity=identity,
                            task_name=task.workflow_base_name,
                            config_fingerprint=current_fingerprint,
                        )
                        write_recovery_manifest(recovery_manifest_path, recovery_manifest)
                    resume_phase = None
                    round_resume = None
                    recovery_summary = None
                else:
                    warnings.extend(
                        warning for warning in resume_warnings if warning not in warnings
                    )
                    # 恢复来源（票 07）：QA 报告、后处理状态与 manifest 各记一份。
                    frozen = recovery_manifest.get("config_fingerprint")
                    frozen = frozen if isinstance(frozen, dict) else None
                    recovery_provenance = RecoveryProvenance(
                        checkpoint_time=recovery_summary.checkpoint_time,
                        completed=dict(recovery_summary.completed),
                        configuration_drift=recovery_summary.configuration_drift,
                        drifted_keys=drifted_keys(frozen, current_fingerprint),
                    )
                    # 恢复继续时冻结摘要沿用检查点原值：本次运行以旧配置
                    # 复用已完成的阶段；漂移已记录进摘要与溯源（ADR-0022
                    # 宽松口径），重写摘要会让下次恢复少掉漂移项。
            else:
                warnings.extend(warning for warning in resume_warnings if warning not in warnings)
                # 无可复用级别（登记而文件缺失 / 不可读）：本次运行以当前
                # 配置重产全部级别——冻结摘要同步刷新，否则下次恢复会把
                # 本次新检查点算成旧配置产物，凭空多出漂移项（票 07）。
                recovery_manifest["config_fingerprint"] = dict(current_fingerprint)
                recovery_manifest["updated_at"] = now_utc()
                write_recovery_manifest(recovery_manifest_path, recovery_manifest)
        else:
            # manifest 匹配但无任何完成级别（上次运行在任何检查点落盘前
            # 中断）：同上刷新冻结摘要——manifest 里的旧摘要属于上次配置。
            if recovery_manifest_path is not None:
                recovery_manifest["config_fingerprint"] = dict(current_fingerprint)
                recovery_manifest["updated_at"] = now_utc()
                write_recovery_manifest(recovery_manifest_path, recovery_manifest)
    if (
        config.speed_mode != "analyze"
        and recovery_manifest is None
        and recovery_manifest_path is not None
    ):
        # 无匹配检查点：新建 manifest（唯一进度真相），中断类型清空；
        # 运行中的完成标记 / 中断类型都写这份内存引用。
        recovery_manifest = new_recovery_manifest(
            identity=identity,
            task_name=task.workflow_base_name,
            config_fingerprint=current_fingerprint,
        )
        write_recovery_manifest(recovery_manifest_path, recovery_manifest)
    evidence = tuple(timing_windows) if config.precise_timing else ()
    # Visible outcome of 媒体增强对齐 / 对齐时间轴 (see CONTEXT.md).  None = not
    # requested; otherwise one of "applied" / "degraded_no_media" / "degraded_failed".
    precise_timing_outcome: str | None = None
    precise_timing_grades: tuple[tuple[str, int], ...] | None = None
    if resume_phase is not None:
        # 恢复装载的阶段末状态：对齐结论与工作字幕 / 报告一起延续，
        # 不重跑 timing resolver（阶段末检查点已含截至该点的结论）。
        precise_timing_outcome = resume_phase.precise_timing_outcome
        precise_timing_grades = resume_phase.precise_timing_grades
    elif config.precise_timing:
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
        # 分析模式同属模块成功：报告与状态也写入过程目录（票 08，D21/D28）；
        # 干跑不写检查点，也不删别的运行留下的检查点（票 06）。
        _publish_module_outputs(
            task,
            report,
            config,
            adapter,
            PostprocessDeliveryContext(
                active_subtitle_path=task.active_subtitle_path,
                precise_timing_outcome=precise_timing_outcome,
                precise_timing_grades=precise_timing_grades,
            ),
            clear_recovery=False,
        )
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
        # 恢复装载（票 06）：跳过前处理与后处理阶段，直接进入修复循环。
        # 阶段末检查点带工作字幕与质量报告累积状态；轮末检查点在其上
        # 续带修复循环全量状态。装载失败（文件缺失 / 损坏）已在发现块
        # 告警并按未完成处理：这里只处理决策为继续的有效装载。
        if resume_phase is not None:
            working = resume_phase.working
            report = resume_phase.report
            if round_resume is not None:
                # 轮末恢复：报告问题清单由检查点重建（终态重扫在修复循环外）。
                report.viewing_problems = list(round_resume.viewing_problems)
            report_progress(25, "从恢复检查点继续，跳过后处理阶段")
            _emit_event(
                stage_event(
                    stage="normalize", message="从恢复检查点继续，跳过后处理阶段", percent=25
                )
            )
        else:
            working, report = run_pre_stage(clone_subtitle_data(original), config, report)
            report_progress(25, "正在规范化字幕")
            _emit_event(stage_event(stage="normalize", message="正在规范化字幕", percent=25))
        # 批量观看问题修复（票 05）：确定性阶段结束后执行；局部回退只影响
        # 对应区域（D13/D14），模块级异常仍走整体回退。分析模式已在上方提前返回。
        # apply 路径只借一次工具网关：compress 与修复循环共用同一实例。
        needs_repair = config.utility_llm_profile is not None and config.any_viewing_single_line()
        # 修复方式按任务冻结快照选择（票 06，D15）：快照角色连接缺失时按
        # 角色身份从方案库显式解析（可验证的资产身份才复用）。
        repair_resolver = store_profile_resolver()
        # 任务冻结并发（票 04，ADR-0018）：自建网关按任务值定闸；
        # 注入网关由注入方定闸（编排/完整 workflow 共享任务线程池网关）。
        with (
            borrow_utility_gateway(gateway, max_concurrency=task.thread_num)
            if needs_repair
            else _nullcontext(gateway)
        ) as runtime:
            if cancelled is not None and cancelled():
                raise InterruptedError("LLM request cancelled")

            def _persist_phase_checkpoint() -> None:
                """阶段末检查点（票 06）：先原子写数据文件，manifest 才登记。

                检查点是瞬时资产：落盘失败只告警，不改变已完成的核心结果
                （与 ``_publish_module_outputs`` 同口径，D28）。
                """

                if recovery_manifest_path is None or recovery_dir is None:
                    return
                try:
                    atomic_write_json(
                        recovery_dir / PHASE_CHECKPOINT_FILENAME,
                        phase_checkpoint_payload(
                            working=working,
                            report=report,
                            precise_timing_outcome=precise_timing_outcome,
                            precise_timing_grades=precise_timing_grades,
                        ),
                    )
                except InterruptedError:
                    raise
                except Exception as exc:  # noqa: BLE001 —— 检查点落盘不得阻断任务
                    warnings.append(f"阶段末恢复检查点写入失败: {exc}")
                    logger.warning("阶段末恢复检查点写入失败，继续任务: %s", exc)
                    return
                if recovery_manifest is not None:
                    # 重写阶段末 = 轮换修复循环基准：旧轮末检查点属于上一份
                    # 工作字幕，与新阶段末错配——rounds 清零并删除旧文件，
                    # 不把上一轮的计数 / 回退记录静默配到新基准上。
                    round_path = recovery_dir / ROUND_CHECKPOINT_FILENAME
                    if round_path.is_file():
                        try:
                            round_path.unlink()
                        except OSError:
                            pass  # 删除失败只保留旧文件；manifest rounds 已清零兜底
                    mark_manifest_completed(
                        recovery_manifest,
                        path=recovery_manifest_path,
                        phase=True,
                        rounds=0,
                    )
                register_transient_recovery_assets(task)

            def _on_round_complete(round_report: RoundCheckpointState) -> None:
                """轮末检查点（票 06）：修复循环完成回调，运行器落盘。

                落盘失败只告警；修复循环侧已吞回调异常，这里是双保险。
                """

                if recovery_manifest_path is None or recovery_dir is None:
                    return
                try:
                    atomic_write_json(
                        recovery_dir / ROUND_CHECKPOINT_FILENAME,
                        round_checkpoint_payload(
                            working=round_report.working,
                            snapshot=round_report.snapshot,
                            origin=round_report.origin,
                            summary=round_report.summary,
                            closed_regions=round_report.closed_regions,
                            attempts=round_report.attempts,
                            last_error=round_report.last_error,
                            last_subject=round_report.last_subject,
                            accepted=round_report.accepted,
                            candidate_fps=round_report.candidate_fps,
                            state_fps=round_report.state_fps,
                            transport_streak=round_report.transport_streak,
                            viewing_problems=round_report.viewing_problems,
                        ),
                    )
                except InterruptedError:
                    raise
                except Exception as exc:  # noqa: BLE001 —— 检查点落盘不得阻断修复
                    warnings.append(f"轮末恢复检查点写入失败: {exc}")
                    logger.warning("轮末恢复检查点写入失败，继续修复: %s", exc)
                    return
                if recovery_manifest is not None:
                    rounds = round_report.summary.rounds
                    mark_manifest_completed(
                        recovery_manifest,
                        path=recovery_manifest_path,
                        phase=True,
                        rounds=rounds,
                    )
                register_transient_recovery_assets(task)

            if resume_phase is None:
                # 阶段事件先于阶段调用发射（票 07 审查修复）：事件是「进入
                # 阶段」的宣告，不是阶段完成的事后记录；简单百分比通道与
                # 事件通道并行保留（既有 progress 消费者不降级）。
                _emit_event(stage_event(stage="post_stage", message="正在优化阅读速度", percent=45))
                report_progress(45, "正在优化阅读速度")
                working, report = run_post_stage(
                    working,
                    config,
                    report,
                    layout=layout,
                    timing_windows=evidence,
                    gateway=runtime,
                )
                # 阶段末检查点（R04）：确定性阶段、压缩重译、语义修复完成、
                # 进入观看问题修复前写入。仅报告流程没有修复轮次，只写这一级。
                _persist_phase_checkpoint()
            if config.any_viewing_single_line():
                if resume_phase is not None:
                    report_progress(55, "从恢复检查点继续，正在修复观看问题")
                else:
                    report_progress(55, "正在修复观看问题")
                working, report = execute_viewing_repair(
                    working,
                    config,
                    report,
                    layout,
                    gateway=runtime,
                    snapshot=snapshot,
                    profile=config.utility_llm_profile,
                    profile_resolver=repair_resolver,
                    thread_num=task.thread_num,
                    progress=progress,
                    cancelled=cancelled,
                    on_event=_emit_event,
                    task_id=task.task_id,
                    resume=round_resume,
                    on_round_complete=_on_round_complete,
                )
                # 修复循环的警告（回退 / 传输失败 / 容量不足）并入任务警告（D10）。
                repair_summary = report.viewing_repair
                if repair_summary is not None:
                    warnings.extend(
                        item for item in repair_summary.warnings if item not in warnings
                    )
        _validate_output(working)
        # 交付前复查取消（票 06，spec 第 6 条）：停止先于交付提交被接受时，
        # 不写后处理结果工作稿、不交付部分后处理字幕——成果写入前是最后
        # 一次明确的取消检查点；检查后的写入不再被追溯（终态竞争规则：
        # 交付提交后到达的停止按已完成的取消请求处理，不伪装成取消成功）。
        if cancelled is not None and cancelled():
            raise InterruptedError("postprocess delivery cancelled")
        output = Path(task.postprocessed_subtitle_path or task.default_output_path()).with_suffix(
            ".srt"
        )
        source = Path(task.source_subtitle_path).resolve()
        if output.resolve() == source:
            raise ValueError("postprocess output must not overwrite its input subtitle")
        _emit_event(stage_event(stage="save", message="正在保存后处理字幕", percent=95))
        output = save_canonical_srt(working, output, layout=layout)
    except InterruptedError:
        # 主动停止保留检查点（R06）：manifest 记录中断类型；停止仍不交付
        # 部分成果、阻断下游、迟到结果不写回（P05 语义不变）。
        if recovery_manifest is not None and recovery_manifest_path is not None:
            recovery_manifest["interruption"] = "stopped"
            recovery_manifest["updated_at"] = now_utc()
            write_recovery_manifest(recovery_manifest_path, recovery_manifest)
        # 取消终态统一在此发射（票 07 审查修复）：修复层只上抛不再发
        # （否则 CLI verbose 渲染两条「修复已停止」）；这是任务层对
        # cancelled 的唯一终态事件（spec「无重复完成」）。
        _emit_event(
            terminal_event(
                status="cancelled",
                counts={
                    "rounds": (
                        report.viewing_repair.rounds if report.viewing_repair is not None else 0
                    ),
                    "requests": (
                        report.viewing_repair.requests if report.viewing_repair is not None else 0
                    ),
                },
            )
        )
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
        # 模块级失败保留检查点（R06）：manifest 记录中断类型。
        if recovery_manifest is not None and recovery_manifest_path is not None:
            recovery_manifest["interruption"] = "failure"
            recovery_manifest["updated_at"] = now_utc()
            write_recovery_manifest(recovery_manifest_path, recovery_manifest)
        # 模块级失败终态（票 07 审查修复）：修复 / 规范化 / 保存期间的
        # 非取消异常也有明确终态（spec「停止和失败均有终态」）。
        _emit_event(terminal_event(status="failed", counts={"段": report.segment_count}))
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
    # 修复拼接可能改变段数：报告的段数以最终交付数据为准（不取中间快照）。
    report.segment_count = len(working.segments)
    # 过程报告 / 检查点 / 状态写入专用过程目录（票 08，D21/D28）：模块
    # 成功后才登记；失败只警告，不改变已完成的核心结果。
    _publish_module_outputs(
        task,
        report,
        config,
        adapter,
        PostprocessDeliveryContext(
            active_subtitle_path=str(output),
            precise_timing_outcome=precise_timing_outcome,
            precise_timing_grades=precise_timing_grades,
            # 恢复来源（票 07）：QA 报告、后处理状态与 manifest 各记一份；
            # 不中断运行为 None（不带该键 / 不渲染该节）。
            recovery_provenance=recovery_provenance,
        ),
    )
    logger.info("后处理完成：%d 段 -> %s", len(working.segments), output.name)
    _emit_event(
        terminal_event(
            status="completed",
            counts={
                "segments": len(working.segments),
                # 带警告完成口径（ticket L21「带警告」终态）：成功交付但
                # 存在任务警告时明确标出，不与干净成功混同。
                "warnings": len(warnings),
            },
            wall_seconds=time.perf_counter() - task_started,
        )
    )
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
        recovery_summary=recovery_summary,
    )


__all__ = ["PostprocessAssetAdapter", "TimingResolver", "run_postprocess_task"]
