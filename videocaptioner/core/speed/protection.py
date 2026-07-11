"""Conservative automatic protection rules for intentional subtitle timing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from ..asr.asr_data import ASRDataSeg
from .policy import SpeedPolicy
from .reading_load import count_reading_units

_MUSIC_RE = re.compile(r"(?i)(?:[♪♫♬]|\[(?:music|song|singing|applause)\]|【(?:音乐|歌曲|掌声)】)")


@dataclass(frozen=True)
class ProtectionMatch:
    index: int
    reason: str
    confidence: str = "high"


def detect_protected_cues(
    segments: Sequence[ASRDataSeg],
    texts: Sequence[str],
    policy: SpeedPolicy,
    *,
    explicit_indices: Iterable[int] = (),
) -> tuple[ProtectionMatch, ...]:
    """Return explicit and high-confidence automatic protection matches."""

    matches: dict[int, ProtectionMatch] = {
        index: ProtectionMatch(index, "explicit")
        for index in explicit_indices
        if 0 <= index < len(segments)
    }
    for index, (segment, text) in enumerate(zip(segments, texts)):
        if index in matches:
            continue
        duration_ms = segment.end_time - segment.start_time
        visible_units = count_reading_units(text, policy).total_units
        previous_gap = segment.start_time - segments[index - 1].end_time if index > 0 else None
        next_gap = (
            segments[index + 1].start_time - segment.end_time if index + 1 < len(segments) else None
        )
        if _MUSIC_RE.search(text):
            matches[index] = ProtectionMatch(index, "music_or_lyric_marker")
        elif visible_units <= 12 and duration_ms >= 4000:
            matches[index] = ProtectionMatch(index, "short_text_long_display")
        elif (
            visible_units <= 16
            and duration_ms >= 2500
            and (previous_gap is None or previous_gap >= policy.hard_rhythm_reset_ms)
            and (next_gap is None or next_gap >= policy.hard_rhythm_reset_ms)
        ):
            matches[index] = ProtectionMatch(index, "isolated_title_card")
    return tuple(matches[index] for index in sorted(matches))
