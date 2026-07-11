import videocaptioner.core.speed.pipeline as pipeline_module
from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.entities import SubtitleLayoutEnum
from videocaptioner.core.speed.models import CueSnapshot
from videocaptioner.core.speed.pipeline import optimize_speed
from videocaptioner.core.speed.semantic import SemanticRewriteResponse
from videocaptioner.core.speed.timing_evidence import (
    TimingAnchor,
    TimingEvidenceWindow,
    TimingGranularity,
    TimingOperation,
    TimingProvenance,
    TimingQualityGrade,
)
from videocaptioner.core.speed.validation import (
    ReviewDecision,
    SemanticReviewResponse,
    SemanticValidationResult,
    ValidationStatus,
)


def _data():
    return ASRData(
        [
            ASRDataSeg("source one", 0, 2000, "短句"),
            ASRDataSeg(
                "source two",
                2400,
                3000,
                "这是一句明显过快并且需要更多显示时间的中文字幕",
            ),
        ]
    )


def test_analyze_and_apply_share_the_same_candidate_snapshot():
    original = _data()
    analyzed_data, analysis = optimize_speed(
        original, mode="analyze", layout=SubtitleLayoutEnum.ONLY_TRANSLATE
    )
    applied_data, applied = optimize_speed(
        original, mode="apply", layout=SubtitleLayoutEnum.ONLY_TRANSLATE
    )
    assert analyzed_data.segments[1].start_time == 2400
    assert analysis.after == applied.after
    assert analysis.changes == applied.changes
    assert applied_data.segments[1].start_time < 2400


def test_original_side_can_be_selected_explicitly():
    data = ASRData([ASRDataSeg("A very long source sentence that is deliberately fast", 0, 300)])
    _, result = optimize_speed(data, mode="analyze", primary_side="original")
    assert result.before.unresolved_hard_count == 1


def test_applied_result_reaches_a_timing_fixed_point():
    first, first_result = optimize_speed(
        _data(), mode="apply", layout=SubtitleLayoutEnum.ONLY_TRANSLATE
    )
    second, second_result = optimize_speed(
        first, mode="apply", layout=SubtitleLayoutEnum.ONLY_TRANSLATE
    )
    assert first_result.changes
    assert not second_result.changes
    assert [(segment.start_time, segment.end_time) for segment in first.segments] == [
        (segment.start_time, segment.end_time) for segment in second.segments
    ]


def test_high_quality_evidence_uses_the_high_boundary_budget():
    data = ASRData(
        [
            ASRDataSeg("slow", 0, 2000, "慢"),
            ASRDataSeg("fast", 2000, 2500, "这是一句需要借时的中文字幕"),
        ]
    )
    _, baseline = optimize_speed(data, mode="analyze")
    assert not baseline.changes
    # Obtain stable IDs from an analysis report even when no low-confidence move is accepted.
    from videocaptioner.core.speed.models import CueSnapshot

    snapshots = [
        CueSnapshot.from_input(
            index=index,
            start_ms=segment.start_time,
            end_ms=segment.end_time,
            text=segment.text,
            translated_text=segment.translated_text,
        )
        for index, segment in enumerate(data.segments)
    ]
    evidence = TimingEvidenceWindow.create(
        cue_ids=tuple(snapshot.cue_id for snapshot in snapshots),
        start_ms=0,
        end_ms=2500,
        provenance=TimingProvenance.FORCED_ALIGNER,
        granularity=TimingGranularity.WORD,
        coverage=1.0,
        quality_grade=TimingQualityGrade.HIGH,
        allowed_operations=frozenset(TimingOperation),
        anchors=(
            TimingAnchor.create(
                cue_id=snapshots[0].cue_id,
                text="slow",
                start_ms=0,
                end_ms=1000,
                quality_grade=TimingQualityGrade.HIGH,
                ordinal=0,
            ),
            TimingAnchor.create(
                cue_id=snapshots[1].cue_id,
                text="fast",
                start_ms=1200,
                end_ms=2400,
                quality_grade=TimingQualityGrade.HIGH,
                ordinal=1,
            ),
        ),
    )
    optimized, result = optimize_speed(data, mode="apply", timing_windows=(evidence,))
    assert result.changes
    assert optimized.segments[1].start_time < 2000
    assert result.structural_operations


def test_high_quality_boundary_does_not_cross_aligned_speech():
    data = ASRData(
        [
            ASRDataSeg("slow", 0, 2000, "慢"),
            ASRDataSeg("fast", 2000, 2500, "这是一句需要借时的中文字幕"),
        ]
    )
    snapshots = [
        CueSnapshot.from_input(
            index=index,
            start_ms=segment.start_time,
            end_ms=segment.end_time,
            text=segment.text,
            translated_text=segment.translated_text,
        )
        for index, segment in enumerate(data.segments)
    ]
    evidence = TimingEvidenceWindow.create(
        cue_ids=tuple(snapshot.cue_id for snapshot in snapshots),
        start_ms=0,
        end_ms=2500,
        provenance=TimingProvenance.FORCED_ALIGNER,
        granularity=TimingGranularity.WORD,
        coverage=1.0,
        quality_grade=TimingQualityGrade.HIGH,
        allowed_operations=frozenset({TimingOperation.MOVE_SHARED_BOUNDARY}),
        anchors=(
            TimingAnchor.create(
                cue_id=snapshots[0].cue_id,
                text="slow",
                start_ms=0,
                end_ms=1900,
                quality_grade=TimingQualityGrade.HIGH,
                ordinal=0,
            ),
            TimingAnchor.create(
                cue_id=snapshots[1].cue_id,
                text="fast",
                start_ms=1950,
                end_ms=2450,
                quality_grade=TimingQualityGrade.HIGH,
                ordinal=1,
            ),
        ),
    )
    optimized, _ = optimize_speed(data, mode="apply", timing_windows=(evidence,))
    assert optimized.segments[0].end_time >= 1900
    assert optimized.segments[0].end_time <= 1950


def test_reference_side_can_be_audited_without_being_rewritten():
    data = _data()
    optimized, result = optimize_speed(
        data,
        mode="apply",
        layout=SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        reference_audit=True,
    )
    assert result.reference_before is not None
    assert result.reference_after is not None
    assert [segment.text for segment in optimized.segments] == [
        segment.text for segment in data.segments
    ]


def test_both_sides_optimizes_reference_only_when_primary_does_not_worsen():
    data = ASRData(
        [
            ASRDataSeg(
                "This source sentence is deliberately much too long for half a second.",
                0,
                500,
                "好",
            ),
            ASRDataSeg("End.", 1000, 2000, "结束"),
        ]
    )

    optimized, result = optimize_speed(
        data,
        mode="apply",
        layout=SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        primary_side="translate",
        optimize_both_sides=True,
    )

    assert optimized.segments[0].end_time > 500
    assert result.reference_before is not None
    assert result.reference_after is not None
    assert result.reference_after.hard_deficit <= result.reference_before.hard_deficit
    assert [segment.translated_text for segment in optimized.segments] == ["好", "结束"]


def test_semantic_repair_is_committed_only_after_semantic_and_metric_acceptance():
    data = ASRData(
        [
            ASRDataSeg(
                "The success rate is 98 percent and performance remains very stable.",
                0,
                1000,
                "成功率是98%，整体表现非常非常稳定。",
            )
        ]
    )

    def rewrite(request):
        return SemanticRewriteResponse(
            request.window_id,
            ((request.target_cue_ids[0], "成功率98%，表现稳定。"),),
        )

    def review(request):
        return SemanticReviewResponse(request.window_id, ReviewDecision.ACCEPT)

    optimized, result = optimize_speed(
        data,
        mode="apply",
        semantic_repair=True,
        semantic_model="fake",
        semantic_rewriter=rewrite,
        semantic_reviewer=review,
        semantic_cache={},
    )

    assert optimized.segments[0].translated_text == "成功率98%，表现稳定。"
    assert result.semantic_records[0].status is ValidationStatus.ACCEPTED
    assert result.after.hard_deficit < result.before.hard_deficit


def test_semantic_repair_rolls_back_a_candidate_that_does_not_improve_speed():
    data = ASRData(
        [
            ASRDataSeg("one", 0, 1000, "这是正常速度字幕"),
            ASRDataSeg("two", 1000, 2000, "这是明显过快而且必须立即进行处理的字幕内容"),
            ASRDataSeg("three", 2000, 3000, "这也是正常速度字幕"),
        ]
    )

    def rewrite(request):
        return SemanticRewriteResponse(
            request.window_id,
            ((request.target_cue_ids[0], "快"),),
        )

    def review(request):
        return SemanticReviewResponse(request.window_id, ReviewDecision.ACCEPT)

    optimized, result = optimize_speed(
        data,
        mode="apply",
        semantic_repair=True,
        semantic_model="fake",
        semantic_rewriter=rewrite,
        semantic_reviewer=review,
        semantic_cache={},
    )

    assert [segment.translated_text for segment in optimized.segments] == [
        segment.translated_text for segment in data.segments
    ]
    assert result.semantic_records[0].status is ValidationStatus.ROLLED_BACK


def _merge_candidate_case():
    data = ASRData(
        [
            ASRDataSeg("slow", 0, 2000, "慢"),
            ASRDataSeg("fast", 2000, 2500, "这是一句需要借时的中文字幕"),
        ]
    )
    snapshots = [
        CueSnapshot.from_input(
            index=index,
            start_ms=segment.start_time,
            end_ms=segment.end_time,
            text=segment.text,
            translated_text=segment.translated_text,
        )
        for index, segment in enumerate(data.segments)
    ]
    evidence = TimingEvidenceWindow.create(
        cue_ids=tuple(snapshot.cue_id for snapshot in snapshots),
        start_ms=0,
        end_ms=2500,
        provenance=TimingProvenance.FORCED_ALIGNER,
        granularity=TimingGranularity.WORD,
        coverage=1.0,
        quality_grade=TimingQualityGrade.HIGH,
        allowed_operations=frozenset(TimingOperation),
        anchors=(
            TimingAnchor.create(
                cue_id=snapshots[0].cue_id,
                text="slow",
                start_ms=0,
                end_ms=1900,
                quality_grade=TimingQualityGrade.HIGH,
                ordinal=0,
            ),
            TimingAnchor.create(
                cue_id=snapshots[1].cue_id,
                text="fast",
                start_ms=1950,
                end_ms=2450,
                quality_grade=TimingQualityGrade.HIGH,
                ordinal=1,
            ),
        ),
    )
    return data, evidence


def test_structural_candidate_rolls_back_when_semantic_validation_rejects(monkeypatch):
    data, evidence = _merge_candidate_case()
    monkeypatch.setattr(
        pipeline_module,
        "validate_semantic_window",
        lambda window: SemanticValidationResult(window.window_id, ValidationStatus.ROLLED_BACK),
    )

    optimized, result = optimize_speed(data, mode="apply", timing_windows=(evidence,))

    assert len(optimized.segments) == 2
    assert [segment.translated_text for segment in optimized.segments] == [
        segment.translated_text for segment in data.segments
    ]
    assert result.structural_operations == ()


def test_structural_candidate_rolls_back_when_m3_rejects(monkeypatch):
    data, evidence = _merge_candidate_case()
    monkeypatch.setattr(pipeline_module, "accepts_candidate", lambda *_args: False)

    optimized, result = optimize_speed(data, mode="apply", timing_windows=(evidence,))

    assert len(optimized.segments) == 2
    assert result.structural_operations == ()
