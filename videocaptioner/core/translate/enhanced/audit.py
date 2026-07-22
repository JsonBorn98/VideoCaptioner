"""Deterministic evidence and safe-fix validation for translation auditing."""

from __future__ import annotations

import re
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence

from .models import (
    AuditIssueDisposition,
    GlossaryEntry,
    SubtitleCue,
    TranslationAuditIssue,
)

_NUMBER_RE = re.compile(
    r"(?<![\dA-Za-z_.])[+-]?"
    r"(?:(?:\d{1,3}(?:,\d{3})+)|(?:\d+(?:_\d+)*))"
    r"(?:\.\d+(?:_\d+)*)?"
    r"(?:%|％|‰)?(?![\dA-Za-z_])"
)
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>()]+", re.IGNORECASE)
_PLACEHOLDER_RE = re.compile(r"\{[^{}\n]+\}|\$\{[^{}\n]+\}|%\([^)]+\)[a-zA-Z]")
_TAG_RE = re.compile(r"</?[A-Za-z][^>\n]*>|\\[Nnh]")
_CODE_RE = re.compile(r"`[^`\n]+`")


def protected_tokens(text: str) -> tuple[str, ...]:
    """Return deterministic source tokens a suggested fix must preserve."""

    values: list[str] = []
    for pattern in (_URL_RE, _PLACEHOLDER_RE, _TAG_RE, _CODE_RE, _NUMBER_RE):
        for match in pattern.finditer(text):
            value = match.group(0)
            if pattern is _URL_RE:
                value = value.rstrip(".,;:!?，。；：！？")
            if value:
                values.append(value)
    return tuple(dict.fromkeys(values))


def _normalized_number(token: str) -> tuple[Decimal, str] | None:
    """Return a comparison key for an extracted number.

    Grouping separators and insignificant decimal zeroes may legitimately change
    during translation.  The unit suffix remains significant, with the ASCII and
    full-width percent signs treated as equivalent.
    """

    match = _NUMBER_RE.fullmatch(token)
    if match is None:
        return None
    suffix = ""
    if token.endswith(("%", "％", "‰")):
        suffix = "%" if token[-1] in {"%", "％"} else "‰"
        token = token[:-1]
    try:
        return Decimal(token.replace(",", "").replace("_", "")), suffix
    except InvalidOperation:
        return None


def _missing_protected_tokens(source_text: str, translated_text: str) -> list[str]:
    translated_numbers = {
        normalized
        for match in _NUMBER_RE.finditer(translated_text)
        if (normalized := _normalized_number(match.group(0))) is not None
    }
    missing: list[str] = []
    for token in protected_tokens(source_text):
        normalized = _normalized_number(token)
        if normalized is not None:
            if normalized not in translated_numbers:
                missing.append(token)
        elif token not in translated_text:
            missing.append(token)
    return missing


def _source_contains_term(source_text: str, source_form: str) -> bool:
    if not source_form.strip():
        return False
    if source_form.isascii() and source_form[0].isalnum() and source_form[-1].isalnum():
        return (
            re.search(
                rf"(?<!\w){re.escape(source_form)}(?!\w)", source_text, re.IGNORECASE
            )
            is not None
        )
    return source_form.casefold() in source_text.casefold()


def local_audit_issues(
    source_cues: Sequence[SubtitleCue],
    translations: Mapping[int, str],
) -> tuple[TranslationAuditIssue, ...]:
    """Build deterministic evidence without touching timing or postprocessing rules."""

    issues: list[TranslationAuditIssue] = []
    for cue in source_cues:
        translated = translations.get(cue.cue_id, "")
        if not translated.strip():
            issues.append(
                TranslationAuditIssue(
                    cue_id=cue.cue_id,
                    category="empty_translation",
                    message="译文为空。",
                    original_text=cue.text,
                    translated_text=translated,
                )
            )
            continue
        if cue.text.strip().casefold() == translated.strip().casefold():
            issues.append(
                TranslationAuditIssue(
                    cue_id=cue.cue_id,
                    category="source_copied",
                    message="译文与原文完全相同，可能未翻译。",
                    original_text=cue.text,
                    translated_text=translated,
                )
            )
        missing = _missing_protected_tokens(cue.text, translated)
        if missing:
            issues.append(
                TranslationAuditIssue(
                    cue_id=cue.cue_id,
                    category="protected_token_missing",
                    message=f"译文缺少原文中的关键信息：{', '.join(missing)}",
                    original_text=cue.text,
                    translated_text=translated,
                )
            )
    return tuple(issues)


def validate_suggested_translation(
    source_text: str,
    suggestion: str,
    authoritative_terms: Sequence[GlossaryEntry] = (),
    current_translation: str | None = None,
) -> tuple[bool, str]:
    if not suggestion.strip():
        return False, "建议译文为空"
    if any(character in suggestion for character in ("\x00", "\r")):
        return False, "建议译文包含非法控制字符"
    if current_translation is not None and suggestion.count("\n") != current_translation.count("\n"):
        return False, "建议译文改变了字幕换行结构"
    missing = _missing_protected_tokens(source_text, suggestion)
    if missing:
        return False, f"建议译文遗漏关键信息：{', '.join(missing)}"
    for term in authoritative_terms:
        source_forms = (term.source_term, *term.aliases)
        if any(_source_contains_term(source_text, form) for form in source_forms):
            if term.translation and term.translation not in suggestion:
                return False, f"建议译文未采用权威术语：{term.source_term} → {term.translation}"
    return True, ""


def apply_review_fixes(
    translations: Mapping[int, str],
    issues: Sequence[TranslationAuditIssue],
    *,
    authoritative_terms: Sequence[GlossaryEntry] = (),
    accepted_ids: set[int] | None = None,
) -> tuple[dict[int, str], tuple[TranslationAuditIssue, ...]]:
    """Apply consolidated review suggestions after deterministic hard validation.

    ``accepted_ids=None`` is automatic mode and applies every valid suggestion.
    A concrete set represents the user's choices in interactive review mode.
    """

    result = dict(translations)
    resolved: list[TranslationAuditIssue] = []
    for issue in issues:
        suggestion = issue.suggested_translation.strip()
        if not suggestion or suggestion == translations.get(issue.cue_id, "").strip():
            resolved.append(replace(issue, disposition=AuditIssueDisposition.REPORTED))
            continue
        valid, reason = validate_suggested_translation(
            issue.original_text,
            suggestion,
            authoritative_terms,
            translations.get(issue.cue_id, ""),
        )
        if not valid:
            resolved.append(
                replace(
                    issue,
                    disposition=AuditIssueDisposition.FIX_VALIDATION_FAILED,
                    message=f"{issue.message} 未应用修复：{reason}",
                )
            )
            continue
        if accepted_ids is not None and issue.cue_id not in accepted_ids:
            resolved.append(replace(issue, disposition=AuditIssueDisposition.USER_REJECTED))
            continue
        result[issue.cue_id] = suggestion
        disposition = (
            AuditIssueDisposition.AUTO_APPLIED
            if accepted_ids is None
            else AuditIssueDisposition.USER_APPLIED
        )
        resolved.append(replace(issue, disposition=disposition))
    return result, tuple(resolved)
