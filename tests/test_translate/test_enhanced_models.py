import pytest

from videocaptioner.core.translate.enhanced.models import (
    AuthoritativeGlossary,
    GlossaryEntry,
    SubtitleCue,
    TranslationBatch,
    TranslationContextBrief,
)


def test_context_brief_formats_only_present_sections() -> None:
    brief = TranslationContextBrief(
        outline="A technical interview",
        themes=("distributed systems",),
        translation_notes=("Keep API names in English",),
    )

    text = brief.as_prompt_text()

    assert "Outline:\nA technical interview" in text
    assert "- distributed systems" in text
    assert "Background:" not in text


def test_translation_batch_rejects_context_subject_overlap() -> None:
    cue = SubtitleCue(1, "hello")

    with pytest.raises(ValueError, match="disjoint"):
        TranslationBatch(subjects=(cue,), context_before=(cue,))


def test_authoritative_glossary_rejects_duplicate_entry_ids() -> None:
    entry = GlossaryEntry("term-1", "Mercury", "planet", "水星")

    with pytest.raises(ValueError, match="unique"):
        AuthoritativeGlossary(
            source_language="English",
            target_language="Chinese",
            subtitle_fingerprint="sha256:abc",
            entries=(entry, entry),
        )
