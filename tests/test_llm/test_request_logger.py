import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest

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


@pytest.fixture(autouse=True)
def _reset_content_logging():
    request_logger.set_llm_content_logging(False)
    request_logger._pending_requests.clear()
    request_logger.discard_pending_legacy_request()
    yield
    request_logger.set_llm_content_logging(False)
    request_logger._pending_requests.clear()
    request_logger.discard_pending_legacy_request()


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
        assert entry["profile"] == {"id": "logged", "model": "model"}
        assert "request" not in entry
        assert "response" not in entry
    assert by_stage["stage-slow"]["usage"]["cache_read_tokens"] == 2
    assert by_stage["stage-fast"]["usage"]["cache_write_tokens"] == 5
    assert "echo" not in log_path.read_text("utf-8")


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
    assert "message" not in entries[0]["error"]
    assert "try again" not in log_path.read_text("utf-8")
    assert entries[1]["status"] == "success"


def test_content_logging_only_adds_prompts_and_normalized_final_text(
    tmp_path, monkeypatch
):
    log_path = tmp_path / "content.jsonl"
    monkeypatch.setattr(request_logger, "LLM_LOG_FILE", log_path)
    request_logger.set_llm_content_logging(True)
    profile = _profile()
    adapter = _OutOfOrderAdapter(profile)

    result = LLMGateway(adapter_factory=lambda _profile: adapter).complete(
        profile,
        LLMRequest(
            messages=(LLMMessage("system", "rules"), LLMMessage("user", "subtitle")),
            response_schema={"type": "object", "secret": "must-not-log"},
            metadata={"stage": "translation", "role": "main", "token": "hidden"},
        ),
    )

    assert result.text == "subtitle"
    entry = json.loads(log_path.read_text("utf-8"))
    assert entry["request"] == {
        "messages": [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "subtitle"},
        ]
    }
    assert entry["response"] == {"text": "subtitle"}
    serialized = log_path.read_text("utf-8")
    assert "must-not-log" not in serialized
    assert "hidden" not in serialized
    assert "echo" not in serialized


def test_legacy_logger_obeys_same_content_policy(tmp_path, monkeypatch):
    log_path = tmp_path / "legacy.jsonl"
    monkeypatch.setattr(request_logger, "LLM_LOG_FILE", log_path)
    request = httpx.Request(
        "POST",
        "https://example.test/v1/chat/completions",
        content=json.dumps(
            {
                "model": "legacy-model",
                "messages": [{"role": "user", "content": "private subtitle"}],
                "extra_body": {"api_key": "never-log"},
            }
        ),
    )
    request_logger._on_request(request)
    request_logger._on_response(httpx.Response(200, request=request))
    response = SimpleNamespace(
        usage={
            "prompt_tokens": 4,
            "completion_tokens": 2,
            "completion_tokens_details": {"reasoning_tokens": 1},
        },
        choices=[SimpleNamespace(message=SimpleNamespace(content="final text"))],
        model_dump=lambda: {"raw": "never-log-raw"},
    )
    request_logger.log_llm_response(response)

    entry = json.loads(log_path.read_text("utf-8"))
    assert entry["profile"] == {"id": "legacy", "model": "legacy-model"}
    assert entry["usage"]["reasoning_tokens"] == 1
    assert "request" not in entry
    assert "response" not in entry
    assert "private subtitle" not in log_path.read_text("utf-8")
    assert "never-log" not in log_path.read_text("utf-8")

    request_logger.set_llm_content_logging(True)
    request2 = httpx.Request(
        "POST",
        "https://example.test/v1/chat/completions",
        content=json.dumps(
            {
                "model": "legacy-model",
                "messages": [{"role": "user", "content": "visible subtitle"}],
                "reasoning": {"effort": "high"},
            }
        ),
    )
    request_logger._on_request(request2)
    request_logger._on_response(httpx.Response(200, request=request2))
    request_logger.log_llm_response(response)

    entries = [json.loads(line) for line in log_path.read_text("utf-8").splitlines()]
    assert entries[-1]["request"]["messages"][-1]["content"] == "visible subtitle"
    assert entries[-1]["response"] == {"text": "final text"}
    assert "reasoning" not in entries[-1]["request"]


def test_legacy_http_error_does_not_log_provider_body(tmp_path, monkeypatch):
    log_path = tmp_path / "legacy-error.jsonl"
    monkeypatch.setattr(request_logger, "LLM_LOG_FILE", log_path)
    request = httpx.Request(
        "POST",
        "https://example.test/v1/chat/completions",
        content=json.dumps({"model": "m", "messages": []}),
    )
    request_logger._on_request(request)
    request_logger._on_response(
        httpx.Response(429, text='{"error":"secret provider detail"}', request=request)
    )

    entry = json.loads(log_path.read_text("utf-8"))
    assert entry["status"] == "error"
    assert entry["error"]["status_code"] == 429
    assert entry["error"]["retryable"] is True
    assert "secret provider detail" not in log_path.read_text("utf-8")


def test_legacy_concurrent_calls_keep_each_response_with_its_request(
    tmp_path, monkeypatch
):
    log_path = tmp_path / "legacy-concurrent.jsonl"
    monkeypatch.setattr(request_logger, "LLM_LOG_FILE", log_path)
    request_logger.set_llm_content_logging(True)
    first_ready = threading.Event()
    second_logged = threading.Event()

    def call(value: str) -> None:
        request = httpx.Request(
            "POST",
            "https://example.test/v1/chat/completions",
            content=json.dumps(
                {
                    "model": f"model-{value}",
                    "messages": [{"role": "user", "content": f"prompt-{value}"}],
                }
            ),
        )
        request_logger._on_request(request)
        request_logger._on_response(httpx.Response(200, request=request))
        if value == "first":
            first_ready.set()
            assert second_logged.wait(2)
        else:
            assert first_ready.wait(2)
        response = SimpleNamespace(
            usage={"prompt_tokens": 1, "completion_tokens": 2},
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=f"response-{value}")
                )
            ],
        )
        request_logger.log_llm_response(response)
        if value == "second":
            second_logged.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        tuple(pool.map(call, ("first", "second")))

    entries = [json.loads(line) for line in log_path.read_text("utf-8").splitlines()]
    assert len(entries) == 2
    by_model = {entry["profile"]["model"]: entry for entry in entries}
    for value in ("first", "second"):
        entry = by_model[f"model-{value}"]
        assert entry["request"]["messages"][0]["content"] == f"prompt-{value}"
        assert entry["response"]["text"] == f"response-{value}"


def test_discard_pending_legacy_request_removes_prompt_after_transport_failure():
    request_logger.set_llm_content_logging(True)
    request = httpx.Request(
        "POST",
        "https://example.test/v1/chat/completions",
        content=json.dumps(
            {
                "model": "failed-model",
                "messages": [{"role": "user", "content": "sensitive prompt"}],
            }
        ),
    )
    request_logger._on_request(request)
    assert request_logger._pending_requests

    request_logger.discard_pending_legacy_request()

    assert request_logger._pending_requests == {}
    assert not hasattr(request_logger._legacy_request_context, "request_key")


def test_log_rotation_replaces_old_backup_without_copying_content(tmp_path, monkeypatch):
    log_path = tmp_path / "llm_requests.jsonl"
    backup = log_path.with_suffix(".jsonl.old")
    log_path.write_text("current-private-content", encoding="utf-8")
    backup.write_text("older-private-content", encoding="utf-8")
    monkeypatch.setattr(request_logger, "LLM_LOG_FILE", log_path)
    monkeypatch.setattr(request_logger, "MAX_LOG_SIZE", 1)

    request_logger._write_log({"status": "success"})

    assert backup.read_text("utf-8") == "current-private-content"
    assert json.loads(log_path.read_text("utf-8")) == {"status": "success"}
