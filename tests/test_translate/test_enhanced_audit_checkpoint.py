"""审计检查点（级别④恢复检查点）的 schema、版本与往返保真。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from videocaptioner.core.translate.enhanced.audit_checkpoint import (
    AUDIT_CHECKPOINT_FILENAME,
    AUDIT_CHECKPOINT_SCHEMA,
    AUDIT_CHECKPOINT_VERSION,
    AuditCheckpointError,
    load_audit_checkpoint,
    save_audit_checkpoint,
)
from videocaptioner.core.translate.enhanced.glossary import subtitle_fingerprint
from videocaptioner.core.translate.enhanced.models import (
    AuditIssueDisposition,
    SubtitleCue,
    TranslationAuditIssue,
)

_CUES = (
    SubtitleCue(1, "Mercury is visible."),
    SubtitleCue(2, "Mercury rises."),
)


def _issue(
    cue_id: int,
    *,
    categories: tuple[str, ...] = ("empty_translation",),
    message: str = "译文为空。",
    translated_text: str = "",
    suggested_translation: str = "",
) -> TranslationAuditIssue:
    return TranslationAuditIssue(
        cue_id=cue_id,
        category="",
        categories=categories,
        message=message,
        original_text=f"Source {cue_id}.",
        translated_text=translated_text,
        suggested_translation=suggested_translation,
    )


def _batches() -> dict[tuple[int, ...], tuple[TranslationAuditIssue, ...]]:
    return {
        (1,): (_issue(1),),
        (2,): (
            _issue(
                2,
                categories=("source_copied", "protected_token_missing"),
                message="译文与原文完全相同。",
                translated_text="Mercury rises.",
                suggested_translation="水星升起。",
            ),
        ),
    }


def _save(
    tmp_path: Path,
    batches: dict[tuple[int, ...], tuple[TranslationAuditIssue, ...]] | None = None,
) -> Path:
    return save_audit_checkpoint(
        tmp_path / AUDIT_CHECKPOINT_FILENAME,
        source_language="English",
        target_language="简体中文",
        subtitle_fingerprint=subtitle_fingerprint(_CUES),
        batches=_batches() if batches is None else batches,
    )


def _load(path: Path):
    return load_audit_checkpoint(
        path,
        source_language="English",
        target_language="简体中文",
        subtitle_fingerprint=subtitle_fingerprint(_CUES),
    )


def _document(tmp_path: Path) -> dict:
    return json.loads(_save(tmp_path).read_text(encoding="utf-8"))


def _write(path: Path, document: dict) -> None:
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


def test_saved_checkpoint_carries_schema_version_and_identity(tmp_path):
    document = _document(tmp_path)

    assert document["schema"] == AUDIT_CHECKPOINT_SCHEMA
    assert document["version"] == AUDIT_CHECKPOINT_VERSION
    assert document["source_language"] == "English"
    assert document["target_language"] == "简体中文"
    assert document["subtitle_fingerprint"] == subtitle_fingerprint(_CUES)


def test_saved_checkpoint_roundtrips_issues_and_keys(tmp_path):
    loaded = _load(_save(tmp_path))

    assert loaded == _batches()
    assert [key for key in loaded] == [(1,), (2,)]
    first = loaded[(1,)][0]
    assert first.category == "empty_translation"
    assert first.categories == ("empty_translation",)
    assert first.disposition is AuditIssueDisposition.REPORTED
    second = loaded[(2,)][0]
    assert second.category == "source_copied"
    assert second.categories == ("source_copied", "protected_token_missing")
    assert second.suggested_translation == "水星升起。"


def test_saved_checkpoint_drops_category_and_disposition_fields(tmp_path):
    document = _document(tmp_path)
    issues = document["batches"][0]["issues"]

    assert set(issues[0]) == {
        "cue_id",
        "categories",
        "message",
        "original_text",
        "translated_text",
        "suggested_translation",
    }
    assert "category" not in issues[0]
    assert "disposition" not in issues[0]


def test_saved_checkpoint_contains_no_prompts_keys_or_model_responses(tmp_path):
    document = _document(tmp_path)
    flattened = json.dumps(document, ensure_ascii=False)

    assert "MAIN USER PROMPT" not in flattened
    assert "secret" not in flattened
    assert "api_key" not in flattened
    for key in document:
        assert key not in {"prompt", "response", "api_key"}


def test_save_is_atomic_and_replaces_previous_content(tmp_path):
    first = _save(tmp_path)
    first.write_text("stale", encoding="utf-8")

    second = _save(tmp_path, {(2,): (_issue(2),)})
    assert second == first
    loaded = _load(second)
    assert list(loaded) == [(2,)]
    assert not list(tmp_path.glob("*.tmp"))


def test_save_sorts_batches_by_key(tmp_path):
    path = _save(tmp_path, {(2,): (_issue(2),), (1,): (_issue(1),)})
    document = json.loads(path.read_text(encoding="utf-8"))

    assert [entry["subtitle_ids"] for entry in document["batches"]] == [[1], [2]]


def test_load_rejects_corrupted_or_non_object_files(tmp_path):
    path = tmp_path / AUDIT_CHECKPOINT_FILENAME
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(AuditCheckpointError):
        _load(path)

    path.write_text('["array"]', encoding="utf-8")
    with pytest.raises(AuditCheckpointError):
        _load(path)


def test_load_rejects_unknown_schema_or_version(tmp_path):
    path = _save(tmp_path)
    document = _document(tmp_path)

    document["version"] = AUDIT_CHECKPOINT_VERSION + 1
    _write(path, document)
    with pytest.raises(AuditCheckpointError):
        _load(path)

    document["version"] = True
    _write(path, document)
    with pytest.raises(AuditCheckpointError):
        _load(path)

    document["version"] = AUDIT_CHECKPOINT_VERSION
    document["schema"] = "some.other.schema"
    _write(path, document)
    with pytest.raises(AuditCheckpointError):
        _load(path)


def test_load_rejects_mismatched_identity(tmp_path):
    path = _save(tmp_path)

    document = _document(tmp_path)
    document["subtitle_fingerprint"] = "sha256:" + "0" * 64
    _write(path, document)
    with pytest.raises(AuditCheckpointError):
        _load(path)

    document = _document(tmp_path)
    document["target_language"] = "日本語"
    _write(path, document)
    with pytest.raises(AuditCheckpointError):
        _load(path)


def test_load_rejects_invalid_batch_shapes(tmp_path):
    path = _save(tmp_path)
    document = _document(tmp_path)

    document["batches"] = {}
    _write(path, document)
    with pytest.raises(AuditCheckpointError):
        _load(path)

    document = _document(tmp_path)
    document["batches"][0]["subtitle_ids"] = []
    _write(path, document)
    with pytest.raises(AuditCheckpointError):
        _load(path)

    document = _document(tmp_path)
    document["batches"][0]["subtitle_ids"] = [0, -1]
    _write(path, document)
    with pytest.raises(AuditCheckpointError):
        _load(path)

    document = _document(tmp_path)
    document["batches"][0]["subtitle_ids"] = [True]
    _write(path, document)
    with pytest.raises(AuditCheckpointError):
        _load(path)

    document = _document(tmp_path)
    document["batches"][0]["issues"] = {}
    _write(path, document)
    with pytest.raises(AuditCheckpointError):
        _load(path)


def test_load_rejects_invalid_issue_shapes(tmp_path):
    path = _save(tmp_path)

    document = _document(tmp_path)
    document["batches"][0]["issues"][0]["cue_id"] = 99
    _write(path, document)
    with pytest.raises(AuditCheckpointError):
        _load(path)

    document = _document(tmp_path)
    document["batches"][0]["issues"][0]["cue_id"] = 0
    _write(path, document)
    with pytest.raises(AuditCheckpointError):
        _load(path)

    document = _document(tmp_path)
    document["batches"][0]["issues"][0]["cue_id"] = True
    _write(path, document)
    with pytest.raises(AuditCheckpointError):
        _load(path)

    document = _document(tmp_path)
    document["batches"][0]["issues"].append(document["batches"][0]["issues"][0])
    _write(path, document)
    with pytest.raises(AuditCheckpointError):
        _load(path)

    document = _document(tmp_path)
    document["batches"][0]["issues"][0]["categories"] = []
    _write(path, document)
    with pytest.raises(AuditCheckpointError):
        _load(path)

    document = _document(tmp_path)
    document["batches"][0]["issues"][0]["categories"] = ["empty_translation", 3]
    _write(path, document)
    with pytest.raises(AuditCheckpointError):
        _load(path)

    document = _document(tmp_path)
    document["batches"][0]["issues"][0]["message"] = ""
    _write(path, document)
    with pytest.raises(AuditCheckpointError):
        _load(path)


def test_load_rejects_duplicate_batch_keys(tmp_path):
    path = _save(tmp_path)
    document = _document(tmp_path)
    document["batches"].append({"subtitle_ids": [1], "issues": []})
    _write(path, document)

    with pytest.raises(AuditCheckpointError):
        _load(path)


def test_load_keeps_empty_optional_texts(tmp_path):
    issues = (
        _issue(1, translated_text="", suggested_translation=""),
    )
    loaded = _load(_save(tmp_path, {(1,): issues}))

    assert loaded == {(1,): issues}
    assert loaded[(1,)][0].translated_text == ""
    assert loaded[(1,)][0].suggested_translation == ""
