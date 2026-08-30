"""Privacy-preserving request logs for both LLM call paths."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

from videocaptioner.config import LOG_PATH

from .context import get_task_context
from .models import LLMCallError, LLMModelProfile, LLMRequest, LLMResult, LLMUsage

LLM_LOG_FILE = LOG_PATH / "llm_requests.jsonl"
MAX_LOG_SIZE = 10 * 1024 * 1024  # 10MB


_log_lock = threading.Lock()
_pending_requests: Dict[int, Dict[str, Any]] = {}
_content_logging_enabled = False
# Set by the CLI when the env credential override swaps a resolved profile's
# api_key; gateway log entries then record key_source="env_override".
_env_api_key_override = False
_legacy_request_context = threading.local()


@dataclass(frozen=True)
class LLMRequestLogHandle:
    """Immutable correlation data for one provider attempt."""

    request_id: str
    started_at: float
    entry: Mapping[str, Any]
    include_content: bool


def set_llm_content_logging(enabled: bool) -> None:
    """Enable or disable prompt/final-text logging for subsequent requests."""

    global _content_logging_enabled
    _content_logging_enabled = bool(enabled)


def is_llm_content_logging_enabled() -> bool:
    return _content_logging_enabled


def set_env_api_key_override(active: bool) -> None:
    """Mark that resolved profiles carry an environment-injected credential.

    Set by the CLI when VIDEOCAPTIONER_LLM_API_KEY swaps a resolved profile's
    api_key; gateway log entries then record key_source="env_override" so the
    injected key stays visible per request. Unset keeps the field absent.
    """

    global _env_api_key_override
    _env_api_key_override = bool(active)


def is_env_api_key_override_active() -> bool:
    return _env_api_key_override


# ==================== 日志写入 ====================


def _rotate_if_needed() -> None:
    """日志文件过大时轮转。"""

    if not LLM_LOG_FILE.exists():
        return
    if LLM_LOG_FILE.stat().st_size < MAX_LOG_SIZE:
        return

    backup = LLM_LOG_FILE.with_suffix(".jsonl.old")
    if backup.exists():
        backup.unlink()
    LLM_LOG_FILE.rename(backup)


def _write_log(entry: Dict[str, Any]) -> None:
    """Write a normalized entry; logging failures must not break the task."""

    try:
        LOG_PATH.mkdir(parents=True, exist_ok=True)
        with _log_lock:
            _rotate_if_needed()
            with open(LLM_LOG_FILE, "a", encoding="utf-8") as file:
                file.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _usage_entry(result: LLMResult) -> dict[str, Optional[int]]:
    return _normalized_usage_entry(result.usage)


def _normalized_usage_entry(usage: LLMUsage) -> dict[str, Optional[int]]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
    }


def _error_entry(error: Optional[BaseException]) -> dict[str, Any]:
    """Return an error summary without provider messages or response bodies."""

    entry: dict[str, Any] = {
        "type": type(error).__name__ if error is not None else "UnknownError",
        "category": "unexpected",
    }
    if isinstance(error, LLMCallError):
        entry.update(
            {
                "category": error.category.value,
                "retryable": error.retryable,
                "status_code": error.status_code,
            }
        )
        diagnostics = {
            "finish_reason": error.finish_reason,
            "response_status": error.response_status,
            "choice_count": error.choice_count,
        }
        entry["diagnostics"] = {
            key: value for key, value in diagnostics.items() if value is not None
        }
    return entry


def _base_entry(profile: LLMModelProfile, request: LLMRequest) -> dict[str, Any]:
    """Return the entry skeleton shared by every gateway log shape."""

    entry: dict[str, Any] = {
        "time": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "request_id": uuid.uuid4().hex,
        "stage": str(request.metadata.get("stage", "")),
        "role": str(request.metadata.get("role", "")),
        "profile": {
            "id": profile.profile_id,
            "model": profile.model,
        },
    }
    # Only emitted when set, so existing entries keep their shape (e.g. the CLI
    # marks CI-injected credentials with key_source="env_override"). Explicit
    # request metadata wins; otherwise the process-wide env-override marker
    # applies (set by the CLI when VIDEOCAPTIONER_LLM_API_KEY swaps a resolved
    # profile's credential — see set_env_api_key_override).
    key_source = request.metadata.get("key_source")
    if not key_source and _env_api_key_override:
        key_source = "env_override"
    if key_source:
        entry["key_source"] = str(key_source)
    return entry


def begin_gateway_request(
    profile: LLMModelProfile,
    request: LLMRequest,
    *,
    attempt: int,
) -> LLMRequestLogHandle:
    """Create correlation data without retaining captions unless opted in."""

    include_content = is_llm_content_logging_enabled()
    entry = _base_entry(profile, request)
    entry["attempt"] = attempt
    entry["max_output_tokens"] = request.max_output_tokens
    entry["adaptive_reasoning"] = request.request_options_override is not None
    if include_content:
        entry["request"] = {
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ]
        }
    return LLMRequestLogHandle(
        request_id=str(entry["request_id"]),
        started_at=time.perf_counter(),
        entry=entry,
        include_content=include_content,
    )


def finish_gateway_request(
    handle: LLMRequestLogHandle,
    *,
    result: Optional[LLMResult] = None,
    error: Optional[BaseException] = None,
) -> None:
    """Write one completed gateway attempt with normalized usage fields."""

    entry = dict(handle.entry)
    entry["duration_ms"] = max(
        0, int((time.perf_counter() - handle.started_at) * 1000)
    )
    if error is None and result is not None:
        entry["status"] = "success"
        entry["usage"] = _usage_entry(result)
        if handle.include_content:
            entry["response"] = {"text": result.text}
    else:
        entry["status"] = "error"
        entry["error"] = _error_entry(error)
        if isinstance(error, LLMCallError) and error.usage is not None:
            entry["usage"] = _normalized_usage_entry(error.usage)
    _write_log(entry)


def log_gateway_cache_hit(
    profile: LLMModelProfile,
    request: LLMRequest,
    result: LLMResult,
) -> None:
    """Write one cache-hit entry shaped like a success entry minus attempt/usage."""

    entry = _base_entry(profile, request)
    entry["status"] = "cache_hit"
    entry["duration_ms"] = 0
    if is_llm_content_logging_enabled():
        entry["response"] = {"text": result.text}
    _write_log(entry)


# ==================== Legacy HTTPX hooks ====================


def _normalized_prompt_messages(value: Any) -> list[dict[str, str]]:
    """Keep only role and textual prompt content from an OpenAI request body."""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    normalized: list[dict[str, str]] = []
    for message in value:
        if not isinstance(message, Mapping):
            continue
        role = message.get("role")
        content = message.get("content")
        if not isinstance(role, str):
            continue
        if isinstance(content, str):
            text = content
        elif isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
            parts = []
            for part in content:
                if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
            text = "".join(parts)
        else:
            continue
        normalized.append({"role": role, "content": text})
    return normalized


def _on_request(request: httpx.Request) -> None:
    """Retain only safe metadata until the legacy SDK response is parsed."""

    if "/chat/completions" not in str(request.url):
        return

    try:
        request_body = json.loads(request.content.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        request_body = {}
    if not isinstance(request_body, Mapping):
        request_body = {}

    include_content = is_llm_content_logging_enabled()
    context = get_task_context()
    pending: dict[str, Any] = {
        "time": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "request_id": uuid.uuid4().hex,
        "started_at": time.perf_counter(),
        "model": str(request_body.get("model", "")),
        "stage": context.stage if context is not None else "",
        "include_content": include_content,
    }
    if include_content:
        pending["messages"] = _normalized_prompt_messages(request_body.get("messages"))
    request_key = id(request)
    previous_key = getattr(_legacy_request_context, "request_key", None)
    with _log_lock:
        if previous_key is not None:
            _pending_requests.pop(previous_key, None)
        _pending_requests[request_key] = pending
    _legacy_request_context.request_key = request_key


def _legacy_base_entry(pending: Mapping[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "time": pending.get("time", ""),
        "request_id": pending.get("request_id", ""),
        "stage": pending.get("stage", ""),
        "role": "",
        "attempt": 1,
        "profile": {"id": "legacy", "model": pending.get("model", "")},
        "duration_ms": max(
            0,
            int((time.perf_counter() - float(pending.get("started_at", 0.0))) * 1000),
        ),
    }
    if pending.get("include_content"):
        entry["request"] = {"messages": pending.get("messages", [])}
    return entry


def _on_response(response: httpx.Response) -> None:
    """Record HTTP failures without persisting the provider response body."""

    request = response.request
    failed: Optional[dict[str, Any]] = None
    with _log_lock:
        pending = _pending_requests.get(id(request))
        if pending is None:
            return
        pending["status_code"] = response.status_code
        if response.status_code >= 400:
            failed = _pending_requests.pop(id(request))
        else:
            pending["completed"] = True
    if failed is not None:
        if getattr(_legacy_request_context, "request_key", None) == id(request):
            delattr(_legacy_request_context, "request_key")
        entry = _legacy_base_entry(failed)
        status_code = int(failed.get("status_code", 0))
        entry["status"] = "error"
        entry["error"] = {
            "type": "HTTPStatusError",
            "category": "provider_error",
            "retryable": status_code == 429 or status_code >= 500,
            "status_code": status_code,
        }
        _write_log(entry)


def _model_dump(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if value is not None and hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump()
            return dumped if isinstance(dumped, Mapping) else {}
        except Exception:
            return {}
    return {}


def _legacy_usage(response: Any) -> dict[str, int]:
    usage = _model_dump(getattr(response, "usage", None))
    prompt_details = _model_dump(usage.get("prompt_tokens_details"))
    completion_details = _model_dump(usage.get("completion_tokens_details"))
    return {
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
        "cache_read_tokens": int(prompt_details.get("cached_tokens") or 0),
        "cache_write_tokens": 0,
        "reasoning_tokens": int(completion_details.get("reasoning_tokens") or 0),
    }


def _legacy_final_text(response: Any) -> str:
    choices = getattr(response, "choices", None)
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    return content if isinstance(content, str) else ""


# ==================== 公开 API ====================


def create_logging_http_client() -> httpx.Client:
    """创建带隐私保护日志记录的 HTTPX 客户端。"""

    return httpx.Client(
        event_hooks={
            "request": [_on_request],
            "response": [_on_response],
        }
    )


def discard_pending_legacy_request() -> None:
    """Forget the current thread's legacy request after an SDK/network failure."""

    request_key = getattr(_legacy_request_context, "request_key", None)
    if request_key is None:
        return
    with _log_lock:
        _pending_requests.pop(request_key, None)
    delattr(_legacy_request_context, "request_key")


def log_llm_response(response: Any) -> None:
    """Finalize one legacy SDK call without writing its raw response."""

    request_key = getattr(_legacy_request_context, "request_key", None)
    if request_key is None:
        return
    with _log_lock:
        pending = _pending_requests.get(request_key)
        if pending is None or not pending.get("completed"):
            return
        pending = _pending_requests.pop(request_key)
    delattr(_legacy_request_context, "request_key")

    entry = _legacy_base_entry(pending)
    entry["status"] = "success"
    entry["usage"] = _legacy_usage(response)
    if pending.get("include_content"):
        entry["response"] = {"text": _legacy_final_text(response)}
    _write_log(entry)


__all__ = [
    "LLMRequestLogHandle",
    "begin_gateway_request",
    "create_logging_http_client",
    "discard_pending_legacy_request",
    "finish_gateway_request",
    "is_env_api_key_override_active",
    "is_llm_content_logging_enabled",
    "log_gateway_cache_hit",
    "log_llm_response",
    "set_env_api_key_override",
    "set_llm_content_logging",
]
