"""Versioned project glossary persistence and deterministic relevance rules."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable, Sequence

from .models import (
    AuthoritativeGlossary,
    GlossaryEntry,
    GlossaryImportMode,
    GlossaryImportResult,
    GlossarySelectionSource,
    SubtitleCue,
)

GLOSSARY_SCHEMA = "videocaptioner.project_glossary"
GLOSSARY_VERSION = 1


class GlossaryFormatError(ValueError):
    """Raised when a project glossary cannot be parsed safely."""


def normalize_subtitle_text(text: str) -> str:
    """Normalize only representation differences, never subtitle meaning."""

    normalized = unicodedata.normalize("NFKC", text).replace("\r\n", "\n")
    normalized = normalized.replace("\r", "\n")
    return " ".join(normalized.split())


def normalize_term(text: str) -> str:
    return normalize_subtitle_text(text).casefold()


def subtitle_fingerprint(cues: Iterable[SubtitleCue]) -> str:
    """Return a deterministic fingerprint of numbered, normalized source cues."""

    payload = [
        {"id": cue.cue_id, "text": normalize_subtitle_text(cue.text)} for cue in cues
    ]
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def glossary_to_dict(glossary: AuthoritativeGlossary) -> dict[str, Any]:
    return {
        "schema": glossary.schema,
        "version": glossary.version,
        "source_language": glossary.source_language,
        "target_language": glossary.target_language,
        "subtitle_fingerprint": glossary.subtitle_fingerprint,
        "entries": [
            {
                "id": entry.entry_id,
                "source_term": entry.source_term,
                "sense": entry.sense,
                "translation": entry.translation,
                "aliases": list(entry.aliases),
                "occurrence_ids": list(entry.occurrence_ids),
                "selection_source": entry.selection_source.value,
                "high_risk": entry.high_risk,
                "metadata": dict(entry.metadata),
            }
            for entry in glossary.entries
        ],
    }


def _expect_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise GlossaryFormatError(f"{key} must be a string")
    return value


def glossary_from_dict(data: Any) -> AuthoritativeGlossary:
    if not isinstance(data, dict):
        raise GlossaryFormatError("glossary root must be an object")
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise GlossaryFormatError("version must be an integer")
    entries_data = data.get("entries")
    if not isinstance(entries_data, list):
        raise GlossaryFormatError("entries must be a list")

    entries: list[GlossaryEntry] = []
    try:
        for raw in entries_data:
            if not isinstance(raw, dict):
                raise GlossaryFormatError("each entry must be an object")
            aliases = raw.get("aliases", [])
            occurrences = raw.get("occurrence_ids", [])
            metadata = raw.get("metadata", {})
            if not isinstance(aliases, list) or not all(
                isinstance(item, str) for item in aliases
            ):
                raise GlossaryFormatError("aliases must be a string list")
            if not isinstance(occurrences, list) or not all(
                isinstance(item, int) and not isinstance(item, bool)
                for item in occurrences
            ):
                raise GlossaryFormatError("occurrence_ids must be an integer list")
            if not isinstance(metadata, dict):
                raise GlossaryFormatError("metadata must be an object")
            entries.append(
                GlossaryEntry(
                    entry_id=_expect_string(raw, "id"),
                    source_term=_expect_string(raw, "source_term"),
                    sense=_expect_string(raw, "sense"),
                    translation=_expect_string(raw, "translation"),
                    aliases=tuple(aliases),
                    occurrence_ids=tuple(occurrences),
                    selection_source=GlossarySelectionSource(
                        _expect_string(raw, "selection_source")
                    ),
                    high_risk=bool(raw.get("high_risk", False)),
                    metadata=metadata,
                )
            )
        return AuthoritativeGlossary(
            schema=_expect_string(data, "schema"),
            version=version,
            source_language=_expect_string(data, "source_language"),
            target_language=_expect_string(data, "target_language"),
            subtitle_fingerprint=_expect_string(data, "subtitle_fingerprint"),
            entries=tuple(entries),
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, GlossaryFormatError):
            raise
        raise GlossaryFormatError(str(exc)) from exc


def save_glossary(path: str | Path, glossary: AuthoritativeGlossary) -> Path:
    """Atomically persist a canonical, versioned ``.vcglossary.json`` file."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = (
        json.dumps(
            glossary_to_dict(glossary),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=destination.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return destination


def load_glossary(path: str | Path) -> AuthoritativeGlossary:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GlossaryFormatError(f"unable to read glossary: {exc}") from exc
    return glossary_from_dict(data)


def classify_glossary_import(
    glossary: AuthoritativeGlossary,
    *,
    source_language: str,
    target_language: str,
    cues: Sequence[SubtitleCue],
) -> GlossaryImportResult:
    """Classify an import and strip stale cue positions from seed entries."""

    if glossary.schema != GLOSSARY_SCHEMA or glossary.version != GLOSSARY_VERSION:
        return GlossaryImportResult(
            GlossaryImportMode.INCOMPATIBLE,
            None,
            "unsupported glossary schema or version",
        )
    if (
        normalize_term(glossary.source_language) != normalize_term(source_language)
        or normalize_term(glossary.target_language) != normalize_term(target_language)
    ):
        return GlossaryImportResult(
            GlossaryImportMode.INCOMPATIBLE,
            None,
            "language pair does not match",
        )
    if glossary.subtitle_fingerprint == subtitle_fingerprint(cues):
        return GlossaryImportResult(GlossaryImportMode.EXACT, glossary)

    seed_entries = tuple(
        replace(entry, occurrence_ids=(), selection_source=GlossarySelectionSource.IMPORTED)
        for entry in glossary.entries
    )
    seed = replace(
        glossary,
        entries=seed_entries,
    )
    return GlossaryImportResult(
        GlossaryImportMode.SEED,
        seed,
        "subtitle fingerprint differs; occurrences must be rescanned",
    )


def _contains_term(normalized_text: str, normalized_candidate: str) -> bool:
    if not normalized_candidate:
        return False
    escaped = re.escape(normalized_candidate)
    if (
        normalized_candidate.isascii()
        and normalized_candidate[0].isalnum()
        and normalized_candidate[-1].isalnum()
    ):
        return re.search(rf"(?<!\w){escaped}(?!\w)", normalized_text) is not None
    return normalized_candidate in normalized_text


def select_relevant_entries(
    glossary: AuthoritativeGlossary,
    cues: Sequence[SubtitleCue],
) -> tuple[GlossaryEntry, ...]:
    """Select a deterministic block-level glossary subset without an LLM call."""

    cue_ids = {cue.cue_id for cue in cues}
    normalized_texts = tuple(normalize_term(cue.text) for cue in cues)
    relevant: list[GlossaryEntry] = []
    for entry in glossary.entries:
        if cue_ids.intersection(entry.occurrence_ids):
            relevant.append(entry)
            continue
        candidates = (entry.source_term, *entry.aliases)
        if any(
            _contains_term(text, normalize_term(candidate))
            for text in normalized_texts
            for candidate in candidates
        ):
            relevant.append(entry)
    return tuple(relevant)
