from videocaptioner.core.speed.deterministic import (
    TimingCue,
    calculate_local_targets,
    candidate_is_non_worsening,
    measure,
    optimize_subtitle_timing,
)
from videocaptioner.core.speed.policy import get_speed_policy


def test_safe_gap_is_used_for_hard_overspeed_cue():
    policy = get_speed_policy()
    cues = [
        TimingCue("slow", 0, 0, 2000, "短句"),
        TimingCue("fast", 1, 2400, 3000, "这是一句明显过快并且需要更多显示时间的中文字幕"),
    ]
    optimized, changes = optimize_subtitle_timing(cues, policy)
    assert optimized[1].start_ms < 2400
    assert any(change.reason == "safe_gap" for change in changes)
    assert measure(optimized, policy).hard_deficit < measure(cues, policy).hard_deficit


def test_boundary_budget_is_relative_to_original_snapshot():
    policy = get_speed_policy()
    cues = [
        TimingCue("left", 0, 0, 2000, "很慢"),
        TimingCue("fast", 1, 2000, 2400, "这是一句非常长而且肯定会严重超速的中文字幕内容"),
        TimingCue("right", 2, 2400, 5000, "也很慢"),
    ]
    optimized, _ = optimize_subtitle_timing(cues, policy, boundary_budget_ms=250)
    assert abs(optimized[1].start_ms - 2000) <= 250
    assert abs(optimized[1].end_ms - 2400) <= 250


def test_protected_cue_is_not_changed():
    policy = get_speed_policy()
    cue = TimingCue("protected", 0, 0, 400, "这是一句严重超速但被保护的字幕", protected=True)
    optimized, changes = optimize_subtitle_timing([cue], policy)
    assert optimized == [cue]
    assert changes == []


def test_local_target_uses_neighbourhood_median_and_materiality_floor():
    policy = get_speed_policy()
    cues = [
        TimingCue("slow-a", 0, 0, 3000, "短句"),
        TimingCue("slow-b", 1, 3200, 6200, "短句"),
        TimingCue("peak", 2, 6400, 7600, "这是一条没有超过硬限但明显更快的中文字幕"),
        TimingCue("slow-c", 3, 7800, 10800, "短句"),
        TimingCue("slow-d", 4, 11000, 14000, "短句"),
        TimingCue("slow-e", 5, 14200, 17200, "短句"),
        TimingCue("slow-f", 6, 17400, 20400, "短句"),
    ]
    targets = calculate_local_targets(cues, policy)
    assert targets["peak"] == policy.effective_jump_load
    before = measure(cues, policy)
    optimized, changes = optimize_subtitle_timing(cues, policy)
    after = measure(optimized, policy)
    assert changes
    assert after.hard_deficit <= before.hard_deficit
    assert optimized[2].duration_ms > cues[2].duration_ms


def test_smooth_profile_balances_contiguous_slow_and_fast_cues_without_gap():
    policy = get_speed_policy("smooth")
    cues = [
        TimingCue("slow", 0, 0, 3000, "这是一条字幕", boundary_budget_ms=1000),
        TimingCue(
            "faster",
            1,
            3000,
            5000,
            "这是一条较长字幕",
            boundary_budget_ms=1000,
        ),
    ]
    optimized, changes = optimize_subtitle_timing(cues, policy)
    assert any(change.reason == "bidirectional_balance" for change in changes)
    assert optimized[0].end_ms == optimized[1].start_ms
    assert optimized[0].duration_ms < cues[0].duration_ms


def test_long_sequence_transaction_keeps_m3_non_worsening():
    policy = get_speed_policy("balanced")
    cues = []
    for index in range(1201):
        start = index * 1250
        duration = 650 if index % 17 == 0 else 1050
        text = "这是需要平稳显示的字幕内容" if index % 17 == 0 else "正常字幕"
        cues.append(TimingCue(str(index), index, start, start + duration, text))

    before = measure(cues, policy)
    optimized, changes = optimize_subtitle_timing(cues, policy)
    after = measure(optimized, policy)

    assert changes
    assert candidate_is_non_worsening(before, after)
    assert after.unresolved_hard_count <= before.unresolved_hard_count
