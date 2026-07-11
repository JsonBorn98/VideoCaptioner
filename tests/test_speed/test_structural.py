from __future__ import annotations

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.entities import SubtitleLayoutEnum
from videocaptioner.core.speed.models import CueSnapshot
from videocaptioner.core.speed.structural import (
    StructuralOperationKind,
    propose_merge_candidate,
    propose_migration_candidate,
    propose_split_candidate,
    propose_structural_candidate,
)
from videocaptioner.core.speed.timing_evidence import (
    TimingAnchor,
    TimingEvidenceWindow,
    TimingGranularity,
    TimingOperation,
    TimingProvenance,
    TimingQualityGrade,
)


def _data(*rows: tuple[str, int, int, str]) -> ASRData:
    return ASRData(
        [ASRDataSeg(text, start, end, translated) for text, start, end, translated in rows]
    )


def _snapshots(data: ASRData) -> tuple[CueSnapshot, ...]:
    return tuple(
        CueSnapshot.from_input(
            index=index,
            start_ms=segment.start_time,
            end_ms=segment.end_time,
            text=segment.text,
            translated_text=segment.translated_text,
        )
        for index, segment in enumerate(data.segments)
    )


def _evidence(
    cues: tuple[CueSnapshot, ...],
    *,
    grade: TimingQualityGrade = TimingQualityGrade.HIGH,
    operations: frozenset[TimingOperation] = frozenset(TimingOperation),
    anchors: tuple[tuple[int, str, int, int], ...] = (),
) -> TimingEvidenceWindow:
    timing_anchors = tuple(
        TimingAnchor.create(
            cue_id=cues[cue_index].cue_id,
            text=text,
            start_ms=start,
            end_ms=end,
            quality_grade=grade,
            ordinal=ordinal,
        )
        for ordinal, (cue_index, text, start, end) in enumerate(anchors)
    )
    return TimingEvidenceWindow.create(
        cue_ids=tuple(cue.cue_id for cue in cues),
        start_ms=min(cue.start_ms for cue in cues),
        end_ms=max(cue.end_ms for cue in cues),
        provenance=TimingProvenance.FORCED_ALIGNER,
        granularity=TimingGranularity.WORD,
        coverage=1.0,
        quality_grade=grade,
        allowed_operations=operations,
        anchors=timing_anchors,
    )


def _texts(data: ASRData) -> tuple[tuple[str, str, int, int], ...]:
    return tuple(
        (segment.text, segment.translated_text, segment.start_time, segment.end_time)
        for segment in data.segments
    )


def test_operation_matrix_requires_both_medium_quality_and_explicit_permission() -> None:
    data = _data(("a", 0, 300, "甲"), ("b", 350, 1200, "乙"))
    snapshots = _snapshots(data)
    anchors = ((0, "a", 0, 250), (1, "b", 350, 1100))

    low = propose_merge_candidate(
        data,
        snapshots,
        _evidence(
            snapshots,
            grade=TimingQualityGrade.LOW,
            operations=frozenset({TimingOperation.MERGE_CUES}),
            anchors=anchors,
        ),
    )
    missing_permission = propose_merge_candidate(
        data,
        snapshots,
        _evidence(
            snapshots,
            grade=TimingQualityGrade.HIGH,
            operations=frozenset({TimingOperation.SPLIT_AT_WORD}),
            anchors=anchors,
        ),
    )
    medium = propose_merge_candidate(
        data,
        snapshots,
        _evidence(
            snapshots,
            grade=TimingQualityGrade.MEDIUM,
            operations=frozenset({TimingOperation.MERGE_CUES}),
            anchors=anchors,
        ),
    )

    assert not low.changed
    assert not missing_permission.changed
    assert medium.changed
    assert medium.operations[0].kind is StructuralOperationKind.MERGE


def test_low_quality_local_anchors_cannot_drive_high_quality_window_operations() -> None:
    data = _data(("a", 0, 300, "甲"), ("b", 350, 1200, "乙"))
    snapshots = _snapshots(data)
    timing_anchors = tuple(
        TimingAnchor.create(
            cue_id=snapshots[index].cue_id,
            text=text,
            start_ms=start,
            end_ms=end,
            quality_grade=TimingQualityGrade.LOW,
            ordinal=index,
        )
        for index, (text, start, end) in enumerate((("a", 0, 250), ("b", 350, 1100)))
    )
    evidence = TimingEvidenceWindow.create(
        cue_ids=tuple(cue.cue_id for cue in snapshots),
        start_ms=0,
        end_ms=1200,
        provenance=TimingProvenance.FORCED_ALIGNER,
        granularity=TimingGranularity.WORD,
        coverage=1.0,
        quality_grade=TimingQualityGrade.HIGH,
        allowed_operations=frozenset({TimingOperation.MERGE_CUES}),
        anchors=timing_anchors,
    )

    assert not propose_merge_candidate(data, snapshots, evidence).changed


def test_bilingual_merge_preserves_order_and_records_many_to_one_lineage() -> None:
    data = _data(("hello", 0, 400, "你好"), ("world", 450, 1500, "世界"))
    snapshots = _snapshots(data)
    before_data = _texts(data)
    candidate = propose_merge_candidate(
        data,
        snapshots,
        _evidence(
            snapshots,
            anchors=((0, "hello", 0, 350), (1, "world", 450, 1400)),
        ),
    )

    assert candidate.changed
    assert _texts(data) == before_data
    assert len(candidate.after) == 1
    merged = candidate.after[0]
    assert (merged.text, merged.translated_text) == ("hello world", "你好世界")
    assert merged.lineage is not None
    assert merged.lineage.parent_ids == tuple(cue.cue_id for cue in snapshots)
    operation = candidate.operations[0]
    assert operation.before_cue_ids == tuple(cue.cue_id for cue in snapshots)
    assert operation.after_cue_ids == (merged.cue_id,)
    assert operation.text_side == "both"
    assert operation.after_lineage == (merged.lineage,)
    assert _texts(candidate.materialize(accept=False)) == before_data
    assert len(candidate.materialize(accept=True).segments) == 1


def test_merge_inserts_script_appropriate_spacing_after_punctuation() -> None:
    data = _data(("hello,", 0, 300, "你好，"), ("world", 350, 1200, "世界"))
    snapshots = _snapshots(data)
    candidate = propose_merge_candidate(
        data,
        snapshots,
        _evidence(
            snapshots,
            anchors=((0, "hello", 0, 250), (1, "world", 350, 1100)),
        ),
    )

    assert candidate.after[0].text == "hello, world"
    assert candidate.after[0].translated_text == "你好，世界"


def test_long_bilingual_cue_splits_at_word_anchor_and_preserves_side_order() -> None:
    data = _data(
        ("one two three four", 0, 8000, "一二，三四"),
    )
    snapshots = _snapshots(data)
    candidate = propose_split_candidate(
        data,
        snapshots,
        _evidence(
            snapshots,
            anchors=(
                (0, "one", 200, 1500),
                (0, "two", 1600, 3500),
                (0, "three", 4500, 6200),
                (0, "four", 6300, 7800),
            ),
        ),
        max_duration_ms=6000,
    )

    assert candidate.changed
    assert [(cue.text, cue.translated_text) for cue in candidate.after] == [
        ("one two", "一二，"),
        ("three four", "三四"),
    ]
    assert candidate.after[0].end_ms == candidate.after[1].start_ms == 4000
    assert all(
        cue.lineage is not None and cue.lineage.parent_ids == (snapshots[0].cue_id,)
        for cue in candidate.after
    )
    assert [cue.lineage.operation for cue in candidate.after if cue.lineage] == ["split", "split"]


def test_pause_only_split_requires_a_reliable_pause_but_word_split_does_not() -> None:
    data = _data(
        ("one two", 0, 7000, "一二"),
    )
    snapshots = _snapshots(data)
    anchors = ((0, "one", 200, 3500), (0, "two", 3550, 6800))
    pause_only = propose_split_candidate(
        data,
        snapshots,
        _evidence(
            snapshots,
            grade=TimingQualityGrade.MEDIUM,
            operations=frozenset({TimingOperation.SPLIT_AT_PAUSE}),
            anchors=anchors,
        ),
    )
    word = propose_split_candidate(
        data,
        snapshots,
        _evidence(
            snapshots,
            grade=TimingQualityGrade.MEDIUM,
            operations=frozenset({TimingOperation.SPLIT_AT_WORD}),
            anchors=anchors,
        ),
    )

    assert not pause_only.changed
    assert word.changed


def test_primary_translation_migration_is_ordered_and_reference_text_is_unchanged() -> None:
    data = _data(
        ("source one", 0, 1000, "这是很长很长的第一部分，后半部分"),
        ("source two", 1050, 4050, "结尾"),
    )
    snapshots = _snapshots(data)
    candidate = propose_migration_candidate(
        data,
        snapshots,
        _evidence(
            snapshots,
            anchors=((0, "source one", 0, 950), (1, "source two", 1050, 4000)),
        ),
        layout=SubtitleLayoutEnum.TRANSLATE_ON_TOP,
        primary_side="translate",
    )

    assert candidate.changed
    assert [cue.text for cue in candidate.after] == ["source one", "source two"]
    assert [cue.translated_text for cue in candidate.after] == [
        "这是很长很长的第一部分，",
        "后半部分结尾",
    ]
    assert candidate.operations[0].text_side == "translate"
    parent_ids = tuple(cue.cue_id for cue in snapshots)
    assert all(cue.lineage and cue.lineage.parent_ids == parent_ids for cue in candidate.after)


def test_protection_reset_and_low_confidence_prevent_cross_cue_text_changes() -> None:
    data = _data(
        ("first", 0, 500, "非常非常长的前半句，后半句"),
        ("second", 2000, 4000, "短句"),
    )
    snapshots = _snapshots(data)
    evidence = _evidence(
        snapshots,
        anchors=((0, "first", 0, 450), (1, "second", 2000, 3900)),
    )

    protected = propose_structural_candidate(data, snapshots, evidence, protected_indices=(0,))
    reset = propose_migration_candidate(
        data, snapshots, evidence, rhythm_reset_ms=800, max_pause_ms=2500
    )
    low = propose_migration_candidate(
        data,
        snapshots,
        _evidence(
            snapshots,
            grade=TimingQualityGrade.LOW,
            operations=frozenset({TimingOperation.MIGRATE_TEXT}),
            anchors=((0, "first", 0, 450), (1, "second", 2000, 3900)),
        ),
        rhythm_reset_ms=5000,
        max_pause_ms=2500,
    )

    assert not protected.changed
    assert not reset.changed
    assert not low.changed
    assert protected.after == protected.before == snapshots


def test_candidate_and_lineage_are_deterministic() -> None:
    data = _data(("alpha", 0, 300, "甲"), ("beta", 350, 1300, "乙"))
    snapshots = _snapshots(data)
    evidence = _evidence(
        snapshots,
        anchors=((0, "alpha", 0, 250), (1, "beta", 350, 1200)),
    )

    first = propose_structural_candidate(data, snapshots, evidence)
    second = propose_structural_candidate(data, snapshots, evidence)

    assert first == second
    assert first.operations[0].operation_id == second.operations[0].operation_id
    assert first.after[0].cue_id == second.after[0].cue_id
