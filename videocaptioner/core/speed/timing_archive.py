"""Atomic persistence for versioned timing-evidence sidecars."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from videocaptioner.core.speed.models import ModelValidationError, canonical_json_bytes
from videocaptioner.core.speed.timing_evidence import (
    TIMING_EVIDENCE_SCHEMA_VERSION,
    TimingEvidenceBundle,
)
from videocaptioner.core.utils.cache import get_timing_cache, is_cache_enabled

TIMING_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60


class TimingArchiveError(ValueError):
    """Base error for invalid or incompatible timing archives."""


class UnsupportedTimingArchiveVersion(TimingArchiveError):
    """Raised when an archive does not use the exact supported schema."""


class TimingArchiveFingerprintMismatch(TimingArchiveError):
    """Raised when an archive belongs to different subtitle or media content."""


def timing_cache_key(
    subtitle_fingerprint: str,
    media_fingerprint: str,
    config_fingerprint: str,
) -> str:
    from videocaptioner.core.speed.models import canonical_sha256

    return "timing:" + canonical_sha256(
        {
            "schema_version": TIMING_EVIDENCE_SCHEMA_VERSION,
            "subtitle": subtitle_fingerprint,
            "media": media_fingerprint,
            "config": config_fingerprint,
        }
    )


def cache_timing_bundle(bundle: TimingEvidenceBundle) -> None:
    if not is_cache_enabled() or not bundle.media_fingerprint:
        return
    key = timing_cache_key(
        bundle.subtitle_fingerprint,
        bundle.media_fingerprint,
        bundle.config_fingerprint or "",
    )
    get_timing_cache().set(key, bundle.to_dict(), expire=TIMING_CACHE_TTL_SECONDS)


def read_cached_timing_bundle(
    subtitle_fingerprint: str,
    media_fingerprint: str,
    config_fingerprint: str,
) -> TimingEvidenceBundle | None:
    if not is_cache_enabled():
        return None
    key = timing_cache_key(subtitle_fingerprint, media_fingerprint, config_fingerprint)
    payload = get_timing_cache().get(key)
    if not isinstance(payload, dict):
        return None
    try:
        return TimingEvidenceBundle.from_dict(payload)
    except ModelValidationError:
        get_timing_cache().delete(key)
        return None


def timing_sidecar_path(subtitle_path: str | Path) -> Path:
    """Return ``<subtitle stem>.vctiming.json`` beside the subtitle."""

    return Path(subtitle_path).with_suffix(".vctiming.json")


def write_timing_archive(path: str | Path, bundle: TimingEvidenceBundle) -> Path:
    """Atomically persist a timing bundle in its canonical JSON representation."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_bytes(bundle.to_dict()) + b"\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, destination)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return destination


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TimingArchiveError(f"cannot read timing archive: {path}") from exc
    if not isinstance(value, dict):
        raise TimingArchiveError("timing archive root must be a JSON object")
    return value


def read_timing_archive(
    path: str | Path,
    *,
    expected_subtitle_fingerprint: str | None = None,
    expected_media_fingerprint: str | None = None,
) -> TimingEvidenceBundle:
    """Load an exact-version archive and optionally verify content fingerprints."""

    source = Path(path)
    data = _load_json_object(source)
    version = data.get("schema_version")
    if type(version) is not int or version != TIMING_EVIDENCE_SCHEMA_VERSION:
        raise UnsupportedTimingArchiveVersion(
            f"unsupported timing archive schema version: {version!r}; "
            f"expected {TIMING_EVIDENCE_SCHEMA_VERSION}"
        )
    try:
        bundle = TimingEvidenceBundle.from_dict(data)
    except ModelValidationError as exc:
        raise TimingArchiveError("invalid timing archive payload") from exc
    if (
        expected_subtitle_fingerprint is not None
        and bundle.subtitle_fingerprint != expected_subtitle_fingerprint
    ):
        raise TimingArchiveFingerprintMismatch("subtitle fingerprint does not match archive")
    if (
        expected_media_fingerprint is not None
        and bundle.media_fingerprint != expected_media_fingerprint
    ):
        raise TimingArchiveFingerprintMismatch("media fingerprint does not match archive")
    return bundle
