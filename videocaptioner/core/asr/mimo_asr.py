import base64
import re
from pathlib import Path
from typing import Any, Callable, List, Optional, Union

from openai import OpenAI

from videocaptioner.core.llm.client import normalize_base_url
from videocaptioner.core.utils.text_utils import count_words, is_mainly_cjk

from ..utils.logger import setup_logger
from .asr_data import ASRDataSeg
from .base import BaseASR
from .qwen_runtime import align_with_qwen, timestamp_items_to_segments

logger = setup_logger("mimo_asr")

MAX_BASE64_AUDIO_SIZE = 10 * 1024 * 1024
MAX_WORD_COUNT_CJK = 25
MAX_WORD_COUNT_ENGLISH = 14
SUPPORTED_AUDIO_FORMATS = {"mp3", "wav"}
MIME_TYPES = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
}


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
    text = re.sub(r"\s+", " ", text.strip())
    # Some ASR gateways return paragraph boundaries without spaces, e.g. "text.Next".
    return re.sub(r"([.!?。！？])(?=(?:[\"'“”‘’(\[]?[A-Z]))", r"\1 ", text)


def _split_long_piece(text: str, max_word_count: int) -> list[str]:
    if count_words(text) <= max_word_count:
        return [text]

    if is_mainly_cjk(text):
        chunks = []
        current = ""
        for char in text:
            current += char
            if count_words(current) >= max_word_count:
                chunks.append(current.strip())
                current = ""
        if current.strip():
            chunks.append(current.strip())
        return chunks

    chunks = []
    current_words: list[str] = []
    for word in text.split():
        next_words = [*current_words, word]
        if current_words and count_words(" ".join(next_words)) > max_word_count:
            chunks.append(" ".join(current_words).strip())
            current_words = [word]
        else:
            current_words = next_words
    if current_words:
        chunks.append(" ".join(current_words).strip())
    return chunks


def _split_transcript_text(text: str) -> list[str]:
    text = _normalize_transcript_text(text)
    if not text:
        return []

    max_word_count = MAX_WORD_COUNT_CJK if is_mainly_cjk(text) else MAX_WORD_COUNT_ENGLISH
    min_word_count = 4 if not is_mainly_cjk(text) else 6

    sentence_pattern = r".+?(?:[.!?。！？]+[\"'”’)\]]*|$)"
    sentence_pieces = [
        piece.strip()
        for piece in re.findall(sentence_pattern, text)
        if piece and piece.strip()
    ]

    pieces: list[str] = []
    for sentence in sentence_pieces:
        pieces.extend(_split_long_piece(sentence, max_word_count))

    segments: list[str] = []
    current = ""
    joiner = "" if is_mainly_cjk(text) else " "
    for piece in pieces:
        if not current:
            current = piece
            continue

        merged = f"{current}{joiner}{piece}".strip()
        if count_words(merged) <= max_word_count or count_words(current) < min_word_count:
            current = merged
        else:
            segments.append(current)
            current = piece

    if current:
        segments.append(current)

    return segments or [text]


def _make_timed_segments(text_segments: list[str], total_ms: int) -> list[ASRDataSeg]:
    if not text_segments:
        return []
    if len(text_segments) == 1:
        return [ASRDataSeg(text=text_segments[0], start_time=0, end_time=max(total_ms, 1))]

    weights = [max(count_words(text), 1) for text in text_segments]
    total_weight = sum(weights)
    segments: list[ASRDataSeg] = []
    start_time = 0
    cumulative_weight = 0

    for index, (text, weight) in enumerate(zip(text_segments, weights)):
        cumulative_weight += weight
        if index == len(text_segments) - 1:
            end_time = total_ms
        else:
            end_time = int(round(total_ms * cumulative_weight / total_weight))
        end_time = max(end_time, start_time + 1)
        segments.append(ASRDataSeg(text=text, start_time=start_time, end_time=end_time))
        start_time = end_time

    return segments


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
        aligner_temp_dir: str = "",
        use_cache: bool = False,
        need_word_time_stamp: bool = False,
    ):
        self.audio_format = _guess_audio_format(audio_input)
        super().__init__(audio_input, use_cache, need_word_time_stamp)

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
        self.aligner_temp_dir = aligner_temp_dir
        self.need_word_time_stamp = need_word_time_stamp

        if not self.api_key:
            raise ValueError("MiMo ASR API Key must be set")

        self.client = OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout,
        )

    def _make_segments(self, resp_data: dict) -> List[ASRDataSeg]:
        aligned_segments = timestamp_items_to_segments(resp_data.get("time_stamps"))
        if aligned_segments:
            return aligned_segments

        text = str(resp_data.get("text", "")).strip()
        if not text:
            logger.warning(
                "MiMo ASR response returned empty text; treating this chunk as silence"
            )
            return []

        if self.need_word_time_stamp:
            raise RuntimeError(
                "MiMo ASR returned text but Qwen3-ForcedAligner did not return timestamps."
            )

        seconds = resp_data.get("seconds")
        if isinstance(seconds, (int, float)) and seconds > 0:
            end_time = int(float(seconds) * 1000)
        else:
            end_time = max(int(self.audio_duration * 1000), 1)

        text_segments = _split_transcript_text(text)
        if len(text_segments) > 1:
            logger.info(
                "MiMo ASR response has no timestamps; split transcript into %s cues",
                len(text_segments),
            )
        return _make_timed_segments(text_segments, end_time)

    def _run(
        self, callback: Optional[Callable[[int, str], None]] = None, **kwargs: Any
    ) -> dict:
        if self.audio_format not in SUPPORTED_AUDIO_FORMATS:
            raise ValueError("MiMo ASR only supports mp3 and wav audio")

        audio_base64 = base64.b64encode(self.file_binary or b"").decode("utf-8")
        if len(audio_base64) > MAX_BASE64_AUDIO_SIZE:
            raise ValueError(
                "MiMo ASR base64 audio payload exceeds 10 MB. "
                "Use a shorter chunk length before calling the API."
            )

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
            "text": _extract_chat_text(response),
            "seconds": _extract_usage_seconds(response),
        }

        if self.need_word_time_stamp and result["text"]:
            if callback:
                callback(90, "Aligning MiMo transcript with Qwen3-ForcedAligner")
            result["time_stamps"] = align_with_qwen(
                audio_input=self.audio_input or self.file_binary or b"",
                transcript=str(result["text"]),
                language=self.language,
                aligner_model=self.aligner_model,
                model_dir=self.aligner_model_dir,
                device=self.aligner_device,
                dtype=self.aligner_dtype,
                temp_dir=self.aligner_temp_dir,
            )

        return result

    def _should_cache_response(self, resp_data: dict, segments: list[ASRDataSeg]) -> bool:
        if not str(resp_data.get("text", "")).strip() and not segments:
            return False
        return True

    def _get_key(self) -> str:
        return (
            f"v2-{self.crc32_hex}-{self.base_url}-{self.model}-{self.language}-"
            f"{self.aligner_model}-{self.aligner_device}-{self.aligner_dtype}-"
            f"{self.need_word_time_stamp}"
        )
