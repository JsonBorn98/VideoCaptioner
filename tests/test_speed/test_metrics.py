import pytest

from videocaptioner.core.speed import (
    CueSpeedSample,
    calculate_adjacent_jump,
    calculate_hard_deficit,
    calculate_speed_metrics,
    calculate_speed_spread,
    get_speed_policy,
    weighted_percentile,
)


def sample(
    stable_id: str,
    order: int,
    load: float | None,
    *,
    duration: float = 1.0,
    required_hard: float | None = None,
    rhythm: str = "a",
) -> CueSpeedSample:
    return CueSpeedSample(
        stable_id=stable_id,
        order=order,
        duration_seconds=duration,
        required_hard_seconds=load
        if required_hard is None and load is not None
        else required_hard or 0,
        comfort_load=load,
        hard_load=load,
        rhythm_id=rhythm,
    )


def test_weighted_percentile_uses_first_cumulative_observation():
    observations = [(1.0, 1.0, "a"), (2.0, 8.0, "b"), (3.0, 1.0, "c")]

    assert weighted_percentile(observations, 0.1) == 1.0
    assert weighted_percentile(observations, 0.9) == 2.0
    assert weighted_percentile(observations, 0.91) == 3.0


def test_hard_deficit_is_missing_time_over_required_time():
    samples = [
        sample("a", 0, 2.0, duration=1.0, required_hard=2.0),
        sample("b", 1, 0.5, duration=1.0, required_hard=0.5),
    ]

    assert calculate_hard_deficit(samples) == pytest.approx(1 / 2.5)
    assert calculate_hard_deficit([sample("empty", 0, 0)]) == 0


def test_speed_spread_is_duration_weighted_and_requires_three_positive_samples():
    samples = [
        sample("slow", 0, 0.5, duration=1.0),
        sample("middle", 1, 1.0, duration=8.0),
        sample("fast", 2, 2.0, duration=1.0),
    ]

    assert calculate_speed_spread(samples) == 2.0
    assert calculate_speed_spread(samples[:2]) is None


def test_adjacent_jump_ignores_resets_zero_load_and_subthreshold_boundaries():
    policy = get_speed_policy()
    samples = [
        sample("a", 0, 0.2),
        sample("b", 1, 0.4),  # faster side is below the 0.6 threshold
        sample("c", 2, 0.8),
        sample("d", 3, 0.0),
        sample("e", 4, 0.8, rhythm="new"),
        sample("f", 5, 0.2, rhythm="new"),
        sample("g", 6, 0.8, rhythm="new"),
    ]

    result = calculate_adjacent_jump(samples, policy)

    assert result.compared_count == 3
    assert result.p90 == 4.0
    assert result.emergency_count == 2


def test_adjacent_jump_needs_two_effective_boundaries_for_p90():
    result = calculate_adjacent_jump([sample("a", 0, 0.5), sample("b", 1, 1.0)], get_speed_policy())

    assert result.compared_count == 1
    assert result.p90 is None


def test_complete_metrics_reports_invalid_and_hard_overspeed():
    samples = [
        sample("a", 0, 0.5),
        sample("b", 1, 1.5, required_hard=1.5),
        sample("invalid", 2, None, duration=0, required_hard=1.0),
    ]

    result = calculate_speed_metrics(samples, get_speed_policy())

    assert result.invalid_count == 1
    assert result.unresolved_hard_count == 1
    assert result.hard_deficit == pytest.approx(0.5 / 2.0)


def test_metric_order_is_stable_by_explicit_order_not_lexical_id():
    result = calculate_adjacent_jump(
        [sample("cue-10", 1, 1.0), sample("cue-2", 0, 0.5)], get_speed_policy()
    )

    assert result.compared_count == 1
    assert result.emergency_count == 0
