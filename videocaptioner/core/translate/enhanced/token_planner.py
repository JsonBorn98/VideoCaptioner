"""Conservative token planning aligned to immutable subtitle boundaries."""

from __future__ import annotations

import json
import math
from typing import Sequence

from .models import AnalysisWindow, SubtitleCue, TranslationBatch


class TokenBudgetExceeded(ValueError):
    """Raised when even one cue cannot fit the requested working budget."""


def estimate_tokens(text: str) -> int:
    """Conservatively estimate tokens without assuming a provider tokenizer.

    ASCII text is charged at one token per three characters, while every
    non-ASCII code point is charged as one token. Structural overhead is added
    by the payload estimators below. This intentionally prefers smaller calls.
    """

    ascii_count = sum(ord(char) < 128 for char in text)
    non_ascii_count = len(text) - ascii_count
    return max(1, math.ceil(ascii_count / 3) + non_ascii_count)


def estimate_cues_tokens(cues: Sequence[SubtitleCue]) -> int:
    payload = [{"id": cue.cue_id, "text": cue.text} for cue in cues]
    # JSON punctuation and IDs are part of the real request, so estimate the
    # serialized payload instead of summing text alone.
    return estimate_tokens(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _validate_cues(cues: Sequence[SubtitleCue]) -> None:
    cue_ids = [cue.cue_id for cue in cues]
    if len(cue_ids) != len(set(cue_ids)):
        raise ValueError("cue IDs must be unique")
    if cue_ids != sorted(cue_ids):
        raise ValueError("cues must be ordered by cue_id")


def plan_analysis_windows(
    cues: Sequence[SubtitleCue],
    *,
    working_context_tokens: int,
    fixed_prompt_tokens: int = 0,
    output_reserve_tokens: int = 0,
    overlap_cues: int = 2,
) -> tuple[AnalysisWindow, ...]:
    """Cover all cues using budgeted windows with deterministic cue overlap."""

    _validate_cues(cues)
    if not cues:
        return ()
    if working_context_tokens <= 0:
        raise ValueError("working_context_tokens must be positive")
    if fixed_prompt_tokens < 0 or output_reserve_tokens < 0 or overlap_cues < 0:
        raise ValueError("token reserves and overlap_cues must not be negative")

    available = working_context_tokens - fixed_prompt_tokens - output_reserve_tokens
    if available <= 0:
        raise TokenBudgetExceeded("fixed prompt and output reserve consume the budget")

    windows: list[AnalysisWindow] = []
    start = 0
    while start < len(cues):
        end = start
        latest_estimate = 0
        while end < len(cues):
            candidate = cues[start : end + 1]
            estimate = estimate_cues_tokens(candidate)
            if estimate > available:
                break
            end += 1
            latest_estimate = estimate

        if end == start:
            cue = cues[start]
            required = fixed_prompt_tokens + estimate_cues_tokens([cue]) + output_reserve_tokens
            raise TokenBudgetExceeded(
                f"cue {cue.cue_id} requires approximately {required} tokens, "
                f"budget is {working_context_tokens}"
            )

        windows.append(
            AnalysisWindow(
                cues=tuple(cues[start:end]),
                estimated_input_tokens=fixed_prompt_tokens + latest_estimate,
            )
        )
        if end >= len(cues):
            break
        # Always make forward progress; a one-cue window cannot overlap itself.
        start = max(start + 1, end - min(overlap_cues, end - start - 1))

    return tuple(windows)


def _batch_estimate(
    before: Sequence[SubtitleCue],
    subjects: Sequence[SubtitleCue],
    after: Sequence[SubtitleCue],
    fixed_prompt_tokens: int,
) -> int:
    payload = {
        "boundary_context": {
            "before": [{"id": cue.cue_id, "text": cue.text} for cue in before],
            "after": [{"id": cue.cue_id, "text": cue.text} for cue in after],
        },
        "translation_subjects": [
            {"id": cue.cue_id, "text": cue.text} for cue in subjects
        ],
    }
    return fixed_prompt_tokens + estimate_tokens(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def plan_translation_batches(
    cues: Sequence[SubtitleCue],
    *,
    batch_size: int,
    working_context_tokens: int,
    fixed_prompt_tokens: int = 0,
    output_reserve_tokens: int = 0,
    context_radius: int = 3,
) -> tuple[TranslationBatch, ...]:
    """Plan formal translation batches with strictly separated boundary context.

    The planner first shrinks the subject count while keeping the requested
    boundary context. Only when a single subject still does not fit does it
    reduce boundary context from ``context_radius`` down to zero.
    """

    _validate_cues(cues)
    if not cues:
        return ()
    if batch_size <= 0 or working_context_tokens <= 0:
        raise ValueError("batch_size and working_context_tokens must be positive")
    if fixed_prompt_tokens < 0 or output_reserve_tokens < 0 or context_radius < 0:
        raise ValueError("token reserves and context_radius must not be negative")
    if context_radius > 3:
        raise ValueError("boundary context cannot exceed three cues per side")

    def make_candidate(
        start: int, subject_count: int, radius: int
    ) -> tuple[tuple[SubtitleCue, ...], tuple[SubtitleCue, ...], tuple[SubtitleCue, ...], int]:
        end = start + subject_count
        before = tuple(cues[max(0, start - radius) : start])
        subjects = tuple(cues[start:end])
        after = tuple(cues[end : min(len(cues), end + radius)])
        estimated = _batch_estimate(before, subjects, after, fixed_prompt_tokens)
        return before, subjects, after, estimated

    batches: list[TranslationBatch] = []
    cursor = 0
    while cursor < len(cues):
        selected = None
        max_subjects = min(batch_size, len(cues) - cursor)

        # Preserve all requested boundary context while shrinking subjects.
        for subject_count in range(max_subjects, 0, -1):
            candidate = make_candidate(cursor, subject_count, context_radius)
            if candidate[3] + output_reserve_tokens <= working_context_tokens:
                selected = candidate
                break

        # A one-subject batch still does not fit: reduce context only now.
        if selected is None:
            for radius in range(context_radius - 1, -1, -1):
                candidate = make_candidate(cursor, 1, radius)
                if candidate[3] + output_reserve_tokens <= working_context_tokens:
                    selected = candidate
                    break

        if selected is None:
            cue = cues[cursor]
            _, _, _, required_input = make_candidate(cursor, 1, 0)
            required = required_input + output_reserve_tokens
            raise TokenBudgetExceeded(
                f"cue {cue.cue_id} requires approximately {required} tokens, "
                f"budget is {working_context_tokens}"
            )

        before, subjects, after, estimated = selected
        batches.append(
            TranslationBatch(
                subjects=subjects,
                context_before=before,
                context_after=after,
                estimated_input_tokens=estimated,
            )
        )
        cursor += len(subjects)

    return tuple(batches)
