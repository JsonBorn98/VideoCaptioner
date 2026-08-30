"""SubtitleOptimizer 可接收规则型后处理注入的额外 prompt 约束。"""

import json

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.llm.models import (
    LLMModelProfile,
    LLMResult,
    LLMTransport,
    ProviderDialect,
)
from videocaptioner.core.optimize.optimize import SubtitleOptimizer


def _profile() -> LLMModelProfile:
    return LLMModelProfile(
        profile_id="optimizer-test",
        name="Optimizer test",
        transport=LLMTransport.OPENAI_COMPATIBLE,
        dialect=ProviderDialect.GENERIC,
        base_url="https://mock.local/v1",
        api_key="test-api-key",
        model="gpt-4o-mini",
    )


def test_optimizer_injects_extra_rules_into_system_prompt():
    captured = {}

    class CapturingGateway:
        def complete(self, profile, request, **kwargs):
            del profile, kwargs
            captured["system"] = request.messages[0].content
            return LLMResult(text=json.dumps({"1": "你好"}, ensure_ascii=False))

    optimizer = SubtitleOptimizer(
        thread_num=1,
        batch_num=5,
        model="gpt-4o-mini",
        custom_prompt="",
        extra_rules="中文引号使用「」/『』。",
        profile=_profile(),
        gateway=CapturingGateway(),
    )

    data = ASRData([ASRDataSeg("你好", 0, 1000)])
    result = optimizer.optimize_subtitle(data)

    assert result.segments[0].text == "你好"
    assert "中文引号使用「」/『』。" in captured["system"]
