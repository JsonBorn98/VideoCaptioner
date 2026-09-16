"""翻译简报文件（级别①恢复检查点）的 schema、版本与往返保真。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from videocaptioner.core.translate.enhanced.brief import (
    BRIEF_SCHEMA,
    BRIEF_VERSION,
    BriefFormatError,
    load_translation_brief,
    save_translation_brief,
)
from videocaptioner.core.translate.enhanced.glossary import subtitle_fingerprint
from videocaptioner.core.translate.enhanced.models import (
    SubtitleCue,
    TermCandidate,
    TranslationContextBrief,
)


def _brief() -> TranslationContextBrief:
    return TranslationContextBrief(
        outline="A planetary science lecture",
        background="An educational video",
        themes=("astronomy", "orbital mechanics"),
        style_notes=("concise", "technical terms kept"),
        translation_notes=("Use established astronomical names",),
    )


def _candidates() -> tuple[TermCandidate, ...]:
    return (
        TermCandidate(
            candidate_id="mercury-planet",
            source_term="Mercury",
            sense="the planet",
            aliases=("planet Mercury",),
            occurrence_ids=(1, 4),
        ),
        TermCandidate(
            candidate_id="mercury-element",
            source_term="Mercury",
            sense="the chemical element",
            aliases=(),
            occurrence_ids=(7,),
        ),
    )


_CUES = (SubtitleCue(1, "Mercury is visible."), SubtitleCue(4, "Mercury rises."),
         SubtitleCue(7, "Mercury poisoning."))


def _save(tmp_path: Path) -> Path:
    return save_translation_brief(
        tmp_path / "context.json",
        source_language="English",
        target_language="简体中文",
        subtitle_fingerprint=subtitle_fingerprint(_CUES),
        brief=_brief(),
        candidates=_candidates(),
    )


def test_saved_brief_carries_schema_version_and_identity(tmp_path):
    path = _save(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))

    assert document["schema"] == BRIEF_SCHEMA
    assert document["version"] == BRIEF_VERSION
    assert document["source_language"] == "English"
    assert document["target_language"] == "简体中文"
    assert document["subtitle_fingerprint"] == subtitle_fingerprint(_CUES)


def test_saved_brief_roundtrips_brief_and_candidates_verbatim(tmp_path):
    brief, candidates = load_translation_brief(
        _save(tmp_path),
        source_language="English",
        target_language="简体中文",
        subtitle_fingerprint=subtitle_fingerprint(_CUES),
    )

    assert brief == _brief()
    assert candidates == _candidates()
    assert [candidate.candidate_id for candidate in candidates] == [
        "mercury-planet",
        "mercury-element",
    ]


def test_saved_brief_contains_no_prompts_keys_or_model_responses(tmp_path):
    document = json.loads(_save(tmp_path).read_text(encoding="utf-8"))
    flattened = json.dumps(document, ensure_ascii=False)

    assert "MAIN USER PROMPT" not in flattened
    assert "secret" not in flattened
    assert "api_key" not in flattened
    for key in document:
        assert key not in {"prompt", "response", "api_key"}


def test_load_rejects_corrupted_or_non_object_files(tmp_path):
    path = tmp_path / "context.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(BriefFormatError):
        load_translation_brief(path, source_language='English', target_language='简体中文', subtitle_fingerprint=subtitle_fingerprint(_CUES))

    path.write_text('["array"]', encoding="utf-8")
    with pytest.raises(BriefFormatError):
        load_translation_brief(path, source_language='English', target_language='简体中文', subtitle_fingerprint=subtitle_fingerprint(_CUES))


def test_load_rejects_unknown_schema_or_version(tmp_path):
    path = _save(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))

    document["version"] = BRIEF_VERSION + 1
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(BriefFormatError):
        load_translation_brief(path, source_language='English', target_language='简体中文', subtitle_fingerprint=subtitle_fingerprint(_CUES))

    document["version"] = BRIEF_VERSION
    document["schema"] = "some.other.schema"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(BriefFormatError):
        load_translation_brief(path, source_language='English', target_language='简体中文', subtitle_fingerprint=subtitle_fingerprint(_CUES))


def test_load_rejects_mismatched_identity(tmp_path):
    path = _save(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["subtitle_fingerprint"] = "sha256:" + "0" * 64
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(BriefFormatError):
        load_translation_brief(path, source_language='English', target_language='简体中文', subtitle_fingerprint=subtitle_fingerprint(_CUES))

    path = _save(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["target_language"] = "日本語"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(BriefFormatError):
        load_translation_brief(path, source_language='English', target_language='简体中文', subtitle_fingerprint=subtitle_fingerprint(_CUES))


def test_load_rejects_invalid_candidate_shapes(tmp_path):
    path = _save(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))

    document["candidates"][0]["occurrence_ids"] = [0, -1]
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(BriefFormatError):
        load_translation_brief(path, source_language='English', target_language='简体中文', subtitle_fingerprint=subtitle_fingerprint(_CUES))

    path = _save(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["candidates"][0]["source_term"] = "  "
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(BriefFormatError):
        load_translation_brief(path, source_language='English', target_language='简体中文', subtitle_fingerprint=subtitle_fingerprint(_CUES))


def test_save_is_atomic_and_replaces_previous_content(tmp_path):
    first = _save(tmp_path)
    first.write_text("stale", encoding="utf-8")

    second = save_translation_brief(
        first,
        source_language="English",
        target_language="简体中文",
        subtitle_fingerprint=subtitle_fingerprint(_CUES),
        brief=_brief(),
        candidates=(),
    )
    assert second == first
    brief, candidates = load_translation_brief(
        second,
        source_language="English",
        target_language="简体中文",
        subtitle_fingerprint=subtitle_fingerprint(_CUES),
    )
    assert candidates == ()
    assert brief == _brief()
    assert not list(tmp_path.glob("*.tmp"))
