import pytest

from videocaptioner.core.speed.timing_evidence import (
    TimingAnchor,
    TimingEvidenceBundle,
    TimingEvidenceWindow,
    TimingGranularity,
    TimingOperation,
    TimingProvenance,
    TimingQualityGrade,
)


def _window() -> TimingEvidenceWindow:
    anchor = TimingAnchor.create(
        cue_id="cue:1",
        text="hello",
        start_ms=100,
        end_ms=500,
        quality_grade=TimingQualityGrade.MEDIUM,
        ordinal=0,
        confidence=0.8,
    )
    return TimingEvidenceWindow.create(
        cue_ids=("cue:1",),
        start_ms=0,
        end_ms=1000,
        provenance=TimingProvenance.FORCED_ALIGNER,
        granularity=TimingGranularity.WORD,
        coverage=0.9,
        quality_grade=TimingQualityGrade.HIGH,
        allowed_operations=frozenset({TimingOperation.SPLIT_AT_WORD}),
        anchors=(anchor,),
        quality_metrics={"monotonic": True, "unmatched_ratio": 0.1},
    )


def test_provenance_quality_granularity_and_operations_are_independent() -> None:
    window = TimingEvidenceWindow.create(
        cue_ids=("cue:1",),
        start_ms=0,
        end_ms=1000,
        provenance=TimingProvenance.FORCED_ALIGNER,
        granularity=TimingGranularity.WORD,
        coverage=0.2,
        quality_grade=TimingQualityGrade.LOW,
        allowed_operations=frozenset({TimingOperation.USE_SAFE_GAP}),
    )

    assert window.provenance is TimingProvenance.FORCED_ALIGNER
    assert window.quality_grade is TimingQualityGrade.LOW
    assert window.allowed_operations == frozenset({TimingOperation.USE_SAFE_GAP})


def test_anchor_may_have_lower_quality_than_window() -> None:
    window = _window()

    assert window.quality_grade is TimingQualityGrade.HIGH
    assert window.anchors[0].quality_grade is TimingQualityGrade.MEDIUM


def test_bundle_round_trip_preserves_stable_identity() -> None:
    bundle = TimingEvidenceBundle(
        subtitle_fingerprint="sub-hash",
        media_fingerprint="media-hash",
        audio_track="0:a:0",
        source_language="en",
        model_name="qwen-forced-aligner",
        model_version="1",
        config_fingerprint="config-hash",
        windows=(_window(),),
    )

    restored = TimingEvidenceBundle.from_dict(bundle.to_dict())

    assert restored == bundle
    assert restored.bundle_id == bundle.bundle_id


def test_window_rejects_anchor_outside_bounds() -> None:
    anchor = TimingAnchor.create(
        cue_id="cue:1",
        text="late",
        start_ms=900,
        end_ms=1100,
        quality_grade=TimingQualityGrade.LOW,
        ordinal=0,
    )

    with pytest.raises(ValueError, match="within"):
        TimingEvidenceWindow.create(
            cue_ids=("cue:1",),
            start_ms=0,
            end_ms=1000,
            provenance=TimingProvenance.ESTIMATED,
            granularity=TimingGranularity.CUE,
            coverage=1.0,
            quality_grade=TimingQualityGrade.LOW,
            allowed_operations=frozenset(),
            anchors=(anchor,),
        )
