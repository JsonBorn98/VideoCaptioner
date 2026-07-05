"""Backend-neutral ASR transcript and alignment anomaly checks."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Optional

from videocaptioner.core.utils.text_utils import count_words, is_mainly_cjk

from .asr_data import ASRDataSeg


@dataclass(frozen=True)
class AnomalyThresholds:
    min_alignment_coverage: float = 0.7
    min_alignment_units_for_coverage: int = 20
    min_audio_time_coverage: float = 0.85
    min_duration_for_time_coverage: float = 30.0
    min_unaligned_tail: float = 15.0
    max_internal_alignment_gap: float = 30.0
    max_alignment_overflow: float = 0.05
    max_words_per_second_en: float = 5.5
    max_words_per_second_cjk: float = 7.0
    min_repetition_phrase_len: int = 8
    min_repetition_count: int = 3
    max_repetition_phrase_len: int = 60
    repeated_ngram_size: int = 10
    max_repeated_ngram_count: int = 3
    min_hard_repetition_count: int = 8
    min_hard_repeated_ngram_count: int = 8


DEFAULT_ANOMALY_THRESHOLDS = AnomalyThresholds()

MIN_ALIGNMENT_COVERAGE = DEFAULT_ANOMALY_THRESHOLDS.min_alignment_coverage
MIN_ALIGNMENT_UNITS_FOR_COVERAGE = (
    DEFAULT_ANOMALY_THRESHOLDS.min_alignment_units_for_coverage
)
MIN_AUDIO_TIME_COVERAGE = DEFAULT_ANOMALY_THRESHOLDS.min_audio_time_coverage
MIN_DURATION_FOR_TIME_COVERAGE = (
    DEFAULT_ANOMALY_THRESHOLDS.min_duration_for_time_coverage
)
MIN_UNALIGNED_TAIL = DEFAULT_ANOMALY_THRESHOLDS.min_unaligned_tail
MAX_INTERNAL_ALIGNMENT_GAP = DEFAULT_ANOMALY_THRESHOLDS.max_internal_alignment_gap
MAX_ALIGNMENT_OVERFLOW = DEFAULT_ANOMALY_THRESHOLDS.max_alignment_overflow

MAX_WORDS_PER_SECOND_EN = DEFAULT_ANOMALY_THRESHOLDS.max_words_per_second_en
MAX_WORDS_PER_SECOND_CJK = DEFAULT_ANOMALY_THRESHOLDS.max_words_per_second_cjk
MIN_REPETITION_PHRASE_LEN = DEFAULT_ANOMALY_THRESHOLDS.min_repetition_phrase_len
MIN_REPETITION_COUNT = DEFAULT_ANOMALY_THRESHOLDS.min_repetition_count
MAX_REPETITION_PHRASE_LEN = DEFAULT_ANOMALY_THRESHOLDS.max_repetition_phrase_len
REPEATED_NGRAM_SIZE = DEFAULT_ANOMALY_THRESHOLDS.repeated_ngram_size
MAX_REPEATED_NGRAM_COUNT = DEFAULT_ANOMALY_THRESHOLDS.max_repeated_ngram_count
MIN_HARD_REPETITION_COUNT = DEFAULT_ANOMALY_THRESHOLDS.min_hard_repetition_count
MIN_HARD_REPEATED_NGRAM_COUNT = (
    DEFAULT_ANOMALY_THRESHOLDS.min_hard_repeated_ngram_count
)


def detect_repetition(
    text: str,
    min_phrase_len: int = MIN_REPETITION_PHRASE_LEN,
    max_phrase_len: int = MAX_REPETITION_PHRASE_LEN,
    min_repeats: int = MIN_REPETITION_COUNT,
) -> bool:
    """Return True when a phrase is consecutively repeated in ``text``."""
    text = text.strip()
    n = len(text)
    if n < min_phrase_len * min_repeats:
        return False

    upper = min(max_phrase_len, n // min_repeats)
    for phrase_len in range(min_phrase_len, upper + 1):
        for start in range(n - phrase_len * min_repeats + 1):
            phrase = text[start : start + phrase_len]
            if not phrase.strip():
                continue
            count = 1
            pos = start + phrase_len
            while pos + phrase_len <= n and text[pos : pos + phrase_len] == phrase:
                count += 1
                pos += phrase_len
            if count >= min_repeats:
                return True
    return False


def detect_repeated_ngram(
    text: str,
    ngram_size: int = REPEATED_NGRAM_SIZE,
    max_count: int = MAX_REPEATED_NGRAM_COUNT,
) -> bool:
    """Return True when a long token n-gram recurs across ``text``."""
    tokens = (
        list(text)
        if is_mainly_cjk(text)
        else re.findall(r"[A-Za-z0-9']+", text.lower())
    )
    if len(tokens) < ngram_size * max_count:
        return False
    counts = Counter(
        tuple(tokens[i : i + ngram_size])
        for i in range(len(tokens) - ngram_size + 1)
    )
    return any(count >= max_count for count in counts.values())


def check_transcript_anomaly(
    text: str,
    audio_duration: float,
    thresholds: AnomalyThresholds = DEFAULT_ANOMALY_THRESHOLDS,
) -> Optional[str]:
    """Detect near-certain ASR hallucination text before/after alignment."""
    if not text or audio_duration <= 0:
        return None
    word_count = count_words(text)
    density = word_count / audio_duration
    max_density = (
        thresholds.max_words_per_second_cjk
        if is_mainly_cjk(text)
        else thresholds.max_words_per_second_en
    )
    if density > max_density:
        return (
            f"text density too high ({word_count}/{audio_duration:.1f}s = "
            f"{density:.1f} words/s, max {max_density})"
        )
    if detect_repetition(
        text,
        min_phrase_len=thresholds.min_repetition_phrase_len,
        max_phrase_len=thresholds.max_repetition_phrase_len,
        min_repeats=thresholds.min_hard_repetition_count,
    ) or detect_repeated_ngram(
        text,
        ngram_size=thresholds.repeated_ngram_size,
        max_count=thresholds.min_hard_repeated_ngram_count,
    ):
        return "repetitive hallucination loop detected"
    return None


def transcript_repetition_suspicion(
    text: str,
    thresholds: AnomalyThresholds = DEFAULT_ANOMALY_THRESHOLDS,
) -> bool:
    """True when text repetition should be deferred to alignment validation."""
    if not text:
        return False
    return detect_repetition(
        text,
        min_phrase_len=thresholds.min_repetition_phrase_len,
        max_phrase_len=thresholds.max_repetition_phrase_len,
        min_repeats=thresholds.min_repetition_count,
    ) or detect_repeated_ngram(
        text,
        ngram_size=thresholds.repeated_ngram_size,
        max_count=thresholds.max_repeated_ngram_count,
    )


def alignment_coverage(text: str, segments: list[ASRDataSeg]) -> tuple[int, int, float]:
    expected_units = count_words(text)
    aligned_units = sum(max(count_words(seg.text), 1) for seg in segments)
    coverage = aligned_units / expected_units if expected_units else 1.0
    return expected_units, aligned_units, coverage


def clamp_segments_to_duration(
    segments: list[ASRDataSeg], max_ms: int
) -> tuple[list[ASRDataSeg], float]:
    """Drop/clip aligned segments that spill past the audio boundary."""
    if max_ms <= 0 or not segments:
        return list(segments), 0.0

    last_end = max(seg.end_time for seg in segments)
    overflow_ratio = (last_end - max_ms) / max_ms if last_end > max_ms else 0.0

    clamped: list[ASRDataSeg] = []
    for seg in segments:
        if seg.start_time >= max_ms:
            continue
        end_time = min(seg.end_time, max_ms)
        if end_time <= seg.start_time:
            continue
        clamped.append(
            ASRDataSeg(
                text=seg.text,
                start_time=seg.start_time,
                end_time=end_time,
                translated_text=seg.translated_text,
            )
        )
    return clamped, overflow_ratio


def _max_internal_gap_ms(segments: list[ASRDataSeg]) -> int:
    if len(segments) < 2:
        return 0
    ordered = sorted(segments, key=lambda seg: seg.start_time)
    return max(
        (b.start_time - a.end_time for a, b in zip(ordered, ordered[1:])),
        default=0,
    )


def alignment_problems(
    text: str,
    segments: list[ASRDataSeg],
    boundary_ms: int,
    overflow_ratio: float,
    thresholds: AnomalyThresholds = DEFAULT_ANOMALY_THRESHOLDS,
) -> list[str]:
    """Return reasons the aligned result looks degraded."""
    problems: list[str] = []

    expected_units, aligned_units, coverage = alignment_coverage(text, segments)
    if (
        expected_units >= thresholds.min_alignment_units_for_coverage
        and coverage < thresholds.min_alignment_coverage
    ):
        problems.append(
            f"alignment coverage too low ({aligned_units}/{expected_units}, "
            f"{coverage * 100:.1f}%)"
        )

    if overflow_ratio > thresholds.max_alignment_overflow:
        problems.append(
            f"aligned timestamps overflow audio by {overflow_ratio * 100:.0f}%"
        )

    boundary_s = boundary_ms / 1000.0
    if boundary_s >= thresholds.min_duration_for_time_coverage and segments:
        aligned_end_ms = max(seg.end_time for seg in segments)
        time_coverage = min(aligned_end_ms / boundary_ms, 1.0)
        unaligned_tail_s = (boundary_ms - aligned_end_ms) / 1000.0
        if (
            time_coverage < thresholds.min_audio_time_coverage
            and unaligned_tail_s > thresholds.min_unaligned_tail
        ):
            problems.append(
                f"audio time coverage too low (aligned {time_coverage * 100:.1f}% "
                f"of {boundary_s:.0f}s, {unaligned_tail_s:.0f}s unaligned tail)"
            )
        gap_ms = _max_internal_gap_ms(segments)
        if gap_ms > thresholds.max_internal_alignment_gap * 1000:
            problems.append(f"large internal gap ({gap_ms / 1000:.0f}s of no speech)")

    return problems
