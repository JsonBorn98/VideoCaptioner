import pytest

from videocaptioner.core.llm.adapters import LLMAdapter
from videocaptioner.core.llm.gateway import LLMGateway
from videocaptioner.core.llm.models import (
    LLMCallError,
    LLMErrorCategory,
    LLMMessage,
    LLMModelProfile,
    LLMRequest,
    LLMResult,
    LLMTransport,
    ProviderDialect,
)


def _profile() -> LLMModelProfile:
    return LLMModelProfile(
        profile_id="shared",
        name="Shared profile",
        transport=LLMTransport.OPENAI_COMPATIBLE,
        dialect=ProviderDialect.GENERIC,
        base_url="https://example.test/v1",
        api_key="secret",
        model="example-model",
        max_concurrency=2,
    )


REQUEST = LLMRequest(messages=(LLMMessage("user", "hello"),))


class _AlwaysFailAdapter(LLMAdapter):
    def __init__(self, profile, *, retryable):
        super().__init__(profile)
        self.retryable = retryable
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        raise LLMCallError(
            "provider failed",
            category=(
                LLMErrorCategory.TRANSIENT
                if self.retryable
                else LLMErrorCategory.AUTHENTICATION
            ),
            retryable=self.retryable,
        )


def test_gateway_attempts_transient_failure_four_times():
    profile = _profile()
    adapter = _AlwaysFailAdapter(profile, retryable=True)
    sleeps = []
    gateway = LLMGateway(
        adapter_factory=lambda unused: adapter,
        sleep=sleeps.append,
        random_source=lambda: 0.5,
    )

    with pytest.raises(LLMCallError) as raised:
        gateway.complete(profile, REQUEST)

    assert adapter.calls == 4
    assert raised.value.attempts == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_gateway_does_not_retry_permanent_failure():
    profile = _profile()
    adapter = _AlwaysFailAdapter(profile, retryable=False)
    sleeps = []
    gateway = LLMGateway(
        adapter_factory=lambda unused: adapter,
        sleep=sleeps.append,
    )

    with pytest.raises(LLMCallError) as raised:
        gateway.complete(profile, REQUEST)

    assert adapter.calls == 1
    assert raised.value.attempts == 1
    assert sleeps == []


class _SuccessAdapter(LLMAdapter):
    def __init__(self, profile):
        super().__init__(profile)
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        return LLMResult(text="ok")


def test_gateway_reuses_adapter_and_semaphore_for_same_profile():
    profile = _profile()
    created = []

    def factory(value):
        adapter = _SuccessAdapter(value)
        created.append(adapter)
        return adapter

    gateway = LLMGateway(adapter_factory=factory)

    first_adapter, first_semaphore = gateway._resources(profile)
    second_adapter, second_semaphore = gateway._resources(profile)
    assert first_adapter is second_adapter
    assert first_semaphore is second_semaphore

    assert gateway.complete(profile, REQUEST).text == "ok"
    assert gateway.complete(profile, REQUEST).text == "ok"
    assert created == [first_adapter]
    assert first_adapter.calls == 2
