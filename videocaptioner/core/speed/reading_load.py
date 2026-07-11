"""Unicode-aware reading units and component reading-load calculation."""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass
from enum import Enum

import regex

from .policy import SpeedPolicy

_GRAPHEME_RE = regex.compile(r"\X")
_CJK_RE = regex.compile(r"[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]")
_STRONG_PUNCTUATION = frozenset("!?！？…‼⁉")


class GraphemeKind(str, Enum):
    CJK = "cjk"
    NON_CJK = "non_cjk"
    WHITESPACE = "whitespace"
    WEAK_PUNCTUATION = "weak_punctuation"
    STRONG_PUNCTUATION = "strong_punctuation"


@dataclass(frozen=True)
class ReadingUnits:
    """Weighted units assigned to the two configured reading-rate components."""

    cjk_units: float = 0.0
    non_cjk_units: float = 0.0
    grapheme_count: int = 0

    @property
    def total_units(self) -> float:
        return self.cjk_units + self.non_cjk_units


@dataclass(frozen=True)
class ReadingLoad:
    """Required reading time and normalized load for one displayed cue side."""

    units: ReadingUnits
    duration_seconds: float
    required_comfort_seconds: float
    required_hard_seconds: float
    comfort_load: float | None
    hard_load: float | None
    valid: bool

    @property
    def hard_overspeed(self) -> bool:
        return self.hard_load is not None and self.hard_load > 1.0


def split_graphemes(text: str) -> tuple[str, ...]:
    """Split text into Unicode extended grapheme clusters."""

    return tuple(_GRAPHEME_RE.findall(text))


def classify_grapheme(grapheme: str) -> GraphemeKind:
    """Classify one grapheme according to the first-version script contract."""

    if not grapheme or grapheme.isspace():
        return GraphemeKind.WHITESPACE
    base_chars = [char for char in grapheme if not unicodedata.category(char).startswith("M")]
    if base_chars and all(unicodedata.category(char).startswith("P") for char in base_chars):
        if any(char in _STRONG_PUNCTUATION for char in grapheme):
            return GraphemeKind.STRONG_PUNCTUATION
        return GraphemeKind.WEAK_PUNCTUATION
    if _CJK_RE.search(grapheme):
        return GraphemeKind.CJK
    return GraphemeKind.NON_CJK


def count_reading_units(text: str, policy: SpeedPolicy) -> ReadingUnits:
    """Count weighted CJK/non-CJK units without double-counting punctuation."""

    cjk_units = 0.0
    non_cjk_units = 0.0
    graphemes = split_graphemes(text)
    for grapheme in graphemes:
        kind = classify_grapheme(grapheme)
        if kind is GraphemeKind.CJK:
            cjk_units += 1.0
        elif kind is GraphemeKind.NON_CJK:
            non_cjk_units += 1.0
        elif kind is GraphemeKind.WHITESPACE:
            non_cjk_units += policy.whitespace_weight
        elif kind is GraphemeKind.STRONG_PUNCTUATION:
            non_cjk_units += policy.strong_punctuation_weight
        else:
            non_cjk_units += policy.weak_punctuation_weight
    return ReadingUnits(cjk_units, non_cjk_units, len(graphemes))


def calculate_reading_load(
    text: str,
    duration_seconds: float,
    policy: SpeedPolicy,
) -> ReadingLoad:
    """Calculate comfort/hard reading load; invalid durations produce no ratio."""

    units = count_reading_units(text, policy)
    required_comfort = (
        units.cjk_units / policy.comfort_cps_cjk + units.non_cjk_units / policy.comfort_cps_latin
    )
    required_hard = (
        units.cjk_units / policy.hard_cps_cjk + units.non_cjk_units / policy.hard_cps_latin
    )
    valid = duration_seconds > 0 and math.isfinite(duration_seconds)
    if not valid:
        comfort_load = None
        hard_load = None
    else:
        comfort_load = required_comfort / duration_seconds
        hard_load = required_hard / duration_seconds
    return ReadingLoad(
        units=units,
        duration_seconds=duration_seconds,
        required_comfort_seconds=required_comfort,
        required_hard_seconds=required_hard,
        comfort_load=comfort_load,
        hard_load=hard_load,
        valid=valid,
    )
