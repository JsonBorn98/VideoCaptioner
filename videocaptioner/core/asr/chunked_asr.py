"""音频分块 ASR 装饰器

为任何 BaseASR 实现添加音频分块转录能力，适用于长音频处理。
使用装饰器模式实现关注点分离。
"""

import io
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, List, Optional, Tuple

from pydub import AudioSegment

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
        audio_path: str,
        asr_kwargs: Optional[dict] = None,
        chunk_length: int = DEFAULT_CHUNK_LENGTH_SEC,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP_SEC,
        chunk_concurrency: int = DEFAULT_CHUNK_CONCURRENCY,
    ):
        self.asr_class = asr_class
        self.audio_path = audio_path
        self.asr_kwargs = asr_kwargs or {}
        self.chunk_length_ms = chunk_length * MS_PER_SECOND
        self.chunk_overlap_ms = chunk_overlap * MS_PER_SECOND
        self.chunk_concurrency = chunk_concurrency

        # Reading完整音频文件（用于分块）
        with open(audio_path, "rb") as f:
            self.file_binary = f.read()

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

    def _split_audio(self) -> List[Tuple[bytes, int]]:
        """使用 pydub 将音频切割为重叠的块

        Returns:
            List[(chunk_bytes, offset_ms), ...]
            每个元素包含音频块的字节数据和时间偏移（毫秒）
        """
        # 从字节数据加载音频
        if self.file_binary is None:
            raise ValueError("file_binary is None, cannot split audio")

        try:
            audio = AudioSegment.from_file(self.audio_path)
        except Exception:
            logger.warning("Failed to load audio by path, falling back to in-memory bytes")
            audio = AudioSegment.from_file(io.BytesIO(self.file_binary))
        total_duration_ms = len(audio)

        logger.debug(
            f"音频总时长: {total_duration_ms/1000:.1f}s, "
            f"分块长度: {self.chunk_length_ms/1000:.1f}s, "
            f"重叠: {self.chunk_overlap_ms/1000:.1f}s"
        )

        chunks = []
        start_ms = 0

        while start_ms < total_duration_ms:
            end_ms = min(start_ms + self.chunk_length_ms, total_duration_ms)
            chunk = audio[start_ms:end_ms]

            buffer = io.BytesIO()
            chunk.export(buffer, format="mp3")
            chunk_bytes = buffer.getvalue()

            chunks.append((chunk_bytes, start_ms))
            logger.debug(
                f"切割 chunk {len(chunks)}: "
                f"{start_ms/1000:.1f}s - {end_ms/1000:.1f}s ({len(chunk_bytes)} bytes)"
            )

            # 下一个块的起始位置（有重叠）
            start_ms += self.chunk_length_ms - self.chunk_overlap_ms

            # 如果已到末尾，停止
            if end_ms >= total_duration_ms:
                break

        # logger.debug(f"音频切割完成，共 {len(chunks)} 个块")
        return chunks

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

    def _run_single_asr(
        self,
        *,
        audio_bytes: bytes,
        callback: Optional[Callable[[int, str], None]],
        allow_degraded: bool = False,
        use_cache: bool = True,
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
        asr_kwargs = {**self.asr_kwargs, "use_cache": use_cache}
        chunk_asr = self.asr_class(audio_bytes, **asr_kwargs)
        return chunk_asr.run(callback, _allow_degraded=allow_degraded)

    def _split_chunk_bytes(self, chunk_bytes: bytes, n: int) -> List[bytes]:
        """把一个 chunk 的 mp3 bytes 拆成 n 个等长子块的 mp3 bytes。"""
        audio = AudioSegment.from_file(io.BytesIO(chunk_bytes))
        total_ms = len(audio)
        sub_len = total_ms // n
        sub_chunks: List[bytes] = []
        for i in range(n):
            start = i * sub_len
            end = (i + 1) * sub_len if i < n - 1 else total_ms
            buf = io.BytesIO()
            audio[start:end].export(buf, format="mp3")
            sub_chunks.append(buf.getvalue())
        return sub_chunks

    def _transcribe_with_retry(
        self,
        *,
        idx: int,
        total_chunks: int,
        chunk_bytes: bytes,
        offset_ms: int,
        callback: Optional[Callable[[int, str], None]],
    ) -> ASRData:
        """分层重试: 首次 → 同块重试 → 拆 2 子块 → 拆 3 子块 → 降级。

        首次尝试走正常缓存路径；ASRResultDegradedError 触发重试，重试时
        关闭缓存（避免异常结果污染缓存）。子块结果在拼接时加上相对父 chunk
        起点的偏移，最终 ASRData 的时间戳仍是相对 chunk 起点的，与主 chunk
        路径一致——绝对偏移统一由 ``ChunkMerger`` 添加。
        """
        # 首次尝试: 走正常缓存路径
        try:
            return self._run_single_asr(
                audio_bytes=chunk_bytes,
                callback=callback,
                use_cache=self.asr_kwargs.get("use_cache", False),
            )
        except ASRResultDegradedError as exc:
            logger.info(
                "chunk %s/%s 首次异常 (%s)，进入重试",
                idx + 1,
                total_chunks,
                exc.reason,
            )

        # 第 1 次重试: 同块重新请求（防偶发网关问题），关闭缓存
        try:
            return self._run_single_asr(
                audio_bytes=chunk_bytes,
                callback=callback,
                use_cache=False,
            )
        except ASRResultDegradedError as exc:
            logger.info(
                "chunk %s/%s 同块重试仍异常 (%s)，拆子块",
                idx + 1,
                total_chunks,
                exc.reason,
            )

        # 第 2、3 次重试: 拆子块
        chunk_audio = AudioSegment.from_file(io.BytesIO(chunk_bytes))
        chunk_duration_ms = len(chunk_audio)
        for attempt_num, n_splits in enumerate(SUBCHUNK_SPLITS, start=2):
            sub_bytes_list = self._split_chunk_bytes(chunk_bytes, n_splits)
            sub_duration_ms = chunk_duration_ms // n_splits
            sub_results: List[ASRData] = []
            all_ok = True
            for j, sub_bytes in enumerate(sub_bytes_list):
                try:
                    sub_asr_data = self._run_single_asr(
                        audio_bytes=sub_bytes,
                        callback=callback,
                        use_cache=False,
                    )
                    # 子块时间戳是相对子块起点的，加上相对父 chunk 起点的
                    # 偏移 (j * sub_duration_ms)，使其与主 chunk 路径一样
                    # 都是相对 chunk 起点的。绝对偏移仍由 ChunkMerger 添加。
                    relative_offset = j * sub_duration_ms
                    if relative_offset > 0:
                        for seg in sub_asr_data.segments:
                            seg.start_time += relative_offset
                            seg.end_time += relative_offset
                    sub_results.append(sub_asr_data)
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
                merged_segments: List[ASRDataSeg] = []
                for sub in sub_results:
                    merged_segments.extend(sub.segments)
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
        )

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
