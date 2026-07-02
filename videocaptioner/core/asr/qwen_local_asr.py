"""Local Qwen3 ASR backend with Qwen3-ForcedAligner timestamps."""

from __future__ import annotations

from typing import Any, Callable, List, Optional, Union

from ..utils.logger import setup_logger
from .asr_data import ASRDataSeg
from .base import BaseASR
from .qwen_runtime import timestamp_items_to_segments, transcribe_with_qwen
from .text_timing import make_timed_segments, split_transcript_text

logger = setup_logger("qwen_local_asr")


class QwenLocalASR(BaseASR):
    """Local Qwen3 ASR backend.

    The backend uses the official qwen-asr runtime. When word timestamps are
    requested it loads Qwen3-ForcedAligner and returns true word/character
    timestamps instead of estimated timings.
    """

    def __init__(
        self,
        audio_input: Union[str, bytes],
        asr_model: str = "Qwen/Qwen3-ASR-1.7B",
        aligner_model: str = "Qwen/Qwen3-ForcedAligner-0.6B",
        model_dir: str = "",
        language: str = "",
        device: str = "auto",
        dtype: str = "auto",
        max_new_tokens: int = 2048,
        temp_dir: str = "",
        use_cache: bool = False,
        need_word_time_stamp: bool = True,
    ):
        super().__init__(audio_input, use_cache, need_word_time_stamp)
        self.asr_model = asr_model.strip() or "Qwen/Qwen3-ASR-1.7B"
        self.aligner_model = aligner_model.strip() or "Qwen/Qwen3-ForcedAligner-0.6B"
        self.model_dir = model_dir
        self.language = language
        self.device = device or "auto"
        self.dtype = dtype or "auto"
        self.max_new_tokens = max_new_tokens
        self.temp_dir = temp_dir
        self.need_word_time_stamp = need_word_time_stamp

    def _run(
        self, callback: Optional[Callable[[int, str], None]] = None, **kwargs: Any
    ) -> dict:
        if callback:
            callback(5, "Loading Qwen ASR model")

        result = transcribe_with_qwen(
            audio_input=self.audio_input or self.file_binary or b"",
            language=self.language,
            asr_model=self.asr_model,
            aligner_model=self.aligner_model,
            model_dir=self.model_dir,
            device=self.device,
            dtype=self.dtype,
            max_new_tokens=self.max_new_tokens,
            return_time_stamps=self.need_word_time_stamp,
            temp_dir=self.temp_dir,
        )

        if callback:
            callback(100, "Qwen ASR completed")

        return result

    def _make_segments(self, resp_data: dict) -> List[ASRDataSeg]:
        segments = timestamp_items_to_segments(resp_data.get("time_stamps"))
        if segments:
            return segments

        text = str(resp_data.get("text", "")).strip()
        if not text:
            raise ValueError("Qwen ASR response missing transcription text.")

        if self.need_word_time_stamp:
            raise RuntimeError(
                "Qwen ASR was requested to return timestamps, but no timestamps were returned. "
                "Check that Qwen3-ForcedAligner is available and the audio chunk is supported."
            )

        end_time = max(int(self.audio_duration * 1000), 1)
        text_segments = split_transcript_text(text)
        logger.warning(
            "Qwen ASR response has no timestamps; split transcript into %s estimated cues",
            len(text_segments),
        )
        return make_timed_segments(text_segments, end_time)

    def _get_key(self) -> str:
        return (
            f"v2-{self.crc32_hex}-{self.asr_model}-{self.aligner_model}-{self.language}-"
            f"{self.device}-{self.dtype}-{self.max_new_tokens}-{self.need_word_time_stamp}"
        )
