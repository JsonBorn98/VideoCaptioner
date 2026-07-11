import json
from pathlib import Path

import pytest

from videocaptioner.core.speed.timing_archive import (
    TimingArchiveFingerprintMismatch,
    UnsupportedTimingArchiveVersion,
    read_timing_archive,
    timing_sidecar_path,
    write_timing_archive,
)
from videocaptioner.core.speed.timing_evidence import TimingEvidenceBundle


def test_timing_cache_key_is_content_based_and_stable():
    from videocaptioner.core.speed.timing_archive import timing_cache_key

    assert timing_cache_key("sub", "media", "cfg") == timing_cache_key("sub", "media", "cfg")
    assert timing_cache_key("sub", "media", "cfg") != timing_cache_key("sub-2", "media", "cfg")


def _bundle() -> TimingEvidenceBundle:
    return TimingEvidenceBundle(
        subtitle_fingerprint="subtitle-hash",
        media_fingerprint="media-hash",
        windows=(),
    )


def test_sidecar_path_replaces_subtitle_suffix() -> None:
    assert timing_sidecar_path("movie.zh.srt") == Path("movie.zh.vctiming.json")


def test_archive_atomic_round_trip_has_no_temporary_file(tmp_path: Path) -> None:
    destination = tmp_path / "movie.vctiming.json"

    assert write_timing_archive(destination, _bundle()) == destination

    restored = read_timing_archive(
        destination,
        expected_subtitle_fingerprint="subtitle-hash",
        expected_media_fingerprint="media-hash",
    )
    assert restored == _bundle()
    assert list(tmp_path.glob("*.tmp")) == []


def test_archive_rejects_unknown_version_before_deserialization(tmp_path: Path) -> None:
    destination = tmp_path / "future.vctiming.json"
    data = _bundle().to_dict()
    data["schema_version"] = 2
    destination.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(UnsupportedTimingArchiveVersion, match="version"):
        read_timing_archive(destination)


def test_archive_rejects_fingerprint_mismatch(tmp_path: Path) -> None:
    destination = write_timing_archive(tmp_path / "movie.vctiming.json", _bundle())

    with pytest.raises(TimingArchiveFingerprintMismatch, match="subtitle"):
        read_timing_archive(destination, expected_subtitle_fingerprint="different")

    with pytest.raises(TimingArchiveFingerprintMismatch, match="media"):
        read_timing_archive(destination, expected_media_fingerprint="different")
