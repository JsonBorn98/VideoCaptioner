import os
import threading
import time
import uuid
import zlib
from io import BytesIO
from typing import Callable, Optional, Union, cast

from pydub import AudioSegment

from videocaptioner.core.utils.cache import get_asr_cache, is_cache_enabled
from videocaptioner.core.utils.logger import setup_logger

from .asr_data import ASRData, ASRDataSeg

logger = setup_logger("asr")


class ASRResultDegradedError(RuntimeError):
    """ASR returned an anomalous or degraded result.

    Raised by MiMoASR (hallucinated transcript or low alignment coverage) so
    that ChunkedASR can intercept it and retry with smaller audio sub-chunks.
    When retries are exhausted, ``_allow_degraded=True`` is forwarded to let
    the backend fall back to estimated cue timings instead of raising.

    Attributes:
        reason: Short human-readable description of the anomaly.
        text_density: Word/char count per second, when known.
        coverage: Alignment coverage ratio (0..1), when known.
    """

    def __init__(
        self,
        reason: str,
        *,
        text_density: float | None = None,
        coverage: float | None = None,
    ):
        self.reason = reason
        self.text_density = text_density
        self.coverage = coverage
        super().__init__(reason)


class BaseASR:
    """Base class for ASR (Automatic Speech Recognition) implementations.

    Provides common functionality including:
    - Audio file loading and validation
    - CRC32-based file identification
    - Disk caching with automatic key generation
    - Template method pattern for subclass implementation
    - Rate limiting for public charity services
    """

    SUPPORTED_SOUND_FORMAT = ["flac", "m4a", "mp3", "wav"]
    _lock = threading.Lock()

    RATE_LIMIT_MAX_CALLS = 100
    RATE_LIMIT_MAX_DURATION = 360 * 60
    RATE_LIMIT_TIME_WINDOW = 12 * 3600

    def __init__(
        self,
        audio_input: Optional[Union[str, bytes]] = None,
        use_cache: bool = False,
        need_word_time_stamp: bool = False,
        audio_duration: float | None = None,
        cache_identity: str | None = None,
        speech_ranges_ms: list[tuple[int, int]] | None = None,
    ):
        """Initialize ASR with audio data.

        Args:
            audio_input: Path to audio file or raw audio bytes
            use_cache: Whether to cache recognition results
            need_word_time_stamp: Whether to return word-level timestamps
            audio_duration: Known audio duration in seconds, if the caller already has it
            cache_identity: Stable cache identity to use instead of the current byte CRC32
            speech_ranges_ms: Known non-silent ranges relative to this audio input
        """
        self.audio_input = audio_input
        self.file_binary = None
        self.use_cache = use_cache
        self.speech_ranges_ms = [
            (int(start), int(end))
            for start, end in (speech_ranges_ms or [])
            if int(end) > int(start)
        ]
        self._set_data()
        self.cache_identity = str(cache_identity).strip() if cache_identity else self.crc32_hex
        self._cache = get_asr_cache()
        self.audio_duration = (
            float(audio_duration)
            if audio_duration is not None and audio_duration > 0
            else self._get_audio_duration()
        )

    def _set_data(self):
        """Load audio data and compute CRC32 hash for cache key."""
        if isinstance(self.audio_input, bytes):
            self.file_binary = self.audio_input
        elif isinstance(self.audio_input, str):
            ext = self.audio_input.split(".")[-1].lower()
            assert (
                ext in self.SUPPORTED_SOUND_FORMAT
            ), f"Unsupported sound format: {ext}"
            assert os.path.exists(
                self.audio_input
            ), f"File not found: {self.audio_input}"
            with open(self.audio_input, "rb") as f:
                self.file_binary = f.read()
        else:
            raise ValueError("audio_input must be provided as string or bytes")
        crc32_value = zlib.crc32(self.file_binary) & 0xFFFFFFFF
        self.crc32_hex = format(crc32_value, "08x")

    def _get_audio_duration(self) -> float:
        """Get audio duration in seconds using pydub."""
        if not self.file_binary:
            return 0.01
        try:
            audio = AudioSegment.from_file(BytesIO(self.file_binary))
            return audio.duration_seconds
        except Exception as e:
            logger.warning(f"Failed to get audio duration: {e}")
            return 60.0 * 10

    def run(
        self, callback: Optional[Callable[[int, str], None]] = None, **kwargs
    ) -> ASRData:
        """Run ASR with caching support.

        Args:
            callback: Optional progress callback(progress: int, message: str)
            **kwargs: Additional arguments passed to _run()

        Returns:
            ASRData: Recognition results with segments
        """
        total_started = time.perf_counter()
        cache_key = f"{self.__class__.__name__}:{self._get_key()}"
        logger.info(
            "ASR backend 开始: class=%s, audio_duration=%.2fs, cache=%s",
            self.__class__.__name__,
            self.audio_duration,
            self.use_cache and is_cache_enabled(),
        )

        # Try cache first
        if self.use_cache and is_cache_enabled():
            cached_result = cast(
                Optional[dict], self._cache.get(cache_key, default=None)
            )
            if cached_result is not None:
                logger.info(
                    "ASR backend 命中缓存: class=%s, elapsed=%.2fs",
                    self.__class__.__name__,
                    time.perf_counter() - total_started,
                )
                segments = self._make_segments(cached_result)
                return ASRData(segments)

        # Run ASR
        step_started = time.perf_counter()
        resp_data = self._run(callback, **kwargs)
        logger.info(
            "ASR backend 原始响应完成: class=%s, elapsed=%.2fs",
            self.__class__.__name__,
            time.perf_counter() - step_started,
        )

        allow_degraded = bool(kwargs.get("_allow_degraded", False))
        step_started = time.perf_counter()
        segments = self._make_segments(resp_data, _allow_degraded=allow_degraded)
        logger.info(
            "ASR backend 响应解析完成: class=%s, elapsed=%.2fs, segments=%s",
            self.__class__.__name__,
            time.perf_counter() - step_started,
            len(segments),
        )

        # Cache only after the raw response can be converted successfully.
        # This avoids persisting partial API results when post-processing fails.
        # Also respect self.use_cache so retry paths (which set use_cache=False)
        # never write degraded/partial results into the cache.
        if (
            self.use_cache
            and is_cache_enabled()
            and self._should_cache_response(resp_data, segments)
        ):
            self._cache.set(cache_key, resp_data, expire=86400 * 2)
        logger.info(
            "ASR backend 完成: class=%s, total=%.2fs, segments=%s",
            self.__class__.__name__,
            time.perf_counter() - total_started,
            len(segments),
        )
        return ASRData(segments)

    def _should_cache_response(self, resp_data: dict, segments: list[ASRDataSeg]) -> bool:
        """Return whether a successful raw ASR response should be cached."""
        return True

    def _get_key(self) -> str:
        """Get cache key for this ASR request.

        Default implementation uses a stable cache identity. Direct ASR calls
        fall back to the current audio byte CRC32, while chunked callers can
        provide a source-audio range identity that survives re-export details.
        Subclasses can override to include additional parameters.

        Returns:
            Cache key string
        """
        return self.cache_identity

    def _make_segments(
        self, resp_data: dict, _allow_degraded: bool = False
    ) -> list[ASRDataSeg]:
        """Convert ASR response to segment list.

        Args:
            resp_data: Raw response from ASR service
            _allow_degraded: When True, fall back to estimated timings instead
                of raising ASRResultDegradedError on anomalous results.

        Returns:
            List of ASRDataSeg objects
        """
        raise NotImplementedError(
            "_make_segments method must be implemented in subclass"
        )

    def _run(
        self, callback: Optional[Callable[[int, str], None]] = None, **kwargs
    ) -> dict:
        """Execute ASR service and return raw response.

        Args:
            callback: Progress callback(progress: int, message: str)
            **kwargs: Implementation-specific parameters

        Returns:
            Raw response data (dict or str depending on implementation)
        """
        raise NotImplementedError("_run method must be implemented in subclass")

    def _check_rate_limit(self) -> None:
        """Check rate limit for public charity services."""
        service_name = self.__class__.__name__
        tag = f"rate_limit:{service_name}"
        time_limit = time.time() - self.RATE_LIMIT_TIME_WINDOW

        # Query recent records
        try:
            query = "SELECT key FROM Cache WHERE tag = ? AND store_time >= ?"
            results = self._cache._sql(query, (tag, time_limit)).fetchall()
        except Exception as e:
            raise RuntimeError(f"Failed to query rate limit: {e}")

        # Get durations using cache API
        durations = []
        for (key,) in results:
            duration = self._cache.get(key, default=None)
            if duration is not None and isinstance(duration, (int, float)):
                durations.append(duration)

        call_count = len(durations)
        total_duration = sum(durations)

        # Check duration limit
        if total_duration + self.audio_duration > self.RATE_LIMIT_MAX_DURATION:
            error_msg = f"{service_name} duration limit exceeded"
            logger.warning(error_msg)
            raise RuntimeError(error_msg)

        # Check call count limit
        if call_count >= self.RATE_LIMIT_MAX_CALLS:
            error_msg = f"{service_name} call count limit exceeded"
            logger.warning(error_msg)
            raise RuntimeError(error_msg)

        # Record current call (store duration directly as float)
        self._cache.set(
            f"rate_limit_record:{service_name}:{uuid.uuid4()}",
            self.audio_duration,
            tag=tag,
            expire=int(self.RATE_LIMIT_TIME_WINDOW) + 3600,
        )
