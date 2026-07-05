"""音频分块 ASR 装饰器

为任何 BaseASR 实现添加音频分块转录能力，适用于长音频处理。
使用装饰器模式实现关注点分离。
"""

import hashlib
import importlib
import inspect
import io
import math
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from typing import Any, Callable, List, Literal, Optional, Tuple, cast

from pydub import AudioSegment
from pydub.silence import detect_nonsilent, detect_silence

from ..utils.logger import setup_logger
from .asr_data import ASRData, ASRDataSeg
from .base import ASRResultDegradedError, BaseASR
from .chunk_merger import ChunkMerger

logger = setup_logger("chunked_asr")

# 常量定义
MS_PER_SECOND = 1000
DEFAULT_CHUNK_LENGTH_SEC = 60 * 10  # 10 minutes
DEFAULT_CHUNK_OVERLAP_SEC = 10  # 10秒重叠
DEFAULT_CHUNK_CONCURRENCY = 3  # 3个并发
DEFAULT_BOUNDARY_SEARCH_BEFORE_SEC = 20
DEFAULT_BOUNDARY_SEARCH_AFTER_SEC = 10
DEFAULT_MIN_SILENCE_DURATION_MS = 400
DEFAULT_SILENCE_SEEK_STEP_MS = 50
DEFAULT_MIN_SPEECH_DURATION_MS = 250
DEFAULT_MIN_SPEECH_RATIO = 0.005
DEFAULT_RETRY_SUBCHUNK_OVERLAP_MS = 3000

# 分层重试: ASR 返回异常文本或对齐覆盖率过低时，先同块重试，再拆子块。
# 第 1 次重试: 同块重新请求（防偶发网关问题）。
# 第 2、3 次重试: 拆成 2/3 个等长子块分别转录，子块更短更稳定。
# 3 次仍失败: 以 _allow_degraded=True 降级为估算时间戳。
SUBCHUNK_SPLITS = (2, 3)


class ChunkedASR:
    """音频分块 ASR 包装器

    为任何 BaseASR 子类添加音频分块能力。
    适用于长音频的分块转录，避免 API 超时或内存溢出。

    工作流程:
        1. 将长音频切割为多个重叠的块
        2. 为每个块创建独立的 ASR 实例并发转录
        3. 使用 ChunkMerger 合并结果，消除重叠区域的重复内容

    示例:
        >>> # 使用 ASR 类和参数创建分块转录器
        >>> chunked_asr = ChunkedASR(
        ...     asr_class=BcutASR,
        ...     audio_path="long_audio.mp3",
        ...     asr_kwargs={"need_word_time_stamp": True},
        ...     chunk_length=1200
        ... )
        >>> result = chunked_asr.run(callback)

    Args:
        asr_class: ASR 类（非实例），如 BcutASR, JianYingASR
        audio_path: 音频文件路径
        asr_kwargs: 传递给 ASR 构造函数的参数字典
        chunk_length: 每块长度（秒），默认 480 秒（8分钟）
        chunk_overlap: 块之间重叠时长（秒），默认 10 秒
        chunk_concurrency: 并发转录数量，默认 3
    """

    def __init__(
        self,
        asr_class: type[BaseASR],
        audio_path: str | None = None,
        asr_kwargs: Optional[dict] = None,
        chunk_length: int = DEFAULT_CHUNK_LENGTH_SEC,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP_SEC,
        chunk_concurrency: int = DEFAULT_CHUNK_CONCURRENCY,
        chunk_audio_format: str = "mp3",
        audio_input: str | None = None,
        retry_same_chunk: bool = True,
        chunk_boundary_mode: Literal["fixed", "silence", "vad"] = "fixed",
        boundary_search_before: int = DEFAULT_BOUNDARY_SEARCH_BEFORE_SEC,
        boundary_search_after: int = DEFAULT_BOUNDARY_SEARCH_AFTER_SEC,
        min_silence_duration_ms: int = DEFAULT_MIN_SILENCE_DURATION_MS,
        silence_seek_step_ms: int = DEFAULT_SILENCE_SEEK_STEP_MS,
        min_speech_duration_ms: int = DEFAULT_MIN_SPEECH_DURATION_MS,
        min_speech_ratio: float = DEFAULT_MIN_SPEECH_RATIO,
        max_chunk_payload_bytes: int | None = None,
        pass_source_range: bool = False,
    ):
        if audio_path is None:
            audio_path = audio_input
        if audio_path is None:
            raise ValueError("audio_path must be provided")
        self.asr_class = asr_class
        self.audio_path = audio_path
        self.asr_kwargs = asr_kwargs or {}
        self.chunk_length_ms = chunk_length * MS_PER_SECOND
        self.chunk_overlap_ms = chunk_overlap * MS_PER_SECOND
        self.chunk_concurrency = chunk_concurrency
        self.chunk_audio_format = (chunk_audio_format or "mp3").lower().lstrip(".")
        self.retry_same_chunk = retry_same_chunk
        self.chunk_boundary_mode = chunk_boundary_mode
        self.boundary_search_before_ms = max(0, boundary_search_before * MS_PER_SECOND)
        self.boundary_search_after_ms = max(0, boundary_search_after * MS_PER_SECOND)
        self.min_silence_duration_ms = max(1, min_silence_duration_ms)
        self.silence_seek_step_ms = max(1, silence_seek_step_ms)
        self.min_speech_duration_ms = max(1, min_speech_duration_ms)
        self.min_speech_ratio = max(0.0, min_speech_ratio)
        self.max_chunk_payload_bytes = (
            int(max_chunk_payload_bytes)
            if max_chunk_payload_bytes is not None and max_chunk_payload_bytes > 0
            else None
        )
        self.pass_source_range = bool(pass_source_range)
        self._silent_chunk_offsets: set[int] = set()
        self._chunk_durations_ms: dict[int, int] = {}
        self._asr_parameter_support: dict[str, bool] = {}
        self._source_audio: AudioSegment | None = None
        self._silero_vad: tuple[Any, Callable[..., Any]] | None = None
        self._silero_unavailable = False

        # Reading完整音频文件（用于分块）
        with open(audio_path, "rb") as f:
            self.file_binary = f.read()
        self._source_cache_id = hashlib.sha1(self.file_binary).hexdigest()

    def run(self, callback: Optional[Callable[[int, str], None]] = None) -> ASRData:
        """执行分块转录

        Args:
            callback: 进度回调函数(progress: int, message: str)

        Returns:
            ASRData: 合并后的转录结果
        """
        total_started = time.perf_counter()
        logger.info(
            "分块 ASR 开始: asr=%s, audio=%s, chunk_length=%.1fs, overlap=%.1fs, concurrency=%s",
            self.asr_class.__name__,
            self.audio_path,
            self.chunk_length_ms / MS_PER_SECOND,
            self.chunk_overlap_ms / MS_PER_SECOND,
            self.chunk_concurrency,
        )

        # 1. 分块音频
        step_started = time.perf_counter()
        chunks = self._split_audio()
        logger.info(
            "音频分块完成: chunks=%s, elapsed=%.2fs",
            len(chunks),
            time.perf_counter() - step_started,
        )

        # 2. 如果只有一块，仍走重试逻辑（单 chunk 无需 ChunkMerger）
        if len(chunks) == 1:
            logger.debug("Audio shorter than chunk length, direct transcription")
            chunk_bytes, _ = chunks[0]
            step_started = time.perf_counter()
            result = self._transcribe_with_retry(
                idx=0,
                total_chunks=1,
                chunk_bytes=chunk_bytes,
                offset_ms=0,
                callback=callback,
            )
            logger.info(
                "单块 ASR 完成: asr=%s, elapsed=%.2fs, total=%.2fs, segments=%s",
                self.asr_class.__name__,
                time.perf_counter() - step_started,
                time.perf_counter() - total_started,
                len(result.segments),
            )
            return result

        logger.debug(f"Audio split into {len(chunks)}  chunks, starting parallel transcription")

        # 3. 并发转录All块
        step_started = time.perf_counter()
        chunk_results = self._transcribe_chunks(chunks, callback)
        logger.info(
            "全部分块转录完成: chunks=%s, elapsed=%.2fs",
            len(chunk_results),
            time.perf_counter() - step_started,
        )

        # 4. 合并结果
        step_started = time.perf_counter()
        merged_result = self._merge_results(chunk_results, chunks)
        logger.info(
            "分块结果合并完成: elapsed=%.2fs, segments=%s",
            time.perf_counter() - step_started,
            len(merged_result.segments),
        )

        logger.debug(f"Chunk transcription complete, {len(merged_result.segments)}  segments")
        logger.info(
            "分块 ASR 完成: asr=%s, total=%.2fs, segments=%s",
            self.asr_class.__name__,
            time.perf_counter() - total_started,
            len(merged_result.segments),
        )
        return merged_result

    def _load_audio(self) -> AudioSegment:
        """Load the source audio once for chunk planning/export."""
        if self.file_binary is None:
            raise ValueError("file_binary is None, cannot split audio")
        try:
            return AudioSegment.from_file(self.audio_path)
        except Exception:
            logger.warning("Failed to load audio by path, falling back to in-memory bytes")
            return AudioSegment.from_file(io.BytesIO(self.file_binary))

    def _split_audio(self) -> List[Tuple[bytes, int]]:
        """使用 pydub 将音频切割为重叠的块

        Returns:
            List[(chunk_bytes, offset_ms), ...]
            每个元素包含音频块的字节数据和时间偏移（毫秒）
        """
        audio = self._load_audio()
        self._source_audio = audio
        total_duration_ms = len(audio)

        logger.debug(
            f"音频总时长: {total_duration_ms/1000:.1f}s, "
            f"分块长度: {self.chunk_length_ms/1000:.1f}s, "
            f"重叠: {self.chunk_overlap_ms/1000:.1f}s"
        )

        spans = self._plan_chunk_spans(audio)
        return self._export_chunks(audio, spans)

    def _plan_chunk_spans(self, audio: AudioSegment) -> list[tuple[int, int]]:
        total_duration_ms = len(audio)
        if self._uses_smart_boundaries():
            return self._plan_silence_chunk_spans(audio)
        return self._plan_fixed_chunk_spans(total_duration_ms)

    def _uses_smart_boundaries(self) -> bool:
        return self.chunk_boundary_mode in {"silence", "vad"}

    def _plan_fixed_chunk_spans(self, total_duration_ms: int) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        start_ms = 0

        while start_ms < total_duration_ms:
            end_ms = min(start_ms + self.chunk_length_ms, total_duration_ms)
            spans.append((start_ms, end_ms))

            # 下一个块的起始位置（有重叠）
            start_ms += self.chunk_length_ms - self.chunk_overlap_ms

            # 如果已到末尾，停止
            if end_ms >= total_duration_ms:
                break

        return spans

    def _plan_silence_chunk_spans(self, audio: AudioSegment) -> list[tuple[int, int]]:
        total_duration_ms = len(audio)
        if total_duration_ms <= self.chunk_length_ms:
            return [(0, total_duration_ms)]

        silence_ranges = self._detect_silence_ranges(audio)
        spans: list[tuple[int, int]] = []
        start_ms = 0
        min_chunk_length_ms = max(1000, min(self.chunk_length_ms // 2, 60_000))

        while start_ms < total_duration_ms:
            hard_limit = min(start_ms + self.chunk_length_ms, total_duration_ms)
            if hard_limit >= total_duration_ms:
                spans.append((start_ms, total_duration_ms))
                break

            target_ms = max(start_ms, hard_limit - self.boundary_search_after_ms)
            search_start = max(
                start_ms + min_chunk_length_ms,
                target_ms - self.boundary_search_before_ms,
            )
            search_end = min(hard_limit, target_ms + self.boundary_search_after_ms)
            boundary_ms = self._choose_silence_boundary(
                silence_ranges,
                search_start,
                search_end,
                target_ms,
            )
            if boundary_ms is None:
                boundary_ms = hard_limit

            if boundary_ms <= start_ms:
                boundary_ms = hard_limit

            spans.append((start_ms, boundary_ms))
            next_start = max(0, boundary_ms - self.chunk_overlap_ms)
            if next_start <= start_ms:
                next_start = boundary_ms
            start_ms = next_start

        logger.info(
            "静音边界分块完成: chunks=%s, fixed_max=%.1fs",
            len(spans),
            self.chunk_length_ms / MS_PER_SECOND,
        )
        return spans

    def _detect_silence_ranges(self, audio: AudioSegment) -> list[tuple[int, int]]:
        if self.chunk_boundary_mode == "vad":
            return self._speech_ranges_to_silence_ranges(
                self._detect_speech_ranges(audio),
                len(audio),
            )
        return self._detect_energy_silence_ranges(audio)

    def _detect_energy_silence_ranges(self, audio: AudioSegment) -> list[tuple[int, int]]:
        silence_thresh = self._silence_threshold(audio)
        ranges = detect_silence(
            audio,
            min_silence_len=self.min_silence_duration_ms,
            silence_thresh=silence_thresh,
            seek_step=self.silence_seek_step_ms,
        )
        normalized = [(max(0, start), min(len(audio), end)) for start, end in ranges]
        logger.debug(
            "静音检测完成: ranges=%s, threshold=%.1fdBFS",
            len(normalized),
            silence_thresh,
        )
        return normalized

    @staticmethod
    def _silence_threshold(audio: AudioSegment) -> int:
        if audio.dBFS == float("-inf") or math.isinf(audio.dBFS):
            return -50
        return int(min(audio.dBFS - 16.0, -35.0))

    def _choose_silence_boundary(
        self,
        silence_ranges: list[tuple[int, int]],
        search_start: int,
        search_end: int,
        target_ms: int,
    ) -> int | None:
        best_boundary: int | None = None
        best_score: tuple[int, int] | None = None

        for silence_start, silence_end in silence_ranges:
            candidate_start = max(silence_start, search_start)
            candidate_end = min(silence_end, search_end)
            if candidate_end - candidate_start < self.min_silence_duration_ms:
                continue

            boundary = (candidate_start + candidate_end) // 2
            distance = abs(boundary - target_ms)
            duration = candidate_end - candidate_start
            score = (distance, -duration)
            if best_score is None or score < best_score:
                best_score = score
                best_boundary = boundary

        return best_boundary

    def _export_chunks(
        self,
        audio: AudioSegment,
        spans: list[tuple[int, int]],
    ) -> List[Tuple[bytes, int]]:
        chunks: list[tuple[bytes, int]] = []
        self._silent_chunk_offsets = set()
        self._chunk_durations_ms = {}
        for start_ms, end_ms in spans:
            self._export_chunk_span(audio, start_ms, end_ms, chunks)

        return chunks

    def _export_chunk_span(
        self,
        audio: AudioSegment,
        start_ms: int,
        end_ms: int,
        chunks: list[tuple[bytes, int]],
    ) -> None:
        start_ms = max(0, int(start_ms))
        end_ms = min(len(audio), max(start_ms, int(end_ms)))
        if end_ms <= start_ms:
            return

        chunk = cast(AudioSegment, audio[start_ms:end_ms])
        self._chunk_durations_ms[start_ms] = max(1, end_ms - start_ms)
        is_silent_chunk = (
            self._uses_smart_boundaries()
            and self._is_effectively_silent(chunk)
        )
        if is_silent_chunk:
            self._silent_chunk_offsets.add(start_ms)
            chunks.append((b"", start_ms))
            logger.debug(
                "跳过导出静音 chunk %s: %.1fs - %.1fs",
                len(chunks),
                start_ms / MS_PER_SECOND,
                end_ms / MS_PER_SECOND,
            )
            return

        if self.pass_source_range:
            chunks.append((b"", start_ms))
            logger.debug(
                "切割 chunk %s: %.1fs - %.1fs (source range)",
                len(chunks),
                start_ms / MS_PER_SECOND,
                end_ms / MS_PER_SECOND,
            )
            return

        buffer = io.BytesIO()
        chunk.export(buffer, format=self.chunk_audio_format)
        chunk_bytes = buffer.getvalue()
        if (
            self.max_chunk_payload_bytes is not None
            and len(chunk_bytes) > self.max_chunk_payload_bytes
            and end_ms - start_ms > MS_PER_SECOND
        ):
            self._chunk_durations_ms.pop(start_ms, None)
            split_at = self._payload_limited_split_point(
                start_ms,
                end_ms,
                len(chunk_bytes),
            )
            logger.info(
                "chunk payload too large, splitting by estimated bitrate: "
                "%.1fs - %.1fs (%s bytes > %s bytes)",
                start_ms / MS_PER_SECOND,
                end_ms / MS_PER_SECOND,
                len(chunk_bytes),
                self.max_chunk_payload_bytes,
            )
            self._export_chunk_span(audio, start_ms, split_at, chunks)
            self._export_chunk_span(audio, split_at, end_ms, chunks)
            return

        chunks.append((chunk_bytes, start_ms))
        logger.debug(
            f"切割 chunk {len(chunks)}: "
            f"{start_ms/1000:.1f}s - {end_ms/1000:.1f}s ({len(chunk_bytes)} bytes)"
        )

    def _payload_limited_split_point(
        self,
        start_ms: int,
        end_ms: int,
        payload_bytes: int,
    ) -> int:
        duration_ms = max(1, end_ms - start_ms)
        if not self.max_chunk_payload_bytes or payload_bytes <= 0:
            return start_ms + duration_ms // 2
        estimated_ms = int(
            duration_ms * self.max_chunk_payload_bytes / payload_bytes * 0.95
        )
        estimated_ms = max(MS_PER_SECOND, min(duration_ms - 1, estimated_ms))
        return start_ms + estimated_ms

    def _is_effectively_silent(self, audio: AudioSegment) -> bool:
        if len(audio) <= 0:
            return True
        if audio.dBFS == float("-inf") or math.isinf(audio.dBFS):
            return True

        nonsilent_ranges = self._detect_speech_ranges(audio)
        speech_ms = sum(max(0, end - start) for start, end in nonsilent_ranges)
        min_speech_ms = max(
            self.min_speech_duration_ms,
            int(len(audio) * self.min_speech_ratio),
        )
        return speech_ms < min_speech_ms

    def _detect_speech_ranges(self, audio: AudioSegment) -> list[tuple[int, int]]:
        if self.chunk_boundary_mode == "vad":
            silero_ranges = self._detect_silero_speech_ranges(audio)
            if silero_ranges is not None:
                return silero_ranges
        return self._detect_energy_speech_ranges(audio)

    def _detect_energy_speech_ranges(self, audio: AudioSegment) -> list[tuple[int, int]]:
        ranges = detect_nonsilent(
            audio,
            min_silence_len=self.min_silence_duration_ms,
            silence_thresh=self._silence_threshold(audio),
            seek_step=self.silence_seek_step_ms,
        )
        return [(max(0, start), min(len(audio), end)) for start, end in ranges]

    def _detect_silero_speech_ranges(
        self, audio: AudioSegment
    ) -> list[tuple[int, int]] | None:
        vad = self._load_silero_vad()
        if vad is None:
            return None

        model, get_speech_timestamps = vad
        sample_rate = 16_000
        try:
            torch_module = importlib.import_module("torch")
            pcm = audio.set_channels(1).set_frame_rate(sample_rate).set_sample_width(2)
            samples = pcm.get_array_of_samples()
            waveform = (
                torch_module.tensor(samples, dtype=torch_module.float32) / 32768.0
            )
            timestamps = get_speech_timestamps(
                waveform,
                model,
                sampling_rate=sample_rate,
                min_speech_duration_ms=self.min_speech_duration_ms,
            )
        except Exception as exc:
            logger.info("Silero VAD failed; falling back to energy detection: %s", exc)
            return None

        ranges: list[tuple[int, int]] = []
        for item in timestamps or []:
            try:
                start_sample = int(item["start"])
                end_sample = int(item["end"])
            except (KeyError, TypeError, ValueError):
                continue
            start_ms = int(round(start_sample / sample_rate * MS_PER_SECOND))
            end_ms = int(round(end_sample / sample_rate * MS_PER_SECOND))
            start_ms = max(0, min(len(audio), start_ms))
            end_ms = max(start_ms, min(len(audio), end_ms))
            if end_ms - start_ms >= self.min_speech_duration_ms:
                ranges.append((start_ms, end_ms))

        return ranges

    def _load_silero_vad(self) -> tuple[Any, Callable[..., Any]] | None:
        if self._silero_vad is not None:
            return self._silero_vad
        if self._silero_unavailable:
            return None

        try:
            try:
                silero_module = importlib.import_module("silero_vad")
                get_speech_timestamps = getattr(
                    silero_module,
                    "get_speech_timestamps",
                )
                load_silero_vad = getattr(silero_module, "load_silero_vad")

                try:
                    model = load_silero_vad(onnx=False)
                except TypeError:
                    model = load_silero_vad()
            except ModuleNotFoundError:
                torch_module = importlib.import_module("torch")
                hub_kwargs = {
                    "repo_or_dir": "snakers4/silero-vad",
                    "model": "silero_vad",
                    "verbose": False,
                }
                try:
                    model, utils = torch_module.hub.load(
                        **hub_kwargs,
                        trust_repo=True,
                    )
                except TypeError:
                    model, utils = torch_module.hub.load(**hub_kwargs)
                get_speech_timestamps = utils[0]
        except Exception as exc:
            self._silero_unavailable = True
            logger.info("Silero VAD unavailable; using energy detection: %s", exc)
            return None

        self._silero_vad = (model, get_speech_timestamps)
        return self._silero_vad

    @staticmethod
    def _speech_ranges_to_silence_ranges(
        speech_ranges: list[tuple[int, int]],
        total_duration_ms: int,
    ) -> list[tuple[int, int]]:
        silence_ranges: list[tuple[int, int]] = []
        cursor = 0
        for speech_start, speech_end in sorted(speech_ranges):
            speech_start = max(0, min(total_duration_ms, speech_start))
            speech_end = max(speech_start, min(total_duration_ms, speech_end))
            if speech_start > cursor:
                silence_ranges.append((cursor, speech_start))
            cursor = max(cursor, speech_end)
        if cursor < total_duration_ms:
            silence_ranges.append((cursor, total_duration_ms))
        return silence_ranges

    def _is_silent_audio_bytes(
        self, audio_bytes: bytes, offset_ms: int | None = None
    ) -> bool:
        if (
            offset_ms is not None
            and self._uses_smart_boundaries()
            and offset_ms in self._silent_chunk_offsets
        ):
            return True
        try:
            audio = AudioSegment.from_file(io.BytesIO(audio_bytes))
        except Exception:
            return False
        return self._is_effectively_silent(audio)

    def _is_silent_audio_range(
        self,
        audio_bytes: bytes,
        *,
        marker_offset_ms: int | None = None,
        source_offset_ms: int | None = None,
        audio_duration_ms: int | None = None,
    ) -> bool:
        if (
            marker_offset_ms is not None
            and self._uses_smart_boundaries()
            and marker_offset_ms in self._silent_chunk_offsets
        ):
            return True
        if (
            self._source_audio is not None
            and source_offset_ms is not None
            and audio_duration_ms is not None
        ):
            start = max(0, int(source_offset_ms))
            end = min(len(self._source_audio), start + max(1, int(audio_duration_ms)))
            if end > start:
                return self._is_effectively_silent(
                    cast(AudioSegment, self._source_audio[start:end])
                )
        return self._is_silent_audio_bytes(audio_bytes, marker_offset_ms)

    def _speech_ranges_for_audio_range(
        self,
        audio_bytes: bytes,
        *,
        source_offset_ms: int | None = None,
        audio_duration_ms: int | None = None,
    ) -> list[tuple[int, int]] | None:
        if not self._uses_smart_boundaries():
            return None

        audio: AudioSegment | None = None
        if (
            self._source_audio is not None
            and source_offset_ms is not None
            and audio_duration_ms is not None
        ):
            start = max(0, int(source_offset_ms))
            end = min(len(self._source_audio), start + max(1, int(audio_duration_ms)))
            if end > start:
                audio = cast(AudioSegment, self._source_audio[start:end])
        elif audio_bytes:
            try:
                audio = AudioSegment.from_file(io.BytesIO(audio_bytes))
            except Exception:
                return None

        if audio is None or len(audio) <= 0:
            return None
        ranges = self._detect_speech_ranges(audio)
        return ranges or None

    def _transcribe_chunks(
        self,
        chunks: List[Tuple[bytes, int]],
        callback: Optional[Callable[[int, str], None]],
    ) -> List[ASRData]:
        """并发转录多个音频块

        Args:
            chunks: 音频块列表 [(chunk_bytes, offset_ms), ...]
            callback: 进度回调

        Returns:
            List[ASRData]: 每个块的转录结果
        """
        if self._supports_two_stage_pipeline():
            return self._transcribe_chunks_two_stage(chunks, callback)
        if self._supports_batch_transcribe():
            return self._transcribe_chunks_batch(chunks, callback)

        results: List[Optional[ASRData]] = [None] * len(chunks)
        total_chunks = len(chunks)

        # 进度追踪: 记录每个 chunk 的进度，确保整体进度单调递增
        chunk_progress = [0] * total_chunks
        last_overall = 0
        progress_lock = threading.Lock()

        def transcribe_single_chunk(
            idx: int, chunk_bytes: bytes, offset_ms: int
        ) -> Tuple[int, ASRData]:
            """转录单个音频块 - 为每个块创建独立的 ASR 实例"""
            nonlocal last_overall
            chunk_started = time.perf_counter()
            logger.info(
                "分块转录开始: chunk=%s/%s, offset=%.2fs, bytes=%s",
                idx + 1,
                total_chunks,
                offset_ms / MS_PER_SECOND,
                len(chunk_bytes),
            )

            def chunk_callback(progress: int, message: str):
                nonlocal last_overall
                if not callback:
                    return
                with progress_lock:
                    chunk_progress[idx] = progress
                    overall = sum(chunk_progress) // total_chunks
                    # 只允许进度单调递增
                    if overall > last_overall:
                        last_overall = overall
                        callback(overall, f"{idx+1}/{total_chunks}: {message}")

            asr_data = self._transcribe_with_retry(
                idx=idx,
                total_chunks=total_chunks,
                chunk_bytes=chunk_bytes,
                offset_ms=offset_ms,
                callback=chunk_callback,
            )

            logger.info(
                "分块转录完成: chunk=%s/%s, elapsed=%.2fs, segments=%s",
                idx + 1,
                total_chunks,
                time.perf_counter() - chunk_started,
                len(asr_data.segments),
            )
            return idx, asr_data

        executor = ThreadPoolExecutor(max_workers=self.chunk_concurrency)
        futures = {}
        next_chunk_index = 0

        def submit_next_chunk() -> None:
            nonlocal next_chunk_index
            if next_chunk_index >= total_chunks:
                return
            chunk_bytes, offset = chunks[next_chunk_index]
            future = executor.submit(
                transcribe_single_chunk,
                next_chunk_index,
                chunk_bytes,
                offset,
            )
            futures[future] = next_chunk_index
            next_chunk_index += 1

        for _ in range(min(self.chunk_concurrency, total_chunks)):
            submit_next_chunk()

        try:
            while futures:
                future = next(as_completed(futures))
                futures.pop(future)
                idx, asr_data = future.result()
                results[idx] = asr_data
                submit_next_chunk()
        except BaseException:
            logger.exception("分块转录失败，取消剩余 chunk", extra={"suppress_console": True})
            for pending_future in futures:
                pending_future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True)

        logger.debug(f"All {total_chunks}  chunks transcription complete")
        return [r for r in results if r is not None]  # 过滤 None

    def _supports_two_stage_pipeline(self) -> bool:
        return all(
            hasattr(self.asr_class, method_name)
            for method_name in ("run_transcript_stage", "run_alignment_stage")
        )

    def _supports_batch_transcribe(self) -> bool:
        return hasattr(self.asr_class, "run_batch_instances")

    def _transcribe_chunks_batch(
        self,
        chunks: List[Tuple[bytes, int]],
        callback: Optional[Callable[[int, str], None]],
    ) -> List[ASRData]:
        """Run first-pass chunk transcription with a backend batch API."""
        results: list[ASRData | None] = [None] * len(chunks)
        batch_entries: list[tuple[int, bytes, int, BaseASR]] = []
        total_chunks = len(chunks)
        last_overall = 0
        progress_lock = threading.Lock()

        def update_progress(progress: int, message: str) -> None:
            nonlocal last_overall
            if not callback:
                return
            progress = max(0, min(100, progress))
            with progress_lock:
                if progress > last_overall:
                    last_overall = progress
                    callback(progress, message)

        for idx, (chunk_bytes, offset_ms) in enumerate(chunks):
            chunk_duration_ms = self._chunk_durations_ms.get(offset_ms)
            if self._uses_smart_boundaries() and self._is_silent_audio_range(
                chunk_bytes,
                marker_offset_ms=offset_ms,
                source_offset_ms=offset_ms,
                audio_duration_ms=chunk_duration_ms,
            ):
                logger.info(
                    "batch chunk %s/%s 是静音块，跳过 ASR",
                    idx + 1,
                    total_chunks,
                )
                results[idx] = ASRData([])
                continue

            cache_identity = self._chunk_cache_identity(offset_ms, chunk_duration_ms)
            asr = self._create_asr_instance(
                audio_bytes=chunk_bytes,
                use_cache=self.asr_kwargs.get("use_cache", False),
                audio_duration_ms=chunk_duration_ms,
                cache_identity=cache_identity,
                source_audio_path=self.audio_path if self.pass_source_range else None,
                source_start_ms=offset_ms,
                speech_ranges_ms=self._speech_ranges_for_audio_range(
                    chunk_bytes,
                    source_offset_ms=offset_ms,
                    audio_duration_ms=chunk_duration_ms,
                ),
            )
            batch_entries.append((idx, chunk_bytes, offset_ms, asr))

        if not batch_entries:
            return [result for result in results if result is not None]

        update_progress(5, "Qwen batch transcription queued")
        logger.info(
            "批量分块转录开始: asr=%s, chunks=%s",
            self.asr_class.__name__,
            len(batch_entries),
        )
        run_batch_instances = getattr(self.asr_class, "run_batch_instances")
        batch_outputs = run_batch_instances(
            [entry[3] for entry in batch_entries],
            lambda progress, message: update_progress(
                min(80, max(5, progress)),
                message,
            ),
        )
        if len(batch_outputs) != len(batch_entries):
            raise RuntimeError(
                "Batch ASR returned "
                f"{len(batch_outputs)} result(s) for {len(batch_entries)} chunk(s)."
            )
        update_progress(80, "Qwen batch transcription completed")

        for (idx, chunk_bytes, offset_ms, _), batch_output in zip(
            batch_entries,
            batch_outputs,
        ):
            if isinstance(batch_output, ASRData):
                chunk_duration_ms = self._chunk_durations_ms.get(offset_ms)
                if (
                    self._uses_smart_boundaries()
                    and not batch_output.has_data()
                    and not self._is_silent_audio_range(
                        chunk_bytes,
                        marker_offset_ms=offset_ms,
                        source_offset_ms=offset_ms,
                        audio_duration_ms=chunk_duration_ms,
                    )
                ):
                    batch_output = ASRResultDegradedError(
                        "ASR returned empty result for non-silent chunk"
                    )
                else:
                    results[idx] = batch_output
                    continue

            if isinstance(batch_output, ASRResultDegradedError):
                logger.info(
                    "batch chunk %s/%s 首次异常 (%s)，进入重试",
                    idx + 1,
                    total_chunks,
                    batch_output.reason,
                )
                results[idx] = self._transcribe_with_retry(
                    idx=idx,
                    total_chunks=total_chunks,
                    chunk_bytes=chunk_bytes,
                    offset_ms=offset_ms,
                    callback=lambda progress, message: update_progress(
                        min(99, max(80, progress)),
                        message,
                    ),
                    skip_initial_attempt=True,
                    initial_error=batch_output,
                )
                continue

            if isinstance(batch_output, BaseException):
                raise batch_output

            raise RuntimeError("Batch ASR returned an invalid chunk result.")

        update_progress(100, "Qwen batch chunks completed")
        logger.debug("All %s batch chunks transcription complete", total_chunks)
        return [result for result in results if result is not None]

    def _transcribe_chunks_two_stage(
        self,
        chunks: List[Tuple[bytes, int]],
        callback: Optional[Callable[[int, str], None]],
    ) -> List[ASRData]:
        """Run API transcription and local alignment as separate pipeline stages."""
        results: list[ASRData | None] = [None] * len(chunks)
        transcript_results: dict[int, tuple[BaseASR, dict]] = {}
        pending_futures = set()
        total_chunks = len(chunks)

        chunk_progress = [0] * total_chunks
        last_overall = 0
        progress_lock = threading.Lock()

        def update_chunk_progress(idx: int, progress: int, message: str) -> None:
            nonlocal last_overall
            if not callback:
                return
            with progress_lock:
                chunk_progress[idx] = max(chunk_progress[idx], progress)
                overall = sum(chunk_progress) // total_chunks
                if overall > last_overall:
                    last_overall = overall
                    callback(overall, f"{idx+1}/{total_chunks}: {message}")

        def transcript_stage(idx: int, chunk_bytes: bytes, offset_ms: int):
            chunk_duration_ms = self._chunk_durations_ms.get(offset_ms)
            if self._uses_smart_boundaries() and self._is_silent_audio_range(
                chunk_bytes,
                marker_offset_ms=offset_ms,
                source_offset_ms=offset_ms,
                audio_duration_ms=chunk_duration_ms,
            ):
                logger.info("pipeline chunk %s/%s 是静音块，跳过 ASR", idx + 1, total_chunks)
                return idx, None, ASRData([])

            cache_identity = self._chunk_cache_identity(offset_ms, chunk_duration_ms)
            speech_ranges_ms = self._speech_ranges_for_audio_range(
                chunk_bytes,
                source_offset_ms=offset_ms,
                audio_duration_ms=chunk_duration_ms,
            )
            asr = self._create_asr_instance(
                audio_bytes=chunk_bytes,
                use_cache=self.asr_kwargs.get("use_cache", False),
                audio_duration_ms=chunk_duration_ms,
                cache_identity=cache_identity,
                source_audio_path=self.audio_path if self.pass_source_range else None,
                source_start_ms=offset_ms,
                speech_ranges_ms=speech_ranges_ms,
            )
            logger.info(
                "MiMo pipeline API 阶段开始: chunk=%s/%s, offset=%.2fs",
                idx + 1,
                total_chunks,
                offset_ms / MS_PER_SECOND,
            )
            update_chunk_progress(idx, 5, "MiMo API transcription queued")
            run_transcript_stage = getattr(asr, "run_transcript_stage")
            response = run_transcript_stage(
                lambda progress, message: update_chunk_progress(
                    idx,
                    min(80, max(5, progress)),
                    message,
                )
            )
            update_chunk_progress(idx, 80, "MiMo API transcription completed")
            return idx, (asr, response), None

        with ThreadPoolExecutor(max_workers=self.chunk_concurrency) as executor:
            for idx, (chunk_bytes, offset_ms) in enumerate(chunks):
                pending_futures.add(
                    executor.submit(transcript_stage, idx, chunk_bytes, offset_ms)
                )

            next_align_index = 0
            try:
                while pending_futures or next_align_index < total_chunks:
                    done = set()
                    if pending_futures:
                        done, pending_futures = wait(
                            pending_futures,
                            timeout=0 if next_align_index in transcript_results else None,
                            return_when=FIRST_COMPLETED,
                        )
                    for future in done:
                        idx, stage_result, silent_result = future.result()
                        if silent_result is not None:
                            results[idx] = silent_result
                            update_chunk_progress(idx, 100, "Silent chunk skipped")
                        elif stage_result is not None:
                            transcript_results[idx] = stage_result

                    while next_align_index < total_chunks and (
                        results[next_align_index] is not None
                        or next_align_index in transcript_results
                    ):
                        if results[next_align_index] is not None:
                            next_align_index += 1
                            continue
                        asr, response = transcript_results.pop(next_align_index)
                        chunk_bytes, offset_ms = chunks[next_align_index]
                        try:
                            update_chunk_progress(
                                next_align_index,
                                85,
                                "Qwen alignment queued",
                            )
                            run_alignment_stage = getattr(asr, "run_alignment_stage")
                            asr_data = run_alignment_stage(
                                response,
                                lambda progress, message: update_chunk_progress(
                                    next_align_index,
                                    min(99, max(85, progress)),
                                    message,
                                ),
                            )
                            if (
                                self._uses_smart_boundaries()
                                and not asr_data.has_data()
                                and not self._is_silent_audio_range(
                                    chunk_bytes,
                                    marker_offset_ms=offset_ms,
                                    source_offset_ms=offset_ms,
                                    audio_duration_ms=self._chunk_durations_ms.get(
                                        offset_ms
                                    ),
                                )
                            ):
                                raise ASRResultDegradedError(
                                    "ASR returned empty result for non-silent chunk"
                                )
                            results[next_align_index] = asr_data
                            update_chunk_progress(
                                next_align_index,
                                100,
                                "Pipeline chunk completed",
                            )
                        except ASRResultDegradedError as exc:
                            logger.info(
                                "MiMo pipeline chunk %s/%s 对齐异常 (%s)，进入重试阶梯",
                                next_align_index + 1,
                                total_chunks,
                                exc.reason,
                            )
                            results[next_align_index] = self._transcribe_with_retry(
                                idx=next_align_index,
                                total_chunks=total_chunks,
                                chunk_bytes=chunk_bytes,
                                offset_ms=offset_ms,
                                callback=lambda progress, message: update_chunk_progress(
                                    next_align_index,
                                    progress,
                                    message,
                                ),
                            )
                        next_align_index += 1
            except BaseException:
                logger.exception(
                    "MiMo pipeline 失败，取消剩余 API chunk",
                    extra={"suppress_console": True},
                )
                for pending_future in pending_futures:
                    pending_future.cancel()
                raise

        logger.debug("All %s pipeline chunks transcription complete", total_chunks)
        return [result for result in results if result is not None]

    def _create_asr_instance(
        self,
        *,
        audio_bytes: bytes,
        use_cache: bool = True,
        audio_duration_ms: int | None = None,
        cache_identity: str | None = None,
        source_audio_path: str | None = None,
        source_start_ms: int | None = None,
        speech_ranges_ms: list[tuple[int, int]] | None = None,
    ) -> BaseASR:
        """Create one ASR instance with the chunk metadata this backend accepts."""
        asr_kwargs = {**self.asr_kwargs, "use_cache": use_cache}
        if audio_duration_ms is not None and self._asr_accepts_parameter(
            "audio_duration"
        ):
            asr_kwargs["audio_duration"] = audio_duration_ms / MS_PER_SECOND
        if cache_identity is not None and self._asr_accepts_parameter(
            "cache_identity"
        ):
            asr_kwargs["cache_identity"] = cache_identity
        if source_audio_path is not None and self._asr_accepts_parameter(
            "source_audio_path"
        ):
            asr_kwargs["source_audio_path"] = source_audio_path
        if source_start_ms is not None and self._asr_accepts_parameter(
            "source_start_ms"
        ):
            asr_kwargs["source_start_ms"] = source_start_ms
        if audio_duration_ms is not None and self._asr_accepts_parameter(
            "source_duration_ms"
        ):
            asr_kwargs["source_duration_ms"] = audio_duration_ms
        if speech_ranges_ms is not None and self._asr_accepts_parameter(
            "speech_ranges_ms"
        ):
            asr_kwargs["speech_ranges_ms"] = speech_ranges_ms
        return self.asr_class(audio_bytes, **asr_kwargs)

    def _run_single_asr(
        self,
        *,
        audio_bytes: bytes,
        callback: Optional[Callable[[int, str], None]],
        allow_degraded: bool = False,
        use_cache: bool = True,
        audio_duration_ms: int | None = None,
        cache_identity: str | None = None,
        source_audio_path: str | None = None,
        source_start_ms: int | None = None,
        speech_ranges_ms: list[tuple[int, int]] | None = None,
    ) -> ASRData:
        """创建单个 ASR 实例运行，返回相对 chunk 起点的 ASRData。

        返回的 segments 时间戳是相对该音频块起点的，**不含 chunk offset**。
        主 chunk 路径的绝对偏移由 ``ChunkMerger._adjust_timestamps`` 统一添加；
        子块路径的相对偏移在 ``_transcribe_with_retry`` 拼接时添加。

        Args:
            audio_bytes: 音频块字节数据。
            callback: 进度回调。
            allow_degraded: 为 True 时走降级路径（估算时间戳），不抛异常。
            use_cache: 是否读写 ASR 缓存（重试时通常关闭）。
        """
        chunk_asr = self._create_asr_instance(
            audio_bytes=audio_bytes,
            use_cache=use_cache,
            audio_duration_ms=audio_duration_ms,
            cache_identity=cache_identity,
            source_audio_path=source_audio_path,
            source_start_ms=source_start_ms,
            speech_ranges_ms=speech_ranges_ms,
        )
        return chunk_asr.run(callback, _allow_degraded=allow_degraded)

    def _asr_accepts_audio_duration(self) -> bool:
        return self._asr_accepts_parameter("audio_duration")

    def _asr_accepts_parameter(self, parameter_name: str) -> bool:
        if parameter_name in self._asr_parameter_support:
            return self._asr_parameter_support[parameter_name]
        try:
            signature = inspect.signature(self.asr_class.__init__)
        except (TypeError, ValueError):
            accepts = False
        else:
            accepts = parameter_name in signature.parameters or any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            )
        self._asr_parameter_support[parameter_name] = accepts
        return accepts

    def _chunk_cache_identity(
        self, start_ms: int, duration_ms: int | None
    ) -> str:
        duration_ms = max(1, int(duration_ms or 1))
        start_ms = max(0, int(start_ms))
        end_ms = start_ms + duration_ms
        return f"chunk-v1-{self._source_cache_id}-{start_ms}-{end_ms}"

    def _run_checked_asr(
        self,
        *,
        audio_bytes: bytes,
        callback: Optional[Callable[[int, str], None]],
        allow_degraded: bool = False,
        use_cache: bool = True,
        offset_ms: int | None = None,
        audio_duration_ms: int | None = None,
        cache_identity: str | None = None,
        source_offset_ms: int | None = None,
    ) -> ASRData:
        """Run ASR and retry empty non-silent chunks instead of accepting them."""
        effective_source_offset_ms = (
            source_offset_ms if source_offset_ms is not None else offset_ms
        )
        if self._uses_smart_boundaries() and self._is_silent_audio_range(
            audio_bytes,
            marker_offset_ms=offset_ms,
            source_offset_ms=effective_source_offset_ms,
            audio_duration_ms=audio_duration_ms,
        ):
            logger.info(
                "跳过静音 chunk%s",
                (
                    f" offset={effective_source_offset_ms / MS_PER_SECOND:.2f}s"
                    if effective_source_offset_ms is not None
                    else ""
                ),
            )
            return ASRData([])

        speech_ranges_ms = self._speech_ranges_for_audio_range(
            audio_bytes,
            source_offset_ms=effective_source_offset_ms,
            audio_duration_ms=audio_duration_ms,
        )
        result = self._run_single_asr(
            audio_bytes=audio_bytes,
            callback=callback,
            allow_degraded=allow_degraded,
            use_cache=use_cache,
            audio_duration_ms=audio_duration_ms,
            cache_identity=cache_identity,
            source_audio_path=self.audio_path if self.pass_source_range else None,
            source_start_ms=effective_source_offset_ms,
            speech_ranges_ms=speech_ranges_ms,
        )
        if (
            self._uses_smart_boundaries()
            and not allow_degraded
            and not result.has_data()
            and not self._is_silent_audio_range(
                audio_bytes,
                marker_offset_ms=offset_ms,
                source_offset_ms=effective_source_offset_ms,
                audio_duration_ms=audio_duration_ms,
            )
        ):
            raise ASRResultDegradedError("ASR returned empty result for non-silent chunk")
        return result

    def _split_chunk_bytes(
        self,
        chunk_bytes: bytes,
        n: int,
        *,
        source_offset_ms: int | None = None,
        source_duration_ms: int | None = None,
    ) -> List[tuple[bytes, int, int]]:
        """把一个 chunk 的音频 bytes 拆成 n 个子块，返回相对父 chunk 的 offset。"""
        if (
            self._source_audio is not None
            and source_offset_ms is not None
            and source_duration_ms is not None
        ):
            start = max(0, int(source_offset_ms))
            end = min(len(self._source_audio), start + max(1, int(source_duration_ms)))
            audio = cast(AudioSegment, self._source_audio[start:end])
        else:
            audio = AudioSegment.from_file(io.BytesIO(chunk_bytes))
        spans = self._plan_retry_subchunk_spans(audio, n)
        sub_chunks: List[tuple[bytes, int, int]] = []
        for start, end in spans:
            duration = max(1, end - start)
            if self.pass_source_range:
                sub_chunks.append((b"", start, duration))
                continue
            buf = io.BytesIO()
            sub_audio = cast(AudioSegment, audio[start:end])
            sub_audio.export(buf, format=self.chunk_audio_format)
            sub_chunks.append((buf.getvalue(), start, duration))
        return sub_chunks

    def _plan_retry_subchunk_spans(
        self, audio: AudioSegment, n: int
    ) -> list[tuple[int, int]]:
        total_ms = len(audio)
        if n <= 1 or total_ms <= 0:
            return [(0, total_ms)]

        if not self._uses_smart_boundaries():
            sub_len = total_ms // n
            return [
                (i * sub_len, (i + 1) * sub_len if i < n - 1 else total_ms)
                for i in range(n)
            ]

        silence_ranges = self._detect_silence_ranges(audio)
        boundaries = [0]
        min_subchunk_ms = max(1000, total_ms // (n * 3))
        for boundary_index in range(1, n):
            target = int(round(total_ms * boundary_index / n))
            search_start = max(
                boundaries[-1] + min_subchunk_ms,
                target - self.boundary_search_before_ms,
            )
            search_end = min(
                total_ms - min_subchunk_ms,
                target + self.boundary_search_after_ms,
            )
            boundary = None
            if search_start < search_end:
                boundary = self._choose_silence_boundary(
                    silence_ranges,
                    search_start,
                    search_end,
                    target,
                )
            if boundary is None:
                boundary = target
            boundary = max(boundaries[-1] + 1, min(boundary, total_ms - 1))
            boundaries.append(boundary)
        boundaries.append(total_ms)

        overlap_ms = min(
            DEFAULT_RETRY_SUBCHUNK_OVERLAP_MS,
            max(0, total_ms // (n * 4)),
        )
        spans: list[tuple[int, int]] = []
        for i, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
            span_start = max(0, start - overlap_ms) if i > 0 else start
            span_end = min(total_ms, end + overlap_ms) if i < n - 1 else end
            if span_end > span_start:
                spans.append((span_start, span_end))
        return spans

    def _transcribe_with_retry(
        self,
        *,
        idx: int,
        total_chunks: int,
        chunk_bytes: bytes,
        offset_ms: int,
        callback: Optional[Callable[[int, str], None]],
        skip_initial_attempt: bool = False,
        initial_error: ASRResultDegradedError | None = None,
    ) -> ASRData:
        """分层重试: 首次 → 同块重试 → 拆 2 子块 → 拆 3 子块 → 降级。

        首次尝试走正常缓存路径；ASRResultDegradedError 触发重试，重试时
        关闭缓存（避免异常结果污染缓存）。子块结果在拼接时加上相对父 chunk
        起点的偏移，最终 ASRData 的时间戳仍是相对 chunk 起点的，与主 chunk
        路径一致——绝对偏移统一由 ``ChunkMerger`` 添加。
        """
        chunk_duration_ms = self._chunk_durations_ms.get(offset_ms)
        chunk_cache_identity = self._chunk_cache_identity(offset_ms, chunk_duration_ms)
        if self._uses_smart_boundaries() and self._is_silent_audio_bytes(
            chunk_bytes, offset_ms
        ):
            logger.info(
                "chunk %s/%s 是静音块，跳过 ASR",
                idx + 1,
                total_chunks,
            )
            return ASRData([])

        if skip_initial_attempt:
            reason = initial_error.reason if initial_error is not None else "unknown"
            logger.info(
                "chunk %s/%s 首次尝试已失败 (%s)，进入重试",
                idx + 1,
                total_chunks,
                reason,
            )
        else:
            # 首次尝试: 走正常缓存路径
            try:
                return self._run_checked_asr(
                    audio_bytes=chunk_bytes,
                    callback=callback,
                    use_cache=self.asr_kwargs.get("use_cache", False),
                    offset_ms=offset_ms,
                    audio_duration_ms=chunk_duration_ms,
                    cache_identity=chunk_cache_identity,
                    source_offset_ms=offset_ms,
                )
            except ASRResultDegradedError as exc:
                logger.info(
                    "chunk %s/%s 首次异常 (%s)，进入重试",
                    idx + 1,
                    total_chunks,
                    exc.reason,
                )

        if self.retry_same_chunk:
            # 第 1 次重试: 同块重新请求（防偶发网关问题），关闭缓存
            try:
                return self._run_checked_asr(
                    audio_bytes=chunk_bytes,
                    callback=callback,
                    use_cache=False,
                    offset_ms=offset_ms,
                    audio_duration_ms=chunk_duration_ms,
                    cache_identity=chunk_cache_identity,
                    source_offset_ms=offset_ms,
                )
            except ASRResultDegradedError as exc:
                logger.info(
                    "chunk %s/%s 同块重试仍异常 (%s)，拆子块",
                    idx + 1,
                    total_chunks,
                    exc.reason,
                )
        else:
            logger.info(
                "chunk %s/%s 跳过同块重试，直接拆子块",
                idx + 1,
                total_chunks,
            )

        # 第 2、3 次重试: 拆子块
        for attempt_num, n_splits in enumerate(SUBCHUNK_SPLITS, start=2):
            sub_bytes_list = self._split_chunk_bytes(
                chunk_bytes,
                n_splits,
                source_offset_ms=offset_ms,
                source_duration_ms=chunk_duration_ms,
            )
            sub_results: List[ASRData] = []
            sub_offsets: list[int] = []
            all_ok = True
            for j, (sub_bytes, relative_offset, sub_duration_ms) in enumerate(
                sub_bytes_list
            ):
                sub_source_offset_ms = offset_ms + relative_offset
                sub_cache_identity = self._chunk_cache_identity(
                    sub_source_offset_ms,
                    sub_duration_ms,
                )
                try:
                    sub_asr_data = self._run_checked_asr(
                        audio_bytes=sub_bytes,
                        callback=callback,
                        use_cache=False,
                        offset_ms=None,
                        audio_duration_ms=sub_duration_ms,
                        cache_identity=sub_cache_identity,
                        source_offset_ms=sub_source_offset_ms,
                    )
                    sub_results.append(sub_asr_data)
                    sub_offsets.append(relative_offset)
                except ASRResultDegradedError as exc:
                    logger.info(
                        "chunk %s/%s 第%s次重试: 拆 %s 子块 %s/%s 失败 (%s)",
                        idx + 1,
                        total_chunks,
                        attempt_num,
                        n_splits,
                        j + 1,
                        n_splits,
                        exc.reason,
                    )
                    all_ok = False
                    break
            if all_ok:
                overlap_duration = self._subchunk_overlap_duration(sub_offsets)
                if overlap_duration > 0 and len(sub_results) > 1:
                    merged_data = ChunkMerger(
                        min_match_count=2, fuzzy_threshold=0.7
                    ).merge_chunks(
                        chunks=sub_results,
                        chunk_offsets=sub_offsets,
                        overlap_duration=overlap_duration,
                    )
                    merged_segments = merged_data.segments
                else:
                    merged_segments = []
                    for sub, relative_offset in zip(sub_results, sub_offsets):
                        for seg in sub.segments:
                            merged_segments.append(
                                ASRDataSeg(
                                    text=seg.text,
                                    start_time=seg.start_time + relative_offset,
                                    end_time=seg.end_time + relative_offset,
                                    translated_text=seg.translated_text,
                                )
                            )
                logger.info(
                    "chunk %s/%s 拆 %s 子块重试成功，合并 %s segments",
                    idx + 1,
                    total_chunks,
                    n_splits,
                    len(merged_segments),
                )
                return ASRData(merged_segments)

        # 全部重试失败: 降级估算时间戳（不缓存）
        logger.warning(
            "chunk %s/%s 重试 %s 次仍失败，降级为估算时间戳",
            idx + 1,
            total_chunks,
            1 + len(SUBCHUNK_SPLITS),
        )
        return self._run_single_asr(
            audio_bytes=chunk_bytes,
            callback=callback,
            allow_degraded=True,
            use_cache=False,
            audio_duration_ms=chunk_duration_ms,
            cache_identity=chunk_cache_identity,
            source_audio_path=self.audio_path if self.pass_source_range else None,
            source_start_ms=offset_ms,
            speech_ranges_ms=self._speech_ranges_for_audio_range(
                chunk_bytes,
                source_offset_ms=offset_ms,
                audio_duration_ms=chunk_duration_ms,
            ),
        )

    def _subchunk_overlap_duration(self, sub_offsets: list[int]) -> int:
        if not self._uses_smart_boundaries() or len(sub_offsets) < 2:
            return 0
        return DEFAULT_RETRY_SUBCHUNK_OVERLAP_MS

    def _merge_results(
        self, chunk_results: List[ASRData], chunks: List[Tuple[bytes, int]]
    ) -> ASRData:
        """使用 ChunkMerger 合并转录结果

        Args:
            chunk_results: 每个块的 ASRData 结果
            chunks: 原始音频块信息（用于获取 offset）

        Returns:
            合并后的 ASRData
        """
        merger = ChunkMerger(min_match_count=2, fuzzy_threshold=0.7)

        # 提取每个 chunk 的时间偏移
        chunk_offsets = [offset for _, offset in chunks]

        # 合并
        merged = merger.merge_chunks(
            chunks=chunk_results,
            chunk_offsets=chunk_offsets,
            overlap_duration=self.chunk_overlap_ms,
        )
        return merged
