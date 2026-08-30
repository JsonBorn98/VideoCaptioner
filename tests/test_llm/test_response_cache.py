import json

import pytest
from diskcache import Cache

from videocaptioner.core.llm import request_logger
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
    LLMUsage,
    ProviderDialect,
)
from videocaptioner.core.llm.response_cache import GatewayResponseCache
from videocaptioner.core.utils import cache as cache_utils


@pytest.fixture(autouse=True)
def _reset_content_logging():
    request_logger.set_llm_content_logging(False)
    yield
    request_logger.set_llm_content_logging(False)


@pytest.fixture
def disk_cache(tmp_path):
    instance = Cache(str(tmp_path / "llm_gateway"))
    yield instance
    instance.close()


@pytest.fixture
def cache_enabled():
    cache_utils.enable_cache()
    yield
    cache_utils.disable_cache()


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


class _CountingAdapter(LLMAdapter):
    def __init__(self, profile):
        super().__init__(profile)
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        return LLMResult(
            text=f"answer-{self.calls}",
            usage=LLMUsage(input_tokens=11, output_tokens=7),
        )


class _FailingAdapter(LLMAdapter):
    def __init__(self, profile):
        super().__init__(profile)
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        raise LLMCallError(
            "provider failed",
            category=LLMErrorCategory.TRANSIENT,
            retryable=True,
        )


def _gateway(adapter, disk) -> LLMGateway:
    return LLMGateway(
        adapter_factory=lambda unused: adapter,
        sleep=lambda _delay: None,
        random_source=lambda: 0.5,
        response_cache=GatewayResponseCache(cache=disk),
    )


def test_second_identical_request_hits_cache_without_adapter_call(
    disk_cache, cache_enabled
):
    profile = _profile()
    adapter = _CountingAdapter(profile)
    gateway = _gateway(adapter, disk_cache)

    first = gateway.complete(profile, REQUEST)
    second = gateway.complete(profile, REQUEST)

    assert first.text == "answer-1"
    assert second.text == "answer-1"
    assert adapter.calls == 1


def test_profiles_differing_only_in_api_key_do_not_cross_hit(disk_cache, cache_enabled):
    """Regression for the old memoize hole: its key lacked connection info."""

    first_profile = _profile(profile_id="first")
    second_profile = _profile(profile_id="second", api_key="other-secret")
    first_adapter = _CountingAdapter(first_profile)
    second_adapter = _CountingAdapter(second_profile)
    first_gateway = _gateway(first_adapter, disk_cache)
    second_gateway = _gateway(second_adapter, disk_cache)

    first_gateway.complete(first_profile, REQUEST)
    result = second_gateway.complete(second_profile, REQUEST)

    assert result.text == "answer-1"
    assert first_adapter.calls == 1
    assert second_adapter.calls == 1


def test_disabled_global_switch_means_no_read_and_no_write(
    disk_cache, monkeypatch
):
    cache_utils.disable_cache()
    profile = _profile()
    adapter = _CountingAdapter(profile)
    gateway = _gateway(adapter, disk_cache)

    first = gateway.complete(profile, REQUEST)
    cache_utils.enable_cache()
    second = gateway.complete(profile, REQUEST)

    assert first.text == "answer-1"
    assert second.text == "answer-2"
    assert adapter.calls == 2
    cache_utils.disable_cache()

    direct = GatewayResponseCache(cache=disk_cache)
    assert direct.lookup(profile, REQUEST) is None
    direct.store(profile, REQUEST, LLMResult(text="never"))
    assert direct.lookup(profile, REQUEST) is None


def test_use_cache_false_bypasses_cache_in_both_directions(disk_cache, cache_enabled):
    profile = _profile()
    adapter = _CountingAdapter(profile)
    gateway = _gateway(adapter, disk_cache)

    gateway.complete(profile, REQUEST, use_cache=False)
    gateway.complete(profile, REQUEST, use_cache=False)

    assert adapter.calls == 2


def test_cache_hit_replays_result_with_all_none_usage(disk_cache, cache_enabled):
    profile = _profile()
    adapter = _CountingAdapter(profile)
    gateway = _gateway(adapter, disk_cache)

    first = gateway.complete(profile, REQUEST)
    second = gateway.complete(profile, REQUEST)

    assert first.usage == LLMUsage(input_tokens=11, output_tokens=7)
    assert second.usage == LLMUsage()
    assert second.usage.input_tokens is None
    assert second.usage.output_tokens is None


def test_cache_hit_logs_entry_without_usage_or_attempt(
    disk_cache, cache_enabled, tmp_path, monkeypatch
):
    log_path = tmp_path / "llm.jsonl"
    monkeypatch.setattr(request_logger, "LLM_LOG_FILE", log_path)
    profile = _profile(profile_id="logged", model="logged-model")
    adapter = _CountingAdapter(profile)
    gateway = _gateway(adapter, disk_cache)

    request = LLMRequest(
        messages=(LLMMessage("user", "hello"),),
        metadata={"stage": "translation", "role": "main"},
    )
    gateway.complete(profile, request)
    gateway.complete(profile, request)

    entries = [json.loads(line) for line in log_path.read_text("utf-8").splitlines()]
    assert len(entries) == 2
    hit = entries[1]
    assert hit["status"] == "cache_hit"
    assert hit["stage"] == "translation"
    assert hit["role"] == "main"
    assert hit["profile"] == {"id": "logged", "model": "logged-model"}
    assert hit["duration_ms"] == 0
    assert "usage" not in hit
    assert "attempt" not in hit
    assert "response" not in hit
    assert hit["request_id"] != entries[0]["request_id"]
    assert "answer-1" not in log_path.read_text("utf-8")


def test_cache_hit_log_includes_text_when_content_logging_enabled(
    disk_cache, cache_enabled, tmp_path, monkeypatch
):
    log_path = tmp_path / "llm.jsonl"
    monkeypatch.setattr(request_logger, "LLM_LOG_FILE", log_path)
    request_logger.set_llm_content_logging(True)
    profile = _profile()
    adapter = _CountingAdapter(profile)
    gateway = _gateway(adapter, disk_cache)

    gateway.complete(profile, REQUEST)
    gateway.complete(profile, REQUEST)

    entries = [json.loads(line) for line in log_path.read_text("utf-8").splitlines()]
    hit = entries[1]
    assert hit["status"] == "cache_hit"
    assert hit["response"] == {"text": "answer-1"}


def test_corrupt_payload_is_treated_as_miss(disk_cache, cache_enabled):
    profile = _profile()
    adapter = _CountingAdapter(profile)
    response_cache = GatewayResponseCache(cache=disk_cache)
    gateway = _gateway(adapter, disk_cache)

    gateway.complete(profile, REQUEST)
    key = response_cache._cache  # reach into disk to corrupt the stored value
    stored_keys = []
    for entry_key in key.iterkeys():
        stored_keys.append(entry_key)
    assert len(stored_keys) == 1
    key.set(stored_keys[0], {"schema": "gateway-cache-v1", "text": 42})

    result = gateway.complete(profile, REQUEST)

    assert result.text == "answer-2"
    assert adapter.calls == 2


def test_wrong_schema_marker_is_treated_as_miss(disk_cache, cache_enabled):
    profile = _profile()
    adapter = _CountingAdapter(profile)
    gateway = _gateway(adapter, disk_cache)

    gateway.complete(profile, REQUEST)
    stored_keys = list(disk_cache.iterkeys())
    disk_cache.set(stored_keys[0], {"schema": "gateway-cache-v0", "text": "stale"})

    result = gateway.complete(profile, REQUEST)

    assert result.text == "answer-2"
    assert adapter.calls == 2


def test_cache_lookup_failure_fails_open_as_miss(cache_enabled):
    class _BrokenCache:
        def get(self, key):
            raise OSError("disk full")

        def set(self, key, value, expire=None):
            raise OSError("disk full")

    profile = _profile()
    adapter = _CountingAdapter(profile)
    gateway = _gateway(adapter, _BrokenCache())

    assert gateway.complete(profile, REQUEST).text == "answer-1"
    assert gateway.complete(profile, REQUEST).text == "answer-2"
    assert adapter.calls == 2


def test_key_version_bump_invalidates_old_entries(disk_cache, cache_enabled):
    profile = _profile()
    adapter = _CountingAdapter(profile)
    gateway = _gateway(adapter, disk_cache)

    gateway.complete(profile, REQUEST)

    from videocaptioner.core.llm import response_cache as response_cache_module

    monkeypatched_version = "gateway-cache-v2"
    original = response_cache_module.KEY_VERSION
    response_cache_module.KEY_VERSION = monkeypatched_version
    try:
        result = gateway.complete(profile, REQUEST)
    finally:
        response_cache_module.KEY_VERSION = original

    assert result.text == "answer-2"
    assert adapter.calls == 2


def test_failures_are_never_cached(disk_cache, cache_enabled):
    profile = _profile()
    adapter = _FailingAdapter(profile)
    gateway = _gateway(adapter, disk_cache)

    with pytest.raises(LLMCallError):
        gateway.complete(profile, REQUEST)

    assert GatewayResponseCache(cache=disk_cache).lookup(profile, REQUEST) is None


def test_invalid_response_is_not_cached(disk_cache, cache_enabled):
    class _InvalidResponseAdapter(LLMAdapter):
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
    adapter = _InvalidResponseAdapter(profile)
    gateway = _gateway(adapter, disk_cache)

    with pytest.raises(LLMCallError):
        gateway.complete(profile, REQUEST)

    assert GatewayResponseCache(cache=disk_cache).lookup(profile, REQUEST) is None


def test_editing_request_shaping_profile_field_changes_the_key(
    disk_cache, cache_enabled
):
    profile = _profile()
    adapter = _CountingAdapter(profile)
    gateway = _gateway(adapter, disk_cache)

    gateway.complete(profile, REQUEST)
    edited = _profile(profile_id="shared", model="other-model")
    other_adapter = _CountingAdapter(edited)
    other_gateway = _gateway(other_adapter, disk_cache)

    result = other_gateway.complete(edited, REQUEST)

    assert result.text == "answer-1"
    assert other_adapter.calls == 1


def test_non_shaping_profile_fields_do_not_change_the_key(
    disk_cache, cache_enabled
):
    profile = _profile(name="Original", max_concurrency=2)
    adapter = _CountingAdapter(profile)
    gateway = _gateway(adapter, disk_cache)

    gateway.complete(profile, REQUEST)
    renamed = _profile(name="Renamed", max_concurrency=5)
    renamed_adapter = _CountingAdapter(renamed)
    renamed_gateway = _gateway(renamed_adapter, disk_cache)

    result = renamed_gateway.complete(renamed, REQUEST)

    assert result.text == "answer-1"
    assert renamed_adapter.calls == 0


def test_excluded_request_fields_do_not_change_the_key(disk_cache, cache_enabled):
    profile = _profile()
    adapter = _CountingAdapter(profile)
    gateway = _gateway(adapter, disk_cache)

    plain = LLMRequest(messages=(LLMMessage("user", "hello"),))
    with_metadata = LLMRequest(
        messages=(LLMMessage("user", "hello"),),
        metadata={"stage": "optimize", "role": "utility"},
    )
    with_temperature = LLMRequest(
        messages=(LLMMessage("user", "hello"),), temperature=0.9
    )

    assert gateway.complete(profile, plain).text == "answer-1"
    assert gateway.complete(profile, with_metadata).text == "answer-1"
    assert gateway.complete(profile, with_temperature).text == "answer-1"
    assert adapter.calls == 1


def test_request_shaping_fields_do_change_the_key(disk_cache, cache_enabled):
    profile = _profile()
    adapter = _CountingAdapter(profile)
    gateway = _gateway(adapter, disk_cache)

    plain = LLMRequest(messages=(LLMMessage("user", "hello"),))
    capped = LLMRequest(
        messages=(LLMMessage("user", "hello"),), max_output_tokens=512
    )
    without_prefix = LLMRequest(
        messages=(LLMMessage("user", "hello"),), cacheable_system_prefix=False
    )

    assert gateway.complete(profile, plain).text == "answer-1"
    assert gateway.complete(profile, capped).text == "answer-2"
    assert gateway.complete(profile, without_prefix).text == "answer-3"
    assert adapter.calls == 3
