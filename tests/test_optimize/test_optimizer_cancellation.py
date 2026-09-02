"""停止信号不得被当成优化失败：InterruptedError 逐层放行、不进降级计数。"""

import pytest

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.llm.models import (
    LLMModelProfile,
    LLMTransport,
    ProviderDialect,
)
from videocaptioner.core.optimize.optimize import SubtitleOptimizer


def _profile() -> LLMModelProfile:
    return LLMModelProfile(
        profile_id="optimizer-cancel-test",
        name="Optimizer cancel test",
        transport=LLMTransport.OPENAI_COMPATIBLE,
        dialect=ProviderDialect.GENERIC,
        base_url="https://optimizer-cancel.test/v1",
        api_key="secret",
        model="optimizer-cancel-model",
    )


class _CancelledGateway:
    """模拟停止后 gateway 的行为：cancelled 置位即抛 InterruptedError。"""

    def complete(self, profile, request, *, cancelled=None, **kwargs):
        del profile, request, kwargs
        raise InterruptedError("LLM request cancelled")


def test_optimize_cancel_propagates_without_failed_batch_counting():
    optimizer = SubtitleOptimizer(
        thread_num=1,
        batch_num=5,
        model="ignored-when-profile-is-set",
        custom_prompt="",
        profile=_profile(),
        gateway=_CancelledGateway(),
    )

    with pytest.raises(InterruptedError):
        optimizer.optimize_subtitle(ASRData([ASRDataSeg("大家好", 0, 1000)]))

    # 取消不是质量降级：不得进 failed_batches（ADR-0009 降级口径）。
    assert optimizer.failed_batches == 0
