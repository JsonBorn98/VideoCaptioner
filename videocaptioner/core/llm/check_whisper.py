"""Whisper API 连接测试工具"""

from typing import Any, Literal, Optional

import openai

from videocaptioner.config import ASSETS_PATH
from videocaptioner.core.llm.client import normalize_base_url
from videocaptioner.core.utils.logger import setup_logger

# 测试音频文件路径
TEST_AUDIO_PATH = ASSETS_PATH / "en.mp3"

logger = setup_logger("check_whisper")


def _extract_transcription_text(response: Any) -> str:
    """Extract readable text from common OpenAI-compatible transcription responses."""
    if isinstance(response, str):
        return response.strip()

    text = getattr(response, "text", None)
    if text:
        return str(text).strip()

    if hasattr(response, "to_dict"):
        response = response.to_dict()

    if isinstance(response, dict):
        text = response.get("text")
        if text:
            return str(text).strip()

        segments = response.get("segments")
        if isinstance(segments, list):
            segment_text = "".join(
                str(seg.get("text", "")) for seg in segments if isinstance(seg, dict)
            ).strip()
            if segment_text:
                return segment_text

    return str(response)


def check_whisper_connection(
    base_url: str, api_key: str, model: str
) -> tuple[Literal[True], Optional[str]] | tuple[Literal[False], Optional[str]]:
    """
    测试 Whisper API 连接

    使用测试音频文件进行转录测试，并返回转录结果文本。

    参数:
        base_url: API 基础 URL
        api_key: API 密钥
        model: 模型名称

    返回:
        (是否成功, 转录结果文本或Error output)
    """
    try:
        # 检查测试音频文件是否存在
        if not TEST_AUDIO_PATH.exists():
            return False, f"Test audio file not found: {TEST_AUDIO_PATH}"

        # 创建 OpenAI 客户端
        base_url = normalize_base_url(base_url)
        api_key = api_key.strip()
        logger.info("Testing Whisper API connection: base_url=%s, model=%s", base_url, model)
        client = openai.OpenAI(base_url=base_url, api_key=api_key, timeout=60)

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

        last_bad_request = ""
        for label, extra_kwargs in attempts:
            try:
                # Reading音频文件。每次重试都重新打开，避免复用已读完的文件句柄。
                with open(TEST_AUDIO_PATH, "rb") as audio_file:
                    response = client.audio.transcriptions.create(
                        model=model,
                        file=audio_file,
                        timeout=30,
                        **extra_kwargs,
                    )
                resp = _extract_transcription_text(response)
                logger.info("Whisper API connection test succeeded via %s", label)
                return True, resp
            except openai.BadRequestError as e:
                last_bad_request = str(e)
                logger.warning("Whisper API test attempt failed via %s: %s", label, e)
                continue

        return False, "Bad Request Error: " + last_bad_request

    except openai.APIConnectionError:
        logger.exception("Whisper API connection error")
        return False, "API Connection Error. Please check your network or VPN."
    except openai.RateLimitError as e:
        logger.exception("Whisper API rate limit error")
        return False, "Rate Limit Error: " + str(e)
    except openai.AuthenticationError:
        logger.exception("Whisper API authentication error")
        return False, "Authentication Error. Please check your API key."
    except openai.NotFoundError as e:
        logger.exception("Whisper API URL not found")
        return (
            False,
            "Not Found Error. Please check your Base URL and model name. "
            f"Details: {e}",
        )
    except openai.BadRequestError as e:
        logger.exception("Whisper API bad request")
        return False, "Bad Request Error: " + str(e)
    except openai.OpenAIError as e:
        logger.exception("Whisper API OpenAI SDK error")
        return False, "OpenAI Error: " + str(e)
    except FileNotFoundError:
        logger.exception("Whisper API test audio file not found")
        return False, f"Test audio file not found: {TEST_AUDIO_PATH}"
    except Exception as e:
        logger.exception("Unexpected Whisper API connection test error")
        return False, str(e)
