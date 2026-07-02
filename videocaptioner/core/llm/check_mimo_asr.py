"""MiMo ASR API connection test utility."""

from typing import Literal, Optional

import openai

from videocaptioner.config import ASSETS_PATH
from videocaptioner.core.asr.mimo_asr import MiMoASR
from videocaptioner.core.utils.logger import setup_logger

TEST_AUDIO_PATH = ASSETS_PATH / "en.mp3"

logger = setup_logger("check_mimo_asr")


def check_mimo_asr_connection(
    base_url: str,
    api_key: str,
    model: str,
    language: str = "",
    timeout: int = 600,
) -> tuple[Literal[True], Optional[str]] | tuple[Literal[False], Optional[str]]:
    """Test MiMo ASR API connection with the bundled sample audio."""
    try:
        if not TEST_AUDIO_PATH.exists():
            return False, f"Test audio file not found: {TEST_AUDIO_PATH}"

        asr = MiMoASR(
            audio_input=str(TEST_AUDIO_PATH),
            api_key=api_key,
            base_url=base_url,
            model=model,
            language=language,
            timeout=timeout,
            use_cache=False,
        )
        result = asr._run()
        return True, str(result.get("text", ""))
    except openai.APIConnectionError:
        logger.exception("MiMo ASR API connection error")
        return False, "API Connection Error. Please check your network or VPN."
    except openai.RateLimitError as e:
        logger.exception("MiMo ASR API rate limit error")
        return False, "Rate Limit Error: " + str(e)
    except openai.AuthenticationError:
        logger.exception("MiMo ASR API authentication error")
        return False, "Authentication Error. Please check your API key."
    except openai.NotFoundError as e:
        logger.exception("MiMo ASR API not found")
        return (
            False,
            "Not Found Error. Please check your Base URL and model name. "
            f"Details: {e}",
        )
    except openai.BadRequestError as e:
        logger.exception("MiMo ASR API bad request")
        return False, "Bad Request Error: " + str(e)
    except openai.OpenAIError as e:
        logger.exception("MiMo ASR API OpenAI SDK error")
        return False, "OpenAI Error: " + str(e)
    except Exception as e:
        logger.exception("Unexpected MiMo ASR connection test error")
        return False, str(e)
