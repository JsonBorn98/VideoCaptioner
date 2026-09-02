"""断句与字幕优化的消费点构造缝测试。

注入 fake gateway 观察请求形状（profile 传递、timeout 语义、stage/role 标签），
取代旧的环境变量假值 + 符号打补丁缝（ADR-0014 / ticket 11）。
"""

import json

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.llm.models import (
    LLMModelProfile,
    LLMResult,
    LLMTransport,
    ProviderDialect,
)
from videocaptioner.core.optimize.optimize import SubtitleOptimizer
from videocaptioner.core.split.split import SubtitleSplitter
from videocaptioner.core.split.split_by_llm import (
    LLM_SPLIT_REQUEST_TIMEOUT_SECONDS,
    split_by_llm,
)


def _utility_profile() -> LLMModelProfile:
    return LLMModelProfile(
        profile_id="utility-profile",
        name="Utility Profile",
        transport=LLMTransport.OPENAI_COMPATIBLE,
        dialect=ProviderDialect.GENERIC,
        base_url="https://utility.test/v1",
        api_key="secret",
        model="utility-model",
        work_context_tokens=16_384,
        max_concurrency=2,
    )


class _CapturingGateway:
    """Record every (profile, request) pair and replay a canned response."""

    def __init__(self, text: str) -> None:
        self.requests = []
        self._text = text

    def complete(self, profile, request, *, cancelled=None):
        self.requests.append((profile, request, cancelled))
        return LLMResult(text=self._text)


# ---------------------------------------------------------------------------
# 断句（split_by_llm 模块函数 + SubtitleSplitter 载体）
# ---------------------------------------------------------------------------


def test_split_request_carries_profile_timeout_and_labels():
    profile = _utility_profile()
    gateway = _CapturingGateway("第一段<br>第二段")

    result = split_by_llm(
        "第一段第二段",
        max_word_count_cjk=18,
        profile=profile,
        gateway=gateway,
    )

    assert result == ["第一段", "第二段"]
    assert len(gateway.requests) == 1
    used_profile, request, cancelled = gateway.requests[0]
    assert used_profile is profile
    assert cancelled is None
    # 断句保真：30 秒超时必须经 LLMRequest.timeout 显式下发。
    assert request.timeout == LLM_SPLIT_REQUEST_TIMEOUT_SECONDS == 30.0
    assert request.metadata == {"stage": "llm_split", "role": "utility"}
    assert [m.role for m in request.messages] == ["system", "user"]
    assert "第一段第二段" in request.messages[1].content


def test_splitter_forwards_profile_and_gateway_to_module_call():
    profile = _utility_profile()
    gateway = _CapturingGateway("Hello<br>world")

    splitter = SubtitleSplitter(
        thread_num=1,
        model="ignored-when-profile-is-set",
        max_word_count_english=4,
        profile=profile,
        gateway=gateway,
    )

    segments = splitter._process_by_llm(
        [
            ASRDataSeg("Hello", 0, 500),
            ASRDataSeg("world", 500, 1000),
        ]
    )

    assert [seg.text for seg in segments] == ["Hello", "world"]
    assert splitter.profile is profile
    assert splitter.gateway is gateway
    assert len(gateway.requests) == 1
    _, request, _ = gateway.requests[0]
    assert request.timeout == 30.0
    assert request.metadata == {"stage": "llm_split", "role": "utility"}


def test_splitter_lazily_builds_gateway_when_profile_present():
    splitter = SubtitleSplitter(
        thread_num=1,
        model="ignored",
        profile=_utility_profile(),
    )

    assert splitter.profile is not None
    assert splitter.gateway is not None


def test_splitter_without_profile_keeps_gateway_none():
    splitter = SubtitleSplitter(thread_num=1, model="gpt-4o-mini")

    assert splitter.profile is None
    assert splitter.gateway is None


# ---------------------------------------------------------------------------
# 字幕优化（SubtitleOptimizer）
# ---------------------------------------------------------------------------


def _sample_asr_data() -> ASRData:
    return ASRData([ASRDataSeg("大家好啊今天我们来讲一下机器学习", 0, 3000)])


def test_optimize_request_defaults_timeout_and_carries_labels():
    profile = _utility_profile()
    payload = json.dumps({"1": "大家好，今天我们来讲一下机器学习"}, ensure_ascii=False)
    gateway = _CapturingGateway(payload)

    optimizer = SubtitleOptimizer(
        thread_num=1,
        batch_num=5,
        model="ignored-when-profile-is-set",
        custom_prompt="",
        profile=profile,
        gateway=gateway,
    )
    optimizer.optimize_subtitle(_sample_asr_data())

    assert optimizer.profile is profile
    assert optimizer.gateway is gateway
    assert len(gateway.requests) >= 1
    used_profile, request, cancelled = gateway.requests[0]
    assert used_profile is profile
    # 停止门随请求下发：取消回调必须反映优化器的运行状态。
    assert callable(cancelled)
    assert cancelled() is False
    optimizer.stop()
    assert cancelled() is True
    # 字幕优化不传 timeout：落 adapter 构造默认 120 秒，由 gateway 重试兜底。
    assert request.timeout is None
    assert request.metadata == {"stage": "llm_optimize", "role": "utility"}
    assert [m.role for m in request.messages] == ["system", "user"]


def test_optimizer_lazily_builds_gateway_when_profile_present():
    optimizer = SubtitleOptimizer(
        thread_num=1,
        batch_num=5,
        model="ignored",
        custom_prompt="",
        profile=_utility_profile(),
    )

    assert optimizer.profile is not None
    assert optimizer.gateway is not None


def test_optimizer_without_profile_keeps_gateway_none():
    optimizer = SubtitleOptimizer(
        thread_num=1,
        batch_num=5,
        model="gpt-4o-mini",
        custom_prompt="",
    )

    assert optimizer.profile is None
    assert optimizer.gateway is None
