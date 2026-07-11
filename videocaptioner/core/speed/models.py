"""Shared, versioned value objects for subtitle speed optimization."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

MODEL_SCHEMA_VERSION = 1


class ModelValidationError(ValueError):
    """Raised when serialized speed-model data violates its schema."""


def _canonical_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _canonical_value(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_canonical_value(item) for item in value]
        return sorted(normalized, key=lambda item: canonical_json_bytes(item))
    if isinstance(value, Path):
        return value.as_posix()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported canonical value: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize a value deterministically for hashing and persistence."""

    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    """Return the canonical SHA-256 digest for structured data."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def content_sha256(data: bytes | str) -> str:
    """Fingerprint content without incorporating its filesystem path."""

    payload = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(payload).hexdigest()


def file_content_sha256(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    """Fingerprint file bytes without incorporating name or absolute path."""

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_stable_id(kind: str, payload: Any) -> str:
    """Build a readable stable ID from immutable, canonical source data."""

    normalized_kind = kind.strip().lower().replace(" ", "-")
    if not normalized_kind:
        raise ValueError("kind must not be empty")
    return f"{normalized_kind}:{canonical_sha256(payload)}"


@dataclass(frozen=True)
class Lineage:
    """Describe how an entity was derived while preserving stable ancestry."""

    entity_id: str
    parent_ids: tuple[str, ...] = ()
    operation: str = "input"
    generation: int = 0

    def __post_init__(self) -> None:
        if not self.entity_id:
            raise ModelValidationError("lineage entity_id must not be empty")
        if not self.operation:
            raise ModelValidationError("lineage operation must not be empty")
        if self.generation < 0:
            raise ModelValidationError("lineage generation must not be negative")
        if any(not parent_id for parent_id in self.parent_ids):
            raise ModelValidationError("lineage parent IDs must not be empty")
        if len(set(self.parent_ids)) != len(self.parent_ids):
            raise ModelValidationError("lineage parent IDs must be unique")

    @classmethod
    def input(cls, entity_id: str) -> Lineage:
        return cls(entity_id=entity_id)

    @classmethod
    def derive(
        cls,
        *,
        kind: str,
        parent_ids: Sequence[str],
        operation: str,
        ordinal: int,
        payload: Any,
        parent_generation: int = 0,
    ) -> Lineage:
        if ordinal < 0:
            raise ModelValidationError("lineage ordinal must not be negative")
        ordered_parents = tuple(parent_ids)
        entity_id = make_stable_id(
            kind,
            {
                "operation": operation,
                "ordinal": ordinal,
                "parents": ordered_parents,
                "payload": payload,
            },
        )
        return cls(
            entity_id=entity_id,
            parent_ids=ordered_parents,
            operation=operation,
            generation=parent_generation + 1,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "parent_ids": list(self.parent_ids),
            "operation": self.operation,
            "generation": self.generation,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Lineage:
        try:
            return cls(
                entity_id=str(data["entity_id"]),
                parent_ids=tuple(str(value) for value in data.get("parent_ids", ())),
                operation=str(data.get("operation", "input")),
                generation=int(data.get("generation", 0)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelValidationError("invalid lineage payload") from exc


@dataclass(frozen=True)
class CueSnapshot:
    """Immutable cue identity used by analysis snapshots and lineage records."""

    cue_id: str
    index: int
    start_ms: int
    end_ms: int
    text: str
    translated_text: str = ""
    lineage: Lineage | None = None

    def __post_init__(self) -> None:
        if not self.cue_id:
            raise ModelValidationError("cue_id must not be empty")
        if self.index < 0:
            raise ModelValidationError("cue index must not be negative")
        if self.end_ms < self.start_ms:
            raise ModelValidationError("cue end_ms must not precede start_ms")
        if self.lineage is not None and self.lineage.entity_id != self.cue_id:
            raise ModelValidationError("cue lineage must describe the cue ID")

    @classmethod
    def from_input(
        cls,
        *,
        index: int,
        start_ms: int,
        end_ms: int,
        text: str,
        translated_text: str = "",
    ) -> CueSnapshot:
        identity_payload = {
            "index": index,
            "text": text,
        }
        cue_id = make_stable_id("cue", identity_payload)
        return cls(
            cue_id=cue_id,
            index=index,
            start_ms=start_ms,
            end_ms=end_ms,
            text=text,
            translated_text=translated_text,
            lineage=Lineage.input(cue_id),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "cue_id": self.cue_id,
            "index": self.index,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "text": self.text,
            "translated_text": self.translated_text,
            "lineage": self.lineage.to_dict() if self.lineage else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CueSnapshot:
        try:
            lineage_data = data.get("lineage")
            return cls(
                cue_id=str(data["cue_id"]),
                index=int(data["index"]),
                start_ms=int(data["start_ms"]),
                end_ms=int(data["end_ms"]),
                text=str(data.get("text", "")),
                translated_text=str(data.get("translated_text", "")),
                lineage=Lineage.from_dict(lineage_data) if lineage_data else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelValidationError("invalid cue snapshot payload") from exc
