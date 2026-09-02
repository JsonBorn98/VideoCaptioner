"""停止信号不得被当成翻译失败：InterruptedError 逐层放行、不进失败计数。"""

import pytest

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.llm.models import (
    LLMModelProfile,
    LLMTransport,
    ProviderDialect,
)
from videocaptioner.core.translate.llm_translator import LLMTranslator
from videocaptioner.core.translate.types import TargetLanguage


def _profile() -> LLMModelProfile:
    return LLMModelProfile(
        profile_id="translator-cancel-test",
        name="Translator cancel test",
        transport=LLMTransport.OPENAI_COMPATIBLE,
        dialect=ProviderDialect.GENERIC,
        base_url="https://translator-cancel.test/v1",
        api_key="secret",
        model="translator-cancel-model",
    )


class _CancelledGateway:
    """模拟停止后 gateway 的行为：cancelled 置位即抛 InterruptedError。"""

    def complete(self, profile, request, *, cancelled=None, **kwargs):
        del profile, request, kwargs
        raise InterruptedError("LLM request cancelled")


def test_translate_cancel_propagates_without_failure_counting():
    translator = LLMTranslator(
        thread_num=1,
        batch_num=5,
        target_language=TargetLanguage.SIMPLIFIED_CHINESE,
        model="ignored-when-profile-is-set",
        custom_prompt="",
        is_reflect=False,
        update_callback=None,
        source_language="auto",
        profile=_profile(),
        gateway=_CancelledGateway(),
    )

    with pytest.raises(InterruptedError):
        translator.translate_subtitle(ASRData([ASRDataSeg("hello", 0, 500)]))

    # 取消不是翻译失败：不得进 failed_count、不得包装成「检查 API key」。
    assert translator.failed_count == 0
