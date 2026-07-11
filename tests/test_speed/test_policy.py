import pytest

from videocaptioner.core.speed import SpeedPolicy, SpeedPreset, get_speed_policy


@pytest.mark.parametrize(
    ("preset", "cjk", "latin", "jump", "duration", "bidirectional"),
    [
        ("loose", (10, 12), (18, 22), (2.2, 3.5), (0.8, 7.0), False),
        ("balanced", (9, 11), (16, 20), (1.8, 3.0), (1.0, 6.0), False),
        ("smooth", (8, 10), (14, 18), (1.5, 2.5), (1.2, 5.5), True),
    ],
)
def test_builtin_policy_contract(preset, cjk, latin, jump, duration, bidirectional):
    policy = get_speed_policy(preset)

    assert policy.preset is SpeedPreset(preset)
    assert (policy.comfort_cps_cjk, policy.hard_cps_cjk) == cjk
    assert (policy.comfort_cps_latin, policy.hard_cps_latin) == latin
    assert (policy.adjacent_p90_target, policy.adjacent_emergency_limit) == jump
    assert (policy.min_duration_seconds, policy.max_duration_seconds) == duration
    assert policy.bidirectional_smoothing is bidirectional
    assert policy.technical_min_duration_seconds == 0.5
    assert policy.local_window_radius == 4
    assert policy.speech_density_adjustment_limit == 0.15
    assert (policy.rhythm_reset_ms, policy.hard_rhythm_reset_ms) == (800, 1500)
    assert (
        policy.low_confidence_boundary_shift_ms,
        policy.medium_confidence_boundary_shift_ms,
        policy.high_confidence_boundary_shift_ms,
    ) == (250, 500, 1000)


def test_policy_override_is_immutable_and_validated():
    balanced = get_speed_policy()
    custom = balanced.with_overrides(hard_cps_cjk=13.0)

    assert balanced.hard_cps_cjk == 11.0
    assert custom.hard_cps_cjk == 13.0
    with pytest.raises(ValueError, match="comfort CPS"):
        balanced.with_overrides(hard_cps_cjk=8.0)


def test_policy_rejects_invalid_duration_bounds():
    with pytest.raises(ValueError, match="minimum duration"):
        SpeedPolicy(min_duration_seconds=7.0, max_duration_seconds=6.0)

    with pytest.raises(ValueError, match="movement budgets"):
        SpeedPolicy(low_confidence_boundary_shift_ms=600)


def test_unknown_preset_lists_available_values():
    with pytest.raises(ValueError, match="loose, balanced, smooth"):
        get_speed_policy("cinematic")
