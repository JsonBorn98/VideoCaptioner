"""Versioned timing-evidence contracts for subtitle speed optimization."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from videocaptioner.core.speed.models import ModelValidationError, canonical_sha256, make_stable_id

TIMING_EVIDENCE_SCHEMA_VERSION = 1


class TimingProvenance(str, Enum):
    SUBTITLE_INPUT = "subtitle_input"
    ESTIMATED = "estimated"
    VAD = "vad"
    FORCED_ALIGNER = "forced_aligner"
    IMPORTED = "imported"


class TimingGranularity(str, Enum):
    CUE = "cue"
    SPEECH_REGION = "speech_region"
    WORD = "word"


class TimingQualityGrade(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TimingOperation(str, Enum):
    USE_SAFE_GAP = "use_safe_gap"
    MOVE_SHARED_BOUNDARY = "move_shared_boundary"
    SPLIT_AT_PAUSE = "split_at_pause"
    MIGRATE_TEXT = "migrate_text"
    SPLIT_AT_WORD = "split_at_word"
    MERGE_CUES = "merge_cues"
    REBUILD_BOUNDARIES = "rebuild_boundaries"


QualityMetricValue = float | int | bool | str | None


@dataclass(frozen=True)
class TimingAnchor:
    anchor_id: str
    cue_id: str
    text: str
    start_ms: int
    end_ms: int
    quality_grade: TimingQualityGrade
    confidence: float | None = None

    def __post_init__(self) -> None:
        if not self.anchor_id or not self.cue_id:
            raise ModelValidationError("anchor and cue IDs must not be empty")
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ModelValidationError("anchor must have a positive time range")
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ModelValidationError("anchor confidence must be between 0 and 1")

    @classmethod
    def create(
        cls,
        *,
        cue_id: str,
        text: str,
        start_ms: int,
        end_ms: int,
        quality_grade: TimingQualityGrade,
        ordinal: int,
        confidence: float | None = None,
    ) -> TimingAnchor:
        anchor_id = make_stable_id(
            "anchor",
            {
                "cue_id": cue_id,
                "ordinal": ordinal,
                "text": text,
                "start_ms": start_ms,
                "end_ms": end_ms,
            },
        )
        return cls(anchor_id, cue_id, text, start_ms, end_ms, quality_grade, confidence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchor_id": self.anchor_id,
            "cue_id": self.cue_id,
            "text": self.text,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "quality_grade": self.quality_grade.value,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TimingAnchor:
        try:
            return cls(
                anchor_id=str(data["anchor_id"]),
                cue_id=str(data["cue_id"]),
                text=str(data.get("text", "")),
                start_ms=int(data["start_ms"]),
                end_ms=int(data["end_ms"]),
                quality_grade=TimingQualityGrade(str(data["quality_grade"])),
                confidence=(
                    float(data["confidence"]) if data.get("confidence") is not None else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelValidationError("invalid timing anchor payload") from exc


@dataclass(frozen=True)
class TimingEvidenceWindow:
    window_id: str
    cue_ids: tuple[str, ...]
    start_ms: int
    end_ms: int
    provenance: TimingProvenance
    granularity: TimingGranularity
    coverage: float
    quality_grade: TimingQualityGrade
    allowed_operations: frozenset[TimingOperation]
    anchors: tuple[TimingAnchor, ...] = ()
    quality_metrics: Mapping[str, QualityMetricValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.window_id or not self.cue_ids or any(not cue_id for cue_id in self.cue_ids):
            raise ModelValidationError("window and cue IDs must not be empty")
        if len(set(self.cue_ids)) != len(self.cue_ids):
            raise ModelValidationError("window cue IDs must be unique")
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ModelValidationError("window must have a positive time range")
        if not 0 <= self.coverage <= 1:
            raise ModelValidationError("window coverage must be between 0 and 1")
        for anchor in self.anchors:
            if anchor.cue_id not in self.cue_ids:
                raise ModelValidationError("anchor cue must belong to its evidence window")
            if anchor.start_ms < self.start_ms or anchor.end_ms > self.end_ms:
                raise ModelValidationError("anchor must stay within its evidence window")

    @classmethod
    def create(
        cls,
        *,
        cue_ids: tuple[str, ...],
        start_ms: int,
        end_ms: int,
        provenance: TimingProvenance,
        granularity: TimingGranularity,
        coverage: float,
        quality_grade: TimingQualityGrade,
        allowed_operations: frozenset[TimingOperation],
        anchors: tuple[TimingAnchor, ...] = (),
        quality_metrics: Mapping[str, QualityMetricValue] | None = None,
    ) -> TimingEvidenceWindow:
        window_id = make_stable_id(
            "timing-window",
            {"cue_ids": cue_ids, "start_ms": start_ms, "end_ms": end_ms},
        )
        return cls(
            window_id=window_id,
            cue_ids=cue_ids,
            start_ms=start_ms,
            end_ms=end_ms,
            provenance=provenance,
            granularity=granularity,
            coverage=coverage,
            quality_grade=quality_grade,
            allowed_operations=allowed_operations,
            anchors=anchors,
            quality_metrics=dict(quality_metrics or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "cue_ids": list(self.cue_ids),
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "provenance": self.provenance.value,
            "granularity": self.granularity.value,
            "coverage": self.coverage,
            "quality_grade": self.quality_grade.value,
            "allowed_operations": sorted(operation.value for operation in self.allowed_operations),
            "anchors": [anchor.to_dict() for anchor in self.anchors],
            "quality_metrics": dict(self.quality_metrics),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TimingEvidenceWindow:
        try:
            return cls(
                window_id=str(data["window_id"]),
                cue_ids=tuple(str(value) for value in data["cue_ids"]),
                start_ms=int(data["start_ms"]),
                end_ms=int(data["end_ms"]),
                provenance=TimingProvenance(str(data["provenance"])),
                granularity=TimingGranularity(str(data["granularity"])),
                coverage=float(data["coverage"]),
                quality_grade=TimingQualityGrade(str(data["quality_grade"])),
                allowed_operations=frozenset(
                    TimingOperation(str(value)) for value in data.get("allowed_operations", ())
                ),
                anchors=tuple(TimingAnchor.from_dict(value) for value in data.get("anchors", ())),
                quality_metrics=dict(data.get("quality_metrics", {})),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelValidationError("invalid timing evidence window payload") from exc


@dataclass(frozen=True)
class TimingEvidenceBundle:
    subtitle_fingerprint: str
    windows: tuple[TimingEvidenceWindow, ...]
    media_fingerprint: str | None = None
    audio_track: str | None = None
    source_language: str | None = None
    model_name: str | None = None
    model_version: str | None = None
    config_fingerprint: str | None = None
    schema_version: int = TIMING_EVIDENCE_SCHEMA_VERSION
    bundle_id: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != TIMING_EVIDENCE_SCHEMA_VERSION:
            raise ModelValidationError(
                f"unsupported timing evidence schema version: {self.schema_version}"
            )
        if not self.subtitle_fingerprint:
            raise ModelValidationError("subtitle fingerprint must not be empty")
        if self.audio_track is not None and self.media_fingerprint is None:
            raise ModelValidationError("audio track requires a media fingerprint")
        window_ids = [window.window_id for window in self.windows]
        if len(set(window_ids)) != len(window_ids):
            raise ModelValidationError("timing evidence window IDs must be unique")
        expected_id = self.calculate_bundle_id()
        if self.bundle_id and self.bundle_id != expected_id:
            raise ModelValidationError("timing evidence bundle ID does not match its content")
        if not self.bundle_id:
            object.__setattr__(self, "bundle_id", expected_id)

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "subtitle_fingerprint": self.subtitle_fingerprint,
            "media_fingerprint": self.media_fingerprint,
            "audio_track": self.audio_track,
            "source_language": self.source_language,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "config_fingerprint": self.config_fingerprint,
            "windows": [window.to_dict() for window in self.windows],
        }

    def calculate_bundle_id(self) -> str:
        return f"timing-bundle:{canonical_sha256(self._identity_payload())}"

    def to_dict(self) -> dict[str, Any]:
        return {"bundle_id": self.bundle_id, **self._identity_payload()}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TimingEvidenceBundle:
        try:
            schema_version = int(data["schema_version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelValidationError(
                "timing evidence schema version is missing or invalid"
            ) from exc
        if schema_version != TIMING_EVIDENCE_SCHEMA_VERSION:
            raise ModelValidationError(
                f"unsupported timing evidence schema version: {schema_version}"
            )
        try:
            return cls(
                schema_version=schema_version,
                bundle_id=str(data.get("bundle_id", "")),
                subtitle_fingerprint=str(data["subtitle_fingerprint"]),
                media_fingerprint=(
                    str(data["media_fingerprint"])
                    if data.get("media_fingerprint") is not None
                    else None
                ),
                audio_track=str(data["audio_track"])
                if data.get("audio_track") is not None
                else None,
                source_language=(
                    str(data["source_language"])
                    if data.get("source_language") is not None
                    else None
                ),
                model_name=str(data["model_name"]) if data.get("model_name") is not None else None,
                model_version=(
                    str(data["model_version"]) if data.get("model_version") is not None else None
                ),
                config_fingerprint=(
                    str(data["config_fingerprint"])
                    if data.get("config_fingerprint") is not None
                    else None
                ),
                windows=tuple(
                    TimingEvidenceWindow.from_dict(value) for value in data.get("windows", ())
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelValidationError("invalid timing evidence bundle payload") from exc
