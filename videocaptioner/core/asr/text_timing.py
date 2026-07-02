"""Text segmentation helpers for ASR responses without native timestamps."""

from __future__ import annotations

import re

from videocaptioner.core.utils.text_utils import count_words, is_mainly_cjk

from .asr_data import ASRDataSeg

MAX_WORD_COUNT_CJK = 25
MAX_WORD_COUNT_ENGLISH = 14


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


def make_timed_segments(text_segments: list[str], total_ms: int) -> list[ASRDataSeg]:
    if not text_segments:
        return []
    if len(text_segments) == 1:
        return [ASRDataSeg(text=text_segments[0], start_time=0, end_time=max(total_ms, 1))]

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
