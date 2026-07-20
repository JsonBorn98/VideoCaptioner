import pytest

from videocaptioner.core.translate.enhanced.models import SubtitleCue
from videocaptioner.core.translate.enhanced.token_planner import (
    TokenBudgetExceeded,
    estimate_cues_tokens,
    estimate_tokens,
    plan_analysis_windows,
    plan_translation_batches,
)


def _cues(count: int, text: str = "subtitle text") -> list[SubtitleCue]:
    return [SubtitleCue(index, f"{text} {index}") for index in range(1, count + 1)]


def test_estimator_is_more_conservative_for_cjk() -> None:
    assert estimate_tokens("中文测试") == 4
    assert estimate_tokens("test") == 2


def test_analysis_windows_align_to_cues_overlap_and_cover_all() -> None:
    cues = _cues(8, "some moderately long subtitle")
    two_cue_budget = estimate_cues_tokens(cues[:2]) + 5

    windows = plan_analysis_windows(
        cues,
        working_context_tokens=two_cue_budget,
        overlap_cues=1,
    )

    assert all(window.cues for window in windows)
    assert {cue_id for window in windows for cue_id in window.cue_ids} == set(range(1, 9))
    assert all(
        left.cue_ids[-1] == right.cue_ids[0]
        for left, right in zip(windows, windows[1:])
    )


def test_analysis_window_fails_when_single_cue_cannot_fit() -> None:
    with pytest.raises(TokenBudgetExceeded, match="cue 1"):
        plan_analysis_windows(
            [SubtitleCue(1, "x" * 100)],
            working_context_tokens=5,
        )


def test_translation_batches_cover_subjects_once_with_three_context_cues() -> None:
    cues = _cues(10)

    batches = plan_translation_batches(
        cues,
        batch_size=4,
        working_context_tokens=10_000,
    )

    assert [cue_id for batch in batches for cue_id in batch.subject_ids] == list(
        range(1, 11)
    )
    assert batches[0].context_before == ()
    assert tuple(cue.cue_id for cue in batches[0].context_after) == (5, 6, 7)
    assert tuple(cue.cue_id for cue in batches[1].context_before) == (2, 3, 4)
    assert len(batches[1].context_after) <= 3


def test_translation_planner_shrinks_subjects_before_boundary_context() -> None:
    cues = _cues(7, "long " * 10)
    # Budget enough for one subject with full surrounding context but not three.
    one_subject = plan_translation_batches(
        cues,
        batch_size=1,
        working_context_tokens=10_000,
    )[0]
    budget = one_subject.estimated_input_tokens

    batches = plan_translation_batches(
        cues,
        batch_size=3,
        working_context_tokens=budget,
    )

    assert len(batches[0].subjects) < 3
    assert len(batches[0].context_after) == 3


def test_translation_payload_budget_includes_output_reserve() -> None:
    with pytest.raises(TokenBudgetExceeded):
        plan_translation_batches(
            [SubtitleCue(1, "hello")],
            batch_size=1,
            working_context_tokens=100,
            output_reserve_tokens=100,
        )
