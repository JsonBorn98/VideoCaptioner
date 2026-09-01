import threading
import time

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


def _profile(**overrides) -> LLMModelProfile:
    values = {
        "profile_id": "shared",
        "name": "Shared profile",
        "transport": LLMTransport.OPENAI_COMPATIBLE,
        "dialect": ProviderDialect.GENERIC,
        "base_url": "https://example.test/v1",
        "api_key": "secret",
        "model": "example-model",
        "max_concurrency": 2,
    }
    values.update(overrides)
    return LLMModelProfile(**values)


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


def test_gateway_retries_invalid_response_only_once():
    class InvalidResponseAdapter(LLMAdapter):
        def __init__(self, profile):
            super().__init__(profile)
            self.calls = 0

        def complete(self, request):
            self.calls += 1
            raise LLMCallError(
                "empty completion",
                category=LLMErrorCategory.INVALID_RESPONSE,
                retryable=True,
            )

    profile = _profile()
    adapter = InvalidResponseAdapter(profile)
    sleeps = []
    gateway = LLMGateway(
        adapter_factory=lambda unused: adapter,
        sleep=sleeps.append,
        random_source=lambda: 0.5,
    )

    with pytest.raises(LLMCallError) as raised:
        gateway.complete(profile, REQUEST)

    assert adapter.calls == 2
    assert raised.value.attempts == 2
    assert sleeps == [1.0]


def test_gateway_does_not_repeat_an_exhausted_output_cap():
    class OutputLimitAdapter(LLMAdapter):
        def __init__(self, profile):
            super().__init__(profile)
            self.calls = 0

        def complete(self, request):
            self.calls += 1
            raise LLMCallError(
                "output limit exhausted",
                category=LLMErrorCategory.INVALID_RESPONSE,
                retryable=True,
                finish_reason="max_output_tokens",
            )

    profile = _profile()
    adapter = OutputLimitAdapter(profile)
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


def test_gateway_attaches_request_duration_to_successful_result():
    class DelayedAdapter(LLMAdapter):
        def complete(self, request):
            time.sleep(0.02)
            return LLMResult(text="ok")

    profile = _profile()
    result = LLMGateway(
        adapter_factory=lambda unused: DelayedAdapter(profile)
    ).complete(profile, REQUEST)

    assert result.text == "ok"
    assert result.duration_ms is not None
    assert result.duration_ms >= 20


class _SlowAdapter(LLMAdapter):
    def __init__(self, profile, *, started, release, in_flight, peak, lock):
        super().__init__(profile)
        self._started = started
        self._release = release
        self._in_flight = in_flight
        self._peak = peak
        self._lock = lock

    def complete(self, request):
        with self._lock:
            current = self._in_flight.get(self.profile.profile_id, 0) + 1
            self._in_flight[self.profile.profile_id] = current
            self._peak[self.profile.profile_id] = max(
                self._peak.get(self.profile.profile_id, 0), current
            )
        self._started.release()
        self._release.wait()
        with self._lock:
            self._in_flight[self.profile.profile_id] -= 1
        return LLMResult(text="ok")


def _run_concurrent(gateway, profile, count, *, started, release, expected_in_flight):
    threads = [
        threading.Thread(target=gateway.complete, args=(profile, REQUEST))
        for _ in range(count)
    ]
    for thread in threads:
        thread.start()
    for _ in range(expected_in_flight):
        assert started.acquire(timeout=1)
    time.sleep(0.05)
    release.set()
    for thread in threads:
        thread.join(timeout=2)
    return threads


def test_gateway_gate_follows_task_concurrency_when_profile_does_not_clamp():
    started = threading.Semaphore(0)
    release = threading.Event()
    in_flight = {}
    peak = {}
    lock = threading.Lock()
    profile = _profile(max_concurrency=None)
    adapter = _SlowAdapter(
        profile, started=started, release=release, in_flight=in_flight, peak=peak, lock=lock
    )
    gateway = LLMGateway(adapter_factory=lambda unused: adapter, max_concurrency=3)

    _run_concurrent(
        gateway, profile, 6, started=started, release=release, expected_in_flight=3
    )

    assert peak[profile.profile_id] == 3


def test_gateway_explicit_profile_clamp_caps_task_concurrency():
    started = threading.Semaphore(0)
    release = threading.Event()
    in_flight = {}
    peak = {}
    lock = threading.Lock()
    profile = _profile(max_concurrency=2)
    adapter = _SlowAdapter(
        profile, started=started, release=release, in_flight=in_flight, peak=peak, lock=lock
    )
    gateway = LLMGateway(adapter_factory=lambda unused: adapter, max_concurrency=5)

    _run_concurrent(
        gateway, profile, 5, started=started, release=release, expected_in_flight=2
    )

    assert peak[profile.profile_id] == 2


def test_gateway_profiles_keep_independent_gates():
    started = threading.Semaphore(0)
    release = threading.Event()
    in_flight = {}
    peak = {}
    lock = threading.Lock()
    adapters = {}

    def factory(profile):
        adapter = adapters.get(profile.profile_id)
        if adapter is None:
            adapter = _SlowAdapter(
                profile,
                started=started,
                release=release,
                in_flight=in_flight,
                peak=peak,
                lock=lock,
            )
            adapters[profile.profile_id] = adapter
        return adapter

    gateway = LLMGateway(adapter_factory=factory, max_concurrency=4)
    main = _profile(profile_id="main", name="Main", max_concurrency=None)
    review = _profile(profile_id="review", name="Review", max_concurrency=2)
    threads = [
        threading.Thread(target=gateway.complete, args=(main, REQUEST))
        for _ in range(4)
    ] + [
        threading.Thread(target=gateway.complete, args=(review, REQUEST))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for _ in range(6):
        assert started.acquire(timeout=1)
    time.sleep(0.05)
    release.set()
    for thread in threads:
        thread.join(timeout=2)

    assert peak["main"] == 4
    assert peak["review"] == 2
