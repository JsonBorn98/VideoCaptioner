import json
import time
from concurrent.futures import ThreadPoolExecutor

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
    yield
    request_logger.set_llm_content_logging(False)


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


def test_env_api_key_override_marks_entries_with_key_source(tmp_path, monkeypatch):
    # The CLI's VIDEOCAPTIONER_LLM_API_KEY override flips this marker; every
    # gateway entry of the process then records key_source="env_override".
    log_path = tmp_path / "env-key.jsonl"
    monkeypatch.setattr(request_logger, "LLM_LOG_FILE", log_path)
    profile = _profile()
    gateway = LLMGateway(adapter_factory=lambda _profile: _OutOfOrderAdapter(profile))

    request_logger.set_env_api_key_override(True)
    try:
        gateway.complete(
            profile,
            LLMRequest(
                messages=(LLMMessage("user", "ci subtitle"),),
                metadata={"stage": "llm_optimize", "role": "utility"},
            ),
        )
    finally:
        request_logger.set_env_api_key_override(False)

    entry = json.loads(log_path.read_text("utf-8"))
    assert entry["key_source"] == "env_override"


def test_entries_without_the_env_override_marker_keep_their_shape(tmp_path, monkeypatch):
    log_path = tmp_path / "no-env-key.jsonl"
    monkeypatch.setattr(request_logger, "LLM_LOG_FILE", log_path)
    profile = _profile()
    gateway = LLMGateway(adapter_factory=lambda _profile: _OutOfOrderAdapter(profile))

    gateway.complete(
        profile,
        LLMRequest(
            messages=(LLMMessage("user", "plain subtitle"),),
            metadata={"stage": "llm_optimize", "role": "utility"},
        ),
    )

    entry = json.loads(log_path.read_text("utf-8"))
    assert "key_source" not in entry


def test_request_metadata_key_source_wins_over_the_process_marker(tmp_path, monkeypatch):
    log_path = tmp_path / "explicit-key.jsonl"
    monkeypatch.setattr(request_logger, "LLM_LOG_FILE", log_path)
    profile = _profile()
    gateway = LLMGateway(adapter_factory=lambda _profile: _OutOfOrderAdapter(profile))

    request_logger.set_env_api_key_override(True)
    try:
        gateway.complete(
            profile,
            LLMRequest(
                messages=(LLMMessage("user", "explicit subtitle"),),
                metadata={"stage": "translation", "role": "main", "key_source": "store"},
            ),
        )
    finally:
        request_logger.set_env_api_key_override(False)

    entry = json.loads(log_path.read_text("utf-8"))
    assert entry["key_source"] == "store"


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
            max_output_tokens=65_536,
            metadata={"stage": "translation", "role": "main"},
            request_options_override={"reasoning_effort": "low"},
        ),
    )

    assert result.text == "ok"
    entries = [json.loads(line) for line in log_path.read_text("utf-8").splitlines()]
    assert [entry["attempt"] for entry in entries] == [1, 2]
    assert all(entry["max_output_tokens"] == 65_536 for entry in entries)
    assert all(entry["adaptive_reasoning"] is True for entry in entries)
    assert entries[0]["status"] == "error"
    assert entries[0]["error"]["category"] == "transient"
    assert "message" not in entries[0]["error"]
    assert "try again" not in log_path.read_text("utf-8")
    assert entries[1]["status"] == "success"


def test_error_log_keeps_safe_finish_reason_and_usage_without_raw_content(
    tmp_path, monkeypatch
):
    class DiagnosticFailureAdapter(LLMAdapter):
        def complete(self, request):
            raise LLMCallError(
                "private provider response must not be logged",
                category=LLMErrorCategory.INVALID_RESPONSE,
                retryable=False,
                finish_reason="length",
                response_status="completed",
                choice_count=1,
                usage=LLMUsage(
                    input_tokens=3000,
                    output_tokens=8192,
                    reasoning_tokens=8192,
                ),
            )

    log_path = tmp_path / "diagnostic-error.jsonl"
    monkeypatch.setattr(request_logger, "LLM_LOG_FILE", log_path)
    profile = _profile()

    with pytest.raises(LLMCallError):
        LLMGateway(
            adapter_factory=lambda _profile: DiagnosticFailureAdapter(profile)
        ).complete(profile, LLMRequest(messages=(LLMMessage("user", "secret"),)))

    entry = json.loads(log_path.read_text("utf-8"))
    assert entry["error"]["diagnostics"] == {
        "finish_reason": "length",
        "response_status": "completed",
        "choice_count": 1,
    }
    assert entry["usage"]["input_tokens"] == 3000
    assert entry["usage"]["output_tokens"] == 8192
    assert entry["usage"]["reasoning_tokens"] == 8192
    serialized = log_path.read_text("utf-8")
    assert "private provider response" not in serialized
    assert "secret" not in serialized


def test_gateway_logs_only_safe_responses_incomplete_diagnostics(tmp_path, monkeypatch):
    class IncompleteAdapter(LLMAdapter):
        def complete(self, request):
            raise LLMCallError(
                "provider response text must not be logged",
                category=LLMErrorCategory.INVALID_RESPONSE,
                retryable=False,
                finish_reason="max_output_tokens",
                response_status="incomplete",
            )

    log_path = tmp_path / "incomplete.jsonl"
    monkeypatch.setattr(request_logger, "LLM_LOG_FILE", log_path)
    profile = _profile()

    with pytest.raises(LLMCallError):
        LLMGateway(adapter_factory=lambda _profile: IncompleteAdapter(profile)).complete(
            profile,
            LLMRequest(messages=(LLMMessage("user", "private subtitle"),)),
        )

    entry = json.loads(log_path.read_text("utf-8"))
    assert entry["error"]["diagnostics"] == {
        "finish_reason": "max_output_tokens",
        "response_status": "incomplete",
    }
    serialized = log_path.read_text("utf-8")
    assert "provider response text" not in serialized
    assert "private subtitle" not in serialized


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
