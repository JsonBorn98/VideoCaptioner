from pathlib import Path

import pytest

from videocaptioner.core.speed.models import (
    CueSnapshot,
    Lineage,
    canonical_json_bytes,
    canonical_sha256,
    file_content_sha256,
)


def test_canonical_hash_is_order_independent() -> None:
    left = {"b": [2, 1], "a": {"value": "字幕"}}
    right = {"a": {"value": "字幕"}, "b": [2, 1]}

    assert canonical_json_bytes(left) == canonical_json_bytes(right)
    assert canonical_sha256(left) == canonical_sha256(right)


def test_file_fingerprint_does_not_depend_on_path(tmp_path: Path) -> None:
    first = tmp_path / "first.bin"
    second = tmp_path / "nested" / "second.bin"
    second.parent.mkdir()
    first.write_bytes(b"same media content")
    second.write_bytes(b"same media content")

    assert file_content_sha256(first) == file_content_sha256(second)


def test_cue_stable_id_and_lineage_are_deterministic() -> None:
    first = CueSnapshot.from_input(index=3, start_ms=1000, end_ms=2000, text="hello")
    second = CueSnapshot.from_input(index=3, start_ms=1000, end_ms=2000, text="hello")

    assert first.cue_id == second.cue_id
    assert CueSnapshot.from_dict(first.to_dict()) == first

    derived = Lineage.derive(
        kind="cue",
        parent_ids=(first.cue_id,),
        operation="split",
        ordinal=0,
        payload={"text": "hel"},
    )
    repeated = Lineage.derive(
        kind="cue",
        parent_ids=(first.cue_id,),
        operation="split",
        ordinal=0,
        payload={"text": "hel"},
    )
    assert derived == repeated
    assert derived.generation == 1


def test_cue_rejects_mismatched_lineage() -> None:
    cue = CueSnapshot.from_input(index=0, start_ms=0, end_ms=1000, text="hello")

    with pytest.raises(ValueError, match="lineage"):
        CueSnapshot(
            cue_id=cue.cue_id,
            index=cue.index,
            start_ms=cue.start_ms,
            end_ms=cue.end_ms,
            text=cue.text,
            lineage=Lineage.input("cue:other"),
        )


def test_translation_does_not_invalidate_source_timing_identity() -> None:
    untranslated = CueSnapshot.from_input(index=0, start_ms=0, end_ms=1000, text="source")
    translated = CueSnapshot.from_input(
        index=0,
        start_ms=0,
        end_ms=1000,
        text="source",
        translated_text="译文",
    )
    assert untranslated.cue_id == translated.cue_id


def test_timing_adjustment_does_not_invalidate_source_identity() -> None:
    original = CueSnapshot.from_input(index=0, start_ms=0, end_ms=1000, text="source")
    adjusted = CueSnapshot.from_input(index=0, start_ms=100, end_ms=1250, text="source")
    assert original.cue_id == adjusted.cue_id
