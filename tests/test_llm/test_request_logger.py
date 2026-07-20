import json
import time
from concurrent.futures import ThreadPoolExecutor

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


def _profile() -> LLMModelProfile:
    return LLMModelProfile(
        profile_id="logged",
        name="Logged",
        transport=LLMTransport.OPENAI_COMPATIBLE,
        dialect=ProviderDialect.GENERIC,
        base_url="https://example.test/v1",
        api_key="secret",
        model="model",
        max_concurrency=2,
    )


class _OutOfOrderAdapter(LLMAdapter):
    def complete(self, request):
        value = request.messages[-1].content
        time.sleep(0.03 if value == "slow" else 0.001)
        return LLMResult(
            text=value,
            usage=LLMUsage(
                input_tokens=len(value),
                output_tokens=1,
                cache_read_tokens=2 if value == "slow" else 3,
                cache_write_tokens=4 if value == "slow" else 5,
            ),
            raw={"echo": value},
        )


def test_gateway_logs_concurrent_attempts_without_cross_pairing(tmp_path, monkeypatch):
    log_path = tmp_path / "llm.jsonl"
    monkeypatch.setattr(request_logger, "LLM_LOG_FILE", log_path)
    profile = _profile()
    adapter = _OutOfOrderAdapter(profile)
    gateway = LLMGateway(adapter_factory=lambda _profile: adapter)

    def call(value):
        return gateway.complete(
            profile,
            LLMRequest(
                messages=(LLMMessage("user", value),),
                metadata={"stage": f"stage-{value}", "role": f"role-{value}"},
            ),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert {item.text for item in pool.map(call, ("slow", "fast"))} == {
            "slow",
            "fast",
        }

    entries = [json.loads(line) for line in log_path.read_text("utf-8").splitlines()]
    assert len(entries) == 2
    assert len({entry["request_id"] for entry in entries}) == 2
    by_stage = {entry["stage"]: entry for entry in entries}
    for value in ("slow", "fast"):
        entry = by_stage[f"stage-{value}"]
        assert entry["role"] == f"role-{value}"
        assert entry["attempt"] == 1
        assert entry["request"]["messages"][-1]["content"] == value
        assert entry["response"] == {"echo": value}
    assert by_stage["stage-slow"]["usage"]["cache_read_tokens"] == 2
    assert by_stage["stage-fast"]["usage"]["cache_write_tokens"] == 5


def test_gateway_logs_each_retry_with_its_own_attempt_number(tmp_path, monkeypatch):
    class RetryOnceAdapter(LLMAdapter):
        def __init__(self, profile):
            super().__init__(profile)
            self.calls = 0

        def complete(self, request):
            self.calls += 1
            if self.calls == 1:
                raise LLMCallError(
                    "try again",
                    category=LLMErrorCategory.TRANSIENT,
                    retryable=True,
                )
            return LLMResult(text="ok", raw={"ok": True})

    log_path = tmp_path / "retry.jsonl"
    monkeypatch.setattr(request_logger, "LLM_LOG_FILE", log_path)
    profile = _profile()
    adapter = RetryOnceAdapter(profile)
    gateway = LLMGateway(
        adapter_factory=lambda _profile: adapter,
        sleep=lambda _delay: None,
    )

    result = gateway.complete(
        profile,
        LLMRequest(
            messages=(LLMMessage("user", "retry"),),
            metadata={"stage": "translation", "role": "main"},
        ),
    )

    assert result.text == "ok"
    entries = [json.loads(line) for line in log_path.read_text("utf-8").splitlines()]
    assert [entry["attempt"] for entry in entries] == [1, 2]
    assert entries[0]["status"] == "error"
    assert entries[0]["error"]["category"] == "transient"
    assert entries[1]["status"] == "success"
