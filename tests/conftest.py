"""Root-level test configuration and shared fixtures."""

import ast
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import pytest

# Set this before importing any project module.  Tests (and Qt subprocesses
# inheriting this environment) must never write the developer's real AppData.
_TEST_APPDATA = tempfile.TemporaryDirectory(
    prefix="videocaptioner-tests-", ignore_cleanup_errors=True
)
os.environ["VIDEOCAPTIONER_APPDATA_PATH"] = _TEST_APPDATA.name

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg  # noqa: E402
from videocaptioner.core.llm.models import LLMResult  # noqa: E402
from videocaptioner.core.translate import SubtitleProcessData, TargetLanguage  # noqa: E402
from videocaptioner.core.utils import cache  # noqa: E402
from videocaptioner.core.utils.text_utils import count_words, is_mainly_cjk  # noqa: E402

# Disable cache for testing
cache.disable_cache()


def pytest_unconfigure(config):
    """Close Windows cache handles before removing the isolated AppData."""

    del config
    for getter in (
        cache.get_gateway_cache,
        cache.get_asr_cache,
        cache.get_translate_cache,
        cache.get_tts_cache,
        cache.get_timing_cache,
    ):
        getter().close()
    _TEST_APPDATA.cleanup()


@pytest.fixture(scope="session", autouse=True)
def _guard_isolated_appdata():
    """Guard against tests regressing to the developer's real settings file."""

    from videocaptioner.config import APPDATA_PATH, ROOT_PATH

    assert APPDATA_PATH == Path(_TEST_APPDATA.name).resolve()
    assert APPDATA_PATH != ROOT_PATH / "AppData"


@pytest.fixture
def sample_asr_data():
    """Create sample ASR data for translation testing."""
    segments = [
        ASRDataSeg(start_time=0, end_time=1000, text="I am a student"),
        ASRDataSeg(start_time=1000, end_time=2000, text="You are a teacher"),
        ASRDataSeg(start_time=2000, end_time=3000, text="VideoCaptioner is a tool for captioning videos"),
    ]
    return ASRData(segments)


@pytest.fixture
def sample_translate_data():
    """Create sample translation data for testing."""
    return [
        SubtitleProcessData(index=1, original_text="I am a student", translated_text=""),
        SubtitleProcessData(index=2, original_text="You are a teacher", translated_text=""),
        SubtitleProcessData(index=3, original_text="VideoCaptioner is a tool for captioning videos", translated_text=""),
    ]


@pytest.fixture
def target_language():
    """Default target language for translation tests."""
    return TargetLanguage.SIMPLIFIED_CHINESE


@pytest.fixture
def check_env_vars():
    """Check if required environment variables are set."""
    def _check(*var_names):
        missing = [var for var in var_names if not os.getenv(var)]
        if missing:
            pytest.skip(f"Required environment variables not set: {', '.join(missing)}")
    return _check


@pytest.fixture
def expected_translations() -> Dict[str, Dict[str, List[str]]]:
    """Expected translation keywords for quality validation."""
    return {
        "简体中文": {
            "I am a student": ["学生"],
            "You are a teacher": ["老师", "教师"],
            "VideoCaptioner is a tool for captioning videos": ["工具"],
        },
        "日本語": {
            "I am a student": ["学生"],
            "You are a teacher": ["先生", "教師"],
        },
        "English": {
            "我是学生": ["student"],
            "你是老师": ["teacher"],
        },
    }


def assert_translation_quality(original: str, translated: str, expected_keywords: List[str]) -> None:
    """Validate translation contains expected keywords."""
    assert translated, f"Translation is empty for: {original}"
    found_keywords = [kw for kw in expected_keywords if kw in translated]
    assert found_keywords, (
        f"Translation quality issue:\n"
        f"  Original: {original}\n"
        f"  Translated: {translated}\n"
        f"  Expected keywords: {expected_keywords}"
    )


def _last_user_content(messages: List[Dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return str(message.get("content", ""))
    return ""


def _parse_dict_from_text(text: str) -> Dict[str, Any]:
    match = re.search(r"<input_subtitle>(.*?)</input_subtitle>", text, re.S)
    if match:
        text = match.group(1)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            parsed = ast.literal_eval(text)
        except (SyntaxError, ValueError):
            return {}

    if isinstance(parsed, dict):
        return {str(key): value for key, value in parsed.items()}
    return {}


def _chunk_cjk_text(text: str, max_count: int) -> List[str]:
    result = []
    pieces = [piece for piece in re.findall(r"[^。！？.!?]+[。！？.!?]?", text) if piece]
    for piece in pieces or [text]:
        current = piece
        while count_words(current) > max_count:
            result.append(current[:max_count])
            current = current[max_count:]
        if current:
            result.append(current)
    return result


def _chunk_english_text(text: str, max_count: int) -> List[str]:
    words = text.split()
    if not words:
        return [text]
    return [
        " ".join(words[index : index + max_count])
        for index in range(0, len(words), max_count)
    ]


def _mock_split_response(messages: List[Dict[str, Any]]) -> str:
    user_content = _last_user_content(messages)
    text = user_content.rsplit("\n", 1)[-1].strip()
    system_content = str(messages[0].get("content", "")) if messages else ""

    cjk_match = re.search(r"CJK.*?≤\s*(\d+)", system_content, re.S)
    english_match = re.search(r"Latin.*?≤\s*(\d+)", system_content, re.S)
    cjk_max = int(cjk_match.group(1)) if cjk_match else 18
    english_max = int(english_match.group(1)) if english_match else 12

    chunks = (
        _chunk_cjk_text(text, cjk_max)
        if is_mainly_cjk(text)
        else _chunk_english_text(text, english_max)
    )
    return "<br>".join(chunks)


class _MockLLMGateway:
    """Fake gateway: replay mock responses at the profile+gateway seam.

    Consumers (split/optimize/llm_translator) dispatch through
    ``LLMGateway.complete``; this fake serves scripted responses without
    environment-variable relays or symbol patching.
    """

    def __init__(self, *args, **kwargs):
        del args, kwargs

    def complete(self, profile, request, **kwargs):
        del kwargs, profile
        messages = [
            {"role": message.role, "content": message.content}
            for message in request.messages
        ]
        user_content = _last_user_content(messages)

        if "Please use multiple <br> tags" in user_content:
            return LLMResult(text=_mock_split_response(messages))

        subtitle_dict = _parse_dict_from_text(user_content)
        if subtitle_dict:
            if "<input_subtitle>" in user_content:
                return LLMResult(
                    text=json.dumps(subtitle_dict, ensure_ascii=False)
                )
            translated = {
                key: f"翻译:{value}"
                for key, value in subtitle_dict.items()
            }
            return LLMResult(text=json.dumps(translated, ensure_ascii=False))

        return LLMResult(text=f"翻译:{user_content}")


@pytest.fixture
def mock_llm_client(monkeypatch):
    """Mock OpenAI-compatible LLM calls used by split/translate/optimize tests.

    Patches the gateway constructor in every consumer module so all requests
    resolve through the fake gateway above (the profile+gateway seam; no
    ambient OPENAI_* environment relays remain).
    """

    # split.split is where SubtitleSplitter builds its gateway; split_by_llm
    # covers standalone calls of the module function.
    monkeypatch.setattr(
        "videocaptioner.core.split.split.LLMGateway",
        _MockLLMGateway,
    )
    monkeypatch.setattr(
        "videocaptioner.core.split.split_by_llm.LLMGateway",
        _MockLLMGateway,
    )
    monkeypatch.setattr(
        "videocaptioner.core.optimize.optimize.LLMGateway",
        _MockLLMGateway,
    )
    monkeypatch.setattr(
        "videocaptioner.core.translate.llm_translator.LLMGateway",
        _MockLLMGateway,
    )
    return _MockLLMGateway
