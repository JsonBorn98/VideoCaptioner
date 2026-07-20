"""Deterministic evidence and safe-fix validation for translation auditing."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Mapping, Sequence

from .models import (
    AuditIssueDisposition,
    SubtitleCue,
    TranslationAuditIssue,
)

_NUMBER_RE = re.compile(r"(?<!\w)[+-]?(?:\d[\d,._]*)(?:%|‰)?(?!\w)")
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>()]+", re.IGNORECASE)
_PLACEHOLDER_RE = re.compile(r"\{[^{}\n]+\}|\$\{[^{}\n]+\}|%\([^)]+\)[a-zA-Z]")
_TAG_RE = re.compile(r"</?[A-Za-z][^>\n]*>|\\[Nnh]")
_CODE_RE = re.compile(r"`[^`\n]+`")


def protected_tokens(text: str) -> tuple[str, ...]:
    """Return deterministic source tokens a suggested fix must preserve."""

    values: list[str] = []
    for pattern in (_URL_RE, _PLACEHOLDER_RE, _TAG_RE, _CODE_RE, _NUMBER_RE):
        values.extend(match.group(0) for match in pattern.finditer(text))
    return tuple(dict.fromkeys(values))


def local_audit_issues(
    source_cues: Sequence[SubtitleCue],
    translations: Mapping[int, str],
) -> tuple[TranslationAuditIssue, ...]:
    """Build objective evidence without touching timing or postprocessing rules."""

    issues: list[TranslationAuditIssue] = []
    for cue in source_cues:
        translated = translations.get(cue.cue_id, "")
        if not translated.strip():
            issues.append(
                TranslationAuditIssue(
                    cue_id=cue.cue_id,
                    category="empty_translation",
                    message="Translation is empty.",
                    original_text=cue.text,
                    translated_text=translated,
                    objective=True,
                )
            )
            continue
        if cue.text.strip().casefold() == translated.strip().casefold():
            issues.append(
                TranslationAuditIssue(
                    cue_id=cue.cue_id,
                    category="source_copied",
                    message="Translation is identical to the source.",
                    original_text=cue.text,
                    translated_text=translated,
                    objective=True,
                )
            )
        missing = [token for token in protected_tokens(cue.text) if token not in translated]
        if missing:
            issues.append(
                TranslationAuditIssue(
                    cue_id=cue.cue_id,
                    category="protected_token_missing",
                    message=f"Protected source tokens are missing: {', '.join(missing)}",
                    original_text=cue.text,
                    translated_text=translated,
                    objective=True,
                )
            )
    return tuple(issues)


def validate_suggested_translation(source_text: str, suggestion: str) -> tuple[bool, str]:
    if not suggestion.strip():
        return False, "suggested translation is empty"
    missing = [token for token in protected_tokens(source_text) if token not in suggestion]
    if missing:
        return False, f"suggested translation drops protected tokens: {', '.join(missing)}"
    return True, ""


def apply_objective_fixes(
    translations: Mapping[int, str],
    issues: Sequence[TranslationAuditIssue],
) -> tuple[dict[int, str], tuple[TranslationAuditIssue, ...]]:
    """Apply only objective suggestions that pass deterministic validation."""

    result = dict(translations)
    resolved: list[TranslationAuditIssue] = []
    for issue in issues:
        if not issue.objective or not issue.suggested_translation:
            resolved.append(replace(issue, disposition=AuditIssueDisposition.REPORTED))
            continue
        valid, reason = validate_suggested_translation(
            issue.original_text, issue.suggested_translation
        )
        if not valid:
            resolved.append(
                replace(
                    issue,
                    disposition=AuditIssueDisposition.FIX_VALIDATION_FAILED,
                    message=f"{issue.message} Fix rejected: {reason}",
                )
            )
            continue
        result[issue.cue_id] = issue.suggested_translation
        resolved.append(replace(issue, disposition=AuditIssueDisposition.AUTO_FIXED))
    return result, tuple(resolved)
