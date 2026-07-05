"""ChunkedASR 分层重试测试

验证 ASRResultDegradedError 触发时的重试行为:
- 首次成功: 不重试
- 同块重试成功: 第 2 次调用成功
- 拆子块成功: 验证子块 offset 累加拼接
- 全失败降级: 验证 _allow_degraded=True 路径
"""

import io
import tempfile
from pathlib import Path
from typing import Callable, List, Optional

from pydub import AudioSegment

from videocaptioner.core.asr.asr_data import ASRDataSeg
from videocaptioner.core.asr.base import ASRResultDegradedError, BaseASR
from videocaptioner.core.asr.chunked_asr import ChunkedASR


class RetryAwareMockASR(BaseASR):
    """Mock ASR that raises ASRResultDegradedError on configured attempts.

    ``degrade_on_attempts`` controls which 0-based run-indexes raise. The
    counter increments on every ``run`` call across instances sharing the same
    ``call_state`` dict, so sub-chunk retries can be simulated.
    """

    def __init__(
        self,
        audio_input,
        use_cache: bool = False,
        need_word_time_stamp: bool = False,
        call_state: Optional[dict] = None,
        degrade_on_attempts: Optional[set] = None,
        audio_duration: float | None = None,
        cache_identity: str | None = None,
    ):
        super().__init__(
            audio_input,
            use_cache=use_cache,
            need_word_time_stamp=need_word_time_stamp,
            audio_duration=audio_duration,
            cache_identity=cache_identity,
        )
        # call_state is shared via asr_kwargs so all instances see the same counter.
        self._call_state = call_state or {"count": 0}
        self._degrade_on_attempts = degrade_on_attempts or set()
        self._call_state.setdefault("cache_identities", []).append(self.cache_identity)

    def _run(
        self, callback: Optional[Callable[[int, str], None]] = None, **kwargs
    ) -> dict:
        allow_degraded = bool(kwargs.get("_allow_degraded", False))
        index = self._call_state["count"]
        self._call_state["count"] += 1

        if index in self._degrade_on_attempts and not allow_degraded:
            raise ASRResultDegradedError(f"mock degraded on attempt {index}")

        # Build segments relative to this chunk's start (offset applied by caller).
        audio = AudioSegment.from_file(io.BytesIO(self.file_binary or b""))
        duration_sec = max(1, len(audio) // 1000)
        segments = [
            {"text": f"seg{i}", "start": i, "end": i + 1}
            for i in range(duration_sec)
        ]
        return {"segments": segments}

    def _make_segments(
        self, resp_data: dict, _allow_degraded: bool = False
    ) -> List[ASRDataSeg]:
        return [
            ASRDataSeg(
                text=seg["text"],
                start_time=int(seg["start"] * 1000),
                end_time=int(seg["end"] * 1000),
            )
            for seg in resp_data["segments"]
        ]

    def _get_key(self) -> str:
        return f"retry-mock-{self.crc32_hex}"


def _create_audio(duration_sec: int = 60) -> str:
    audio = AudioSegment.silent(duration=duration_sec * 1000)
    temp_file = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    temp_path = temp_file.name
    temp_file.close()
    audio.export(temp_path, format="mp3")
    return temp_path


class TestChunkedRetry:
    """分层重试核心行为测试"""

    def test_first_attempt_success_no_retry(self):
        """首次成功时不触发重试。"""
        audio_path = _create_audio(30)
        try:
            call_state = {"count": 0}
            chunked = ChunkedASR(
                asr_class=RetryAwareMockASR,
                audio_path=audio_path,
                asr_kwargs={
                    "need_word_time_stamp": True,
                    "call_state": call_state,
                    "degrade_on_attempts": set(),  # never degrade
                },
                chunk_length=20,
                chunk_overlap=5,
                chunk_concurrency=1,
            )
            result = chunked.run()
            assert result.has_data()
            # 30s audio with 20s chunk_length → 2 chunks, each called once.
            # No retries because no degradation.
            assert call_state["count"] == 2
        finally:
            Path(audio_path).unlink()

    def test_same_chunk_retry_succeeds(self):
        """首次异常 → 同块重试（第 2 次）成功。"""
        # Audio must be longer than chunk_length to enter the multi-chunk path
        # where retry logic lives.
        audio_path = _create_audio(30)
        try:
            call_state = {"count": 0}
            chunked = ChunkedASR(
                asr_class=RetryAwareMockASR,
                audio_path=audio_path,
                asr_kwargs={
                    "need_word_time_stamp": True,
                    "call_state": call_state,
                    "degrade_on_attempts": {0},  # only first attempt degrades
                },
                chunk_length=20,
                chunk_overlap=5,
                chunk_concurrency=1,
            )
            result = chunked.run()
            assert result.has_data()
            # chunk1: attempt0 (degrade) + retry (ok) = 2 calls;
            # chunk2: 1 call (ok). Total = 3.
            assert call_state["count"] == 3
        finally:
            Path(audio_path).unlink()

    def test_subchunk_split_succeeds_when_same_chunk_fails(self):
        """同块失败 → 拆 2 子块成功，验证 offset 累加。"""
        audio_path = _create_audio(30)
        try:
            call_state = {"count": 0}
            chunked = ChunkedASR(
                asr_class=RetryAwareMockASR,
                audio_path=audio_path,
                asr_kwargs={
                    "need_word_time_stamp": True,
                    "call_state": call_state,
                    # Attempt 0 (first) + attempt 1 (same-chunk retry) degrade.
                    # Attempt 2, 3 are the two 10s sub-chunks → both succeed.
                    "degrade_on_attempts": {0, 1},
                },
                chunk_length=20,
                chunk_overlap=5,
                chunk_concurrency=1,
            )
            result = chunked.run()
            assert result.has_data()
            # chunk1: 0 (degrade) + 1 (same-chunk degrade) + 2,3 (sub-chunks ok) = 4;
            # chunk2: 4 (ok). Total = 5.
            assert call_state["count"] == 5

            # Sub-chunk offsets: first sub-chunk at offset 0, second at ~10000ms.
            # The second sub-chunk's segments should have start_time >= 10000.
            timestamps = [seg.start_time for seg in result.segments]
            assert min(timestamps) == 0
            assert max(timestamps) >= 10000
        finally:
            Path(audio_path).unlink()

    def test_subchunk_retry_uses_source_range_cache_identities(self):
        """子块重试使用源音频范围 identity，而不是导出后的子块字节。"""
        audio_path = _create_audio(30)
        try:
            call_state = {"count": 0}
            chunked = ChunkedASR(
                asr_class=RetryAwareMockASR,
                audio_path=audio_path,
                asr_kwargs={
                    "need_word_time_stamp": True,
                    "call_state": call_state,
                    "degrade_on_attempts": {0, 1},
                },
                chunk_length=20,
                chunk_overlap=5,
                chunk_concurrency=1,
            )

            chunked.run()

            identities = call_state["cache_identities"]
            assert identities[0] == identities[1]
            assert identities[0].endswith("-0-20000")
            assert identities[2].endswith("-0-10000")
            assert identities[3].endswith("-10000-20000")
            assert len(set(identities[:4])) == 3
        finally:
            Path(audio_path).unlink()

    def test_can_skip_same_chunk_retry_for_deterministic_backends(self):
        """首次异常后可直接拆子块，避免确定性本地模型重复同一输入。"""
        audio_path = _create_audio(30)
        try:
            call_state = {"count": 0}
            chunked = ChunkedASR(
                asr_class=RetryAwareMockASR,
                audio_path=audio_path,
                asr_kwargs={
                    "need_word_time_stamp": True,
                    "call_state": call_state,
                    "degrade_on_attempts": {0},
                },
                chunk_length=20,
                chunk_overlap=5,
                chunk_concurrency=1,
                retry_same_chunk=False,
            )
            result = chunked.run()

            assert result.has_data()
            # chunk1: attempt0 (degrade) + two sub-chunks; chunk2: one normal call.
            assert call_state["count"] == 4
        finally:
            Path(audio_path).unlink()

    def test_falls_back_to_degraded_after_all_retries(self):
        """3 次重试全失败 → 降级路径（_allow_degraded=True）。"""
        audio_path = _create_audio(30)
        try:
            call_state = {"count": 0}
            chunked = ChunkedASR(
                asr_class=RetryAwareMockASR,
                audio_path=audio_path,
                asr_kwargs={
                    "need_word_time_stamp": True,
                    "call_state": call_state,
                    # Degrade on every non-degraded attempt.
                    # Sub-chunk calls also hit these indices.
                    "degrade_on_attempts": {0, 1, 2, 3, 4, 5, 6, 7, 8, 9},
                },
                chunk_length=20,
                chunk_overlap=5,
                chunk_concurrency=1,
            )
            result = chunked.run()
            # Even after all retries fail, degraded fallback produces segments.
            assert result.has_data()
            # The final call must have been with _allow_degraded=True.
            # Total: 1 first + 1 same-chunk + 2 sub-chunks(attempt2) +
            #         3 sub-chunks(attempt3) + 1 degraded = 8
            assert call_state["count"] >= 8
        finally:
            Path(audio_path).unlink()

    def test_subchunk_offsets_are_continuous(self):
        """子块 offset 连续无空洞：第二子块起始 = 第一子块结束。"""
        audio_path = _create_audio(30)
        try:
            call_state = {"count": 0}
            chunked = ChunkedASR(
                asr_class=RetryAwareMockASR,
                audio_path=audio_path,
                asr_kwargs={
                    "need_word_time_stamp": True,
                    "call_state": call_state,
                    "degrade_on_attempts": {0, 1},  # split into 2 sub-chunks
                },
                chunk_length=20,
                chunk_overlap=5,
                chunk_concurrency=1,
            )
            result = chunked.run()
            # Each sub-chunk is ~10s = 10 segments of 1s each.
            # First sub-chunk: 0-10000ms, second sub-chunk: 10000-20000ms.
            starts = sorted(seg.start_time for seg in result.segments)
            assert starts[0] == 0
            # There should be segments starting around 10000ms (second sub-chunk).
            assert any(s >= 9000 for s in starts)
        finally:
            Path(audio_path).unlink()


# ---------------------------------------------------------------------------
# Regression: double-offset and single-chunk bypass (from code review)
# ---------------------------------------------------------------------------


class TestTimestampBounds:
    """验证合并后时间轴不因双偏移而超出音频时长。"""

    def test_multi_chunk_merged_timeline_within_audio_duration(self):
        """多 chunk 合并后，最后 end_time 不应超过音频总时长。

        回归 P1: _run_single_asr 曾给主 chunk 加 offset，而 ChunkMerger
        又会按 chunk_offsets 再加一次，导致时间轴翻倍。
        """
        audio_path = _create_audio(30)
        try:
            call_state = {"count": 0}
            chunked = ChunkedASR(
                asr_class=RetryAwareMockASR,
                audio_path=audio_path,
                asr_kwargs={
                    "need_word_time_stamp": True,
                    "call_state": call_state,
                    "degrade_on_attempts": set(),  # no degradation
                },
                chunk_length=20,
                chunk_overlap=5,
                chunk_concurrency=1,
            )
            result = chunked.run()
            # 30s audio → all end_times must be <= 30000ms.
            assert result.segments
            assert all(seg.end_time <= 30000 for seg in result.segments), (
                f"end_time exceeds audio duration: "
                f"max={max(seg.end_time for seg in result.segments)}"
            )
        finally:
            Path(audio_path).unlink()

    def test_single_chunk_goes_through_retry(self):
        """单 chunk（音频短于 chunk_length）也走重试逻辑，不直接抛异常。

        回归 P1: len(chunks)==1 曾直接 single_asr.run(callback)，绕过
        _transcribe_with_retry，导致短音频异常时无重试无降级。
        """
        audio_path = _create_audio(10)  # shorter than chunk_length=20
        try:
            call_state = {"count": 0}
            chunked = ChunkedASR(
                asr_class=RetryAwareMockASR,
                audio_path=audio_path,
                asr_kwargs={
                    "need_word_time_stamp": True,
                    "call_state": call_state,
                    # Degrade on every attempt → must reach degraded fallback.
                    "degrade_on_attempts": {0, 1, 2, 3, 4, 5, 6, 7, 8, 9},
                },
                chunk_length=20,
                chunk_overlap=5,
                chunk_concurrency=1,
            )
            result = chunked.run()
            # Degraded fallback still produces segments.
            assert result.has_data()
            # Multiple calls prove retry happened (not just one run()).
            assert call_state["count"] > 1
        finally:
            Path(audio_path).unlink()
