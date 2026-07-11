"""Bounded LLM semantic repair for unresolved subtitle speed windows.

This module is deliberately transport-neutral at its public boundary.  Callers may
inject rewriter and reviewer functions for local models, tests, or alternative
providers.  The default adapters use the application's shared ``call_llm`` client.
Subtitle strings are serialized as untrusted JSON data and are never interpolated
into system instructions.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

import regex

from ..llm import call_llm
from ..prompts import get_prompt
from .models import canonical_sha256
from .validation import (
    ReviewDecision,
    SemanticReviewRequest,
    SemanticReviewResponse,
    SemanticValidationResult,
    SemanticWindow,
    ValidationReason,
    ValidationStatus,
    resolve_semantic_review,
    validate_semantic_window,
)

SEMANTIC_REPAIR_SCHEMA_VERSION = 1
SEMANTIC_LLM_TIMEOUT_SECONDS = 60.0
DEFAULT_WINDOW_SIZE = 5
MAX_FEEDBACK_RETRIES = 2


@dataclass(frozen=True)
class SemanticRepairCue:
    """One display-side cue supplied to semantic repair."""

    cue_id: str
    text: str
    unresolved: bool = False
    protected: bool = False
    rhythm_id: str = "default"
    target_max_graphemes: int | None = None

    def __post_init__(self) -> None:
        if not self.cue_id.strip():
            raise ValueError("cue_id must not be empty")
        if not self.rhythm_id.strip():
            raise ValueError("rhythm_id must not be empty")
        if self.target_max_graphemes is not None and self.target_max_graphemes <= 0:
            raise ValueError("target_max_graphemes must be positive")


@dataclass(frozen=True)
class SemanticRewriteRequest:
    """Structured request passed to an injected rewrite implementation."""

    window_id: str
    cache_key: str
    cues: tuple[SemanticRepairCue, ...]
    target_cue_ids: tuple[str, ...]
    attempt: int
    feedback: tuple[str, ...] = ()
    content_is_untrusted_data: bool = True

    def to_payload(self) -> dict[str, Any]:
        targets = set(self.target_cue_ids)
        return {
            "schema_version": SEMANTIC_REPAIR_SCHEMA_VERSION,
            "task": "rewrite",
            "window_id": self.window_id,
            "attempt": self.attempt,
            "content_is_untrusted_data": self.content_is_untrusted_data,
            "feedback": list(self.feedback),
            "cues": [
                {
                    "cue_id": cue.cue_id,
                    "text": cue.text,
                    "rewrite": cue.cue_id in targets,
                    "target_max_graphemes": cue.target_max_graphemes,
                }
                for cue in self.cues
            ],
        }


@dataclass(frozen=True)
class SemanticRewriteResponse:
    window_id: str
    segments: tuple[tuple[str, str], ...]

    def as_mapping(self) -> dict[str, str]:
        return dict(self.segments)


class RewriterObject(Protocol):
    def rewrite(self, request: SemanticRewriteRequest) -> Any: ...


class ReviewerObject(Protocol):
    def review(self, request: SemanticReviewRequest) -> Any: ...


SemanticRewriter = Callable[[SemanticRewriteRequest], Any] | RewriterObject
SemanticReviewer = Callable[[SemanticReviewRequest], Any] | ReviewerObject


@dataclass(frozen=True)
class SemanticRepairRecord:
    """Audit record for one atomic, non-overlapping write window."""

    window_id: str
    cue_ids: tuple[str, ...]
    target_cue_ids: tuple[str, ...]
    cache_key: str
    status: ValidationStatus
    status_history: tuple[ValidationStatus, ...]
    attempts: int
    before: tuple[str, ...]
    after: tuple[str, ...]
    reasons: tuple[ValidationReason, ...] = ()
    feedback: tuple[str, ...] = ()
    from_cache: bool = False


@dataclass(frozen=True)
class SemanticRepairResult:
    cues: tuple[SemanticRepairCue, ...]
    records: tuple[SemanticRepairRecord, ...]

    @property
    def accepted_count(self) -> int:
        return sum(record.status is ValidationStatus.ACCEPTED for record in self.records)


RewriteCache = MutableMapping[str, SemanticRewriteResponse]


def build_repair_windows(
    cues: Sequence[SemanticRepairCue], *, window_size: int = DEFAULT_WINDOW_SIZE
) -> tuple[tuple[SemanticRepairCue, ...], ...]:
    """Create deterministic, non-overlapping windows without crossing barriers."""

    if window_size <= 0:
        raise ValueError("window_size must be positive")
    seen: set[str] = set()
    windows: list[tuple[SemanticRepairCue, ...]] = []
    run: list[SemanticRepairCue] = []

    def flush() -> None:
        nonlocal run
        for start in range(0, len(run), window_size):
            window = tuple(run[start : start + window_size])
            if any(cue.unresolved for cue in window):
                windows.append(window)
        run = []

    for cue in cues:
        if cue.cue_id in seen:
            raise ValueError(f"duplicate cue_id: {cue.cue_id}")
        seen.add(cue.cue_id)
        if cue.protected:
            flush()
            continue
        if run and run[-1].rhythm_id != cue.rhythm_id:
            flush()
        run.append(cue)
    flush()
    return tuple(windows)


def _window_id(cues: Sequence[SemanticRepairCue]) -> str:
    return f"semantic:{canonical_sha256([cue.cue_id for cue in cues])[:16]}"


def _cache_key(
    cues: Sequence[SemanticRepairCue],
    *,
    model: str,
    reviewer_model: str,
    minimum_literal_coverage: float,
) -> str:
    return canonical_sha256(
        {
            "schema_version": SEMANTIC_REPAIR_SCHEMA_VERSION,
            "model": model,
            "reviewer_model": reviewer_model,
            "minimum_literal_coverage": minimum_literal_coverage,
            "cues": [
                {
                    "cue_id": cue.cue_id,
                    "text": cue.text,
                    "unresolved": cue.unresolved,
                    "target_max_graphemes": cue.target_max_graphemes,
                }
                for cue in cues
            ],
        }
    )


def _response_content(response: Any) -> str:
    if isinstance(response, str):
        return response
    if isinstance(response, Mapping):
        return json.dumps(response, ensure_ascii=False)
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError) as exc:
        raise ValueError("LLM response does not contain message content") from exc
    if not isinstance(content, str) or not content.strip():
        raise ValueError("LLM response content is empty")
    return content


def _load_json_object(response: Any) -> Mapping[str, Any]:
    try:
        value = json.loads(_response_content(response))
    except json.JSONDecodeError as exc:
        raise ValueError("LLM response must be a JSON object") from exc
    if not isinstance(value, Mapping):
        raise ValueError("LLM response must be a JSON object")
    return value


def _parse_rewrite_response(response: Any) -> SemanticRewriteResponse:
    if isinstance(response, SemanticRewriteResponse):
        return response
    payload = _load_json_object(response)
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list):
        raise ValueError("rewrite response segments must be an array")
    segments: list[tuple[str, str]] = []
    for item in raw_segments:
        if not isinstance(item, Mapping):
            raise ValueError("rewrite response segment must be an object")
        cue_id = item.get("cue_id")
        text = item.get("text")
        if not isinstance(cue_id, str) or not isinstance(text, str):
            raise ValueError("rewrite response segment requires string cue_id and text")
        segments.append((cue_id, text))
    window_id = payload.get("window_id")
    if not isinstance(window_id, str):
        raise ValueError("rewrite response requires string window_id")
    return SemanticRewriteResponse(window_id, tuple(segments))


def _parse_review_response(response: Any) -> SemanticReviewResponse:
    if isinstance(response, SemanticReviewResponse):
        return response
    payload = _load_json_object(response)
    try:
        decision = ReviewDecision(str(payload["decision"]))
    except (KeyError, ValueError) as exc:
        raise ValueError("review response has an invalid decision") from exc
    changed_facts = payload.get("changed_facts", [])
    if not isinstance(changed_facts, list) or not all(
        isinstance(value, str) for value in changed_facts
    ):
        raise ValueError("review changed_facts must be an array of strings")
    window_id = payload.get("window_id")
    explanation = payload.get("explanation", "")
    if not isinstance(window_id, str) or not isinstance(explanation, str):
        raise ValueError("review response requires string window_id and explanation")
    return SemanticReviewResponse(
        window_id,
        decision,
        explanation,
        tuple(changed_facts),
    )


def _default_rewriter(model: str) -> SemanticRewriter:
    prompt = get_prompt("optimize/speed_repair")

    def rewrite(request: SemanticRewriteRequest) -> SemanticRewriteResponse:
        response = call_llm(
            messages=[
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": json.dumps(request.to_payload(), ensure_ascii=False),
                },
            ],
            model=model,
            temperature=0.2,
            response_format={"type": "json_object"},
            timeout=SEMANTIC_LLM_TIMEOUT_SECONDS,
        )
        return _parse_rewrite_response(response)

    return rewrite


def _default_reviewer(model: str) -> SemanticReviewer:
    prompt = get_prompt("optimize/speed_repair")

    def review(request: SemanticReviewRequest) -> SemanticReviewResponse:
        payload = {
            "schema_version": SEMANTIC_REPAIR_SCHEMA_VERSION,
            "task": "review",
            "window_id": request.window_id,
            "content_is_untrusted_data": request.content_is_untrusted_data,
            "source_segments": list(request.source_segments),
            "candidate_segments": list(request.candidate_segments),
            "deterministic_reasons": [
                {
                    "code": reason.code.value,
                    "message": reason.message,
                    "details": list(reason.details),
                }
                for reason in request.deterministic_reasons
            ],
        }
        response = call_llm(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            model=model,
            temperature=0,
            response_format={"type": "json_object"},
            timeout=SEMANTIC_LLM_TIMEOUT_SECONDS,
        )
        return _parse_review_response(response)

    return review


def _rewrite(implementation: SemanticRewriter, request: SemanticRewriteRequest) -> Any:
    if callable(implementation):
        return implementation(request)
    return implementation.rewrite(request)


def _review(implementation: SemanticReviewer, request: SemanticReviewRequest) -> Any:
    if callable(implementation):
        return implementation(request)
    return implementation.review(request)


def _candidate_feedback(
    request: SemanticRewriteRequest, response: SemanticRewriteResponse
) -> tuple[dict[str, str] | None, tuple[str, ...]]:
    errors: list[str] = []
    if response.window_id != request.window_id:
        errors.append("response window_id does not match the request")
    values: dict[str, str] = {}
    for cue_id, text in response.segments:
        if cue_id in values:
            errors.append(f"duplicate output cue_id: {cue_id}")
        values[cue_id] = text.strip()
    expected = set(request.target_cue_ids)
    actual = set(values)
    if actual != expected:
        errors.append(
            f"output cue IDs must exactly match targets; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )
    by_id = {cue.cue_id: cue for cue in request.cues}
    for cue_id in sorted(expected & actual):
        value = values[cue_id]
        cue = by_id[cue_id]
        if not value:
            errors.append(f"{cue_id}: candidate text is empty")
        if value == cue.text.strip():
            errors.append(f"{cue_id}: candidate did not repair the unresolved text")
        if cue.target_max_graphemes is not None:
            length = len(regex.findall(r"\X", value))
            if length > cue.target_max_graphemes:
                errors.append(
                    f"{cue_id}: {length} graphemes exceeds target {cue.target_max_graphemes}"
                )
    return (None, tuple(errors)) if errors else (values, ())


def _validation_feedback(result: SemanticValidationResult) -> tuple[str, ...]:
    return tuple(
        f"{reason.code.value}: {reason.message}"
        + (f" ({', '.join(reason.details)})" if reason.details else "")
        for reason in result.reasons
    )


def _apply_candidate(
    window: Sequence[SemanticRepairCue], candidate: Mapping[str, str]
) -> tuple[SemanticRepairCue, ...]:
    return tuple(
        replace(cue, text=candidate[cue.cue_id], unresolved=False)
        if cue.cue_id in candidate
        else cue
        for cue in window
    )


def _validate_candidate_window(
    *,
    window_id: str,
    before: Sequence[SemanticRepairCue],
    after: Sequence[SemanticRepairCue],
    target_ids: Sequence[str],
    minimum_literal_coverage: float,
) -> SemanticValidationResult:
    """Validate each write target so unchanged context cannot mask information loss."""

    target_set = set(target_ids)
    results = [
        validate_semantic_window(
            SemanticWindow(
                f"{window_id}:{source.cue_id}",
                (source.text,),
                (candidate.text,),
            ),
            minimum_literal_coverage=minimum_literal_coverage,
        )
        for source, candidate in zip(before, after)
        if source.cue_id in target_set
    ]
    reasons = tuple(reason for result in results for reason in result.reasons)
    if any(result.status is ValidationStatus.ROLLED_BACK for result in results):
        return SemanticValidationResult(window_id, ValidationStatus.ROLLED_BACK, reasons)
    if any(result.status is ValidationStatus.UNRESOLVED for result in results):
        return SemanticValidationResult(window_id, ValidationStatus.UNRESOLVED, reasons)
    if any(result.status is ValidationStatus.REVIEW_REQUIRED for result in results):
        review_request = SemanticReviewRequest(
            window_id=window_id,
            source_segments=tuple(cue.text for cue in before),
            candidate_segments=tuple(cue.text for cue in after),
            deterministic_reasons=reasons,
        )
        return SemanticValidationResult(
            window_id,
            ValidationStatus.REVIEW_REQUIRED,
            reasons,
            review_request,
        )
    return SemanticValidationResult(window_id, ValidationStatus.ACCEPTED)


def _record(
    *,
    window: Sequence[SemanticRepairCue],
    window_id: str,
    cache_key: str,
    status: ValidationStatus,
    history: Sequence[ValidationStatus],
    attempts: int,
    after: Sequence[SemanticRepairCue] | None = None,
    validation: SemanticValidationResult | None = None,
    feedback: Sequence[str] = (),
    from_cache: bool = False,
) -> SemanticRepairRecord:
    return SemanticRepairRecord(
        window_id=window_id,
        cue_ids=tuple(cue.cue_id for cue in window),
        target_cue_ids=tuple(cue.cue_id for cue in window if cue.unresolved),
        cache_key=cache_key,
        status=status,
        status_history=tuple(history),
        attempts=attempts,
        before=tuple(cue.text for cue in window),
        after=tuple(cue.text for cue in (after or window)),
        reasons=validation.reasons if validation else (),
        feedback=tuple(feedback),
        from_cache=from_cache,
    )


def repair_semantic_windows(
    cues: Sequence[SemanticRepairCue],
    *,
    model: str,
    reviewer_model: str | None = None,
    rewriter: SemanticRewriter | None = None,
    reviewer: SemanticReviewer | None = None,
    cache: RewriteCache | None = None,
    window_size: int = DEFAULT_WINDOW_SIZE,
    max_feedback_retries: int = MAX_FEEDBACK_RETRIES,
    minimum_literal_coverage: float = 0.55,
) -> SemanticRepairResult:
    """Repair unresolved cues as independent window transactions.

    An initial rewrite plus at most two feedback retries is permitted.  A candidate
    is committed only after deterministic validation and, when requested, an
    independent semantic review.  All other outcomes preserve the original window.
    """

    if not model.strip():
        raise ValueError("model must not be empty")
    if not 0 <= max_feedback_retries <= MAX_FEEDBACK_RETRIES:
        raise ValueError(f"max_feedback_retries must be between 0 and {MAX_FEEDBACK_RETRIES}")
    if not 0 <= minimum_literal_coverage <= 1:
        raise ValueError("minimum_literal_coverage must be between 0 and 1")
    review_model = reviewer_model or model
    rewrite_fn = rewriter or _default_rewriter(model)
    review_fn = reviewer or _default_reviewer(review_model)
    output_by_id = {cue.cue_id: cue for cue in cues}
    records: list[SemanticRepairRecord] = []

    for cue in cues:
        if not cue.protected or not cue.unresolved:
            continue
        window_id = _window_id((cue,))
        key = _cache_key(
            (cue,),
            model=model,
            reviewer_model=review_model,
            minimum_literal_coverage=minimum_literal_coverage,
        )
        records.append(
            _record(
                window=(cue,),
                window_id=window_id,
                cache_key=key,
                status=ValidationStatus.UNRESOLVED,
                history=(ValidationStatus.UNRESOLVED,),
                attempts=0,
                feedback=("protected cue is not eligible for semantic rewrite",),
            )
        )

    for original_window in build_repair_windows(cues, window_size=window_size):
        window = tuple(output_by_id[cue.cue_id] for cue in original_window)
        window_id = _window_id(window)
        key = _cache_key(
            window,
            model=model,
            reviewer_model=review_model,
            minimum_literal_coverage=minimum_literal_coverage,
        )
        target_ids = tuple(cue.cue_id for cue in window if cue.unresolved)
        feedback: list[str] = []
        history: list[ValidationStatus] = []
        last_validation: SemanticValidationResult | None = None
        cached_response = cache.get(key) if cache is not None else None

        for attempt in range(max_feedback_retries + 1):
            request = SemanticRewriteRequest(
                window_id,
                key,
                window,
                target_ids,
                attempt,
                tuple(feedback),
            )
            from_cache = attempt == 0 and cached_response is not None
            try:
                raw_response = cached_response if from_cache else _rewrite(rewrite_fn, request)
                response = _parse_rewrite_response(raw_response)
                candidate, structural_feedback = _candidate_feedback(request, response)
            except Exception as exc:  # A failed window must not stop the task.
                candidate = None
                structural_feedback = (f"rewrite_error: {type(exc).__name__}: {exc}",)

            if candidate is None:
                feedback.extend(structural_feedback)
                if attempt < max_feedback_retries:
                    continue
                history.append(ValidationStatus.UNRESOLVED)
                records.append(
                    _record(
                        window=window,
                        window_id=window_id,
                        cache_key=key,
                        status=ValidationStatus.UNRESOLVED,
                        history=history,
                        attempts=attempt + 1,
                        feedback=feedback,
                        from_cache=from_cache,
                    )
                )
                break

            candidate_window = _apply_candidate(window, candidate)
            validation = _validate_candidate_window(
                window_id=window_id,
                before=window,
                after=candidate_window,
                target_ids=target_ids,
                minimum_literal_coverage=minimum_literal_coverage,
            )
            last_validation = validation
            history.append(validation.status)
            if validation.status is ValidationStatus.REVIEW_REQUIRED:
                pending_validation = validation
                try:
                    assert validation.review_request is not None
                    review_response = _review(review_fn, validation.review_request)
                    validation = resolve_semantic_review(
                        validation, _parse_review_response(review_response)
                    )
                except Exception:
                    validation = resolve_semantic_review(validation, None)
                last_validation = (
                    pending_validation
                    if validation.status is ValidationStatus.ACCEPTED
                    else validation
                )
                history.append(validation.status)

            if validation.status is ValidationStatus.ACCEPTED:
                for cue in candidate_window:
                    output_by_id[cue.cue_id] = cue
                if cache is not None and not from_cache:
                    cache[key] = response
                records.append(
                    _record(
                        window=window,
                        window_id=window_id,
                        cache_key=key,
                        status=ValidationStatus.ACCEPTED,
                        history=history,
                        attempts=attempt + 1,
                        after=candidate_window,
                        validation=last_validation,
                        feedback=feedback,
                        from_cache=from_cache,
                    )
                )
                break

            feedback.extend(_validation_feedback(validation))
            if attempt < max_feedback_retries:
                continue
            records.append(
                _record(
                    window=window,
                    window_id=window_id,
                    cache_key=key,
                    status=validation.status,
                    history=history,
                    attempts=attempt + 1,
                    validation=last_validation,
                    feedback=feedback,
                    from_cache=from_cache,
                )
            )
            break

    return SemanticRepairResult(
        tuple(output_by_id[cue.cue_id] for cue in cues),
        tuple(records),
    )


__all__ = [
    "DEFAULT_WINDOW_SIZE",
    "MAX_FEEDBACK_RETRIES",
    "SEMANTIC_REPAIR_SCHEMA_VERSION",
    "SemanticRepairCue",
    "SemanticRepairRecord",
    "SemanticRepairResult",
    "SemanticReviewer",
    "SemanticRewriter",
    "ReviewerObject",
    "RewriterObject",
    "SemanticRewriteRequest",
    "SemanticRewriteResponse",
    "build_repair_windows",
    "repair_semantic_windows",
]
