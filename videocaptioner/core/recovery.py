"""Shared, module-neutral contracts and storage for resumable work checkpoints."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

RECOVERY_MANIFEST_SCHEMA = "videocaptioner.recovery_manifest"
RECOVERY_MANIFEST_VERSION = 1


class RecoveryDecision(str, Enum):
    """The choice made when a matching unfinished checkpoint is discovered."""

    CONTINUE = "continue"
    START_FRESH = "start_fresh"


@dataclass(frozen=True)
class RecoverySummary:
    """Safe, module-neutral information presented before resuming work.

    ``identity`` and ``completed`` deliberately contain only durable identifiers and
    counts. Prompt content, model responses, and connection credentials never
    belong in a recovery prompt.
    """

    module: str
    identity: Mapping[str, str]
    completed: Mapping[str, int]
    checkpoint_time: str
    configuration_drift: tuple[str, ...] = ()


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def software_version() -> str:
    """Return the installed application version without making recovery fail."""

    try:
        from videocaptioner._version import version
    except InterruptedError:
        raise
    except Exception:  # noqa: BLE001 — version module is generated and may be absent
        return "unknown"
    return str(version or "unknown")


def atomic_write_json(path: str | Path, payload: Mapping[str, Any]) -> Path:
    """Atomically replace a canonical JSON document via a sibling temp file.

    This is the one atomic-write shape shared by recovery-adjacent persisted
    files (recovery manifest, project glossary, translation brief): fsync the
    temporary file before ``os.replace`` so a reader never sees a half-written
    document, and never trust a temp file that survived an error.
    """

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = (
        json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return destination


def write_recovery_manifest(path: str | Path, manifest: Mapping[str, Any]) -> Path:
    """Atomically replace a manifest after its corresponding data file is durable."""

    return atomic_write_json(path, manifest)


def load_recovery_manifest(path: str | Path) -> dict[str, Any] | None:
    """Return a JSON-object manifest, treating absent or corrupted data as unusable."""

    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except InterruptedError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None
