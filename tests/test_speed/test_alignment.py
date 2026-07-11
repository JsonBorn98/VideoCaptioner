from types import SimpleNamespace

from videocaptioner.core.speed.alignment import (
    AlignmentConfig,
    align_timing_windows,
    load_or_align_timing,
    plan_alignment_windows,
    preflight_alignment,
)
from videocaptioner.core.speed.models import CueSnapshot
from videocaptioner.core.speed.timing_archive import (
    timing_sidecar_path,
    write_timing_archive,
)
from videocaptioner.core.speed.timing_evidence import (
    TimingGranularity,
    TimingOperation,
    TimingProvenance,
    TimingQualityGrade,
)


def _cue(index: int, start_ms: int, end_ms: int, text: str) -> CueSnapshot:
    return CueSnapshot.from_input(
        index=index,
        start_ms=start_ms,
        end_ms=end_ms,
        text=text,
    )


def _probe(*, duration_seconds: float = 120.0, tracks: int = 1):
    return lambda _path: SimpleNamespace(
        duration_seconds=duration_seconds,
        audio_streams=[object() for _ in range(tracks)],
    )


def test_preflight_checks_media_language_source_text_and_audio_track(tmp_path) -> None:
    media = tmp_path / "movie.mp4"
    media.write_bytes(b"media")
    cue = _cue(0, 0, 1000, "hello")

    valid = preflight_alignment(
        str(media), [cue], "en", audio_track_index=1, media_probe=_probe(tracks=2)
    )
    invalid = preflight_alignment(
        str(media),
        [_cue(0, 0, 1000, "")],
        "th",
        audio_track_index=2,
        media_probe=_probe(tracks=1),
    )

    assert valid.eligible
    assert valid.media_duration_ms == 120_000
    assert not invalid.eligible
    assert "subtitle has no source-language text" in invalid.issues
    assert "source language is not supported by the forced aligner" in invalid.issues
    assert "selected audio track does not exist" in invalid.issues


def test_window_planning_targets_45_seconds_and_never_crosses_hard_reset() -> None:
    cues = [
        _cue(index, index * 10_000, index * 10_000 + 9_000, f"cue {index}") for index in range(6)
    ]
    cues.append(_cue(6, 70_000, 79_000, "after reset"))

    plans = plan_alignment_windows(cues, media_duration_ms=80_000)

    assert [len(plan.cue_ids) for plan in plans] == [5, 1, 1]
    assert all(plan.cue_end_ms - plan.cue_start_ms <= 90_000 for plan in plans)
    assert plans[0].clip_start_ms == 0
    assert plans[-1].clip_end_ms == 80_000
    assert cues[5].cue_id not in plans[-1].cue_ids


def test_successful_alignment_produces_absolute_high_quality_word_evidence(tmp_path) -> None:
    media = tmp_path / "movie.mp4"
    media.write_bytes(b"media")
    cues = [_cue(0, 10_000, 11_000, "hello"), _cue(1, 11_000, 12_000, "world")]
    calls = []

    def aligner(**kwargs):
        calls.append(kwargs)
        return [
            {"text": "hello", "start_time": 1.5, "end_time": 2.5},
            {"text": "world", "start_time": 2.5, "end_time": 3.5},
        ]

    result = align_timing_windows(str(media), cues, "en", aligner=aligner, media_probe=_probe())

    assert not result.failed_window_ids
    assert len(result.windows) == 1
    evidence = result.windows[0]
    assert evidence.provenance is TimingProvenance.FORCED_ALIGNER
    assert evidence.granularity is TimingGranularity.WORD
    assert evidence.quality_grade is TimingQualityGrade.HIGH
    assert evidence.coverage == 1.0
    assert evidence.anchors[0].start_ms == 10_000
    assert TimingOperation.REBUILD_BOUNDARIES in evidence.allowed_operations
    assert calls[0]["clip_start_ms"] == 8_500
    assert calls[0]["clip_duration_ms"] == 5_000


def test_partial_alignment_is_medium_and_has_restricted_operations(tmp_path) -> None:
    media = tmp_path / "movie.mp4"
    media.write_bytes(b"media")
    cues = [_cue(0, 0, 1000, "hello"), _cue(1, 1000, 2000, "world")]

    def aligner(**_kwargs):
        return [
            {"text": "hello", "start_time": 0.0, "end_time": 0.8},
            {"text": "wo", "start_time": 0.8, "end_time": 1.6},
        ]

    result = align_timing_windows(
        str(media), cues, "English", aligner=aligner, media_probe=_probe()
    )

    evidence = result.windows[0]
    assert evidence.quality_grade is TimingQualityGrade.MEDIUM
    assert evidence.coverage == 0.7
    assert TimingOperation.SPLIT_AT_WORD in evidence.allowed_operations
    assert TimingOperation.REBUILD_BOUNDARIES not in evidence.allowed_operations


def test_window_failure_falls_back_locally_and_later_windows_continue(tmp_path) -> None:
    media = tmp_path / "movie.mp4"
    media.write_bytes(b"media")
    cues = [_cue(0, 0, 1000, "first"), _cue(1, 3000, 4000, "second")]
    calls = 0

    def aligner(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("model failed")
        return [{"text": "second", "start_time": 1.5, "end_time": 2.5}]

    result = align_timing_windows(str(media), cues, "en", aligner=aligner, media_probe=_probe())

    assert calls == 2
    assert len(result.failed_window_ids) == 1
    failed, successful = result.windows
    assert failed.provenance is TimingProvenance.SUBTITLE_INPUT
    assert failed.granularity is TimingGranularity.CUE
    assert failed.quality_grade is TimingQualityGrade.LOW
    assert failed.allowed_operations == frozenset({TimingOperation.USE_SAFE_GAP})
    assert failed.quality_metrics["fallback"] is True
    assert successful.provenance is TimingProvenance.FORCED_ALIGNER


def test_unusable_alignment_result_also_uses_subtitle_fallback(tmp_path) -> None:
    media = tmp_path / "movie.mp4"
    media.write_bytes(b"media")
    cue = _cue(0, 0, 1000, "hello")

    result = align_timing_windows(
        str(media),
        [cue],
        "en",
        aligner=lambda **_kwargs: [],
        media_probe=_probe(),
        config=AlignmentConfig(),
    )

    assert result.windows[0].provenance is TimingProvenance.SUBTITLE_INPUT
    assert "no usable timestamps" in result.issues[0]


def test_timing_resolution_reuses_input_sidecar_before_aligner(tmp_path) -> None:
    subtitle = tmp_path / "captions.srt"
    subtitle.write_text("source", encoding="utf-8")
    media = tmp_path / "unique-movie.mp4"
    media.write_bytes(b"unique-media-sidecar")
    cues = [_cue(0, 0, 1000, "unique hello")]

    bundle, _, cache_hit = load_or_align_timing(
        str(subtitle),
        str(media),
        cues,
        "en",
        aligner=lambda **_kwargs: [{"text": "unique hello", "start_time": 0.0, "end_time": 1.0}],
        media_probe=_probe(),
    )
    assert bundle is not None
    assert not cache_hit
    write_timing_archive(timing_sidecar_path(subtitle), bundle)

    reused, issues, cache_hit = load_or_align_timing(
        str(subtitle),
        str(media),
        cues,
        "en",
        aligner=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("aligner must not run when sidecar matches")
        ),
        media_probe=_probe(),
    )

    assert cache_hit
    assert issues == ()
    assert reused == bundle


def test_failed_alignment_windows_are_not_cached(tmp_path, monkeypatch) -> None:
    subtitle = tmp_path / "captions.srt"
    subtitle.write_text("source", encoding="utf-8")
    media = tmp_path / "failed-movie.mp4"
    media.write_bytes(b"unique-media-failure")
    cues = [_cue(0, 0, 1000, "failure cue")]
    cached = []

    import videocaptioner.core.speed.timing_archive as archive

    monkeypatch.setattr(archive, "read_cached_timing_bundle", lambda *_args: None)
    monkeypatch.setattr(archive, "cache_timing_bundle", cached.append)
    bundle, issues, cache_hit = load_or_align_timing(
        str(subtitle),
        str(media),
        cues,
        "en",
        aligner=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("failed")),
        media_probe=_probe(),
    )

    assert bundle is not None
    assert issues
    assert not cache_hit
    assert cached == []
