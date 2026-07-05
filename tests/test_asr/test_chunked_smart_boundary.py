import io
import tempfile
from pathlib import Path

from pydub import AudioSegment
from pydub.generators import Sine

from videocaptioner.core.asr.asr_data import ASRDataSeg
from videocaptioner.core.asr.base import BaseASR
from videocaptioner.core.asr.chunked_asr import ChunkedASR


class BoundaryMockASR(BaseASR):
    def _run(self, callback=None, **kwargs):
        return {"segments": []}

    def _make_segments(self, resp_data: dict, _allow_degraded: bool = False):
        return []


class CountingMockASR(BaseASR):
    calls = 0

    def _run(self, callback=None, **kwargs):
        CountingMockASR.calls += 1
        return {"segments": [{"text": "speech", "start": 0.0, "end": 1.0}]}

    def _make_segments(self, resp_data: dict, _allow_degraded: bool = False):
        return [
            ASRDataSeg(
                text=seg["text"],
                start_time=int(seg["start"] * 1000),
                end_time=int(seg["end"] * 1000),
            )
            for seg in resp_data["segments"]
        ]


class SpeechRangeCaptureASR(BaseASR):
    captured_ranges: list[list[tuple[int, int]]] = []

    def __init__(
        self,
        audio_input,
        use_cache=False,
        need_word_time_stamp=False,
        audio_duration=None,
        speech_ranges_ms=None,
    ):
        super().__init__(
            audio_input,
            use_cache=use_cache,
            need_word_time_stamp=need_word_time_stamp,
            audio_duration=audio_duration,
            speech_ranges_ms=speech_ranges_ms,
        )
        SpeechRangeCaptureASR.captured_ranges.append(self.speech_ranges_ms)

    def _run(self, callback=None, **kwargs):
        return {"segments": [{"text": "speech", "start": 0.0, "end": 1.0}]}

    def _make_segments(self, resp_data: dict, _allow_degraded: bool = False):
        return [
            ASRDataSeg(
                text=seg["text"],
                start_time=int(seg["start"] * 1000),
                end_time=int(seg["end"] * 1000),
            )
            for seg in resp_data["segments"]
        ]


class EmptyThenOkASR(BaseASR):
    def __init__(
        self, audio_input, use_cache=False, need_word_time_stamp=False, call_state=None
    ):
        super().__init__(audio_input, use_cache, need_word_time_stamp)
        self.call_state = call_state or {"count": 0}

    def _run(self, callback=None, **kwargs):
        index = self.call_state["count"]
        self.call_state["count"] += 1
        if index == 0:
            return {"segments": []}
        return {"segments": [{"text": "recovered", "start": 0.0, "end": 1.0}]}

    def _make_segments(self, resp_data: dict, _allow_degraded: bool = False):
        return [
            ASRDataSeg(
                text=seg["text"],
                start_time=int(seg["start"] * 1000),
                end_time=int(seg["end"] * 1000),
            )
            for seg in resp_data["segments"]
        ]


def _write_audio(audio: AudioSegment, suffix: str = ".wav") -> str:
    temp_file = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    temp_path = temp_file.name
    temp_file.close()
    audio.export(temp_path, format=suffix.lstrip("."))
    return temp_path


def _tone(duration_ms: int) -> AudioSegment:
    return Sine(440).to_audio_segment(duration=duration_ms).apply_gain(-6)


def test_fixed_boundary_mode_preserves_existing_offsets():
    audio_path = _write_audio(AudioSegment.silent(duration=30_000))
    try:
        chunked = ChunkedASR(
            asr_class=BoundaryMockASR,
            audio_path=audio_path,
            chunk_length=10,
            chunk_overlap=2,
            chunk_boundary_mode="fixed",
        )

        spans = chunked._plan_chunk_spans(chunked._load_audio())

        assert spans == [(0, 10_000), (8_000, 18_000), (16_000, 26_000), (24_000, 30_000)]
    finally:
        Path(audio_path).unlink()


def test_silence_boundary_mode_snaps_first_boundary_near_quiet_gap():
    audio = (
        _tone(8_000)
        + AudioSegment.silent(duration=1_200)
        + _tone(8_800)
    )
    audio_path = _write_audio(audio)
    try:
        chunked = ChunkedASR(
            asr_class=BoundaryMockASR,
            audio_path=audio_path,
            chunk_length=10,
            chunk_overlap=1,
            chunk_boundary_mode="silence",
            boundary_search_before=3,
            boundary_search_after=1,
            min_silence_duration_ms=400,
        )

        spans = chunked._plan_chunk_spans(chunked._load_audio())

        assert 8_300 <= spans[0][1] <= 8_900
        assert spans[1][0] == spans[0][1] - 1_000
    finally:
        Path(audio_path).unlink()


def test_vad_boundary_mode_prefers_silero_speech_ranges(monkeypatch):
    audio_path = _write_audio(_tone(18_000))
    calls = []

    def fake_silero_ranges(self, audio):
        calls.append(len(audio))
        return [(0, 8_000), (9_200, len(audio))]

    monkeypatch.setattr(
        ChunkedASR,
        "_detect_silero_speech_ranges",
        fake_silero_ranges,
    )
    try:
        chunked = ChunkedASR(
            asr_class=BoundaryMockASR,
            audio_path=audio_path,
            chunk_length=10,
            chunk_overlap=1,
            chunk_boundary_mode="vad",
            boundary_search_before=3,
            boundary_search_after=1,
            min_silence_duration_ms=400,
        )

        spans = chunked._plan_chunk_spans(chunked._load_audio())

        assert calls
        assert 8_400 <= spans[0][1] <= 8_800
        assert spans[1][0] == spans[0][1] - 1_000
    finally:
        Path(audio_path).unlink()


def test_vad_boundary_mode_falls_back_to_energy_detection(monkeypatch):
    audio = _tone(8_000) + AudioSegment.silent(duration=1_200) + _tone(8_800)
    audio_path = _write_audio(audio)

    monkeypatch.setattr(
        ChunkedASR,
        "_detect_silero_speech_ranges",
        lambda self, audio: None,
    )
    try:
        chunked = ChunkedASR(
            asr_class=BoundaryMockASR,
            audio_path=audio_path,
            chunk_length=10,
            chunk_overlap=1,
            chunk_boundary_mode="vad",
            boundary_search_before=3,
            boundary_search_after=1,
            min_silence_duration_ms=400,
        )

        spans = chunked._plan_chunk_spans(chunked._load_audio())

        assert 8_300 <= spans[0][1] <= 8_900
        assert spans[1][0] == spans[0][1] - 1_000
    finally:
        Path(audio_path).unlink()


def test_silence_boundary_mode_falls_back_when_no_candidate_exists():
    audio_path = _write_audio(_tone(18_000))
    try:
        chunked = ChunkedASR(
            asr_class=BoundaryMockASR,
            audio_path=audio_path,
            chunk_length=10,
            chunk_overlap=1,
            chunk_boundary_mode="silence",
            boundary_search_before=3,
            boundary_search_after=1,
            min_silence_duration_ms=400,
        )

        spans = chunked._plan_chunk_spans(chunked._load_audio())

        assert spans[0] == (0, 10_000)
        assert spans[1][0] == 9_000
    finally:
        Path(audio_path).unlink()


def test_silence_boundary_mode_rejects_candidates_after_hard_limit():
    audio = _tone(10_200) + AudioSegment.silent(duration=1_200) + _tone(6_600)
    audio_path = _write_audio(audio)
    try:
        chunked = ChunkedASR(
            asr_class=BoundaryMockASR,
            audio_path=audio_path,
            chunk_length=10,
            chunk_overlap=1,
            chunk_boundary_mode="silence",
            boundary_search_before=1,
            boundary_search_after=3,
            min_silence_duration_ms=400,
        )

        spans = chunked._plan_chunk_spans(chunked._load_audio())

        assert spans[0] == (0, 10_000)
    finally:
        Path(audio_path).unlink()


def test_silence_boundary_exports_never_exceed_configured_chunk_length():
    audio = _tone(8_000) + AudioSegment.silent(duration=1_200) + _tone(18_800)
    audio_path = _write_audio(audio)
    try:
        chunked = ChunkedASR(
            asr_class=BoundaryMockASR,
            audio_path=audio_path,
            chunk_length=10,
            chunk_overlap=1,
            chunk_boundary_mode="silence",
            boundary_search_before=3,
            boundary_search_after=1,
            min_silence_duration_ms=400,
        )

        chunks = chunked._split_audio()

        for chunk_bytes, _ in chunks:
            chunk_audio = AudioSegment.from_file(io.BytesIO(chunk_bytes))
            assert len(chunk_audio) <= 10_000
    finally:
        Path(audio_path).unlink()


def test_silence_boundary_skips_pure_silence_chunk():
    audio_path = _write_audio(AudioSegment.silent(duration=10_000))
    try:
        CountingMockASR.calls = 0
        chunked = ChunkedASR(
            asr_class=CountingMockASR,
            audio_path=audio_path,
            chunk_length=20,
            chunk_overlap=1,
            chunk_boundary_mode="silence",
        )

        chunks = chunked._split_audio()
        result = chunked.run()

        assert chunks == [(b"", 0)]
        assert result.segments == []
        assert CountingMockASR.calls == 0
    finally:
        Path(audio_path).unlink()


def test_silence_boundary_retries_empty_result_for_non_silent_chunk():
    audio_path = _write_audio(_tone(10_000))
    try:
        call_state = {"count": 0}
        chunked = ChunkedASR(
            asr_class=EmptyThenOkASR,
            audio_path=audio_path,
            asr_kwargs={"call_state": call_state},
            chunk_length=20,
            chunk_overlap=1,
            chunk_boundary_mode="silence",
        )

        result = chunked.run()

        assert call_state["count"] == 2
        assert result.segments[0].text == "recovered"
    finally:
        Path(audio_path).unlink()


def test_silence_boundary_passes_relative_speech_ranges_to_backend():
    audio = (
        AudioSegment.silent(duration=1_000)
        + _tone(2_000)
        + AudioSegment.silent(duration=4_000)
        + _tone(2_000)
        + AudioSegment.silent(duration=1_000)
    )
    audio_path = _write_audio(audio)
    try:
        SpeechRangeCaptureASR.captured_ranges = []
        chunked = ChunkedASR(
            asr_class=SpeechRangeCaptureASR,
            audio_path=audio_path,
            chunk_length=20,
            chunk_overlap=1,
            chunk_boundary_mode="silence",
            min_silence_duration_ms=400,
        )

        chunked.run()

        ranges = SpeechRangeCaptureASR.captured_ranges[0]
        assert len(ranges) == 2
        assert 900 <= ranges[0][0] <= 1_100
        assert 2_900 <= ranges[0][1] <= 3_100
        assert 6_900 <= ranges[1][0] <= 7_100
        assert 8_900 <= ranges[1][1] <= 9_100
    finally:
        Path(audio_path).unlink()


def test_retry_subchunk_split_uses_silence_boundary_with_overlap():
    audio = _tone(8_000) + AudioSegment.silent(duration=1_200) + _tone(8_800)
    audio_path = _write_audio(audio)
    try:
        chunked = ChunkedASR(
            asr_class=BoundaryMockASR,
            audio_path=audio_path,
            chunk_length=20,
            chunk_overlap=1,
            chunk_boundary_mode="silence",
            boundary_search_before=3,
            boundary_search_after=1,
            min_silence_duration_ms=400,
        )
        buffer = io.BytesIO()
        audio.export(buffer, format="wav")

        sub_chunks = chunked._split_chunk_bytes(buffer.getvalue(), 2)
        first_audio = AudioSegment.from_file(io.BytesIO(sub_chunks[0][0]))

        assert len(sub_chunks) == 2
        assert 5_500 <= sub_chunks[1][1] <= 7_500
        assert len(first_audio) > sub_chunks[1][1]
    finally:
        Path(audio_path).unlink()
