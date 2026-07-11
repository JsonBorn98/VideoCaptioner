"""Deterministic M3 quality metrics for subtitle reading speed."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

from .policy import SpeedPolicy
from .reading_load import ReadingLoad


@dataclass(frozen=True)
class CueSpeedSample:
    """Minimal immutable observation consumed by aggregate metrics."""

    stable_id: str
    order: int
    duration_seconds: float
    required_hard_seconds: float
    comfort_load: float | None
    hard_load: float | None
    rhythm_id: str = "default"

    @classmethod
    def from_reading_load(
        cls,
        stable_id: str,
        order: int,
        reading_load: ReadingLoad,
        rhythm_id: str = "default",
    ) -> "CueSpeedSample":
        return cls(
            stable_id=stable_id,
            order=order,
            duration_seconds=reading_load.duration_seconds,
            required_hard_seconds=reading_load.required_hard_seconds,
            comfort_load=reading_load.comfort_load,
            hard_load=reading_load.hard_load,
            rhythm_id=rhythm_id,
        )

    @property
    def valid(self) -> bool:
        return (
            self.duration_seconds > 0
            and math.isfinite(self.duration_seconds)
            and self.comfort_load is not None
            and math.isfinite(self.comfort_load)
            and self.hard_load is not None
            and math.isfinite(self.hard_load)
            and self.required_hard_seconds >= 0
            and math.isfinite(self.required_hard_seconds)
        )


@dataclass(frozen=True)
class AdjacentJump:
    p90: float | None
    emergency_count: int
    compared_count: int


@dataclass(frozen=True)
class SpeedMetrics:
    hard_deficit: float
    unresolved_hard_count: int
    speed_spread: float | None
    adjacent_jump: AdjacentJump
    invalid_count: int


def weighted_percentile(
    observations: Iterable[tuple[float, float, str]],
    quantile: float,
) -> float | None:
    """Return the stable nearest-rank weighted percentile from positive weights."""

    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between 0 and 1")
    valid = [
        (value, weight, stable_id)
        for value, weight, stable_id in observations
        if math.isfinite(value) and math.isfinite(weight) and weight > 0
    ]
    if not valid:
        return None
    valid.sort(key=lambda item: (item[0], item[2]))
    total_weight = sum(weight for _, weight, _ in valid)
    target = quantile * total_weight
    cumulative = 0.0
    for value, weight, _ in valid:
        cumulative += weight
        if cumulative >= target:
            return value
    return valid[-1][0]


def calculate_hard_deficit(samples: Iterable[CueSpeedSample]) -> float:
    """Calculate missing hard-limit display time as a fraction of required time."""

    valid = [sample for sample in samples if sample.valid]
    required_total = sum(sample.required_hard_seconds for sample in valid)
    if required_total == 0:
        return 0.0
    deficit = sum(
        max(0.0, sample.required_hard_seconds - sample.duration_seconds) for sample in valid
    )
    return deficit / required_total


def calculate_speed_spread(samples: Iterable[CueSpeedSample]) -> float | None:
    """Calculate duration-weighted P90/P10 comfort-load spread."""

    positive = [
        sample
        for sample in samples
        if sample.valid and sample.comfort_load is not None and sample.comfort_load > 0
    ]
    if len(positive) < 3:
        return None
    observations = [
        (sample.comfort_load or 0.0, sample.duration_seconds, sample.stable_id)
        for sample in positive
    ]
    p10 = weighted_percentile(observations, 0.1)
    p90 = weighted_percentile(observations, 0.9)
    if p10 is None or p90 is None or p10 == 0:
        return None
    return p90 / p10


def calculate_adjacent_jump(
    samples: Sequence[CueSpeedSample],
    policy: SpeedPolicy,
) -> AdjacentJump:
    """Calculate effective adjacent jumps without crossing rhythm resets."""

    ordered = sorted(samples, key=lambda sample: (sample.order, sample.stable_id))
    jumps: list[tuple[float, float, str]] = []
    emergency_count = 0
    for left, right in zip(ordered, ordered[1:]):
        if left.rhythm_id != right.rhythm_id or not left.valid or not right.valid:
            continue
        lower = min(left.comfort_load or 0.0, right.comfort_load or 0.0)
        higher = max(left.comfort_load or 0.0, right.comfort_load or 0.0)
        if lower <= 0 or higher < policy.effective_jump_load:
            continue
        ratio = higher / lower
        boundary_id = f"{left.stable_id}\0{right.stable_id}"
        jumps.append((ratio, 1.0, boundary_id))
        if ratio > policy.adjacent_emergency_limit:
            emergency_count += 1
    p90 = weighted_percentile(jumps, 0.9) if len(jumps) >= 2 else None
    return AdjacentJump(p90, emergency_count, len(jumps))


def calculate_speed_metrics(
    samples: Sequence[CueSpeedSample],
    policy: SpeedPolicy,
) -> SpeedMetrics:
    """Calculate the complete first-version serialized M3 snapshot."""

    valid = [sample for sample in samples if sample.valid]
    return SpeedMetrics(
        hard_deficit=calculate_hard_deficit(valid),
        unresolved_hard_count=sum((sample.hard_load or 0.0) > 1.0 for sample in valid),
        speed_spread=calculate_speed_spread(valid),
        adjacent_jump=calculate_adjacent_jump(samples, policy),
        invalid_count=len(samples) - len(valid),
    )
