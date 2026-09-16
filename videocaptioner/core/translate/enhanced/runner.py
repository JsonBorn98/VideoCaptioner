"""ASRData integration and durable enhanced-translation artifacts."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from videocaptioner.core.asr.asr_data import ASRData
from videocaptioner.core.llm import LLMGateway
from videocaptioner.core.postprocess.translation import (
    METHOD_ENHANCED_LLM,
    TranslationExecutionSnapshot,
)
from videocaptioner.core.postprocess.workspace import (
    ASSET_FILENAMES,
    normalize_task_name,
    publish_translation_workspace,
    resolve_output_workspace_root,
    resolve_translation_staging_dir,
)
from videocaptioner.core.recovery import (
    RECOVERY_MANIFEST_SCHEMA,
    RECOVERY_MANIFEST_VERSION,
    RecoveryDecision,
    RecoveryProvenance,
    RecoverySummary,
    config_drift,
    drifted_keys,
    load_recovery_manifest,
    now_utc,
    software_version,
    text_digest,
    write_recovery_manifest,
)
from videocaptioner.core.utils.logger import setup_logger

from .audit_checkpoint import (
    AUDIT_CHECKPOINT_FILENAME,
    AuditCheckpointError,
    load_audit_checkpoint,
    save_audit_checkpoint,
)
from .brief import BriefFormatError, load_translation_brief, save_translation_brief
from .glossary import load_glossary, save_glossary, subtitle_fingerprint
from .models import (
    AuthoritativeGlossary,
    CancellationToken,
    EnhancedTranslationConfig,
    EnhancedTranslationError,
    EnhancedTranslationResult,
    SubtitleCue,
    TermCandidate,
    TranslationAuditIssue,
    TranslationAuditReport,
    TranslationContextBrief,
    translation_config_drift_labels,
)
from .orchestrator import EnhancedTranslationOrchestrator
from .report import save_audit_markdown

logger = setup_logger("enhanced_translation_runner")

_RECOVERY_MANIFEST_FILENAME = "recovery-manifest.json"
_RECOVERY_MODULE = "enhanced_translation"


def _profile_fingerprint(profile: Any) -> dict[str, Any]:
    """模型配置方案的冻结摘要：方案名、模型、接口类型；绝不含连接机密。"""

    return {
        "name": str(getattr(profile, "name", "") or ""),
        "model": str(getattr(profile, "model", "") or ""),
        "transport": str(getattr(getattr(profile, "transport", None), "value", "") or ""),
    }


def translation_config_fingerprint(config: Any) -> dict[str, Any]:
    """冻结配置摘要（票 05）：提示词只存哈希，方案只存名/模型/接口类型。"""

    main_role = getattr(config, "main_role", None)
    review_role = getattr(config, "review_role", None)
    return {
        "main_prompt": text_digest(str(getattr(main_role, "user_prompt", "") or "")),
        "review_prompt": text_digest(str(getattr(review_role, "user_prompt", "") or "")),
        "main_profile": _profile_fingerprint(getattr(main_role, "profile", None)),
        "review_profile": _profile_fingerprint(getattr(review_role, "profile", None)),
        "batch_size": int(getattr(config, "batch_size", 0) or 0),
        "boundary_context_radius": int(
            getattr(config, "boundary_context_radius", 0) or 0
        ),
    }


@dataclass(frozen=True)
class EnhancedTranslationArtifacts:
    glossary_path: Path
    audit_report_path: Path
    translation_checkpoint_path: Optional[Path] = None
    context_path: Optional[Path] = None


@dataclass(frozen=True)
class EnhancedTranslationRun:
    subtitle_data: ASRData
    result: EnhancedTranslationResult
    artifacts: EnhancedTranslationArtifacts
    recovery_summary: RecoverySummary | None = None
    # 恢复来源（票 05）：恢复过的运行才带；不中断运行为 None。
    recovery_provenance: RecoveryProvenance | None = None


def _translated_copy(
    subtitle_data: ASRData,
    translations: Mapping[int, str],
    *,
    require_complete: bool = True,
) -> ASRData:
    translated = ASRData.from_json(subtitle_data.to_json())
    expected = set(range(1, len(translated.segments) + 1))
    extra = set(translations) - expected
    if extra:
        raise ValueError("translation checkpoint contains unknown subtitle IDs")
    if require_complete and set(translations) != expected:
        raise ValueError("translation checkpoint does not cover every subtitle ID")
    for index, segment in enumerate(translated.segments, 1):
        segment.translated_text = translations.get(index, "")
    return translated


def _save_checkpoint(path: Path, subtitle_data: ASRData) -> None:
    """Atomically persist translated data without exposing prompts or responses."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(subtitle_data.to_json(), stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _load_checkpoint_translations(
    path: Path, subtitle_data: ASRData
) -> tuple[dict[int, str], tuple[str, ...]]:
    """Load valid checkpoint translations without ever trusting them over source text."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except InterruptedError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {}, (f"译文检查点不可读，已重新翻译未完成字幕段：{exc}",)
    if not isinstance(document, dict):
        return {}, ("译文检查点格式无效，已重新翻译未完成字幕段",)
    translations: dict[int, str] = {}
    warnings: list[str] = []
    for index, segment in enumerate(subtitle_data.segments, 1):
        item = document.get(str(index))
        if not isinstance(item, dict) or item.get("original_subtitle") != segment.text:
            if item is not None:
                warnings.append(f"字幕段 {index} 的译文检查点不一致，已重新翻译")
            continue
        translation = item.get("translated_subtitle")
        if isinstance(translation, str) and translation.strip():
            translations[index] = translation.strip()
    return translations, tuple(warnings)


def _recovery_identity(
    *, source_fingerprint: str, source_language: str, target_language: str
) -> dict[str, str]:
    return {
        "source_fingerprint": source_fingerprint,
        "source_language": source_language,
        "target_language": target_language,
        "translation_method": METHOD_ENHANCED_LLM,
    }


def _new_recovery_manifest(
    identity: Mapping[str, str], task_name: str, config: Any
) -> dict[str, Any]:
    timestamp = now_utc()
    return {
        "schema": RECOVERY_MANIFEST_SCHEMA,
        "version": RECOVERY_MANIFEST_VERSION,
        "module": _RECOVERY_MODULE,
        "identity": dict(identity),
        "task_name": normalize_task_name(task_name),
        "created_at": timestamp,
        "updated_at": timestamp,
        "software_version": software_version(),
        # 冻结配置摘要（票 05）：供恢复时逐项比对配置漂移；只存哈希与身份字段。
        "config_fingerprint": translation_config_fingerprint(config),
        "completed": {
            "analysis": False,
            "glossary": False,
            "translation_ids": [],
            "audit_batches": [],
        },
        "interruption": None,
    }


def _matching_recovery_manifest(
    path: Path, identity: Mapping[str, str]
) -> dict[str, Any] | None:
    manifest = load_recovery_manifest(path)
    if manifest is None:
        return None
    if (
        manifest.get("schema") != RECOVERY_MANIFEST_SCHEMA
        or manifest.get("version") != RECOVERY_MANIFEST_VERSION
        or manifest.get("module") != _RECOVERY_MODULE
        or manifest.get("identity") != dict(identity)
    ):
        return None
    completed = manifest.get("completed")
    return manifest if isinstance(completed, dict) else None


def _remove_empty_ancestors(directory: Path, stop: Path) -> None:
    """Delete now-empty staging ancestors up to (excluding) the workspace root."""

    try:
        current = directory.resolve()
        boundary = stop.resolve()
    except OSError:
        return
    while current != boundary and boundary in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def run_enhanced_translation(
    subtitle_data: ASRData,
    config: EnhancedTranslationConfig,
    *,
    output_dir: str | Path,
    base_name: str,
    imported_glossary_path: str | Path | None = None,
    gateway: Optional[LLMGateway] = None,
    cancellation: Optional[CancellationToken] = None,
    progress: Optional[Callable[[int, str], None]] = None,
    confirm_terms: Optional[
        Callable[[tuple[TermCandidate, ...]], Sequence[TermCandidate]]
    ] = None,
    confirm_audit: Optional[Callable[[TranslationAuditReport], Sequence[int]]] = None,
    recovery_decision: Optional[Callable[[RecoverySummary], RecoveryDecision | str]] = None,
) -> EnhancedTranslationRun:
    """Run enhanced translation and persist glossary/report at their safe boundaries."""

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    source_language = str(getattr(config, "source_language", "") or "")
    target_language = str(getattr(config, "target_language", "") or "")
    workspace_root = resolve_output_workspace_root(destination)
    cues = tuple(
        SubtitleCue(cue_id=index, text=segment.text)
        for index, segment in enumerate(subtitle_data.segments, 1)
    )
    identity = _recovery_identity(
        source_fingerprint=subtitle_fingerprint(cues),
        source_language=source_language,
        target_language=target_language,
    )
    staging_dir = resolve_translation_staging_dir(
        workspace_root,
        task_name=base_name,
        source_fingerprint=identity["source_fingerprint"],
        source_language=source_language,
        target_language=target_language,
    )
    staging_dir.mkdir(parents=True, exist_ok=True)
    glossary_path = staging_dir / ASSET_FILENAMES["glossary"]
    audit_path = staging_dir / ASSET_FILENAMES["audit"]
    checkpoint_path = staging_dir / ASSET_FILENAMES["checkpoint"]
    # 翻译简报文件（级别①）：先原子写数据文件，manifest 才登记该级完成。
    context_path = staging_dir / ASSET_FILENAMES["context"]
    # 审计检查点（级别④）：每审计批完成后先原子写数据文件再更新 manifest。
    audit_checkpoint_path = staging_dir / AUDIT_CHECKPOINT_FILENAME
    recovery_manifest_path = staging_dir / _RECOVERY_MANIFEST_FILENAME
    explicit_imported = (
        load_glossary(imported_glossary_path) if imported_glossary_path is not None else None
    )
    manifest = _matching_recovery_manifest(recovery_manifest_path, identity)
    current_fingerprint = translation_config_fingerprint(config)
    resume_warnings: list[str] = []
    resumed_translations: dict[int, str] = {}
    checkpoint_glossary: AuthoritativeGlossary | None = None
    resumed_analysis: tuple[TranslationContextBrief, tuple[TermCandidate, ...]] | None = None
    resumed_audit_batches: dict[tuple[int, ...], tuple[TranslationAuditIssue, ...]] = {}
    recovery_summary: RecoverySummary | None = None
    recovery_provenance: RecoveryProvenance | None = None
    if manifest is not None:
        completed = manifest["completed"]
        if completed.get("analysis") is True:
            # 恢复只信 manifest；文件缺失、损坏或身份不符时该级视为未完成并告警。
            try:
                resumed_analysis = load_translation_brief(
                    context_path,
                    source_language=source_language,
                    target_language=target_language,
                    subtitle_fingerprint=identity["source_fingerprint"],
                )
            except BriefFormatError as exc:
                resume_warnings.append(f"翻译简报文件不可用，已重跑全文分析：{exc}")
        completed_ids = completed.get("translation_ids", [])
        if not isinstance(completed_ids, list) or not all(
            type(item) is int and item > 0 for item in completed_ids
        ):
            completed_ids = []
            resume_warnings.append("恢复 manifest 的译文进度无效，已重新翻译未完成字幕段")
        if completed.get("glossary") is True and glossary_path.is_file():
            try:
                checkpoint_glossary = load_glossary(glossary_path)
            except ValueError as exc:
                resume_warnings.append(f"检查点术语表不可读，已重新生成：{exc}")
        elif completed.get("glossary") is True:
            resume_warnings.append("恢复 manifest 登记的术语表缺失，已重新生成")
        if completed_ids:
            loaded_translations, warnings = _load_checkpoint_translations(checkpoint_path, subtitle_data)
            resumed_translations = {
                cue_id: text for cue_id, text in loaded_translations.items() if cue_id in completed_ids
            }
            resume_warnings.extend(warnings)
        audit_batch_keys = completed.get("audit_batches", [])
        if audit_batch_keys:
            # 恢复只信 manifest；文件缺失或损坏时该级视为未完成并告警。
            if not audit_checkpoint_path.is_file():
                resume_warnings.append("恢复 manifest 登记的审计检查点缺失，已重跑全部审计批")
            else:
                try:
                    resumed_audit_batches = load_audit_checkpoint(
                        audit_checkpoint_path,
                        source_language=source_language,
                        target_language=target_language,
                        subtitle_fingerprint=identity["source_fingerprint"],
                    )
                except AuditCheckpointError as exc:
                    resume_warnings.append(f"审计检查点不可读，已重跑全部审计批：{exc}")
                    resumed_audit_batches = {}
        resume_available = (
            resumed_analysis is not None
            or checkpoint_glossary is not None
            or bool(resumed_translations)
            or bool(resumed_audit_batches)
        )
        if resume_available:
            # 配置漂移（票 05）：逐项比对冻结摘要；任何漂移不作废检查点（ADR-0022）。
            frozen_fingerprint = manifest.get("config_fingerprint")
            frozen_fingerprint = (
                frozen_fingerprint if isinstance(frozen_fingerprint, dict) else None
            )
            drift_items = config_drift(
                frozen_fingerprint, current_fingerprint, translation_config_drift_labels()
            )
            drift_keys = drifted_keys(frozen_fingerprint, current_fingerprint)
            recovery_summary = RecoverySummary(
                module=_RECOVERY_MODULE,
                identity=identity,
                completed={
                    "analysis": int(resumed_analysis is not None),
                    "glossary": int(checkpoint_glossary is not None),
                    "translation_segments": len(resumed_translations),
                    "audit_batches": len(resumed_audit_batches),
                },
                checkpoint_time=str(manifest.get("updated_at", "")),
                configuration_drift=drift_items,
            )
            decision = (
                RecoveryDecision.CONTINUE
                if recovery_decision is None
                else RecoveryDecision(recovery_decision(recovery_summary))
            )
            if decision is RecoveryDecision.START_FRESH:
                manifest = _new_recovery_manifest(identity, base_name, config)
                write_recovery_manifest(recovery_manifest_path, manifest)
                resumed_analysis = None
                checkpoint_glossary = None
                resumed_translations = {}
                resumed_audit_batches = {}
                recovery_summary = None
                drift_items = ()
                drift_keys = ()
            else:
                # 恢复来源（票 05）：成果里的溯源标注——报告、快照与 manifest 各记一份。
                recovery_provenance = RecoveryProvenance(
                    checkpoint_time=recovery_summary.checkpoint_time,
                    completed=dict(recovery_summary.completed),
                    configuration_drift=drift_items,
                    drifted_keys=drift_keys,
                )
    if manifest is None:
        manifest = _new_recovery_manifest(identity, base_name, config)
        write_recovery_manifest(recovery_manifest_path, manifest)
    # 恢复告警在进入编排前输出：本次运行中途失败时告警也不丢失。
    if resume_warnings:
        logger.warning("恢复检查点告警：%s", "；".join(resume_warnings))
    imported = explicit_imported or checkpoint_glossary
    orchestrator = EnhancedTranslationOrchestrator(
        config,
        gateway=gateway,
        cancellation=cancellation,
        progress=progress,
    )

    def _mark_completed(**fields: Any) -> None:
        """Record completed recovery levels after their data files are durable."""

        completed = dict(manifest["completed"])
        completed.update(fields)
        manifest["completed"] = completed
        manifest["updated_at"] = now_utc()
        write_recovery_manifest(recovery_manifest_path, manifest)

    def persist_glossary(glossary: AuthoritativeGlossary) -> None:
        # The glossary file is atomically durable before the manifest declares it complete.
        save_glossary(glossary_path, glossary)
        _mark_completed(glossary=True)

    # 简报已落盘（恢复装载或本次写入）时不再重写检查点，发布时带 context 资产。
    analysis_durable = resumed_analysis is not None

    def persist_analysis(
        brief: TranslationContextBrief, candidates: tuple[TermCandidate, ...]
    ) -> None:
        nonlocal analysis_durable
        # The brief file is atomically durable before the manifest declares level ① complete.
        save_translation_brief(
            context_path,
            source_language=source_language,
            target_language=target_language,
            subtitle_fingerprint=identity["source_fingerprint"],
            brief=brief,
            candidates=candidates,
        )
        _mark_completed(analysis=True)
        analysis_durable = True

    checkpoint_written = bool(resumed_translations)
    accumulated_translations: dict[int, str] = dict(resumed_translations)

    def persist_translations(translations: Mapping[int, str]) -> None:
        nonlocal checkpoint_written
        accumulated_translations.update(translations)
        try:
            _save_checkpoint(
                checkpoint_path,
                _translated_copy(
                    subtitle_data,
                    accumulated_translations,
                    require_complete=False,
                ),
            )
        except OSError as exc:
            logger.warning("无法保存增强翻译检查点 %s: %s", checkpoint_path, exc)
            return
        checkpoint_written = True
        # The checkpoint file is atomically durable before the manifest records its IDs.
        _mark_completed(translation_ids=sorted(accumulated_translations))

    # 审计检查点按批累积：编排器对恢复的批与新跑的批都走 on_audit_batch
    # 回调（含键完全匹配的采纳批），这里只登记本次规划内的批；装载的
    # 检查点里键不匹配当前规划的批不进 manifest，避免虚记完成。
    accumulated_audit_batches: dict[tuple[int, ...], tuple[TranslationAuditIssue, ...]] = {}

    def persist_audit_batch(
        subtitle_ids: tuple[int, ...],
        issues: tuple[TranslationAuditIssue, ...],
    ) -> None:
        accumulated_audit_batches[subtitle_ids] = tuple(issues)
        try:
            save_audit_checkpoint(
                audit_checkpoint_path,
                source_language=source_language,
                target_language=target_language,
                subtitle_fingerprint=identity["source_fingerprint"],
                batches=accumulated_audit_batches,
            )
        except OSError as exc:
            logger.warning("无法保存审计检查点 %s: %s", audit_checkpoint_path, exc)
            return
        # The audit checkpoint file is atomically durable before the manifest records it.
        _mark_completed(audit_batches=[list(key) for key in sorted(accumulated_audit_batches)])

    try:
        result = orchestrator.run(
            cues,
            imported_glossary=imported,
            resume_analysis=resumed_analysis,
            resume_translations=resumed_translations,
            resume_audit_batches=resumed_audit_batches,
            confirm_terms=confirm_terms,
            confirm_audit=confirm_audit,
            on_analysis=None if analysis_durable else persist_analysis,
            on_glossary=persist_glossary,
            on_translations=persist_translations,
            on_audit_batch=persist_audit_batch,
        )
    except EnhancedTranslationError as exc:
        manifest["interruption"] = "failure"
        manifest["updated_at"] = now_utc()
        write_recovery_manifest(recovery_manifest_path, manifest)
        if checkpoint_written:
            raise EnhancedTranslationError(
                f"{exc}；主翻译结果已保存到检查点：{checkpoint_path}",
                stage=exc.stage,
                category=exc.category,
                retryable=exc.retryable,
                attempts=exc.attempts,
            ) from exc
        raise
    except InterruptedError:
        manifest["interruption"] = "stopped"
        manifest["updated_at"] = now_utc()
        write_recovery_manifest(recovery_manifest_path, manifest)
        raise
    translated = _translated_copy(subtitle_data, result.translations)
    persist_translations(result.translations)
    # 恢复过的运行在审计报告带上恢复来源与漂移标注（票 05）；不中断运行为 None。
    audit_report = (
        replace(result.audit_report, recovery=recovery_provenance)
        if recovery_provenance is not None
        else result.audit_report
    )
    save_audit_markdown(audit_path, audit_report)
    snapshot = TranslationExecutionSnapshot(
        method=METHOD_ENHANCED_LLM,
        boundary_context_radius=int(getattr(config, "boundary_context_radius", 3) or 3),
        main_profile=getattr(getattr(config, "main_role", None), "profile", None),
        review_profile=getattr(getattr(config, "review_role", None), "profile", None),
        main_prompt=str(getattr(getattr(config, "main_role", None), "user_prompt", "") or ""),
        review_prompt=str(getattr(getattr(config, "review_role", None), "user_prompt", "") or ""),
        source_language=source_language,
        target_language=target_language,
        # 恢复来源（票 05）：快照与 manifest 各带一份；不中断运行不带该字段。
        recovery=recovery_provenance,
    )
    published_assets = {
        "glossary": glossary_path,
        "audit": audit_path,
    }
    if checkpoint_written:
        published_assets["checkpoint"] = checkpoint_path
    if analysis_durable:
        published_assets["context"] = context_path
    task_dir = publish_translation_workspace(
        output_dir=destination,
        task_name=base_name,
        subtitle_data=translated,
        source_language=source_language,
        target_language=target_language,
        translation_method=METHOD_ENHANCED_LLM,
        assets=published_assets,
        snapshot_payload=snapshot.to_persisted(),
        recovery_payload=(
            recovery_provenance.to_persisted() if recovery_provenance is not None else None
        ),
    )
    published_glossary = task_dir / ASSET_FILENAMES["glossary"]
    published_audit = task_dir / ASSET_FILENAMES["audit"]
    published_checkpoint = task_dir / ASSET_FILENAMES["checkpoint"]
    published_context = task_dir / ASSET_FILENAMES["context"]
    artifacts = EnhancedTranslationArtifacts(
        glossary_path=published_glossary if published_glossary.is_file() else glossary_path,
        audit_report_path=published_audit if published_audit.is_file() else audit_path,
        translation_checkpoint_path=(
            published_checkpoint
            if checkpoint_written and published_checkpoint.is_file()
            else (checkpoint_path if checkpoint_written else None)
        ),
        context_path=(
            published_context
            if analysis_durable and published_context.is_file()
            else (context_path if analysis_durable else None)
        ),
    )
    # Recovery data is never a deliverable. Only delete it after all publication
    # work completed successfully, so a failing publish preserves the checkpoint.
    shutil.rmtree(staging_dir, ignore_errors=True)
    # 暂存目录整体删除（票 03）：连同只剩空壳的 .in-progress 祖先一起清掉，
    # 否则成功发布后会残留空目录、让同一目录可被误认作可恢复。
    _remove_empty_ancestors(staging_dir.parent, workspace_root)
    return EnhancedTranslationRun(
        subtitle_data=translated,
        result=replace(result, audit_report=audit_report),
        artifacts=artifacts,
        recovery_summary=recovery_summary,
        recovery_provenance=recovery_provenance,
    )
