"""Versioned audit-issue persistence for the translation audit recovery checkpoint.

级别④审计检查点：每批翻译质量审计完成后，把该批产出的结构化审计问题按该批
覆盖的字幕编号集合为键原子写盘；恢复时键完全一致的批次直接采用，不发起任何
请求。文件只含结构化问题数据，不含完整 prompt、API key 或模型原始响应；
``category`` 与 ``disposition`` 不入盘，前者由 ``categories[0]`` 重建、后者
在写盘时必为默认的 REPORTED。遵守 ADR-0022。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from videocaptioner.core.recovery import atomic_write_json

from .glossary import normalize_term
from .models import TranslationAuditIssue

AUDIT_CHECKPOINT_SCHEMA = "videocaptioner.audit_checkpoint"
AUDIT_CHECKPOINT_VERSION = 1
AUDIT_CHECKPOINT_FILENAME = "audit-checkpoint.json"

_ISSUE_TEXT_FIELDS = ("original_text", "translated_text", "suggested_translation")


class AuditCheckpointError(ValueError):
    """Raised when an audit-checkpoint file cannot be parsed safely."""


def _is_positive_int(value: Any) -> bool:
    # ``type(value) is int`` rejects bool (``True`` is not a subtitle number).
    return type(value) is int and value > 0


def _issue_to_dict(issue: TranslationAuditIssue) -> dict[str, Any]:
    return {
        "cue_id": issue.cue_id,
        "categories": list(issue.categories),
        "message": issue.message,
        "original_text": issue.original_text,
        "translated_text": issue.translated_text,
        "suggested_translation": issue.suggested_translation,
    }


def _issue_from_dict(raw: Any, subtitle_ids: set[int]) -> TranslationAuditIssue:
    if not isinstance(raw, dict):
        raise AuditCheckpointError("each audit issue must be an object")
    cue_id = raw.get("cue_id")
    if not _is_positive_int(cue_id):
        raise AuditCheckpointError("issue cue_id must be a positive integer")
    if cue_id not in subtitle_ids:
        raise AuditCheckpointError("issue cue_id must belong to its batch")
    categories = raw.get("categories")
    if (
        not isinstance(categories, list)
        or not categories
        or not all(isinstance(item, str) and item for item in categories)
    ):
        raise AuditCheckpointError("issue categories must be a non-empty string list")
    message = raw.get("message")
    if not isinstance(message, str) or not message:
        raise AuditCheckpointError("issue message must be a non-empty string")
    texts: dict[str, str] = {}
    for name in _ISSUE_TEXT_FIELDS:
        value = raw.get(name)
        if not isinstance(value, str):
            raise AuditCheckpointError(f"issue {name} must be a string")
        texts[name] = value
    return TranslationAuditIssue(
        cue_id=cue_id,
        category="",
        categories=tuple(categories),
        message=message,
        original_text=texts["original_text"],
        translated_text=texts["translated_text"],
        suggested_translation=texts["suggested_translation"],
    )


def _batch_from_dict(raw: Any) -> tuple[tuple[int, ...], tuple[TranslationAuditIssue, ...]]:
    if not isinstance(raw, dict):
        raise AuditCheckpointError("each batch must be an object")
    ids_raw = raw.get("subtitle_ids")
    if (
        not isinstance(ids_raw, list)
        or not ids_raw
        or not all(_is_positive_int(item) for item in ids_raw)
    ):
        raise AuditCheckpointError("batch subtitle_ids must be a non-empty integer list")
    key = tuple(sorted(set(ids_raw)))
    issues_raw = raw.get("issues")
    if not isinstance(issues_raw, list):
        raise AuditCheckpointError("batch issues must be a list")

    subtitle_ids = set(key)
    seen: set[int] = set()
    issues: list[TranslationAuditIssue] = []
    for item in issues_raw:
        issue = _issue_from_dict(item, subtitle_ids)
        if issue.cue_id in seen:
            raise AuditCheckpointError("batch must not repeat an issue cue_id")
        seen.add(issue.cue_id)
        issues.append(issue)
    return key, tuple(issues)


def save_audit_checkpoint(
    path: str | Path,
    *,
    source_language: str,
    target_language: str,
    subtitle_fingerprint: str,
    batches: Mapping[tuple[int, ...], tuple[TranslationAuditIssue, ...]],
) -> Path:
    """Atomically persist a canonical, versioned audit-checkpoint file.

    Batches are written in ascending key order so identical work produces an
    identical file; each batch's subtitle IDs are written verbatim as
    ``list(key)`` under the caller's already-sorted contract.
    """

    return atomic_write_json(
        path,
        {
            "schema": AUDIT_CHECKPOINT_SCHEMA,
            "version": AUDIT_CHECKPOINT_VERSION,
            "source_language": source_language,
            "target_language": target_language,
            "subtitle_fingerprint": subtitle_fingerprint,
            "batches": [
                {
                    "subtitle_ids": list(key),
                    "issues": [_issue_to_dict(issue) for issue in issues],
                }
                for key, issues in sorted(batches.items(), key=lambda item: item[0])
            ],
        },
    )


def load_audit_checkpoint(
    path: str | Path,
    *,
    source_language: str,
    target_language: str,
    subtitle_fingerprint: str,
) -> dict[tuple[int, ...], tuple[TranslationAuditIssue, ...]]:
    """Load audit issues keyed by covered subtitle IDs, rejecting damaged data.

    Damaged files, unknown schema/version, identity (language pair or source
    fingerprint) mismatches, and malformed batch/issue shapes all raise
    ``AuditCheckpointError``; the caller decides whether that means re-running
    the affected audit batches.
    """

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuditCheckpointError(f"unable to read audit checkpoint: {exc}") from exc
    if not isinstance(data, dict):
        raise AuditCheckpointError("audit checkpoint root must be an object")
    if data.get("schema") != AUDIT_CHECKPOINT_SCHEMA:
        raise AuditCheckpointError("audit checkpoint schema is not supported")
    version = data.get("version")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != AUDIT_CHECKPOINT_VERSION
    ):
        raise AuditCheckpointError("audit checkpoint version is not supported")
    for key in ("source_language", "target_language", "subtitle_fingerprint"):
        if not isinstance(data.get(key), str):
            raise AuditCheckpointError(f"{key} must be a string")
    if (
        normalize_term(str(data["source_language"])) != normalize_term(source_language)
        or normalize_term(str(data["target_language"])) != normalize_term(target_language)
    ):
        raise AuditCheckpointError("audit checkpoint languages do not match this task")
    if str(data["subtitle_fingerprint"]) != subtitle_fingerprint:
        raise AuditCheckpointError("audit checkpoint fingerprint does not match this task")
    batches_raw = data.get("batches")
    if not isinstance(batches_raw, list):
        raise AuditCheckpointError("batches must be a list")

    batches: dict[tuple[int, ...], tuple[TranslationAuditIssue, ...]] = {}
    for raw in batches_raw:
        key, issues = _batch_from_dict(raw)
        if key in batches:
            raise AuditCheckpointError("audit checkpoint must not repeat a batch key")
        batches[key] = issues
    return batches
