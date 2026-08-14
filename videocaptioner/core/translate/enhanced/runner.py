"""ASRData integration and durable enhanced-translation artifacts."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from videocaptioner.core.asr.asr_data import ASRData
from videocaptioner.core.llm import LLMGateway
from videocaptioner.core.utils.logger import setup_logger

from .glossary import load_glossary, save_glossary
from .models import (
    AuthoritativeGlossary,
    CancellationToken,
    EnhancedTranslationConfig,
    EnhancedTranslationError,
    EnhancedTranslationResult,
    SubtitleCue,
    TermCandidate,
    TranslationAuditReport,
)
from .orchestrator import EnhancedTranslationOrchestrator
from .report import save_audit_markdown

logger = setup_logger("enhanced_translation_runner")


@dataclass(frozen=True)
class EnhancedTranslationArtifacts:
    glossary_path: Path
    audit_report_path: Path
    translation_checkpoint_path: Optional[Path] = None


@dataclass(frozen=True)
class EnhancedTranslationRun:
    subtitle_data: ASRData
    result: EnhancedTranslationResult
    artifacts: EnhancedTranslationArtifacts


def _translated_copy(
    subtitle_data: ASRData, translations: Mapping[int, str]
) -> ASRData:
    translated = ASRData.from_json(subtitle_data.to_json())
    expected = set(range(1, len(translated.segments) + 1))
    if set(translations) != expected:
        raise ValueError("translation checkpoint does not cover every subtitle ID")
    for index, segment in enumerate(translated.segments, 1):
        segment.translated_text = translations[index]
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
) -> EnhancedTranslationRun:
    """Run enhanced translation and persist glossary/report at their safe boundaries."""

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    glossary_path = destination / f"【项目术语表】{base_name}.vcglossary.json"
    audit_path = destination / f"【翻译审计】{base_name}.md"
    checkpoint_path = destination / f"【增强翻译检查点】{base_name}.json"
    imported = (
        load_glossary(imported_glossary_path) if imported_glossary_path is not None else None
    )
    cues = tuple(
        SubtitleCue(cue_id=index, text=segment.text)
        for index, segment in enumerate(subtitle_data.segments, 1)
    )
    orchestrator = EnhancedTranslationOrchestrator(
        config,
        gateway=gateway,
        cancellation=cancellation,
        progress=progress,
    )

    def persist_glossary(glossary: AuthoritativeGlossary) -> None:
        save_glossary(glossary_path, glossary)

    checkpoint_written = False

    def persist_translations(translations: Mapping[int, str]) -> None:
        nonlocal checkpoint_written
        try:
            _save_checkpoint(
                checkpoint_path, _translated_copy(subtitle_data, translations)
            )
        except OSError as exc:
            logger.warning("无法保存增强翻译检查点 %s: %s", checkpoint_path, exc)
            return
        checkpoint_written = True

    try:
        result = orchestrator.run(
            cues,
            imported_glossary=imported,
            confirm_terms=confirm_terms,
            confirm_audit=confirm_audit,
            on_glossary=persist_glossary,
            on_translations=persist_translations,
        )
    except EnhancedTranslationError as exc:
        if checkpoint_written:
            raise EnhancedTranslationError(
                f"{exc}；主翻译结果已保存到检查点：{checkpoint_path}",
                stage=exc.stage,
                category=exc.category,
                retryable=exc.retryable,
                attempts=exc.attempts,
            ) from exc
        raise
    translated = _translated_copy(subtitle_data, result.translations)
    persist_translations(result.translations)
    save_audit_markdown(audit_path, result.audit_report)
    return EnhancedTranslationRun(
        subtitle_data=translated,
        result=result,
        artifacts=EnhancedTranslationArtifacts(
            glossary_path=glossary_path,
            audit_report_path=audit_path,
            translation_checkpoint_path=(checkpoint_path if checkpoint_written else None),
        ),
    )
