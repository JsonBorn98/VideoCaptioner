"""Unified LLM client for the application."""

import os
import threading
from collections.abc import Mapping
from typing import Any, List, Optional
from urllib.parse import urlparse, urlunparse

import openai
from openai import OpenAI
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from videocaptioner.core.utils.cache import get_llm_cache, memoize
from videocaptioner.core.utils.logger import setup_logger

from .models import LLMCallError, LLMErrorCategory
from .request_logger import (
    create_logging_http_client,
    discard_pending_legacy_request,
    log_llm_response,
)

_global_client: Optional[OpenAI] = None
_client_lock = threading.Lock()

logger = setup_logger("llm_client")


def _sanitized_openai_error(exc: BaseException) -> LLMCallError:
    """Convert legacy SDK errors without retaining provider bodies or URLs."""

    status_code = getattr(exc, "status_code", None)
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return LLMCallError(
            "LLM provider authentication or permission check failed",
            category=LLMErrorCategory.AUTHENTICATION,
            retryable=False,
            status_code=status_code,
        )
    if isinstance(
        exc,
        (
            openai.RateLimitError,
            openai.APITimeoutError,
            openai.APIConnectionError,
            openai.InternalServerError,
        ),
    ):
        return LLMCallError(
            (
                "LLM provider rate limit exceeded"
                if isinstance(exc, openai.RateLimitError)
                else "LLM provider request timed out"
                if isinstance(exc, openai.APITimeoutError)
                else "Could not connect to LLM provider"
                if isinstance(exc, openai.APIConnectionError)
                else f"LLM provider returned HTTP {status_code or 500}"
            ),
            category=LLMErrorCategory.TRANSIENT,
            retryable=True,
            status_code=status_code,
        )
    return LLMCallError(
        (
            f"LLM provider returned HTTP {status_code}"
            if status_code is not None
            else f"LLM provider call failed ({type(exc).__name__})"
        ),
        category=LLMErrorCategory.CONFIGURATION,
        retryable=False,
        status_code=status_code,
    )


def normalize_base_url(base_url: str) -> str:
    """Normalize API base URL by ensuring /v1 suffix when needed."""
    url = base_url.strip()
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")

    if not path:
        path = "/v1"

    normalized = urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )

    return normalized


def get_llm_client() -> OpenAI:
    """Get global LLM client instance (thread-safe singleton)."""
    global _global_client

    if _global_client is None:
        with _client_lock:
            if _global_client is None:
                base_url = os.getenv("OPENAI_BASE_URL", "").strip()
                base_url = normalize_base_url(base_url)
                api_key = os.getenv("OPENAI_API_KEY", "").strip()

                if not base_url or not api_key:
                    raise ValueError(
                        "OPENAI_BASE_URL and OPENAI_API_KEY environment variables must be set"
                    )

                _global_client = OpenAI(
                    base_url=base_url,
                    api_key=api_key,
                    http_client=create_logging_http_client(),
                )

    return _global_client


def before_sleep_log(retry_state: RetryCallState) -> None:
    # Expected backoff, not a failure: tenacity retries the rate-limited call.
    logger.debug(
        "Rate Limit Error, sleeping and retrying... Please lower your thread concurrency or use better OpenAI API."
    )


def _sanitize_legacy_extra_body(kwargs: dict[str, Any]) -> None:
    """Remove retired sampling controls without mutating the caller's mapping."""

    extra_body = kwargs.get("extra_body")
    if not isinstance(extra_body, Mapping):
        return

    sanitized = dict(extra_body)
    sanitized.pop("temperature", None)
    for container_name in ("extra_body", "chat_template_kwargs"):
        container = sanitized.get(container_name)
        if isinstance(container, Mapping):
            container = dict(container)
            container.pop("temperature", None)
            sanitized[container_name] = container
    kwargs["extra_body"] = sanitized


@retry(
    stop=stop_after_attempt(10),
    wait=wait_random_exponential(multiplier=1, min=5, max=60),
    retry=retry_if_exception_type(openai.RateLimitError),
    before_sleep=before_sleep_log,
)
def _call_llm_api(
    messages: List[dict],
    model: str,
    temperature: float = 1,
    **kwargs: Any,
) -> Any:
    """实际调用 LLM API（带重试）。

    ``temperature`` is retained only for compatibility with legacy callers.  Modern
    providers increasingly reject it, so it must never be included in the SDK call.
    """
    client = get_llm_client()
    _sanitize_legacy_extra_body(kwargs)

    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,  # pyright: ignore[reportArgumentType]
            **kwargs,
        )
    except BaseException:
        discard_pending_legacy_request()
        raise

    # 记录响应内容
    log_llm_response(response)

    return response


@memoize(get_llm_cache(), expire=3600, typed=True)
def call_llm(
    messages: List[dict],
    model: str,
    temperature: float = 1,
    **kwargs: Any,
) -> Any:
    """Call LLM API with automatic caching.

    ``temperature`` remains an ignored compatibility parameter for external callers.
    """
    try:
        response = _call_llm_api(messages, model, **kwargs)
    except LLMCallError:
        raise
    except Exception as exc:
        raise _sanitized_openai_error(exc) from None

    if not (
        response
        and hasattr(response, "choices")
        and response.choices
        and len(response.choices) > 0
        and hasattr(response.choices[0], "message")
        and response.choices[0].message.content
    ):
        raise ValueError("Invalid OpenAI API response: empty choices or content")

    return response
