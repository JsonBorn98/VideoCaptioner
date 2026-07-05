"""Text segmentation helpers for ASR responses without native timestamps."""

from __future__ import annotations

import re
from collections.abc import Sequence

from videocaptioner.core.utils.text_utils import count_words, is_mainly_cjk

from .asr_data import ASRDataSeg

MAX_WORD_COUNT_CJK = 25
MAX_WORD_COUNT_ENGLISH = 14
SpeechRangeMs = tuple[int, int]


def normalize_transcript_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text.strip())
    # Some ASR gateways return paragraph boundaries without spaces, e.g. "text.Next".
    return re.sub(r"([.!?。！？])(?=(?:[\"'“”‘’(\[]?[A-Z]))", r"\1 ", text)


def split_long_piece(text: str, max_word_count: int) -> list[str]:
    if count_words(text) <= max_word_count:
        return [text]

    if is_mainly_cjk(text):
        chunks = []
        current = ""
        for char in text:
            current += char
            if count_words(current) >= max_word_count:
                chunks.append(current.strip())
                current = ""
        if current.strip():
            chunks.append(current.strip())
        return chunks

    chunks = []
    current_words: list[str] = []
    for word in text.split():
        next_words = [*current_words, word]
        if current_words and count_words(" ".join(next_words)) > max_word_count:
            chunks.append(" ".join(current_words).strip())
            current_words = [word]
        else:
            current_words = next_words
    if current_words:
        chunks.append(" ".join(current_words).strip())
    return chunks


def split_transcript_text(text: str) -> list[str]:
    text = normalize_transcript_text(text)
    if not text:
        return []

    max_word_count = MAX_WORD_COUNT_CJK if is_mainly_cjk(text) else MAX_WORD_COUNT_ENGLISH
    min_word_count = 4 if not is_mainly_cjk(text) else 6

    sentence_pattern = r".+?(?:[.!?。！？]+[\"'”’)\]]*|$)"
    sentence_pieces = [
        piece.strip()
        for piece in re.findall(sentence_pattern, text)
        if piece and piece.strip()
    ]

    pieces: list[str] = []
    for sentence in sentence_pieces:
        pieces.extend(split_long_piece(sentence, max_word_count))

    segments: list[str] = []
    current = ""
    joiner = "" if is_mainly_cjk(text) else " "
    for piece in pieces:
        if not current:
            current = piece
            continue

        merged = f"{current}{joiner}{piece}".strip()
        if count_words(merged) <= max_word_count or count_words(current) < min_word_count:
            current = merged
        else:
            segments.append(current)
            current = piece

    if current:
        segments.append(current)

    return segments or [text]


def normalize_speech_ranges(
    speech_ranges_ms: Sequence[Sequence[int]] | None,
    total_ms: int,
) -> list[SpeechRangeMs]:
    if not speech_ranges_ms:
        return []

    boundary_ms = max(int(total_ms), 1)
    ranges: list[SpeechRangeMs] = []
    for item in speech_ranges_ms:
        if len(item) < 2:
            continue
        start = max(0, min(boundary_ms, int(item[0])))
        end = max(0, min(boundary_ms, int(item[1])))
        if end <= start:
            continue
        ranges.append((start, end))

    ranges.sort()
    merged: list[SpeechRangeMs] = []
    for start, end in ranges:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _make_linear_timed_segments(
    text_segments: list[str],
    total_ms: int,
) -> list[ASRDataSeg]:
    if not text_segments:
        return []
    total_ms = max(int(total_ms), 1)
    if len(text_segments) == 1:
        return [ASRDataSeg(text=text_segments[0], start_time=0, end_time=total_ms)]

    weights = [max(count_words(text), 1) for text in text_segments]
    total_weight = sum(weights)
    segments: list[ASRDataSeg] = []
    start_time = 0
    cumulative_weight = 0

    for index, (text, weight) in enumerate(zip(text_segments, weights)):
        cumulative_weight += weight
        if index == len(text_segments) - 1:
            end_time = total_ms
        else:
            end_time = int(round(total_ms * cumulative_weight / total_weight))
        end_time = max(end_time, start_time + 1)
        segments.append(ASRDataSeg(text=text, start_time=start_time, end_time=end_time))
        start_time = end_time

    return segments


def _speech_axis_intervals(
    speech_ranges: Sequence[SpeechRangeMs],
) -> list[tuple[int, int, int, int]]:
    intervals: list[tuple[int, int, int, int]] = []
    cursor = 0
    for start, end in speech_ranges:
        duration = end - start
        if duration <= 0:
            continue
        intervals.append((start, end, cursor, cursor + duration))
        cursor += duration
    return intervals


def _find_speech_axis_interval(
    intervals: Sequence[tuple[int, int, int, int]],
    axis_ms: int,
    *,
    prefer_next_on_boundary: bool,
) -> int:
    for index, (_, _, _axis_start, axis_end) in enumerate(intervals):
        if axis_ms < axis_end:
            return index
        if axis_ms == axis_end:
            if prefer_next_on_boundary and index + 1 < len(intervals):
                return index + 1
            return index
    return len(intervals) - 1


def _map_axis_to_interval(
    interval: tuple[int, int, int, int],
    axis_ms: int,
) -> int:
    start, end, axis_start, axis_end = interval
    offset = max(0, min(axis_end - axis_start, axis_ms - axis_start))
    return max(start, min(end, start + offset))


def _make_speech_constrained_timed_segments(
    text_segments: list[str],
    total_ms: int,
    speech_ranges: Sequence[SpeechRangeMs],
) -> list[ASRDataSeg]:
    if len(text_segments) == 1:
        return [
            ASRDataSeg(
                text=text_segments[0],
                start_time=speech_ranges[0][0],
                end_time=max(speech_ranges[-1][1], speech_ranges[0][0] + 1),
            )
        ]

    intervals = _speech_axis_intervals(speech_ranges)
    total_speech_ms = intervals[-1][3] if intervals else 0
    if total_speech_ms <= 0:
        return _make_linear_timed_segments(text_segments, total_ms)

    weights = [max(count_words(text), 1) for text in text_segments]
    total_weight = sum(weights)
    segments: list[ASRDataSeg] = []
    axis_start = 0
    cumulative_weight = 0

    for index, (text, weight) in enumerate(zip(text_segments, weights)):
        cumulative_weight += weight
        if index == len(text_segments) - 1:
            axis_end = total_speech_ms
        else:
            axis_end = int(round(total_speech_ms * cumulative_weight / total_weight))
        axis_end = max(axis_end, axis_start + 1)

        start_index = _find_speech_axis_interval(
            intervals,
            min(axis_start, total_speech_ms),
            prefer_next_on_boundary=True,
        )
        end_index = _find_speech_axis_interval(
            intervals,
            min(axis_end, total_speech_ms),
            prefer_next_on_boundary=False,
        )

        start_time = _map_axis_to_interval(intervals[start_index], axis_start)
        if end_index == start_index:
            end_time = _map_axis_to_interval(intervals[end_index], axis_end)
        else:
            # Keep the cue inside the current speech island instead of spanning
            # a long detected silence gap.
            end_time = intervals[start_index][1]
            if end_time <= start_time and end_index > start_index:
                start_time = intervals[end_index][0]
                end_time = _map_axis_to_interval(intervals[end_index], axis_end)

        if end_time <= start_time:
            end_time = min(max(total_ms, 1), start_time + 1)
        segments.append(
            ASRDataSeg(text=text, start_time=start_time, end_time=end_time)
        )
        axis_start = axis_end

    return segments


def make_timed_segments(
    text_segments: list[str],
    total_ms: int,
    speech_ranges_ms: Sequence[Sequence[int]] | None = None,
) -> list[ASRDataSeg]:
    speech_ranges = normalize_speech_ranges(speech_ranges_ms, total_ms)
    if speech_ranges:
        return _make_speech_constrained_timed_segments(
            text_segments,
            max(int(total_ms), 1),
            speech_ranges,
        )
    return _make_linear_timed_segments(text_segments, total_ms)
