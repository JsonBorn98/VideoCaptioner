"""Authoritative policies for subtitle reading-speed optimization."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class SpeedPreset(str, Enum):
    """Built-in policy identifiers persisted in task configuration."""

    LOOSE = "loose"
    BALANCED = "balanced"
    SMOOTH = "smooth"


@dataclass(frozen=True)
class SpeedPolicy:
    """Versioned, immutable reading-speed policy.

    A task must keep one policy snapshot instead of consulting mutable GUI
    defaults while it runs.
    """

    schema_version: int = 1
    preset: SpeedPreset = SpeedPreset.BALANCED
    comfort_cps_cjk: float = 9.0
    hard_cps_cjk: float = 11.0
    comfort_cps_latin: float = 16.0
    hard_cps_latin: float = 20.0
    adjacent_p90_target: float = 1.8
    adjacent_emergency_limit: float = 3.0
    min_duration_seconds: float = 1.0
    max_duration_seconds: float = 6.0
    technical_min_duration_seconds: float = 0.5
    bidirectional_smoothing: bool = False
    effective_jump_load: float = 0.6
    whitespace_weight: float = 0.0
    weak_punctuation_weight: float = 0.25
    strong_punctuation_weight: float = 0.5
    local_window_radius: int = 4
    speech_density_adjustment_limit: float = 0.15
    rhythm_reset_ms: int = 800
    hard_rhythm_reset_ms: int = 1500
    low_confidence_boundary_shift_ms: int = 250
    medium_confidence_boundary_shift_ms: int = 500
    high_confidence_boundary_shift_ms: int = 1000

    def __post_init__(self) -> None:
        positive = {
            "comfort_cps_cjk": self.comfort_cps_cjk,
            "hard_cps_cjk": self.hard_cps_cjk,
            "comfort_cps_latin": self.comfort_cps_latin,
            "hard_cps_latin": self.hard_cps_latin,
            "adjacent_p90_target": self.adjacent_p90_target,
            "adjacent_emergency_limit": self.adjacent_emergency_limit,
            "min_duration_seconds": self.min_duration_seconds,
            "max_duration_seconds": self.max_duration_seconds,
            "technical_min_duration_seconds": self.technical_min_duration_seconds,
        }
        for name, value in positive.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.comfort_cps_cjk >= self.hard_cps_cjk:
            raise ValueError("CJK comfort CPS must be lower than hard CPS")
        if self.comfort_cps_latin >= self.hard_cps_latin:
            raise ValueError("Non-CJK comfort CPS must be lower than hard CPS")
        if self.min_duration_seconds > self.max_duration_seconds:
            raise ValueError("minimum duration cannot exceed maximum duration")
        if self.technical_min_duration_seconds > self.min_duration_seconds:
            raise ValueError("technical minimum cannot exceed normal minimum duration")
        for name in (
            "effective_jump_load",
            "whitespace_weight",
            "weak_punctuation_weight",
            "strong_punctuation_weight",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")
        if self.local_window_radius < 1:
            raise ValueError("local_window_radius must be positive")
        if not 0 <= self.speech_density_adjustment_limit <= 1:
            raise ValueError("speech_density_adjustment_limit must be between 0 and 1")
        if self.rhythm_reset_ms <= 0 or self.hard_rhythm_reset_ms < self.rhythm_reset_ms:
            raise ValueError("hard rhythm reset must be at least the normal rhythm reset")
        movement_budgets = (
            self.low_confidence_boundary_shift_ms,
            self.medium_confidence_boundary_shift_ms,
            self.high_confidence_boundary_shift_ms,
        )
        if movement_budgets[0] < 0 or not (
            movement_budgets[0] <= movement_budgets[1] <= movement_budgets[2]
        ):
            raise ValueError("boundary movement budgets must be non-negative and ordered")

    def with_overrides(self, **overrides: Any) -> "SpeedPolicy":
        """Return a validated custom policy based on this snapshot."""

        return replace(self, **overrides)


_BUILTIN_POLICIES: Mapping[SpeedPreset, SpeedPolicy] = MappingProxyType(
    {
        SpeedPreset.LOOSE: SpeedPolicy(
            preset=SpeedPreset.LOOSE,
            comfort_cps_cjk=10.0,
            hard_cps_cjk=12.0,
            comfort_cps_latin=18.0,
            hard_cps_latin=22.0,
            adjacent_p90_target=2.2,
            adjacent_emergency_limit=3.5,
            min_duration_seconds=0.8,
            max_duration_seconds=7.0,
        ),
        SpeedPreset.BALANCED: SpeedPolicy(),
        SpeedPreset.SMOOTH: SpeedPolicy(
            preset=SpeedPreset.SMOOTH,
            comfort_cps_cjk=8.0,
            hard_cps_cjk=10.0,
            comfort_cps_latin=14.0,
            hard_cps_latin=18.0,
            adjacent_p90_target=1.5,
            adjacent_emergency_limit=2.5,
            min_duration_seconds=1.2,
            max_duration_seconds=5.5,
            bidirectional_smoothing=True,
        ),
    }
)


def get_speed_policy(preset: SpeedPreset | str = SpeedPreset.BALANCED) -> SpeedPolicy:
    """Return an immutable built-in policy by persisted identifier."""

    try:
        preset_id = SpeedPreset(preset)
    except ValueError as exc:
        available = ", ".join(item.value for item in SpeedPreset)
        raise ValueError(f"Unknown speed preset: {preset}. Available presets: {available}") from exc
    return _BUILTIN_POLICIES[preset_id]


def available_speed_presets() -> tuple[str, ...]:
    """Return built-in identifiers in stable product order."""

    return tuple(item.value for item in SpeedPreset)
