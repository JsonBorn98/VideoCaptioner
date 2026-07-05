"""ChunkedASR 全面测试

测试策略：
1. 使用 Mock ASR 避免实际 API 调用
2. 覆盖所有核心功能（分块、并发、合并）
3. 测试边界情况（短音频、单块、错误等）
4. 验证进度回调机制
5. 确保线程安全和并发正确性

重构后设计：
- ChunkedASR 接收 ASR 类和参数，而非实例
- 为每个 chunk 创建独立的 ASR 实例
- 避免共享状态，支持真正的并发
"""

import io
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

import pytest
from pydub import AudioSegment
from pydub.generators import Sine

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.asr.base import ASRResultDegradedError, BaseASR
from videocaptioner.core.asr.chunked_asr import ChunkedASR

# ============================================================================
# Mock ASR 辅助类
# ============================================================================


class MockASR(BaseASR):
    """Mock ASR 用于测试，避免实际 API 调用

    支持接收 bytes 或 str 作为 audio_input（适配 ChunkedASR）
    """

    # 类变量：跨实例共享的调用计数（用于测试并发）
    global_run_count = 0

    def __init__(
        self,
        audio_input,
        use_cache: bool = False,
        need_word_time_stamp: bool = False,
        # Mock 专用参数
        mock_text_per_second: str = "Mock",
        fail_on_run: bool = False,
    ):
        super().__init__(audio_input, use_cache, need_word_time_stamp)
        self.mock_text_per_second = mock_text_per_second
        self.fail_on_run = fail_on_run

    def _run(
        self, callback: Optional[Callable[[int, str], None]] = None, **kwargs
    ) -> dict:
        """模拟 ASR 转录，返回假数据"""
        MockASR.global_run_count += 1

        if self.fail_on_run:
            raise RuntimeError("Mock ASR failed")

        if callback:
            callback(50, "processing")
            callback(100, "completed")

        # 生成模拟的转录结果（每秒一个字）
        if self.file_binary:
            audio = AudioSegment.from_file(io.BytesIO(self.file_binary))
            duration_sec = len(audio) / 1000  # 毫秒转秒
            num_segments = max(1, int(duration_sec))

            segments = [
                {
                    "text": f"{self.mock_text_per_second}{i+1}",
                    "start": i,
                    "end": i + 1,
                }
                for i in range(num_segments)
            ]
        else:
            segments = [{"text": "Mock", "start": 0, "end": 1}]

        return {"segments": segments}

    def _make_segments(
        self, resp_data: dict, _allow_degraded: bool = False
    ) -> List[ASRDataSeg]:
        """将模拟数据转换为 ASRDataSeg"""
        return [
            ASRDataSeg(
                text=seg["text"],
                start_time=int(seg["start"] * 1000),
                end_time=int(seg["end"] * 1000),
            )
            for seg in resp_data["segments"]
        ]


class DurationAwareMockASR(BaseASR):
    """Mock backend that records precomputed chunk durations."""

    duration_values: list[float] = []
    duration_probe_count = 0

    def __init__(
        self,
        audio_input,
        use_cache: bool = False,
        need_word_time_stamp: bool = False,
        audio_duration: float | None = None,
    ):
        super().__init__(
            audio_input,
            use_cache,
            need_word_time_stamp,
            audio_duration=audio_duration,
        )
        self.duration_values.append(self.audio_duration)

    def _get_audio_duration(self) -> float:
        self.__class__.duration_probe_count += 1
        return super()._get_audio_duration()

    def _run(
        self, callback: Optional[Callable[[int, str], None]] = None, **kwargs
    ) -> dict:
        return {
            "segments": [
                {
                    "text": "duration",
                    "start": 0,
                    "end": max(0.001, self.audio_duration),
                }
            ]
        }

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


class PipelineMockASR(BaseASR):
    """Two-stage backend used to prove API work continues during alignment."""

    lock = threading.Lock()
    next_instance_id = 0
    transcript_completed_count = 0
    first_alignment_finished_transcript_count = 0
    alignment_order: list[int] = []

    @classmethod
    def reset(cls):
        with cls.lock:
            cls.next_instance_id = 0
            cls.transcript_completed_count = 0
            cls.first_alignment_finished_transcript_count = 0
            cls.alignment_order = []

    def __init__(
        self,
        audio_input,
        use_cache: bool = False,
        audio_duration: float | None = None,
        **kwargs,
    ):
        super().__init__(audio_input, use_cache=use_cache, audio_duration=audio_duration)
        source_start_ms = kwargs.get("source_start_ms")
        with self.lock:
            if source_start_ms is None:
                self.instance_id = self.next_instance_id
                type(self).next_instance_id += 1
            else:
                duration_ms = max(1, int((audio_duration or 1) * 1000))
                self.instance_id = int(source_start_ms) // duration_ms

    def run_transcript_stage(self, callback=None, **kwargs):
        time.sleep(0.02)
        with self.lock:
            type(self).transcript_completed_count += 1
        return {"text": f"pipe-{self.instance_id}", "seconds": 1.0}

    def run_alignment_stage(self, resp_data: dict, callback=None, **kwargs):
        with self.lock:
            type(self).alignment_order.append(self.instance_id)
            is_first_alignment = len(self.alignment_order) == 1
        if is_first_alignment:
            time.sleep(0.3)
            with self.lock:
                type(self).first_alignment_finished_transcript_count = (
                    self.transcript_completed_count
                )
        return ASRData(
            [
                ASRDataSeg(
                    text=str(resp_data["text"]),
                    start_time=0,
                    end_time=1000,
                )
            ]
        )

    def _run(self, callback=None, **kwargs):
        raise AssertionError("PipelineMockASR should use the two-stage path")

    def _make_segments(self, resp_data: dict, _allow_degraded: bool = False):
        return []


class CacheIdentityMockASR(BaseASR):
    """Mock backend that records cache identity propagation."""

    cache_identities: list[str] = []

    def __init__(
        self,
        audio_input,
        use_cache: bool = False,
        need_word_time_stamp: bool = False,
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
        self.__class__.cache_identities.append(self.cache_identity)

    def _run(
        self, callback: Optional[Callable[[int, str], None]] = None, **kwargs
    ) -> dict:
        return {
            "segments": [
                {
                    "text": "cache",
                    "start": 0,
                    "end": max(0.001, self.audio_duration),
                }
            ]
        }

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


class SourceRangeMockASR(BaseASR):
    """Mock backend that records source-range handoff instead of chunk payload."""

    calls: list[dict] = []

    def __init__(
        self,
        audio_input,
        use_cache: bool = False,
        need_word_time_stamp: bool = False,
        audio_duration: float | None = None,
        cache_identity: str | None = None,
        source_audio_path: str = "",
        source_start_ms: int | None = None,
        source_duration_ms: int | None = None,
    ):
        super().__init__(
            audio_input,
            use_cache=use_cache,
            need_word_time_stamp=need_word_time_stamp,
            audio_duration=audio_duration,
            cache_identity=cache_identity,
        )
        self.source_audio_path = source_audio_path
        self.source_start_ms = source_start_ms
        self.source_duration_ms = source_duration_ms
        self.__class__.calls.append(
            {
                "payload_size": len(self.file_binary or b""),
                "source_audio_path": source_audio_path,
                "source_start_ms": source_start_ms,
                "source_duration_ms": source_duration_ms,
                "audio_duration": self.audio_duration,
            }
        )

    def _run(
        self, callback: Optional[Callable[[int, str], None]] = None, **kwargs
    ) -> dict:
        return {
            "segments": [
                {
                    "text": "source",
                    "start": 0,
                    "end": max(0.001, self.audio_duration),
                }
            ]
        }

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


class BatchMockASR(BaseASR):
    """Mock backend that supports batch first-pass transcription."""

    batch_calls: list[list[int | None]] = []
    single_run_count = 0
    fail_offsets_ms: set[int] = set()

    def __init__(
        self,
        audio_input,
        use_cache: bool = False,
        need_word_time_stamp: bool = False,
        audio_duration: float | None = None,
        cache_identity: str | None = None,
        source_audio_path: str = "",
        source_start_ms: int | None = None,
        source_duration_ms: int | None = None,
    ):
        super().__init__(
            audio_input,
            use_cache=use_cache,
            need_word_time_stamp=need_word_time_stamp,
            audio_duration=audio_duration,
            cache_identity=cache_identity,
        )
        self.source_audio_path = source_audio_path
        self.source_start_ms = source_start_ms
        self.source_duration_ms = source_duration_ms

    @classmethod
    def reset(cls):
        cls.batch_calls = []
        cls.single_run_count = 0
        cls.fail_offsets_ms = set()

    @classmethod
    def run_batch_instances(cls, instances, callback=None):
        cls.batch_calls.append([instance.source_start_ms for instance in instances])
        if callback:
            callback(50, "batch processing")
        results = []
        for instance in instances:
            if instance.source_start_ms in cls.fail_offsets_ms:
                results.append(ASRResultDegradedError("batch degraded"))
                continue
            results.append(
                ASRData(
                    [
                        ASRDataSeg(
                            text=f"batch-{instance.source_start_ms}",
                            start_time=0,
                            end_time=max(1, int(instance.audio_duration * 1000)),
                        )
                    ]
                )
            )
        return results

    def _run(
        self, callback: Optional[Callable[[int, str], None]] = None, **kwargs
    ) -> dict:
        type(self).single_run_count += 1
        return {
            "segments": [
                {
                    "text": f"single-{self.source_start_ms}",
                    "start": 0,
                    "end": max(0.001, self.audio_duration),
                }
            ]
        }

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


def create_test_audio_file(duration_sec: int = 60) -> str:
    """创建测试用音频文件（静音）

    Args:
        duration_sec: 音频时长（秒）

    Returns:
        音频文件路径（临时文件）
    """
    # 创建静音音频
    audio = AudioSegment.silent(duration=duration_sec * 1000)

    # 保存到临时文件（delete=False 避免 Windows 权限问题）
    temp_file = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    temp_path = temp_file.name
    temp_file.close()  # 关闭文件句柄，让 pydub 可以写入
    audio.export(temp_path, format="mp3")
    return temp_path


def create_tone_audio_file(duration_sec: int = 60) -> str:
    """Create a non-silent test audio file."""
    audio = Sine(440).to_audio_segment(duration=duration_sec * 1000)
    temp_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    temp_path = temp_file.name
    temp_file.close()
    audio.export(temp_path, format="wav")
    return temp_path


# ============================================================================
# 测试 ChunkedASR 基础功能
# ============================================================================


class TestChunkedASRBasics:
    """测试 ChunkedASR 的基础功能"""

    def test_init_default_params(self):
        """测试默认参数初始化"""
        audio_input = create_test_audio_file(60)
        try:
            chunked = ChunkedASR(
                asr_class=MockASR, audio_path=audio_input, asr_kwargs={}
            )

            assert chunked.asr_class is MockASR
            assert chunked.audio_path == audio_input
            assert chunked.chunk_length_ms == 600 * 1000  # 10 分钟
            assert chunked.chunk_overlap_ms == 10 * 1000  # 10 秒
            assert chunked.chunk_concurrency == 3
        finally:
            Path(audio_input).unlink()

    def test_init_custom_params(self):
        """测试自定义参数初始化"""
        audio_input = create_test_audio_file(60)
        try:
            chunked = ChunkedASR(
                asr_class=MockASR,
                audio_path=audio_input,
                asr_kwargs={"mock_text_per_second": "Test"},
                chunk_length=600,
                chunk_overlap=5,
                chunk_concurrency=5,
            )

            assert chunked.chunk_length_ms == 600 * 1000
            assert chunked.chunk_overlap_ms == 5 * 1000
            assert chunked.chunk_concurrency == 5
            assert chunked.asr_kwargs["mock_text_per_second"] == "Test"
        finally:
            Path(audio_input).unlink()

    def test_short_audio_no_chunking(self):
        """测试短音频（< chunk_length）不分块直接转录"""
        # 创建 5 分钟音频（小于默认的 8 分钟）
        audio_input = create_test_audio_file(300)
        try:
            MockASR.global_run_count = 0

            chunked = ChunkedASR(
                asr_class=MockASR,
                audio_path=audio_input,
                asr_kwargs={"mock_text_per_second": "Short"},
            )

            result = chunked.run()

            # 验证：只调用了一次 ASR（未分块）
            assert MockASR.global_run_count == 1
            assert len(result.segments) > 0
            assert result.segments[0].text.startswith("Short")
        finally:
            Path(audio_input).unlink()

    def test_long_audio_with_chunking(self):
        """测试长音频（> chunk_length）自动分块转录"""
        # 创建 20 分钟音频（会分成 3 块：0-8min, 8-16min, 16-20min）
        audio_input = create_test_audio_file(1200)
        try:
            MockASR.global_run_count = 0

            chunked = ChunkedASR(
                asr_class=MockASR,
                audio_path=audio_input,
                asr_kwargs={"mock_text_per_second": "Long"},
                chunk_length=480,  # 8分钟
                chunk_overlap=10,
            )

            result = chunked.run()

            # 验证：调用了 3 次 ASR（分成 3 块）
            # 计算公式：(1200s - 480s) / (480s - 10s) + 1 = 2.53... = 3 块
            assert MockASR.global_run_count == 3
            assert len(result.segments) > 0
        finally:
            Path(audio_input).unlink()

    def test_batch_backend_uses_one_batch_for_first_pass(self):
        """Batch-capable backends can transcribe all initial chunks in one request."""
        audio_input = create_test_audio_file(25)
        try:
            BatchMockASR.reset()
            chunked = ChunkedASR(
                asr_class=BatchMockASR,
                audio_path=audio_input,
                chunk_length=10,
                chunk_overlap=0,
                chunk_concurrency=1,
                pass_source_range=True,
            )

            result = chunked.run()

            assert BatchMockASR.batch_calls == [[0, 10_000, 20_000]]
            assert BatchMockASR.single_run_count == 0
            assert [seg.text for seg in result.segments] == [
                "batch-0",
                "batch-10000",
                "batch-20000",
            ]
        finally:
            Path(audio_input).unlink()

    def test_batch_backend_retries_only_degraded_chunks(self):
        """A degraded batch item falls back to the existing single-chunk retry path."""
        audio_input = create_test_audio_file(25)
        try:
            BatchMockASR.reset()
            BatchMockASR.fail_offsets_ms = {10_000}
            chunked = ChunkedASR(
                asr_class=BatchMockASR,
                audio_path=audio_input,
                chunk_length=10,
                chunk_overlap=0,
                chunk_concurrency=1,
                pass_source_range=True,
                retry_same_chunk=True,
            )

            result = chunked.run()

            assert BatchMockASR.batch_calls == [[0, 10_000, 20_000]]
            assert BatchMockASR.single_run_count == 1
            assert [seg.text for seg in result.segments] == [
                "batch-0",
                "single-10000",
                "batch-20000",
            ]
        finally:
            Path(audio_input).unlink()


# ============================================================================
# 测试音频分块逻辑
# ============================================================================


class TestAudioSplitting:
    """测试 _split_audio() 方法"""

    def test_split_exact_chunks(self):
        """测试精确分块（音频长度正好是块长度的倍数）"""
        # 16分钟 = 2块 × 8分钟
        audio_input = create_test_audio_file(960)
        try:
            chunked = ChunkedASR(
                asr_class=MockASR,
                audio_path=audio_input,
                chunk_length=480,
                chunk_overlap=0,
            )

            chunks = chunked._split_audio()

            assert len(chunks) == 2
            assert chunks[0][1] == 0  # 第一块 offset = 0ms
            assert chunks[1][1] == 480 * 1000  # 第二块 offset = 480s
        finally:
            Path(audio_input).unlink()

    def test_split_with_overlap(self):
        """测试带重叠的分块"""
        # 20分钟，8分钟/块，10秒重叠
        audio_input = create_test_audio_file(1200)
        try:
            chunked = ChunkedASR(
                asr_class=MockASR,
                audio_path=audio_input,
                chunk_length=480,
                chunk_overlap=10,
            )

            chunks = chunked._split_audio()

            # 计算块数：(1200 - 480) / (480 - 10) + 1 = 2.53 ≈ 3 块
            assert len(chunks) == 3

            # 验证 offset 正确
            assert chunks[0][1] == 0
            assert chunks[1][1] == 470 * 1000  # 480 - 10
            assert chunks[2][1] == 940 * 1000  # 470 + 470
        finally:
            Path(audio_input).unlink()

    def test_split_remainder_chunk(self):
        """测试剩余块（最后一块不足完整长度）"""
        # 10分钟，8分钟/块 -> 2块（第二块仅2分钟）
        audio_input = create_test_audio_file(600)
        try:
            chunked = ChunkedASR(
                asr_class=MockASR,
                audio_path=audio_input,
                chunk_length=480,
                chunk_overlap=0,
            )

            chunks = chunked._split_audio()

            assert len(chunks) == 2
            # 第二块应该只有 120 秒
            chunk2_audio = AudioSegment.from_file(io.BytesIO(chunks[1][0]))
            assert abs(len(chunk2_audio) - 120 * 1000) < 100  # 允许误差 100ms
        finally:
            Path(audio_input).unlink()

    def test_chunk_asr_receives_precomputed_audio_duration(self):
        """ChunkedASR passes known chunk duration instead of probing each ASR instance."""
        audio_input = create_test_audio_file(25)
        try:
            DurationAwareMockASR.duration_values = []
            DurationAwareMockASR.duration_probe_count = 0
            chunked = ChunkedASR(
                asr_class=DurationAwareMockASR,
                audio_path=audio_input,
                chunk_length=10,
                chunk_overlap=0,
                chunk_concurrency=1,
            )

            chunked.run()

            assert DurationAwareMockASR.duration_probe_count == 0
            assert [round(value) for value in DurationAwareMockASR.duration_values] == [
                10,
                10,
                5,
            ]
        finally:
            Path(audio_input).unlink()

    def test_direct_asr_cache_identity_defaults_to_crc32(self):
        """Direct ASR instances keep byte-CRC cache identity unless caller overrides it."""
        audio_input = create_test_audio_file(1)
        try:
            CacheIdentityMockASR.cache_identities = []
            audio_bytes = Path(audio_input).read_bytes()
            asr = CacheIdentityMockASR(audio_bytes, audio_duration=1)

            assert asr.cache_identity == asr.crc32_hex
            assert asr._get_key() == asr.crc32_hex
        finally:
            Path(audio_input).unlink()

    def test_chunk_cache_identity_is_stable_across_export_formats(self):
        """Chunk cache identity depends on source range, not exported chunk bytes."""
        audio_input = create_test_audio_file(25)
        try:
            CacheIdentityMockASR.cache_identities = []
            mp3_chunked = ChunkedASR(
                asr_class=CacheIdentityMockASR,
                audio_path=audio_input,
                chunk_length=10,
                chunk_overlap=0,
                chunk_concurrency=1,
                chunk_audio_format="mp3",
            )
            mp3_chunked.run()
            mp3_identities = list(CacheIdentityMockASR.cache_identities)

            CacheIdentityMockASR.cache_identities = []
            wav_chunked = ChunkedASR(
                asr_class=CacheIdentityMockASR,
                audio_path=audio_input,
                chunk_length=10,
                chunk_overlap=0,
                chunk_concurrency=1,
                chunk_audio_format="wav",
            )
            wav_chunked.run()

            assert mp3_identities == CacheIdentityMockASR.cache_identities
            assert [identity.rsplit("-", 2)[1:] for identity in mp3_identities] == [
                ["0", "10000"],
                ["10000", "20000"],
                ["20000", "25000"],
            ]
        finally:
            Path(audio_input).unlink()

    def test_retry_subchunk_split_reuses_loaded_source_audio(self):
        """Retry splitting can use the decoded source audio instead of parent bytes."""
        audio_input = create_test_audio_file(20)
        try:
            chunked = ChunkedASR(
                asr_class=MockASR,
                audio_path=audio_input,
                chunk_length=10,
                chunk_overlap=0,
            )
            chunked._split_audio()

            subchunks = chunked._split_chunk_bytes(
                b"not a decodable parent chunk",
                2,
                source_offset_ms=0,
                source_duration_ms=10_000,
            )

            assert len(subchunks) == 2
            assert [offset for _, offset, _ in subchunks] == [0, 5000]
            assert [duration for _, _, duration in subchunks] == [5000, 5000]
        finally:
            Path(audio_input).unlink()

    def test_export_chunks_respects_payload_byte_limit(self):
        """Oversized exported chunks are split again using source byte rate."""
        audio_input = create_test_audio_file(20)
        try:
            chunked = ChunkedASR(
                asr_class=MockASR,
                audio_path=audio_input,
                chunk_length=20,
                chunk_overlap=0,
                chunk_audio_format="wav",
                max_chunk_payload_bytes=100_000,
            )

            chunks = chunked._split_audio()

            assert len(chunks) > 1
            assert all(len(chunk_bytes) <= 100_000 for chunk_bytes, _ in chunks)
            assert [offset for _, offset in chunks] == sorted(offset for _, offset in chunks)
        finally:
            Path(audio_input).unlink()

    def test_source_range_backend_skips_chunk_payload_export(self):
        """Backends can receive source path/range instead of exported chunk bytes."""
        audio_input = create_tone_audio_file(25)
        try:
            SourceRangeMockASR.calls = []
            chunked = ChunkedASR(
                asr_class=SourceRangeMockASR,
                audio_path=audio_input,
                chunk_length=10,
                chunk_overlap=0,
                chunk_concurrency=1,
                pass_source_range=True,
            )

            chunked.run()

            assert [call["payload_size"] for call in SourceRangeMockASR.calls] == [
                0,
                0,
                0,
            ]
            assert [
                call["source_audio_path"] for call in SourceRangeMockASR.calls
            ] == [audio_input, audio_input, audio_input]
            assert [
                call["source_start_ms"] for call in SourceRangeMockASR.calls
            ] == [0, 10_000, 20_000]
            assert [
                call["source_duration_ms"] for call in SourceRangeMockASR.calls
            ] == [10_000, 10_000, 5_000]
        finally:
            Path(audio_input).unlink()


# ============================================================================
# 测试并发转录
# ============================================================================


class TestConcurrentTranscription:
    """测试并发转录逻辑"""

    def test_concurrency_3_workers(self):
        """测试 3 个并发 worker"""
        # 20分钟 -> 3块
        audio_input = create_test_audio_file(1200)
        try:
            MockASR.global_run_count = 0

            chunked = ChunkedASR(
                asr_class=MockASR,
                audio_path=audio_input,
                chunk_length=480,
                chunk_concurrency=3,
            )

            result = chunked.run()

            # 验证：所有块都被转录
            assert MockASR.global_run_count == 3
            assert len(result.segments) > 0
        finally:
            Path(audio_input).unlink()

    def test_independent_asr_instances(self):
        """测试每个 chunk 使用独立的 ASR 实例"""
        # 20分钟 -> 3块
        audio_input = create_test_audio_file(1200)
        try:
            MockASR.global_run_count = 0

            # 使用不同的 mock_text_per_second 标记不同实例
            chunked = ChunkedASR(
                asr_class=MockASR,
                audio_path=audio_input,
                asr_kwargs={"mock_text_per_second": "Chunk"},
                chunk_length=480,
            )

            result = chunked.run()

            # 验证：每个块都生成了结果
            assert MockASR.global_run_count == 3
            # 所有 segment 的文本都应该包含 "Chunk"
            for seg in result.segments:
                assert "Chunk" in seg.text
        finally:
            Path(audio_input).unlink()

    def test_two_stage_pipeline_keeps_api_stage_running_during_alignment(self):
        audio_input = create_test_audio_file(40)
        try:
            PipelineMockASR.reset()
            chunked = ChunkedASR(
                asr_class=PipelineMockASR,
                audio_path=audio_input,
                chunk_length=10,
                chunk_overlap=0,
                chunk_concurrency=2,
            )

            result = chunked.run()

            assert len(result.segments) == 4
            assert PipelineMockASR.alignment_order == [0, 1, 2, 3]
            assert PipelineMockASR.transcript_completed_count == 4
            assert PipelineMockASR.first_alignment_finished_transcript_count >= 3
        finally:
            Path(audio_input).unlink()


# ============================================================================
# 测试结果合并
# ============================================================================


class TestChunkMerging:
    """测试 _merge_results() 方法"""

    def test_merge_preserves_order(self):
        """测试合并后时间戳顺序正确"""
        # 20分钟 -> 3块
        audio_input = create_test_audio_file(1200)
        try:
            chunked = ChunkedASR(
                asr_class=MockASR, audio_path=audio_input, chunk_length=480
            )

            result = chunked.run()

            # 验证时间戳递增
            for i in range(len(result.segments) - 1):
                assert result.segments[i].end_time <= result.segments[i + 1].start_time
        finally:
            Path(audio_input).unlink()


# ============================================================================
# 测试边界情况
# ============================================================================


class TestEdgeCases:
    """测试边界情况"""

    def test_very_short_audio(self):
        """测试极短音频（1秒）"""
        audio_input = create_test_audio_file(1)
        try:
            chunked = ChunkedASR(asr_class=MockASR, audio_path=audio_input)

            result = chunked.run()

            assert len(result.segments) >= 1
        finally:
            Path(audio_input).unlink()

    def test_zero_overlap(self):
        """测试零重叠"""
        audio_input = create_test_audio_file(1000)
        try:
            chunked = ChunkedASR(
                asr_class=MockASR,
                audio_path=audio_input,
                chunk_length=480,
                chunk_overlap=0,
            )

            chunks = chunked._split_audio()

            # 验证无重叠：每个 chunk 的 offset 是前一个的结束位置
            assert len(chunks) >= 2
            assert chunks[1][1] == 480 * 1000
        finally:
            Path(audio_input).unlink()


# ============================================================================
# 测试错误处理
# ============================================================================


class TestErrorHandling:
    """测试错误处理"""

    def test_asr_failure_propagates(self):
        """测试 ASR 失败时错误正确传播"""
        audio_input = create_test_audio_file(1000)
        try:
            chunked = ChunkedASR(
                asr_class=MockASR,
                audio_path=audio_input,
                asr_kwargs={"fail_on_run": True},
                chunk_length=480,
            )

            with pytest.raises(RuntimeError, match="Mock ASR failed"):
                chunked.run()
        finally:
            Path(audio_input).unlink()

    def test_asr_failure_stops_submitting_remaining_chunks(self):
        """首个 chunk 失败后不再继续启动后续 chunk"""
        audio_input = create_test_audio_file(1000)
        try:
            MockASR.global_run_count = 0
            chunked = ChunkedASR(
                asr_class=MockASR,
                audio_path=audio_input,
                asr_kwargs={"fail_on_run": True},
                chunk_length=300,
                chunk_overlap=0,
                chunk_concurrency=1,
            )

            with pytest.raises(RuntimeError, match="Mock ASR failed"):
                chunked.run()

            assert MockASR.global_run_count == 1
        finally:
            Path(audio_input).unlink()


# ============================================================================
# 测试进度回调
# ============================================================================


class TestProgressCallback:
    """测试进度回调机制"""

    def test_callback_invoked(self):
        """测试回调函数被正确调用"""
        audio_input = create_test_audio_file(1000)
        try:
            callback_calls = []

            def mock_callback(progress: int, message: str):
                callback_calls.append((progress, message))

            chunked = ChunkedASR(
                asr_class=MockASR, audio_path=audio_input, chunk_length=480
            )

            chunked.run(callback=mock_callback)

            # 验证回调被调用
            assert len(callback_calls) > 0
            # 验证进度在 0-100 之间
            for progress, _ in callback_calls:
                assert 0 <= progress <= 100
        finally:
            Path(audio_input).unlink()


# ============================================================================
# 集成测试
# ============================================================================


class TestIntegration:
    """端到端集成测试"""

    def test_full_pipeline_short_audio(self):
        """测试完整流程：短音频（不分块）"""
        audio_input = create_test_audio_file(300)
        try:
            MockASR.global_run_count = 0

            chunked = ChunkedASR(
                asr_class=MockASR,
                audio_path=audio_input,
                asr_kwargs={"mock_text_per_second": "Test"},
            )

            result = chunked.run()

            assert MockASR.global_run_count == 1
            assert len(result.segments) > 0
            assert all("Test" in seg.text for seg in result.segments)
        finally:
            Path(audio_input).unlink()

    def test_full_pipeline_long_audio(self):
        """测试完整流程：长音频（分块）"""
        audio_input = create_test_audio_file(1200)
        try:
            MockASR.global_run_count = 0

            chunked = ChunkedASR(
                asr_class=MockASR,
                audio_path=audio_input,
                asr_kwargs={"mock_text_per_second": "Long"},
                chunk_length=480,
                chunk_overlap=10,
                chunk_concurrency=3,
            )

            result = chunked.run()

            # 验证分块转录
            assert MockASR.global_run_count == 3

            # 验证结果完整性
            assert len(result.segments) > 0
            assert all("Long" in seg.text for seg in result.segments)

            # 验证时间戳顺序
            for i in range(len(result.segments) - 1):
                assert result.segments[i].end_time <= result.segments[i + 1].start_time
        finally:
            Path(audio_input).unlink()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
