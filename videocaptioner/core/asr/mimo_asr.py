import base64
import re
from pathlib import Path
from typing import Any, Callable, List, Optional, Union

from openai import OpenAI

from videocaptioner.core.llm.client import normalize_base_url
from videocaptioner.core.utils.cache import is_cache_enabled
from videocaptioner.core.utils.text_utils import count_words

from ..utils.logger import setup_logger
from . import anomaly as _asr_anomaly
from .asr_data import ASRData, ASRDataSeg
from .base import ASRResultDegradedError, BaseASR
from .qwen_local_asr import run_qwen_alignment_worker
from .qwen_runtime import timestamp_items_to_segments
from .text_timing import (
    make_timed_segments as _make_shared_timed_segments,
)
from .text_timing import (
    normalize_transcript_text as _shared_normalize_transcript_text,
)
from .text_timing import (
    split_long_piece as _shared_split_long_piece,
)
from .text_timing import (
    split_transcript_text as _shared_split_transcript_text,
)

logger = setup_logger("mimo_asr")

MAX_BASE64_AUDIO_SIZE = 10 * 1024 * 1024
MAX_RAW_AUDIO_BYTES_FOR_BASE64 = (MAX_BASE64_AUDIO_SIZE // 4) * 3
MAX_WORD_COUNT_CJK = 25
MAX_WORD_COUNT_ENGLISH = 14
MIN_ALIGNMENT_COVERAGE = _asr_anomaly.MIN_ALIGNMENT_COVERAGE
SUPPORTED_AUDIO_FORMATS = {"mp3", "wav"}
MIME_TYPES = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
}


def _base64_encoded_size(byte_count: int) -> int:
    return 4 * ((max(0, byte_count) + 2) // 3)

_detect_repetition = _asr_anomaly.detect_repetition
_detect_repeated_ngram = _asr_anomaly.detect_repeated_ngram
_check_transcript_anomaly = _asr_anomaly.check_transcript_anomaly
_transcript_repetition_suspicion = _asr_anomaly.transcript_repetition_suspicion


def _normalize_mimo_language(language: str) -> str:
    """Map VideoCaptioner language codes to MiMo ASR API options."""
    language = (language or "").strip().lower()
    if language in {"zh", "en"}:
        return language
    if language in {"yue", "cmn"}:
        return "zh"
    return "auto"


def _guess_audio_format(audio_input: Union[str, bytes]) -> str:
    if isinstance(audio_input, str):
        suffix = Path(audio_input).suffix.lower().lstrip(".")
        if suffix in SUPPORTED_AUDIO_FORMATS:
            return suffix
    return "mp3"


def _extract_chat_text(response: Any) -> str:
    if hasattr(response, "choices") and response.choices:
        content = response.choices[0].message.content
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return "".join(
                str(item.get("text", "")) if isinstance(item, dict) else str(item)
                for item in content
            ).strip()

    if hasattr(response, "to_dict"):
        response = response.to_dict()

    if isinstance(response, dict):
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()

    return str(response).strip()


def _extract_usage_seconds(response: Any) -> float | None:
    usage = getattr(response, "usage", None)
    seconds = getattr(usage, "seconds", None)
    if seconds is not None:
        return float(seconds)

    if hasattr(response, "to_dict"):
        usage_dict = response.to_dict().get("usage", {})
        if isinstance(usage_dict, dict) and usage_dict.get("seconds") is not None:
            return float(usage_dict["seconds"])

    return None


def _normalize_transcript_text(text: str) -> str:
    return _shared_normalize_transcript_text(text)


def _clean_mimo_transcript_text(text: str) -> str:
    """Remove markup tokens sometimes returned by MiMo-compatible gateways."""
    text = str(text or "")
    text = re.sub(
        r"(?i)<\s*/?\s*(?:chinese|english|transcript|text|think|thinking)\s*>",
        " ",
        text,
    )
    text = re.sub(r"(?i)\b(?:think|thinking)\s*>\s*", " ", text)
    return _normalize_transcript_text(text)


def _alignment_coverage(text: str, segments: list[ASRDataSeg]) -> tuple[int, int, float]:
    return _asr_anomaly.alignment_coverage(_clean_mimo_transcript_text(text), segments)


def _clamp_segments_to_duration(
    segments: list[ASRDataSeg], max_ms: int
) -> tuple[list[ASRDataSeg], float]:
    return _asr_anomaly.clamp_segments_to_duration(segments, max_ms)


def _alignment_problems(
    text: str, segments: list[ASRDataSeg], boundary_ms: int, overflow_ratio: float
) -> list[str]:
    return _asr_anomaly.alignment_problems(
        _clean_mimo_transcript_text(text), segments, boundary_ms, overflow_ratio
    )


def _split_long_piece(text: str, max_word_count: int) -> list[str]:
    return _shared_split_long_piece(text, max_word_count)


def _split_transcript_text(text: str) -> list[str]:
    return _shared_split_transcript_text(text)


def _make_timed_segments(
    text_segments: list[str],
    total_ms: int,
    speech_ranges_ms: list[tuple[int, int]] | None = None,
) -> list[ASRDataSeg]:
    return _make_shared_timed_segments(text_segments, total_ms, speech_ranges_ms)


def _make_estimated_segments(
    text: str,
    total_ms: int,
    speech_ranges_ms: list[tuple[int, int]] | None = None,
) -> list[ASRDataSeg]:
    text_segments = _split_transcript_text(text)
    if len(text_segments) > 1:
        logger.info(
            "MiMo ASR response is using estimated timestamps; split transcript into %s cues",
            len(text_segments),
        )
    return _make_timed_segments(text_segments, total_ms, speech_ranges_ms)


class MiMoASR(BaseASR):
    """Xiaomi MiMo ASR API backend.

    MiMo's documented API returns high-quality transcription text without
    segment or word timestamps. When word timestamps are requested, this backend
    runs Qwen3-ForcedAligner locally on the same audio chunk and transcript.
    """

    def __init__(
        self,
        audio_input: Union[str, bytes],
        api_key: str,
        base_url: str = "https://api.xiaomimimo.com/v1",
        model: str = "mimo-v2.5-asr",
        language: str = "",
        timeout: int = 600,
        aligner_model: str = "Qwen/Qwen3-ForcedAligner-0.6B",
        aligner_model_dir: str = "",
        aligner_device: str = "auto",
        aligner_dtype: str = "auto",
        aligner_compile: bool = False,
        aligner_temp_dir: str = "",
        use_cache: bool = False,
        need_word_time_stamp: bool = False,
        audio_duration: float | None = None,
        cache_identity: str | None = None,
        speech_ranges_ms: list[tuple[int, int]] | None = None,
        request_memo: dict[str, dict[str, Any]] | None = None,
    ):
        self.audio_format = _guess_audio_format(audio_input)
        super().__init__(
            audio_input,
            use_cache=use_cache,
            need_word_time_stamp=need_word_time_stamp,
            audio_duration=audio_duration,
            cache_identity=cache_identity,
            speech_ranges_ms=speech_ranges_ms,
        )

        self.api_key = api_key.strip()
        raw_base_url = base_url.strip() or "https://api.xiaomimimo.com/v1"
        self.base_url = normalize_base_url(raw_base_url)
        self.model = model.strip() or "mimo-v2.5-asr"
        self.language = _normalize_mimo_language(language)
        self.timeout = timeout
        self.aligner_model = aligner_model.strip() or "Qwen/Qwen3-ForcedAligner-0.6B"
        self.aligner_model_dir = aligner_model_dir
        self.aligner_device = aligner_device or "auto"
        self.aligner_dtype = aligner_dtype or "auto"
        self.aligner_compile = bool(aligner_compile)
        self.aligner_temp_dir = aligner_temp_dir
        self.need_word_time_stamp = need_word_time_stamp
        self.request_memo = request_memo

        if not self.api_key:
            raise ValueError("MiMo ASR API Key must be set")

        self.client = OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout,
        )

    def _request_memo_key(self) -> str:
        return (
            f"mimo-request-v1:{self.cache_identity}:{self.audio_format}:"
            f"{self.base_url}:{self.model}:{self.language}"
        )

    def _cache_key(self) -> str:
        return f"{self.__class__.__name__}:{self._get_key()}"

    def _boundary_ms(self, resp_data: dict) -> int:
        """Audio boundary (ms) for this chunk, preferring the API's reported seconds."""
        seconds = resp_data.get("seconds")
        if isinstance(seconds, (int, float)) and seconds > 0:
            return int(float(seconds) * 1000)
        return max(int(self.audio_duration * 1000), 1)

    def _make_segments(
        self, resp_data: dict, _allow_degraded: bool = False
    ) -> List[ASRDataSeg]:
        text = _clean_mimo_transcript_text(str(resp_data.get("text", "")))
        if not text:
            logger.warning(
                "MiMo ASR response returned empty text; treating this chunk as silence"
            )
            return []

        end_time = self._boundary_ms(resp_data)

        aligned_segments = timestamp_items_to_segments(resp_data.get("time_stamps"))
        if aligned_segments:
            if self.need_word_time_stamp:
                clamped, overflow_ratio = _clamp_segments_to_duration(
                    aligned_segments, end_time
                )
                problems = _alignment_problems(
                    text, clamped, end_time, overflow_ratio
                )
                if problems:
                    _, _, coverage = _alignment_coverage(text, clamped)
                    reason = "; ".join(problems)
                    if _allow_degraded:
                        # Prefer the clamped aligned segments when they still
                        # place most of the transcript: for a truncated chunk
                        # they carry *correct* timings for everything MiMo did
                        # transcribe, whereas estimated timings smear that text
                        # across the whole chunk and misplace every cue.
                        if clamped and coverage >= MIN_ALIGNMENT_COVERAGE:
                            logger.warning(
                                "MiMo ASR/Qwen alignment degraded (%s); keeping "
                                "clamped aligned segments (text coverage %.0f%%)",
                                reason,
                                coverage * 100,
                            )
                            return clamped
                        logger.warning(
                            "MiMo ASR/Qwen alignment degraded (%s); falling back "
                            "to estimated cue timings",
                            reason,
                        )
                        return _make_estimated_segments(
                            text, end_time, self.speech_ranges_ms
                        )
                    raise ASRResultDegradedError(reason, coverage=coverage)
                return clamped
            return aligned_segments

        if self.need_word_time_stamp:
            if _allow_degraded:
                logger.warning(
                    "MiMo ASR returned text but Qwen3-ForcedAligner did not return "
                    "timestamps; falling back to estimated cue timings"
                )
                return _make_estimated_segments(
                    text, end_time, self.speech_ranges_ms
                )
            raise ASRResultDegradedError(
                "Qwen3-ForcedAligner returned no timestamps for MiMo transcript"
            )

        return _make_estimated_segments(text, end_time, self.speech_ranges_ms)

    def _run(
        self, callback: Optional[Callable[[int, str], None]] = None, **kwargs: Any
    ) -> dict:
        if self.audio_format not in SUPPORTED_AUDIO_FORMATS:
            raise ValueError("MiMo ASR only supports mp3 and wav audio")

        allow_degraded = bool(kwargs.get("_allow_degraded", False))
        skip_alignment = bool(kwargs.get("_skip_alignment", False))

        memo_key = self._request_memo_key()
        result = None
        if self.request_memo is not None:
            memo_result = self.request_memo.get(memo_key)
            if memo_result is not None:
                logger.info("MiMo ASR request memo hit: key=%s", memo_key)
                result = dict(memo_result)

        if result is None:
            file_binary = self.file_binary or b""
            if _base64_encoded_size(len(file_binary)) > MAX_BASE64_AUDIO_SIZE:
                raise ValueError(
                    "MiMo ASR base64 audio payload exceeds 10 MB. "
                    "Use a shorter chunk length before calling the API."
                )
            audio_base64 = base64.b64encode(file_binary).decode("utf-8")

            mime_type = MIME_TYPES[self.audio_format]
            input_audio = {
                "data": f"data:{mime_type};base64,{audio_base64}",
            }
            messages: Any = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": input_audio,
                        }
                    ],
                }
            ]

            logger.info("Calling MiMo ASR: base_url=%s, model=%s", self.base_url, self.model)
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=False,
                timeout=self.timeout,
                extra_body={
                    "asr_options": {
                        "language": self.language,
                    }
                },
            )

            result = {
                "text": _clean_mimo_transcript_text(_extract_chat_text(response)),
                "seconds": _extract_usage_seconds(response),
            }
            if self.request_memo is not None:
                self.request_memo[memo_key] = dict(result)

        if not skip_alignment:
            result = self._align_result_if_needed(
                result,
                callback=callback,
                allow_degraded=allow_degraded,
            )

        return result

    def _align_result_if_needed(
        self,
        result: dict,
        *,
        callback: Optional[Callable[[int, str], None]] = None,
        allow_degraded: bool = False,
    ) -> dict:
        """Attach Qwen timestamps to a MiMo text response when requested."""
        result = dict(result)
        if (
            not self.need_word_time_stamp
            or not result.get("text")
            or result.get("time_stamps")
        ):
            return result

        anomaly = _check_transcript_anomaly(str(result["text"]), self.audio_duration)
        if anomaly:
            density = count_words(str(result["text"])) / max(self.audio_duration, 0.001)
            if not allow_degraded:
                raise ASRResultDegradedError(
                    f"transcript anomaly: {anomaly}", text_density=density
                )
            logger.warning(
                "MiMo transcript anomaly (%s); continuing to align under "
                "degraded mode",
                anomaly,
            )
        elif _transcript_repetition_suspicion(str(result["text"])):
            logger.info(
                "MiMo transcript repeats a phrase (may be genuine speech); "
                "deferring to alignment validation"
            )
        if callback:
            callback(90, "Aligning MiMo transcript with Qwen3-ForcedAligner")
        result["time_stamps"] = run_qwen_alignment_worker(
            audio_input=self.audio_input or self.file_binary or b"",
            transcript=str(result["text"]),
            language=self.language,
            aligner_model=self.aligner_model,
            model_dir=self.aligner_model_dir,
            device=self.aligner_device,
            dtype=self.aligner_dtype,
            compile_aligner=self.aligner_compile,
            temp_dir=self.aligner_temp_dir,
            callback=callback,
        )
        return result

    def run_transcript_stage(
        self,
        callback: Optional[Callable[[int, str], None]] = None,
        **kwargs: Any,
    ) -> dict:
        """Run only the remote MiMo transcription stage.

        A full cached response may already include timestamps; the alignment
        stage will detect that and avoid calling Qwen again.
        """
        if self.use_cache and is_cache_enabled():
            cached_result = self._cache.get(self._cache_key(), default=None)
            if isinstance(cached_result, dict):
                logger.info("MiMo ASR pipeline hit full ASR cache")
                return dict(cached_result)
        return self._run(callback, _skip_alignment=True, **kwargs)

    def run_alignment_stage(
        self,
        resp_data: dict,
        callback: Optional[Callable[[int, str], None]] = None,
        **kwargs: Any,
    ) -> ASRData:
        """Run local alignment/post-processing for a transcript-stage response."""
        allow_degraded = bool(kwargs.get("_allow_degraded", False))
        aligned_resp = self._align_result_if_needed(
            resp_data,
            callback=callback,
            allow_degraded=allow_degraded,
        )
        segments = self._make_segments(
            aligned_resp,
            _allow_degraded=allow_degraded,
        )
        if (
            self.use_cache
            and is_cache_enabled()
            and self._should_cache_response(aligned_resp, segments)
        ):
            self._cache.set(self._cache_key(), aligned_resp, expire=86400 * 2)
        return ASRData(segments)

    def _should_cache_response(self, resp_data: dict, segments: list[ASRDataSeg]) -> bool:
        if not str(resp_data.get("text", "")).strip() and not segments:
            return False
        if self.need_word_time_stamp and not resp_data.get("time_stamps"):
            return False
        if self.need_word_time_stamp and resp_data.get("time_stamps") and not segments:
            return False
        if self.need_word_time_stamp and resp_data.get("time_stamps") and segments:
            text = _clean_mimo_transcript_text(str(resp_data.get("text", "")))
            aligned_segments = timestamp_items_to_segments(resp_data.get("time_stamps"))
            boundary_ms = self._boundary_ms(resp_data)
            clamped, overflow_ratio = _clamp_segments_to_duration(
                aligned_segments, boundary_ms
            )
            problems = _alignment_problems(text, clamped, boundary_ms, overflow_ratio)
            if problems:
                logger.warning(
                    "Skip MiMo ASR cache because alignment looks degraded: %s",
                    "; ".join(problems),
                )
                return False
        return True

    def _get_key(self) -> str:
        return (
            f"v4-{self.cache_identity}-{self.base_url}-{self.model}-{self.language}-"
            f"{self.aligner_model}-{self.aligner_device}-{self.aligner_dtype}-"
            f"{self.aligner_compile}-{self.need_word_time_stamp}"
        )
