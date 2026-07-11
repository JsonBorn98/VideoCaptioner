"""Low-confidence deterministic timing optimization for subtitle-only inputs."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Sequence

from .metrics import (
    CueSpeedSample,
    SpeedMetrics,
    calculate_speed_metrics,
    weighted_percentile,
)
from .policy import SpeedPolicy
from .reading_load import calculate_reading_load

_EPSILON = 1e-9


@dataclass(frozen=True)
class TimingCue:
    cue_id: str
    order: int
    start_ms: int
    end_ms: int
    text: str
    rhythm_id: str = "default"
    protected: bool = False
    original_start_ms: int | None = None
    original_end_ms: int | None = None
    boundary_budget_ms: int | None = None
    speech_start_ms: int | None = None
    speech_end_ms: int | None = None

    def __post_init__(self) -> None:
        if self.original_start_ms is None:
            object.__setattr__(self, "original_start_ms", self.start_ms)
        if self.original_end_ms is None:
            object.__setattr__(self, "original_end_ms", self.end_ms)

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass(frozen=True)
class TimingChange:
    cue_id: str
    boundary: str
    before_ms: int
    after_ms: int
    reason: str

    @property
    def delta_ms(self) -> int:
        return self.after_ms - self.before_ms


def build_speed_samples(cues: Sequence[TimingCue], policy: SpeedPolicy) -> list[CueSpeedSample]:
    samples: list[CueSpeedSample] = []
    for cue in cues:
        load = calculate_reading_load(cue.text, cue.duration_ms / 1000, policy)
        samples.append(CueSpeedSample.from_reading_load(cue.cue_id, cue.order, load, cue.rhythm_id))
    return samples


def measure(cues: Sequence[TimingCue], policy: SpeedPolicy) -> SpeedMetrics:
    return calculate_speed_metrics(build_speed_samples(cues, policy), policy)


def _not_worse_optional(before: float | None, after: float | None) -> bool:
    if before is None:
        return after is None
    if after is None:
        return True
    return after <= before * 1.01 + _EPSILON


def _strictly_better_optional(before: float | None, after: float | None) -> bool:
    return before is not None and after is not None and after < before * 0.99 - _EPSILON


def candidate_is_non_worsening(before: SpeedMetrics, after: SpeedMetrics) -> bool:
    if after.invalid_count > before.invalid_count:
        return False
    if after.hard_deficit > before.hard_deficit + _EPSILON:
        return False
    if after.unresolved_hard_count > before.unresolved_hard_count:
        return False
    if after.adjacent_jump.emergency_count > before.adjacent_jump.emergency_count:
        return False
    if not _not_worse_optional(before.speed_spread, after.speed_spread):
        return False
    if not _not_worse_optional(before.adjacent_jump.p90, after.adjacent_jump.p90):
        return False
    return True


def accepts_candidate(before: SpeedMetrics, after: SpeedMetrics) -> bool:
    """Apply the single v1 Pareto acceptance predicate from the specification."""

    if not candidate_is_non_worsening(before, after):
        return False
    return (
        after.hard_deficit < before.hard_deficit - _EPSILON
        or after.adjacent_jump.emergency_count < before.adjacent_jump.emergency_count
        or _strictly_better_optional(before.speed_spread, after.speed_spread)
        or _strictly_better_optional(before.adjacent_jump.p90, after.adjacent_jump.p90)
    )


def _replace(cues: Sequence[TimingCue], index: int, cue: TimingCue) -> list[TimingCue]:
    candidate = list(cues)
    candidate[index] = cue
    return candidate


def _weighted_median(
    cues: Sequence[TimingCue], policy: SpeedPolicy, *, minimum_samples: int = 3
) -> float | None:
    observations = []
    for cue in cues:
        load = calculate_reading_load(cue.text, cue.duration_ms / 1000, policy)
        if (
            cue.protected
            or not load.valid
            or load.comfort_load is None
            or load.comfort_load <= 0
            or load.hard_overspeed
        ):
            continue
        observations.append((load.comfort_load, cue.duration_ms / 1000, cue.cue_id))
    if len(observations) < minimum_samples:
        return None
    return weighted_percentile(observations, 0.5)


def calculate_local_targets(cues: Sequence[TimingCue], policy: SpeedPolicy) -> dict[str, float]:
    """Calculate robust local target loads with deterministic fallback levels."""

    rhythm_groups: dict[str, list[TimingCue]] = {}
    for cue in cues:
        rhythm_groups.setdefault(cue.rhythm_id, []).append(cue)
    global_median = _weighted_median(cues, policy)
    rhythm_medians = {
        rhythm_id: _weighted_median(group, policy) for rhythm_id, group in rhythm_groups.items()
    }
    targets: dict[str, float] = {}
    for index, cue in enumerate(cues):
        start = max(0, index - policy.local_window_radius)
        end = min(len(cues), index + policy.local_window_radius + 1)
        neighbourhood = [
            candidate
            for candidate in cues[start:end]
            if candidate.rhythm_id == cue.rhythm_id and candidate.cue_id != cue.cue_id
        ]
        local = _weighted_median(neighbourhood, policy)
        rhythm = rhythm_medians[cue.rhythm_id]
        baseline = local if local is not None else rhythm
        if baseline is None:
            baseline = global_median if global_median is not None else 1.0
        targets[cue.cue_id] = max(policy.effective_jump_load, baseline)
    return targets


def _desired_extra_ms(cue: TimingCue, target_load: float, policy: SpeedPolicy) -> int:
    load = calculate_reading_load(cue.text, cue.duration_ms / 1000, policy)
    hard_extra = load.required_hard_seconds * 1000 - cue.duration_ms
    smooth_extra = load.required_comfort_seconds * 1000 / target_load - cue.duration_ms
    return max(0, round(max(hard_extra, smooth_extra)))


def _hard_extra_ms(cue: TimingCue, policy: SpeedPolicy) -> int:
    load = calculate_reading_load(cue.text, cue.duration_ms / 1000, policy)
    return max(0, round(load.required_hard_seconds * 1000 - cue.duration_ms))


def _bounded_borrow(needed: int, cue: TimingCue, budget: int, policy: SpeedPolicy) -> int:
    if needed <= budget:
        return needed
    hard_needed = _hard_extra_ms(cue, policy)
    return hard_needed if 0 < hard_needed <= budget else 0


def optimize_subtitle_timing(
    cues: Sequence[TimingCue],
    policy: SpeedPolicy,
    *,
    boundary_budget_ms: int | None = None,
    technical_min_duration_ms: int | None = None,
) -> tuple[list[TimingCue], list[TimingChange]]:
    """Greedily use safe gaps and shared boundaries without changing cue text."""

    if boundary_budget_ms is not None and boundary_budget_ms < 0:
        raise ValueError("boundary_budget_ms cannot be negative")
    minimum = technical_min_duration_ms or round(policy.technical_min_duration_seconds * 1000)
    current = sorted(cues, key=lambda cue: (cue.order, cue.cue_id))
    original = list(current)
    current_metrics = measure(current, policy)
    deferred_validation = len(current) > 1000
    local_targets = calculate_local_targets(current, policy)
    changes: list[TimingChange] = []

    def commit(
        candidate: list[TimingCue], change_factory: Callable[[], list[TimingChange]]
    ) -> None:
        nonlocal current, current_metrics
        if not deferred_validation:
            candidate_metrics = measure(candidate, policy)
            if not accepts_candidate(current_metrics, candidate_metrics):
                return
            current_metrics = candidate_metrics
        current = candidate
        changes.extend(change_factory())

    for index in range(len(current)):
        cue = current[index]
        target_load = local_targets[cue.cue_id]
        if (
            cue.protected
            or cue.duration_ms <= 0
            or _desired_extra_ms(cue, target_load, policy) <= 0
        ):
            continue

        needed = _desired_extra_ms(cue, target_load, policy)
        if index > 0:
            previous = current[index - 1]
            gap = cue.start_ms - previous.end_ms
            shift = min(needed, max(0, gap))
            if shift > 0 and not previous.protected and previous.rhythm_id == cue.rhythm_id:
                updated = replace(cue, start_ms=cue.start_ms - shift)
                candidate = _replace(current, index, updated)
                before = cue.start_ms
                commit(
                    candidate,
                    lambda: [
                        TimingChange(cue.cue_id, "start", before, updated.start_ms, "safe_gap")
                    ],
                )
                cue = current[index]
                needed = _desired_extra_ms(cue, target_load, policy)

        if needed > 0 and index + 1 < len(current):
            following = current[index + 1]
            gap = following.start_ms - cue.end_ms
            shift = min(needed, max(0, gap))
            if shift > 0 and not following.protected and following.rhythm_id == cue.rhythm_id:
                updated = replace(cue, end_ms=cue.end_ms + shift)
                candidate = _replace(current, index, updated)
                before = cue.end_ms
                commit(
                    candidate,
                    lambda: [TimingChange(cue.cue_id, "end", before, updated.end_ms, "safe_gap")],
                )
                cue = current[index]
                needed = _desired_extra_ms(cue, target_load, policy)

        # A shared-boundary move borrows time only from a valid, unprotected neighbour.
        if needed > 0 and index > 0:
            previous = current[index - 1]
            if previous.end_ms == cue.start_ms and not previous.protected:
                available = previous.duration_ms - minimum
                if previous.speech_end_ms is not None:
                    available = min(available, cue.start_ms - previous.speech_end_ms)
                budgets = [
                    cue.boundary_budget_ms
                    if cue.boundary_budget_ms is not None
                    else policy.low_confidence_boundary_shift_ms,
                    previous.boundary_budget_ms
                    if previous.boundary_budget_ms is not None
                    else policy.low_confidence_boundary_shift_ms,
                ]
                if boundary_budget_ms is not None:
                    budgets.append(boundary_budget_ms)
                boundary_budget = min(budgets)
                borrow = _bounded_borrow(needed, cue, boundary_budget, policy)
                anchor_shift = (
                    max(0, cue.start_ms - cue.speech_start_ms)
                    if cue.speech_start_ms is not None
                    else 0
                )
                requested = max(borrow, anchor_shift)
                if previous.speech_end_ms is not None or cue.speech_start_ms is not None:
                    shift = min(requested, max(0, available), boundary_budget)
                else:
                    shift = requested if requested <= available else 0
                if shift > 0 and previous.rhythm_id == cue.rhythm_id:
                    moved = cue.start_ms - shift
                    candidate = list(current)
                    candidate[index - 1] = replace(previous, end_ms=moved)
                    candidate[index] = replace(cue, start_ms=moved)
                    before = cue.start_ms
                    commit(
                        candidate,
                        lambda: [
                            TimingChange(previous.cue_id, "end", before, moved, "shared_boundary"),
                            TimingChange(cue.cue_id, "start", before, moved, "shared_boundary"),
                        ],
                    )
                    cue = current[index]
                    needed = _desired_extra_ms(cue, target_load, policy)

        if needed > 0 and index + 1 < len(current):
            following = current[index + 1]
            if cue.end_ms == following.start_ms and not following.protected:
                available = following.duration_ms - minimum
                if following.speech_start_ms is not None:
                    available = min(available, following.speech_start_ms - cue.end_ms)
                budgets = [
                    cue.boundary_budget_ms
                    if cue.boundary_budget_ms is not None
                    else policy.low_confidence_boundary_shift_ms,
                    following.boundary_budget_ms
                    if following.boundary_budget_ms is not None
                    else policy.low_confidence_boundary_shift_ms,
                ]
                if boundary_budget_ms is not None:
                    budgets.append(boundary_budget_ms)
                boundary_budget = min(budgets)
                borrow = _bounded_borrow(needed, cue, boundary_budget, policy)
                anchor_shift = (
                    max(0, cue.speech_end_ms - cue.end_ms) if cue.speech_end_ms is not None else 0
                )
                requested = max(borrow, anchor_shift)
                if cue.speech_end_ms is not None or following.speech_start_ms is not None:
                    shift = min(requested, max(0, available), boundary_budget)
                else:
                    shift = requested if requested <= available else 0
                if shift > 0 and following.rhythm_id == cue.rhythm_id:
                    moved = cue.end_ms + shift
                    candidate = list(current)
                    candidate[index] = replace(cue, end_ms=moved)
                    candidate[index + 1] = replace(following, start_ms=moved)
                    before = cue.end_ms
                    commit(
                        candidate,
                        lambda: [
                            TimingChange(cue.cue_id, "end", before, moved, "shared_boundary"),
                            TimingChange(
                                following.cue_id, "start", before, moved, "shared_boundary"
                            ),
                        ],
                    )

    if deferred_validation:
        final_metrics = measure(current, policy)
        if not accepts_candidate(current_metrics, final_metrics):
            current = original
            changes = []
        else:
            current_metrics = final_metrics

    if policy.bidirectional_smoothing:
        current, smoothing_changes = smooth_bidirectional_boundaries(
            current,
            policy,
            boundary_budget_ms=boundary_budget_ms,
            technical_min_duration_ms=minimum,
        )
        changes.extend(smoothing_changes)

    return current, changes


def smooth_bidirectional_boundaries(
    cues: Sequence[TimingCue],
    policy: SpeedPolicy,
    *,
    boundary_budget_ms: int | None = None,
    technical_min_duration_ms: int | None = None,
) -> tuple[list[TimingCue], list[TimingChange]]:
    """Move shared boundaries toward the unique two-cue reading-time balance."""

    minimum = technical_min_duration_ms or round(policy.technical_min_duration_seconds * 1000)
    current = list(cues)
    original = list(current)
    initial_metrics = measure(current, policy)
    deferred_validation = len(current) > 1000
    changes: list[TimingChange] = []
    for index in range(len(current) - 1):
        left = current[index]
        right = current[index + 1]
        if (
            left.protected
            or right.protected
            or left.rhythm_id != right.rhythm_id
            or left.end_ms != right.start_ms
        ):
            continue
        left_load = calculate_reading_load(left.text, left.duration_ms / 1000, policy)
        right_load = calculate_reading_load(right.text, right.duration_ms / 1000, policy)
        if (
            not left_load.valid
            or not right_load.valid
            or not left_load.required_comfort_seconds
            or not right_load.required_comfort_seconds
        ):
            continue
        lower = min(left_load.comfort_load or 0, right_load.comfort_load or 0)
        higher = max(left_load.comfort_load or 0, right_load.comfort_load or 0)
        if lower <= 0 or higher / lower <= policy.adjacent_p90_target:
            continue

        total_duration = left.duration_ms + right.duration_ms
        required_total = left_load.required_comfort_seconds + right_load.required_comfort_seconds
        ideal_left_duration = round(
            total_duration * left_load.required_comfort_seconds / required_total
        )
        movement = ideal_left_duration - left.duration_ms
        if movement == 0:
            continue
        budget_values = [
            left.boundary_budget_ms
            if left.boundary_budget_ms is not None
            else policy.low_confidence_boundary_shift_ms,
            right.boundary_budget_ms
            if right.boundary_budget_ms is not None
            else policy.low_confidence_boundary_shift_ms,
        ]
        if boundary_budget_ms is not None:
            budget_values.append(boundary_budget_ms)
        budget = min(budget_values)
        if abs(movement) > budget and left.speech_end_ms is None and right.speech_start_ms is None:
            continue
        movement = max(-budget, min(budget, movement))
        new_boundary = left.end_ms + movement
        new_boundary = max(left.start_ms + minimum, new_boundary)
        new_boundary = min(right.end_ms - minimum, new_boundary)
        if left.speech_end_ms is not None:
            new_boundary = max(left.speech_end_ms, new_boundary)
        if right.speech_start_ms is not None:
            new_boundary = min(right.speech_start_ms, new_boundary)
        if new_boundary == left.end_ms:
            continue
        candidate = list(current)
        candidate[index] = replace(left, end_ms=new_boundary)
        candidate[index + 1] = replace(right, start_ms=new_boundary)
        before_metrics = initial_metrics if deferred_validation else measure(current, policy)
        after_metrics = None if deferred_validation else measure(candidate, policy)
        balanced_left = calculate_reading_load(
            candidate[index].text,
            candidate[index].duration_ms / 1000,
            policy,
        )
        balanced_right = calculate_reading_load(
            candidate[index + 1].text,
            candidate[index + 1].duration_ms / 1000,
            policy,
        )
        balanced_lower = min(
            balanced_left.comfort_load or 0,
            balanced_right.comfort_load or 0,
        )
        balanced_higher = max(
            balanced_left.comfort_load or 0,
            balanced_right.comfort_load or 0,
        )
        pair_improved = (
            balanced_lower > 0 and balanced_higher / balanced_lower < higher / lower * 0.99
        )
        if not deferred_validation:
            assert after_metrics is not None
            if not candidate_is_non_worsening(before_metrics, after_metrics) or not (
                accepts_candidate(before_metrics, after_metrics) or pair_improved
            ):
                continue
        current = candidate
        changes.extend(
            (
                TimingChange(
                    left.cue_id,
                    "end",
                    left.end_ms,
                    new_boundary,
                    "bidirectional_balance",
                ),
                TimingChange(
                    right.cue_id,
                    "start",
                    right.start_ms,
                    new_boundary,
                    "bidirectional_balance",
                ),
            )
        )
    if deferred_validation and changes:
        final_metrics = measure(current, policy)
        if not candidate_is_non_worsening(initial_metrics, final_metrics):
            return original, []
    return current, changes
