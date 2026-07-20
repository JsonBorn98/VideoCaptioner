"""ASRData integration and durable enhanced-translation artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from videocaptioner.core.asr.asr_data import ASRData
from videocaptioner.core.llm import LLMGateway

from .glossary import load_glossary, save_glossary
from .models import (
    AuthoritativeGlossary,
    CancellationToken,
    EnhancedTranslationConfig,
    EnhancedTranslationResult,
    SubtitleCue,
    TermCandidate,
)
from .orchestrator import EnhancedTranslationOrchestrator
from .report import save_audit_markdown


@dataclass(frozen=True)
class EnhancedTranslationArtifacts:
    glossary_path: Path
    audit_report_path: Path


@dataclass(frozen=True)
class EnhancedTranslationRun:
    subtitle_data: ASRData
    result: EnhancedTranslationResult
    artifacts: EnhancedTranslationArtifacts


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
) -> EnhancedTranslationRun:
    """Run enhanced translation and persist glossary/report at their safe boundaries."""

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    glossary_path = destination / f"【项目术语表】{base_name}.vcglossary.json"
    audit_path = destination / f"【翻译审计】{base_name}.md"
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

    result = orchestrator.run(
        cues,
        imported_glossary=imported,
        confirm_terms=confirm_terms,
        on_glossary=persist_glossary,
    )
    translated = ASRData.from_json(subtitle_data.to_json())
    for index, segment in enumerate(translated.segments, 1):
        segment.translated_text = result.translations[index]
    save_audit_markdown(audit_path, result.audit_report)
    return EnhancedTranslationRun(
        subtitle_data=translated,
        result=result,
        artifacts=EnhancedTranslationArtifacts(
            glossary_path=glossary_path,
            audit_report_path=audit_path,
        ),
    )
