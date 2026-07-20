import json
from dataclasses import replace

import pytest

from videocaptioner.core.translate.enhanced.glossary import (
    GlossaryFormatError,
    classify_glossary_import,
    glossary_from_dict,
    load_glossary,
    save_glossary,
    select_relevant_entries,
    subtitle_fingerprint,
)
from videocaptioner.core.translate.enhanced.models import (
    AuthoritativeGlossary,
    GlossaryEntry,
    GlossaryImportMode,
    GlossarySelectionSource,
    SubtitleCue,
)


def _glossary(cues: list[SubtitleCue]) -> AuthoritativeGlossary:
    return AuthoritativeGlossary(
        source_language="English",
        target_language="简体中文",
        subtitle_fingerprint=subtitle_fingerprint(cues),
        entries=(
            GlossaryEntry(
                entry_id="mercury-planet",
                source_term="Mercury",
                sense="planet",
                translation="水星",
                aliases=("the planet Mercury",),
                occurrence_ids=(2,),
                selection_source=GlossarySelectionSource.REVIEW_MODEL_ACCEPTED,
            ),
        ),
    )


def test_fingerprint_normalizes_representation_but_keeps_numbering() -> None:
    canonical = [SubtitleCue(1, "ＡＰＩ   server"), SubtitleCue(2, "line\nbreak")]
    equivalent = [SubtitleCue(1, "API server"), SubtitleCue(2, "line break")]

    assert subtitle_fingerprint(canonical) == subtitle_fingerprint(equivalent)
    assert subtitle_fingerprint(canonical) != subtitle_fingerprint(
        [SubtitleCue(2, "API server"), SubtitleCue(3, "line break")]
    )


def test_glossary_round_trip_and_exact_import(tmp_path) -> None:
    cues = [SubtitleCue(1, "Look up"), SubtitleCue(2, "Mercury is visible")]
    glossary = _glossary(cues)
    path = tmp_path / "【项目术语表】demo.vcglossary.json"

    assert save_glossary(path, glossary) == path
    loaded = load_glossary(path)
    result = classify_glossary_import(
        loaded,
        source_language="english",
        target_language="简体中文",
        cues=cues,
    )

    assert loaded == glossary
    assert result.mode is GlossaryImportMode.EXACT
    assert result.glossary == glossary
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1
    assert not [item for item in tmp_path.iterdir() if item != path]


def test_changed_fingerprint_becomes_seed_and_drops_stale_occurrences() -> None:
    old_cues = [SubtitleCue(1, "Mercury")]
    new_cues = [SubtitleCue(1, "Mercury appears later")]

    result = classify_glossary_import(
        _glossary(old_cues),
        source_language="English",
        target_language="简体中文",
        cues=new_cues,
    )

    assert result.mode is GlossaryImportMode.SEED
    assert result.glossary is not None
    assert result.glossary.subtitle_fingerprint == _glossary(old_cues).subtitle_fingerprint
    assert result.glossary.entries[0].occurrence_ids == ()
    assert (
        result.glossary.entries[0].selection_source
        is GlossarySelectionSource.IMPORTED
    )


@pytest.mark.parametrize("change", ["language", "schema"])
def test_incompatible_imports_are_not_exposed_as_seeds(change: str) -> None:
    cues = [SubtitleCue(1, "Mercury")]
    glossary = _glossary(cues)
    if change == "schema":
        glossary = replace(glossary, schema="unknown")

    result = classify_glossary_import(
        glossary,
        source_language="French" if change == "language" else "English",
        target_language="简体中文",
        cues=cues,
    )

    assert result.mode is GlossaryImportMode.INCOMPATIBLE
    assert result.glossary is None


def test_block_relevance_uses_occurrences_then_alias_scan_without_substrings() -> None:
    fingerprint = "sha256:test"
    by_occurrence = GlossaryEntry(
        "one", "unseen", "sense", "甲", occurrence_ids=(7,)
    )
    by_alias = GlossaryEntry("two", "OpenAI", "company", "开放人工智能", aliases=("GPT",))
    false_substring = GlossaryEntry("three", "AI", "concept", "人工智能")
    glossary = AuthoritativeGlossary(
        "English", "简体中文", fingerprint, (by_occurrence, by_alias, false_substring)
    )

    relevant = select_relevant_entries(
        glossary,
        [SubtitleCue(7, "We discussed it"), SubtitleCue(8, "GPT-5 and said")],
    )

    assert relevant == (by_occurrence, by_alias)


def test_malformed_glossary_is_rejected() -> None:
    with pytest.raises(GlossaryFormatError, match="entries"):
        glossary_from_dict({"schema": "x", "version": 1, "entries": {}})


def test_non_ascii_terms_use_normalized_substring_matching() -> None:
    entry = GlossaryEntry("cjk", "水星", "planet", "Mercury")
    glossary = AuthoritativeGlossary("Chinese", "English", "sha256:x", (entry,))

    assert select_relevant_entries(glossary, [SubtitleCue(1, "我们看看水星吧")]) == (
        entry,
    )
