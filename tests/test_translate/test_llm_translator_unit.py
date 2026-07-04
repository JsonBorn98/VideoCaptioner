"""LLMTranslator 单元测试

不依赖外部 LLM API，通过 monkeypatch mock call_llm 验证:
- 反思模式: 嵌套 dict 的 native_translation 被正确提取，不被拍扁
- 普通模式: value 被转为字符串
- 重试耗尽: 抛 ValueError 而非返回 None
"""

import json
from types import SimpleNamespace

import pytest

import videocaptioner.core.translate.llm_translator as llm_translator_module
from videocaptioner.core.entities import SubtitleProcessData
from videocaptioner.core.translate.llm_translator import LLMTranslator
from videocaptioner.core.translate.types import TargetLanguage


def _make_translator(is_reflect: bool = False) -> LLMTranslator:
    return LLMTranslator(
        thread_num=1,
        batch_num=5,
        target_language=TargetLanguage.SIMPLIFIED_CHINESE,
        model="test-model",
        custom_prompt="",
        is_reflect=is_reflect,
        update_callback=None,
    )


def _mock_llm_response(content: str) -> SimpleNamespace:
    """构造一个最小的 call_llm 返回值。"""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


class TestAgentLoopReflectMode:
    """反思模式下 _agent_loop 不应把嵌套 dict 拍扁成字符串。"""

    def test_reflect_mode_preserves_nested_dict(self, monkeypatch):
        """反思模式返回 {key: {"native_translation": "译文", ...}}，
        _agent_loop 应保留嵌套结构，调用方提取 native_translation。
        """
        translator = _make_translator(is_reflect=True)
        subtitle_dict = {"1": "hello", "2": "world"}

        reflect_response = {
            "1": {
                "native_translation": "你好",
                "literal_translation": "你 好",
            },
            "2": {
                "native_translation": "世界",
                "literal_translation": "世 界",
            },
        }
        monkeypatch.setattr(
            llm_translator_module,
            "call_llm",
            lambda **kwargs: _mock_llm_response(
                json.dumps(reflect_response, ensure_ascii=False)
            ),
        )

        result = translator._agent_loop("system prompt", subtitle_dict)

        # Values must remain dicts, not strings.
        assert isinstance(result["1"], dict)
        assert isinstance(result["2"], dict)
        assert result["1"]["native_translation"] == "你好"
        assert result["2"]["native_translation"] == "世界"

    def test_reflect_mode_end_to_end_translated_text(self, monkeypatch):
        """完整 _translate_chunk 流程: 反思模式最终 translated_text == 译文，
        不包含 "native_translation" 或 dict 字符串。
        """
        translator = _make_translator(is_reflect=True)
        subtitle_chunk = [
            SubtitleProcessData(index=1, original_text="hello"),
            SubtitleProcessData(index=2, original_text="world"),
        ]

        reflect_response = {
            "1": {"native_translation": "你好", "literal_translation": "你 好"},
            "2": {"native_translation": "世界", "literal_translation": "世 界"},
        }
        monkeypatch.setattr(
            llm_translator_module,
            "call_llm",
            lambda **kwargs: _mock_llm_response(
                json.dumps(reflect_response, ensure_ascii=False)
            ),
        )

        translator._translate_chunk(subtitle_chunk)

        assert subtitle_chunk[0].translated_text == "你好"
        assert subtitle_chunk[1].translated_text == "世界"
        # Must not contain the dict-as-string artifact.
        assert "native_translation" not in subtitle_chunk[0].translated_text
        assert "{" not in subtitle_chunk[0].translated_text


class TestAgentLoopStandardMode:
    """普通模式下 value 被转为字符串。"""

    def test_standard_mode_returns_string_values(self, monkeypatch):
        translator = _make_translator(is_reflect=False)
        subtitle_dict = {"1": "hello", "2": "world"}

        monkeypatch.setattr(
            llm_translator_module,
            "call_llm",
            lambda **kwargs: _mock_llm_response(
                json.dumps({"1": "你好", "2": "世界"}, ensure_ascii=False)
            ),
        )

        result = translator._agent_loop("system prompt", subtitle_dict)
        assert result["1"] == "你好"
        assert isinstance(result["1"], str)


class TestAgentLoopRetryExhaustion:
    """重试耗尽时抛 ValueError，不返回 None。"""

    def test_raises_after_max_steps(self, monkeypatch):
        translator = _make_translator(is_reflect=False)
        subtitle_dict = {"1": "hello"}

        # Always return invalid JSON (missing keys) so validation fails every step.
        monkeypatch.setattr(
            llm_translator_module,
            "call_llm",
            lambda **kwargs: _mock_llm_response('{"999": "wrong key"}'),
        )

        with pytest.raises(ValueError, match="valid translation dictionary"):
            translator._agent_loop("system prompt", subtitle_dict)

    def test_raises_on_non_dict_response(self, monkeypatch):
        translator = _make_translator(is_reflect=False)
        subtitle_dict = {"1": "hello"}

        monkeypatch.setattr(
            llm_translator_module,
            "call_llm",
            lambda **kwargs: _mock_llm_response("not json at all"),
        )

        with pytest.raises(ValueError, match="valid translation dictionary"):
            translator._agent_loop("system prompt", subtitle_dict)
