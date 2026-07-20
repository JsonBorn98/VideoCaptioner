"""Deterministic enhanced-translation prompt composition."""

from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape
from typing import Any, Mapping, Sequence

from .models import (
    GlossaryEntry,
    SubtitleCue,
    TranslationBatch,
    TranslationContextBrief,
)


@dataclass(frozen=True)
class PromptAssembly:
    """Stable prefix and request suffix kept separately for provider caching."""

    stable_prefix: str
    request_suffix: str

    @property
    def full_prompt(self) -> str:
        return f"{self.stable_prefix}\n\n{self.request_suffix}"


def _section(name: str, content: str) -> str:
    return f"<{name}>\n{content}\n</{name}>"


def _brief_text(brief: TranslationContextBrief | str) -> str:
    return brief.as_prompt_text() if isinstance(brief, TranslationContextBrief) else brief


def _glossary_payload(entries: Sequence[GlossaryEntry]) -> str:
    payload = [
        {
            "id": entry.entry_id,
            "source_term": entry.source_term,
            "sense": entry.sense,
            "translation": entry.translation,
            "aliases": list(entry.aliases),
        }
        for entry in entries
    ]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _dynamic_text(payload: str | Mapping[str, Any] | Sequence[Any]) -> str:
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def assemble_prompt(
    *,
    system_constraints: str,
    user_role_prompt: str,
    context_brief: TranslationContextBrief | str,
    glossary_entries: Sequence[GlossaryEntry],
    stage_instruction: str,
    dynamic_subtitles: str | Mapping[str, Any] | Sequence[Any],
    glossary_version: str = "1",
) -> PromptAssembly:
    """Assemble the six required prompt components in their fixed order.

    Source strings are preserved verbatim inside explicit sections. The stable
    prefix ends after the task brief and glossary version marker; block-level
    terms and subtitles remain in the per-request suffix.
    """

    glossary_opening = (
        '<RELEVANT_AUTHORITATIVE_TERMS version="'
        f'{escape(glossary_version, quote=True)}">'
    )
    stable_prefix = "\n\n".join(
        (
            _section("SYSTEM_CONSTRAINTS", system_constraints),
            _section("USER_ROLE_PROMPT", user_role_prompt),
            _section("TRANSLATION_CONTEXT_BRIEF", _brief_text(context_brief)),
            glossary_opening,
        )
    )
    request_suffix = "\n\n".join(
        (
            f"{_glossary_payload(glossary_entries)}\n</RELEVANT_AUTHORITATIVE_TERMS>",
            _section("STAGE_INSTRUCTION", stage_instruction),
            _section("DYNAMIC_SUBTITLES", _dynamic_text(dynamic_subtitles)),
        )
    )
    return PromptAssembly(stable_prefix=stable_prefix, request_suffix=request_suffix)


def translation_batch_payload(batch: TranslationBatch) -> dict[str, Any]:
    """Serialize subjects and read-only context into deliberately distinct fields."""

    def dump(cues: Sequence[SubtitleCue]) -> list[dict[str, Any]]:
        return [{"id": cue.cue_id, "text": cue.text} for cue in cues]

    return {
        "boundary_context_read_only": {
            "before": dump(batch.context_before),
            "after": dump(batch.context_after),
        },
        "translation_subjects": dump(batch.subjects),
        "allowed_output_ids": list(batch.subject_ids),
    }
