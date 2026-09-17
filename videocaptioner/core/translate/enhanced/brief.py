"""Versioned translation-brief persistence for the analysis recovery checkpoint.

级别①恢复检查点（翻译简报文件）：分层全文分析完成后把翻译上下文简报与
去重后的疑难术语候选原子写盘，恢复时替代重跑全文分析。文件只含简报与
候选的结构化数据（含代表语境所需的出现字幕段编号），不含完整 prompt、
API key 或模型原始响应；不随项目术语表文件导出。遵守 ADR-0022。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from videocaptioner.core.recovery import atomic_write_json

from .glossary import normalize_term
from .models import TermCandidate, TranslationContextBrief

BRIEF_SCHEMA = "videocaptioner.translation_brief"
BRIEF_VERSION = 1

# 简报字段名固定顺序只影响可读性；校验用别处一致的 sort_keys 序列化。
_BRIEF_SECTIONS = ("outline", "background", "themes", "style_notes", "translation_notes")


class BriefFormatError(ValueError):
    """Raised when a translation-brief file cannot be parsed safely."""


def _string_list(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise BriefFormatError(f"{name} must be a string list")
    return tuple(value)


def _candidate_from_dict(raw: Any) -> TermCandidate:
    if not isinstance(raw, dict):
        raise BriefFormatError("each candidate must be an object")
    source_term = raw.get("source_term")
    candidate_id = raw.get("id")
    sense = raw.get("sense", "")
    aliases = raw.get("aliases", [])
    occurrences = raw.get("occurrence_ids", [])
    if not isinstance(source_term, str) or not source_term.strip():
        raise BriefFormatError("candidate source_term must be a non-empty string")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise BriefFormatError("candidate id must be a non-empty string")
    if not isinstance(sense, str):
        raise BriefFormatError("candidate sense must be a string")
    if not isinstance(aliases, list) or not all(
        isinstance(item, str) for item in aliases
    ):
        raise BriefFormatError("candidate aliases must be a string list")
    if not isinstance(occurrences, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) and item > 0
        for item in occurrences
    ):
        raise BriefFormatError("candidate occurrence_ids must be positive integers")
    representative = raw.get("representative_context_ids", [])
    if not isinstance(representative, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) and item > 0
        for item in representative
    ):
        raise BriefFormatError("candidate representative_context_ids must be positive integers")
    return TermCandidate(
        candidate_id=candidate_id.strip(),
        source_term=source_term.strip(),
        sense=sense,
        aliases=tuple(dict.fromkeys(alias for alias in aliases if alias.strip())),
        occurrence_ids=tuple(sorted(set(occurrences))),
        representative_context_ids=tuple(sorted(set(representative))),
    )


def _brief_to_dict(brief: TranslationContextBrief) -> dict[str, Any]:
    return {
        "outline": brief.outline,
        "background": brief.background,
        "themes": list(brief.themes),
        "style_notes": list(brief.style_notes),
        "translation_notes": list(brief.translation_notes),
    }


def _brief_from_dict(raw: Any) -> TranslationContextBrief:
    if not isinstance(raw, dict):
        raise BriefFormatError("brief must be an object")
    for name in _BRIEF_SECTIONS:
        if name not in raw:
            raise BriefFormatError(f"brief.{name} is required")
    return TranslationContextBrief(
        outline=str(raw["outline"]),
        background=str(raw["background"]),
        themes=_string_list(raw["themes"], "brief.themes"),
        style_notes=_string_list(raw["style_notes"], "brief.style_notes"),
        translation_notes=_string_list(raw["translation_notes"], "brief.translation_notes"),
    )


def translation_brief_to_dict(
    *,
    source_language: str,
    target_language: str,
    subtitle_fingerprint: str,
    brief: TranslationContextBrief,
    candidates: tuple[TermCandidate, ...],
) -> dict[str, Any]:
    return {
        "schema": BRIEF_SCHEMA,
        "version": BRIEF_VERSION,
        "source_language": source_language,
        "target_language": target_language,
        "subtitle_fingerprint": subtitle_fingerprint,
        "brief": _brief_to_dict(brief),
        "candidates": [
            {
                "id": candidate.candidate_id,
                "source_term": candidate.source_term,
                "sense": candidate.sense,
                "aliases": list(candidate.aliases),
                "occurrence_ids": list(candidate.occurrence_ids),
                "representative_context_ids": list(candidate.representative_context_ids),
            }
            for candidate in candidates
        ],
    }


def save_translation_brief(
    path: str | Path,
    *,
    source_language: str,
    target_language: str,
    subtitle_fingerprint: str,
    brief: TranslationContextBrief,
    candidates: tuple[TermCandidate, ...],
) -> Path:
    """Atomically persist a canonical, versioned translation-brief file."""

    return atomic_write_json(
        path,
        translation_brief_to_dict(
            source_language=source_language,
            target_language=target_language,
            subtitle_fingerprint=subtitle_fingerprint,
            brief=brief,
            candidates=candidates,
        ),
    )


def load_translation_brief(
    path: str | Path,
    *,
    source_language: str,
    target_language: str,
    subtitle_fingerprint: str,
) -> tuple[TranslationContextBrief, tuple[TermCandidate, ...]]:
    """Load a translation-brief file, rejecting damaged or mismatched data.

    Damaged files, unknown schema/version, and identity (language pair or
    source fingerprint) mismatches all raise ``BriefFormatError``; the caller
    decides whether that means re-running whole-subtitle analysis.
    """

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BriefFormatError(f"unable to read translation brief: {exc}") from exc
    if not isinstance(data, dict):
        raise BriefFormatError("translation brief root must be an object")
    if data.get("schema") != BRIEF_SCHEMA:
        raise BriefFormatError("translation brief schema is not supported")
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version != BRIEF_VERSION:
        raise BriefFormatError("translation brief version is not supported")
    for key in ("source_language", "target_language", "subtitle_fingerprint"):
        if not isinstance(data.get(key), str):
            raise BriefFormatError(f"{key} must be a string")
    if (
        normalize_term(str(data["source_language"])) != normalize_term(source_language)
        or normalize_term(str(data["target_language"])) != normalize_term(target_language)
    ):
        raise BriefFormatError("translation brief languages do not match this task")
    if str(data["subtitle_fingerprint"]) != subtitle_fingerprint:
        raise BriefFormatError("translation brief fingerprint does not match this task")
    candidates_raw = data.get("candidates")
    if not isinstance(candidates_raw, list):
        raise BriefFormatError("candidates must be a list")
    brief = _brief_from_dict(data.get("brief"))
    candidates = tuple(_candidate_from_dict(raw) for raw in candidates_raw)
    return brief, candidates
