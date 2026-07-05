from typing import Any, Callable, List, Optional, Union

from openai import BadRequestError, OpenAI

from videocaptioner.core.llm.client import normalize_base_url

from ..utils.logger import setup_logger
from .asr_data import ASRDataSeg
from .base import BaseASR

logger = setup_logger("whisper_api")


class WhisperAPI(BaseASR):
    """OpenAI-compatible Whisper API implementation.

    Supports any OpenAI-compatible ASR API endpoint.
    """

    def __init__(
        self,
        audio_input: Union[str, bytes],
        whisper_model: str,
        need_word_time_stamp: bool = False,
        language: str = "zh",
        prompt: str = "",
        base_url: str = "",
        api_key: str = "",
        use_cache: bool = False,
        audio_duration: float | None = None,
        cache_identity: str | None = None,
    ):
        """Initialize Whisper API.

        Args:
            audio_input: Path to audio file or raw audio bytes
            whisper_model: Model name
            need_word_time_stamp: Return word-level timestamps
            language: Language code (default: zh)
            prompt: Initial prompt for model
            base_url: API base URL
            api_key: API key
            use_cache: Enable caching
        """
        super().__init__(
            audio_input,
            use_cache=use_cache,
            need_word_time_stamp=need_word_time_stamp,
            audio_duration=audio_duration,
            cache_identity=cache_identity,
        )

        self.base_url = normalize_base_url(base_url)
        self.api_key = api_key.strip()

        if not self.base_url or not self.api_key:
            raise ValueError("Whisper BASE_URL and API_KEY must be set")

        self.model = whisper_model
        self.language = language
        self.prompt = prompt
        self.need_word_time_stamp = need_word_time_stamp

        self.client = OpenAI(base_url=self.base_url, api_key=self.api_key)

    def _run(
        self, callback: Optional[Callable[[int, str], None]] = None, **kwargs: Any
    ) -> dict:
        """Execute ASR via API."""
        return self._submit()

    def _make_segments(
        self, resp_data: dict, _allow_degraded: bool = False
    ) -> List[ASRDataSeg]:
        """Convert API response to segments."""
        if self.need_word_time_stamp and resp_data.get("words"):
            return [
                ASRDataSeg(
                    text=word["word"],
                    start_time=int(float(word["start"]) * 1000),
                    end_time=int(float(word["end"]) * 1000),
                )
                for word in resp_data["words"]
            ]

        if resp_data.get("segments"):
            return [
                ASRDataSeg(
                    text=seg["text"].strip(),
                    start_time=int(float(seg["start"]) * 1000),
                    end_time=int(float(seg["end"]) * 1000),
                )
                for seg in resp_data["segments"]
            ]

        text = str(resp_data.get("text", "")).strip()
        if text:
            end_time = max(int(self.audio_duration * 1000), 1)
            logger.warning(
                "WhisperAPI response did not include timestamps; using one full-duration segment"
            )
            return [ASRDataSeg(text=text, start_time=0, end_time=end_time)]

        raise ValueError("WhisperAPI response missing both 'segments' and 'text'.")

    def _get_key(self) -> str:
        """Get cache key including model and language."""
        return f"{self.cache_identity}-{self.model}-{self.language}-{self.prompt}"

    def _submit(self) -> dict:
        """Submit audio for transcription."""
        try:
            if self.language == "zh" and not self.prompt:
                self.prompt = "你好，我们需要使用简体中文，以下是普通话的句子"

            if not self.base_url:
                raise ValueError("Whisper BASE_URL must be set")

            attempts: list[tuple[str, dict[str, Any]]] = [
                (
                    "verbose_json with word/segment timestamps",
                    {
                        "response_format": "verbose_json",
                        "timestamp_granularities": ["word", "segment"],
                    },
                ),
                ("verbose_json", {"response_format": "verbose_json"}),
                ("json", {"response_format": "json"}),
                ("text", {"response_format": "text"}),
            ]

            last_bad_request: Exception | None = None
            for label, extra_kwargs in attempts:
                api_kwargs: dict[str, Any] = {
                    "model": self.model,
                    "file": ("audio.mp3", self.file_binary or b"", "audio/mp3"),
                    "prompt": self.prompt,
                    **extra_kwargs,
                }
                # 空字符串表示自动检测，不传 language 参数让 API 自行判断
                if self.language:
                    api_kwargs["language"] = self.language

                try:
                    completion = self.client.audio.transcriptions.create(**api_kwargs)
                    logger.debug("WhisperAPI request succeeded via %s", label)
                    if isinstance(completion, str):
                        return {"text": completion}
                    if hasattr(completion, "to_dict"):
                        return completion.to_dict()
                    if isinstance(completion, dict):
                        return completion
                    return {"text": str(completion)}
                except BadRequestError as exc:
                    # Some OpenAI-compatible ASR endpoints reject timestamp_granularities
                    # or verbose_json. Retry progressively simpler request shapes.
                    last_bad_request = exc
                    logger.warning(
                        "WhisperAPI request attempt failed via %s: %s", label, exc
                    )
                    continue

            if last_bad_request:
                raise last_bad_request
            raise RuntimeError("WhisperAPI request failed before sending any attempt")
        except Exception:
            logger.exception("WhisperAPI failed")
            raise
