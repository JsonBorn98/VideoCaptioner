"""Privacy-preserving request logs for gateway LLM calls."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from videocaptioner.config import LOG_PATH

from .models import LLMCallError, LLMModelProfile, LLMRequest, LLMResult, LLMUsage

LLM_LOG_FILE = LOG_PATH / "llm_requests.jsonl"
MAX_LOG_SIZE = 10 * 1024 * 1024  # 10MB


_log_lock = threading.Lock()
_content_logging_enabled = False
# Set by the CLI when the env credential override swaps a resolved profile's
# api_key; gateway log entries then record key_source="env_override".
_env_api_key_override = False


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
) -> int:
    """Write one completed gateway attempt with normalized usage fields."""

    entry = dict(handle.entry)
    duration_ms = max(0, int((time.perf_counter() - handle.started_at) * 1000))
    entry["duration_ms"] = duration_ms
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
    return duration_ms


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


__all__ = [
    "LLMRequestLogHandle",
    "begin_gateway_request",
    "finish_gateway_request",
    "is_env_api_key_override_active",
    "is_llm_content_logging_enabled",
    "log_gateway_cache_hit",
    "set_env_api_key_override",
    "set_llm_content_logging",
]
