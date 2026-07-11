"""Windowed forced-alignment adapter for subtitle timing evidence.

This module deliberately does not own task orchestration or model lifecycle.  It
turns immutable source cues into bounded alignment requests and translates the
responses into the timing-evidence contract used by the speed optimizer.
"""

from __future__ import annotations

import re
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence

from .models import CueSnapshot
from .timing_evidence import (
    TimingAnchor,
    TimingEvidenceWindow,
    TimingGranularity,
    TimingOperation,
    TimingProvenance,
    TimingQualityGrade,
)

DEFAULT_ALIGNER_MODEL = "Qwen/Qwen3-ForcedAligner-0.6B"
ALIGNMENT_ADAPTER_VERSION = 1
_SUPPORTED_LANGUAGES = frozenset(
    {"zh", "en", "yue", "fr", "de", "it", "ja", "ko", "pt", "ru", "es"}
)
_LANGUAGE_ALIASES = {
    "chinese": "zh",
    "english": "en",
    "cantonese": "yue",
    "french": "fr",
    "german": "de",
    "italian": "it",
    "japanese": "ja",
    "korean": "ko",
    "portuguese": "pt",
    "russian": "ru",
    "spanish": "es",
}
_ALIGNMENT_CHARACTER = re.compile(r"[\w\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", re.UNICODE)


class AlignerCallable(Protocol):
    def __call__(self, **kwargs: Any) -> list[dict[str, float | str]]: ...


MediaProbe = Callable[[str], Any]


@dataclass(frozen=True)
class AlignmentConfig:
    """Boundaries and quality thresholds for precise-timing requests."""

    target_window_ms: int = 45_000
    max_window_ms: int = 90_000
    padding_ms: int = 1_500
    hard_reset_ms: int = 1_500
    high_coverage: float = 0.90
    medium_coverage: float = 0.70
    high_max_out_of_bounds_ratio: float = 0.0
    medium_max_out_of_bounds_ratio: float = 0.05

    def __post_init__(self) -> None:
        if self.target_window_ms <= 0 or self.max_window_ms < self.target_window_ms:
            raise ValueError("alignment window bounds must be positive and ordered")
        if self.padding_ms < 0 or self.hard_reset_ms <= 0:
            raise ValueError("alignment padding must be non-negative and reset positive")
        if not 0 <= self.medium_coverage <= self.high_coverage <= 1:
            raise ValueError("alignment coverage thresholds must be ordered within [0, 1]")
        if not 0 <= self.high_max_out_of_bounds_ratio <= self.medium_max_out_of_bounds_ratio <= 1:
            raise ValueError("out-of-bounds thresholds must be ordered within [0, 1]")


@dataclass(frozen=True)
class AlignmentPreflight:
    eligible: bool
    issues: tuple[str, ...]
    media_duration_ms: int | None
    audio_track_index: int


@dataclass(frozen=True)
class AlignmentWindowPlan:
    cue_ids: tuple[str, ...]
    cue_start_ms: int
    cue_end_ms: int
    clip_start_ms: int
    clip_end_ms: int
    transcript: str

    @property
    def clip_duration_ms(self) -> int:
        return self.clip_end_ms - self.clip_start_ms


@dataclass(frozen=True)
class AlignmentRunResult:
    preflight: AlignmentPreflight
    plans: tuple[AlignmentWindowPlan, ...]
    windows: tuple[TimingEvidenceWindow, ...]
    failed_window_ids: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()


def alignment_config_fingerprint(
    source_language: str,
    *,
    audio_track_index: int = 0,
    aligner_model: str = DEFAULT_ALIGNER_MODEL,
    config: AlignmentConfig | None = None,
) -> str:
    """Fingerprint every setting that can change timing evidence or quality."""

    from .models import canonical_sha256

    return canonical_sha256(
        {
            "adapter_version": ALIGNMENT_ADAPTER_VERSION,
            "source_language": _normalize_language(source_language),
            "audio_track_index": audio_track_index,
            "aligner_model": aligner_model,
            "config": asdict(config or AlignmentConfig()),
        }
    )


def _normalize_language(language: str) -> str:
    value = (language or "").strip().lower().replace("_", "-")
    value = _LANGUAGE_ALIASES.get(value, value)
    return value.split("-", 1)[0]


def _default_media_probe(media_path: str) -> Any:
    from videocaptioner.core.utils.video_utils import get_video_info

    return get_video_info(media_path)


def preflight_alignment(
    media_path: str,
    cues: Sequence[CueSnapshot],
    source_language: str,
    *,
    audio_track_index: int = 0,
    media_probe: MediaProbe | None = None,
) -> AlignmentPreflight:
    """Check all inputs required before a model or worker is started."""

    issues: list[str] = []
    path = Path(media_path).expanduser() if media_path else None
    if path is None or not path.is_file():
        issues.append("media file does not exist")
    if not cues:
        issues.append("subtitle has no cues")
    elif not any(cue.text.strip() for cue in cues):
        issues.append("subtitle has no source-language text")
    elif any(cue.end_ms <= cue.start_ms for cue in cues):
        issues.append("subtitle contains an invalid cue time range")

    normalized_language = _normalize_language(source_language)
    if normalized_language not in _SUPPORTED_LANGUAGES:
        issues.append("source language is not supported by the forced aligner")
    if audio_track_index < 0:
        issues.append("audio track index must not be negative")

    duration_ms: int | None = None
    if path is not None and path.is_file() and audio_track_index >= 0:
        try:
            info = (media_probe or _default_media_probe)(str(path))
        except Exception as exc:
            issues.append(f"media probe failed: {exc}")
        else:
            if info is None:
                issues.append("media probe found no readable stream")
            else:
                audio_streams = getattr(info, "audio_streams", ()) or ()
                if audio_track_index >= len(audio_streams):
                    issues.append("selected audio track does not exist")
                duration_seconds = getattr(info, "duration_seconds", 0.0) or 0.0
                if duration_seconds > 0:
                    duration_ms = round(float(duration_seconds) * 1000)
                    if any(cue.end_ms > duration_ms for cue in cues):
                        issues.append("subtitle extends beyond media duration")

    return AlignmentPreflight(not issues, tuple(issues), duration_ms, audio_track_index)


def _split_reset_groups(cues: Sequence[CueSnapshot], hard_reset_ms: int) -> list[list[CueSnapshot]]:
    groups: list[list[CueSnapshot]] = []
    for cue in sorted(cues, key=lambda item: (item.start_ms, item.end_ms, item.index)):
        if groups and cue.start_ms - groups[-1][-1].end_ms >= hard_reset_ms:
            groups.append([])
        if not groups:
            groups.append([])
        groups[-1].append(cue)
    return groups


def _split_group_at_gaps(
    cues: Sequence[CueSnapshot], config: AlignmentConfig
) -> list[list[CueSnapshot]]:
    chunks: list[list[CueSnapshot]] = []
    start = 0
    while start < len(cues):
        last_within_max = start
        candidates: list[tuple[int, int]] = []
        for end in range(start, len(cues)):
            span = cues[end].end_ms - cues[start].start_ms
            if span > config.max_window_ms and end > start:
                break
            last_within_max = end
            if end < len(cues) - 1:
                candidates.append((abs(span - config.target_window_ms), end))
            if span >= config.target_window_ms:
                break

        if last_within_max == len(cues) - 1:
            split_end = last_within_max
        elif candidates:
            # A larger inter-cue gap wins ties, then the earlier boundary.
            split_end = min(
                candidates,
                key=lambda item: (
                    item[0],
                    -(cues[item[1] + 1].start_ms - cues[item[1]].end_ms),
                    item[1],
                ),
            )[1]
        else:
            split_end = last_within_max
        chunks.append(list(cues[start : split_end + 1]))
        start = split_end + 1
    return chunks


def plan_alignment_windows(
    cues: Sequence[CueSnapshot],
    *,
    media_duration_ms: int | None = None,
    config: AlignmentConfig | None = None,
) -> tuple[AlignmentWindowPlan, ...]:
    """Make deterministic cue-gap windows without crossing hard resets."""

    selected = config or AlignmentConfig()
    plans: list[AlignmentWindowPlan] = []
    nonempty = [cue for cue in cues if cue.text.strip() and cue.end_ms > cue.start_ms]
    for reset_group in _split_reset_groups(nonempty, selected.hard_reset_ms):
        for chunk in _split_group_at_gaps(reset_group, selected):
            cue_start = chunk[0].start_ms
            cue_end = chunk[-1].end_ms
            clip_start = max(0, cue_start - selected.padding_ms)
            clip_end = cue_end + selected.padding_ms
            if media_duration_ms is not None:
                clip_end = min(media_duration_ms, clip_end)
            clip_end = max(clip_start + 1, clip_end)
            plans.append(
                AlignmentWindowPlan(
                    cue_ids=tuple(cue.cue_id for cue in chunk),
                    cue_start_ms=cue_start,
                    cue_end_ms=cue_end,
                    clip_start_ms=clip_start,
                    clip_end_ms=clip_end,
                    transcript="\n".join(cue.text.strip() for cue in chunk),
                )
            )
    return tuple(plans)


def _alignment_text(text: str) -> str:
    return "".join(
        character.casefold() for character in text if _ALIGNMENT_CHARACTER.match(character)
    )


def _timestamp_ms(item: Mapping[str, Any], key: str) -> int:
    value = item.get(key, item.get(key.removesuffix("_time"), 0))
    return round(float(value) * 1000)


def _allowed_operations(grade: TimingQualityGrade) -> frozenset[TimingOperation]:
    if grade is TimingQualityGrade.HIGH:
        return frozenset(TimingOperation)
    if grade is TimingQualityGrade.MEDIUM:
        return frozenset(
            {
                TimingOperation.USE_SAFE_GAP,
                TimingOperation.MOVE_SHARED_BOUNDARY,
                TimingOperation.SPLIT_AT_PAUSE,
                TimingOperation.SPLIT_AT_WORD,
                TimingOperation.MERGE_CUES,
            }
        )
    return frozenset({TimingOperation.USE_SAFE_GAP})


def _subtitle_fallback(
    plan: AlignmentWindowPlan, cue_by_id: Mapping[str, CueSnapshot], reason: str
) -> TimingEvidenceWindow:
    anchors = tuple(
        TimingAnchor.create(
            cue_id=cue_id,
            text=cue_by_id[cue_id].text,
            start_ms=cue_by_id[cue_id].start_ms,
            end_ms=cue_by_id[cue_id].end_ms,
            quality_grade=TimingQualityGrade.LOW,
            ordinal=index,
        )
        for index, cue_id in enumerate(plan.cue_ids)
    )
    return TimingEvidenceWindow.create(
        cue_ids=plan.cue_ids,
        start_ms=plan.clip_start_ms,
        end_ms=plan.clip_end_ms,
        provenance=TimingProvenance.SUBTITLE_INPUT,
        granularity=TimingGranularity.CUE,
        coverage=1.0,
        quality_grade=TimingQualityGrade.LOW,
        allowed_operations=_allowed_operations(TimingQualityGrade.LOW),
        anchors=anchors,
        quality_metrics={"fallback": True, "failure_reason": reason},
    )


def _evidence_from_items(
    plan: AlignmentWindowPlan,
    cues: Sequence[CueSnapshot],
    items: Sequence[Mapping[str, Any]],
    config: AlignmentConfig,
) -> TimingEvidenceWindow | None:
    transcript = _alignment_text(plan.transcript)
    if not transcript or not items:
        return None

    cue_spans: list[tuple[int, int, CueSnapshot]] = []
    offset = 0
    for cue in cues:
        text = _alignment_text(cue.text)
        cue_spans.append((offset, offset + len(text), cue))
        offset += len(text)

    cursor = 0
    covered = 0
    out_of_bounds = 0
    monotonic = True
    previous_end = -1
    anchors: list[TimingAnchor] = []
    for item in items:
        item_text = str(item.get("text") or item.get("word") or "").strip()
        normalized = _alignment_text(item_text)
        if not normalized:
            continue
        found = transcript.find(normalized, cursor)
        if found < 0:
            continue
        item_start = plan.clip_start_ms + _timestamp_ms(item, "start_time")
        item_end = plan.clip_start_ms + _timestamp_ms(item, "end_time")
        if item_end <= item_start:
            continue
        if item_start < previous_end:
            monotonic = False
        previous_end = item_end
        if item_start < plan.clip_start_ms or item_end > plan.clip_end_ms:
            out_of_bounds += 1
        clamped_start = max(plan.clip_start_ms, item_start)
        clamped_end = min(plan.clip_end_ms, item_end)
        if clamped_end <= clamped_start:
            continue

        midpoint = found + len(normalized) / 2
        owner = next(
            (cue for start, end, cue in cue_spans if start <= midpoint < end),
            cue_spans[-1][2],
        )
        anchors.append(
            TimingAnchor.create(
                cue_id=owner.cue_id,
                text=item_text,
                start_ms=clamped_start,
                end_ms=clamped_end,
                quality_grade=TimingQualityGrade.HIGH,
                ordinal=len(anchors),
            )
        )
        cursor = found + len(normalized)
        covered += len(normalized)

    if not anchors:
        return None
    coverage = min(1.0, covered / len(transcript))
    out_of_bounds_ratio = out_of_bounds / max(len(items), 1)
    aligned_span = anchors[-1].end_ms - anchors[0].start_ms
    cue_span = max(1, plan.cue_end_ms - plan.cue_start_ms)
    span_ratio = aligned_span / cue_span
    if (
        coverage >= config.high_coverage
        and monotonic
        and out_of_bounds_ratio <= config.high_max_out_of_bounds_ratio
        and 0.5 <= span_ratio <= 1.5
    ):
        grade = TimingQualityGrade.HIGH
    elif (
        coverage >= config.medium_coverage
        and monotonic
        and out_of_bounds_ratio <= config.medium_max_out_of_bounds_ratio
        and 0.35 <= span_ratio <= 2.0
    ):
        grade = TimingQualityGrade.MEDIUM
    else:
        grade = TimingQualityGrade.LOW

    anchors = [
        TimingAnchor(
            anchor.anchor_id,
            anchor.cue_id,
            anchor.text,
            anchor.start_ms,
            anchor.end_ms,
            grade,
            anchor.confidence,
        )
        for anchor in anchors
    ]
    return TimingEvidenceWindow.create(
        cue_ids=plan.cue_ids,
        start_ms=plan.clip_start_ms,
        end_ms=plan.clip_end_ms,
        provenance=TimingProvenance.FORCED_ALIGNER,
        granularity=TimingGranularity.WORD,
        coverage=coverage,
        quality_grade=grade,
        allowed_operations=_allowed_operations(grade),
        anchors=tuple(anchors),
        quality_metrics={
            "monotonic": monotonic,
            "out_of_bounds_ratio": out_of_bounds_ratio,
            "span_ratio": span_ratio,
            "matched_anchor_count": len(anchors),
        },
    )


def _default_aligner(**kwargs: Any) -> list[dict[str, float | str]]:
    from videocaptioner.core.asr.qwen_local_asr import run_qwen_alignment_worker

    return run_qwen_alignment_worker(**kwargs)


@contextmanager
def _selected_audio_source(media_path: str, audio_track_index: int) -> Iterator[str]:
    """Materialize a non-default audio track once for all alignment windows."""

    if audio_track_index == 0:
        yield media_path
        return

    from videocaptioner.core.utils.video_utils import video2audio

    with tempfile.TemporaryDirectory(prefix="videocaptioner-align-") as temp_dir:
        audio_path = str(Path(temp_dir) / "selected-track.wav")
        if not video2audio(media_path, audio_path, audio_track_index=audio_track_index):
            raise RuntimeError("failed to extract the selected audio track")
        yield audio_path


def align_timing_windows(
    media_path: str,
    cues: Sequence[CueSnapshot],
    source_language: str,
    *,
    audio_track_index: int = 0,
    aligner: AlignerCallable | None = None,
    media_probe: MediaProbe | None = None,
    config: AlignmentConfig | None = None,
    aligner_model: str = DEFAULT_ALIGNER_MODEL,
    aligner_options: Mapping[str, Any] | None = None,
) -> AlignmentRunResult:
    """Align each independent window, degrading failures locally to cue timing."""

    selected = config or AlignmentConfig()
    preflight = preflight_alignment(
        media_path,
        cues,
        source_language,
        audio_track_index=audio_track_index,
        media_probe=media_probe,
    )
    if not preflight.eligible:
        return AlignmentRunResult(preflight, (), (), issues=preflight.issues)

    plans = plan_alignment_windows(
        cues,
        media_duration_ms=preflight.media_duration_ms,
        config=selected,
    )
    cue_by_id = {cue.cue_id: cue for cue in cues}
    callable_aligner = aligner or _default_aligner
    options = dict(aligner_options or {})
    windows: list[TimingEvidenceWindow] = []
    failures: list[str] = []
    issues: list[str] = []
    try:
        audio_source_context = (
            _selected_audio_source(media_path, audio_track_index)
            if aligner is None
            else _selected_audio_source(media_path, 0)
        )
        with audio_source_context as audio_source:
            for plan in plans:
                try:
                    items = callable_aligner(
                        audio_input=audio_source,
                        transcript=plan.transcript,
                        language=source_language,
                        aligner_model=aligner_model,
                        clip_start_ms=plan.clip_start_ms,
                        clip_duration_ms=plan.clip_duration_ms,
                        **options,
                    )
                    evidence = _evidence_from_items(
                        plan,
                        [cue_by_id[cue_id] for cue_id in plan.cue_ids],
                        items,
                        selected,
                    )
                    if evidence is None:
                        raise ValueError("aligner returned no usable timestamps")
                except Exception as exc:
                    evidence = _subtitle_fallback(plan, cue_by_id, str(exc))
                    failures.append(evidence.window_id)
                    issues.append(f"{evidence.window_id}: {exc}")
                windows.append(evidence)
    except Exception as exc:
        for plan in plans:
            evidence = _subtitle_fallback(plan, cue_by_id, str(exc))
            windows.append(evidence)
            failures.append(evidence.window_id)
            issues.append(f"{evidence.window_id}: {exc}")

    return AlignmentRunResult(
        preflight=preflight,
        plans=plans,
        windows=tuple(windows),
        failed_window_ids=tuple(failures),
        issues=tuple(issues),
    )


def load_or_align_timing(
    subtitle_path: str,
    media_path: str,
    cues: Sequence[CueSnapshot],
    source_language: str,
    *,
    audio_track_index: int = 0,
    aligner: AlignerCallable | None = None,
    media_probe: MediaProbe | None = None,
    config: AlignmentConfig | None = None,
    aligner_model: str = DEFAULT_ALIGNER_MODEL,
    aligner_options: Mapping[str, Any] | None = None,
):
    """Resolve precise timing from input sidecar, app cache, or fresh alignment."""

    from .models import canonical_sha256, file_content_sha256
    from .timing_archive import (
        cache_timing_bundle,
        read_cached_timing_bundle,
        read_timing_archive,
        timing_sidecar_path,
    )
    from .timing_evidence import TimingEvidenceBundle

    subtitle_fingerprint = canonical_sha256(
        [{"cue_id": cue.cue_id, "text": cue.text} for cue in cues]
    )
    media_fingerprint = file_content_sha256(media_path)
    config_fingerprint = alignment_config_fingerprint(
        source_language,
        audio_track_index=audio_track_index,
        aligner_model=aligner_model,
        config=config,
    )
    sidecar = timing_sidecar_path(subtitle_path)
    if sidecar.exists():
        try:
            archived = read_timing_archive(
                sidecar,
                expected_subtitle_fingerprint=subtitle_fingerprint,
                expected_media_fingerprint=media_fingerprint,
            )
        except ValueError:
            archived = None
        if archived is not None and archived.config_fingerprint == config_fingerprint:
            return archived, (), True

    cached = read_cached_timing_bundle(
        subtitle_fingerprint,
        media_fingerprint,
        config_fingerprint,
    )
    if cached is not None:
        return cached, (), True

    alignment = align_timing_windows(
        media_path,
        cues,
        source_language,
        audio_track_index=audio_track_index,
        aligner=aligner,
        media_probe=media_probe,
        config=config,
        aligner_model=aligner_model,
        aligner_options=aligner_options,
    )
    if not alignment.windows:
        return None, alignment.issues, False
    bundle = TimingEvidenceBundle(
        subtitle_fingerprint=subtitle_fingerprint,
        media_fingerprint=media_fingerprint,
        audio_track=str(audio_track_index),
        source_language=source_language,
        model_name=aligner_model,
        config_fingerprint=config_fingerprint,
        windows=alignment.windows,
    )
    if not alignment.failed_window_ids:
        cache_timing_bundle(bundle)
    return bundle, alignment.issues, False
