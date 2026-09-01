"""Concurrency-limited, retrying dispatch for model profiles."""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Optional

from videocaptioner.core.utils.logger import setup_logger

from .adapters import (
    AnthropicMessagesAdapter,
    GeminiAdapter,
    LLMAdapter,
    OpenAICompatibleAdapter,
)
from .models import (
    LLMCallError,
    LLMErrorCategory,
    LLMModelProfile,
    LLMRequest,
    LLMResult,
    LLMTransport,
    is_output_limit_finish_reason,
)
from .request_logger import (
    begin_gateway_request,
    finish_gateway_request,
    log_gateway_cache_hit,
)
from .response_cache import GatewayResponseCache

logger = setup_logger("llm_gateway")

# Shared module-level cache so separate gateway instances (one per consumer)
# still deduplicate across runs through the same disk directory.
_shared_response_cache = GatewayResponseCache()


class LLMGateway:
    def __init__(
        self,
        adapter_factory: Optional[Callable[[LLMModelProfile], LLMAdapter]] = None,
        sleep: Callable[[float], None] = time.sleep,
        random_source: Callable[[], float] = random.random,
        response_cache: Optional[GatewayResponseCache] = None,
        max_concurrency: int = 10,
    ) -> None:
        if type(max_concurrency) is not int or max_concurrency < 1:
            raise ValueError("max_concurrency must be a positive integer")
        self._adapter_factory = adapter_factory or self._default_adapter
        self._sleep = sleep
        self._random = random_source
        self._max_concurrency = max_concurrency
        self._response_cache = (
            _shared_response_cache if response_cache is None else response_cache
        )
        self._adapters: dict[str, LLMAdapter] = {}
        self._semaphores: dict[str, threading.BoundedSemaphore] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _default_adapter(profile: LLMModelProfile) -> LLMAdapter:
        if profile.transport is LLMTransport.OPENAI_COMPATIBLE:
            return OpenAICompatibleAdapter(profile)
        if profile.transport is LLMTransport.ANTHROPIC_MESSAGES:
            return AnthropicMessagesAdapter(profile)
        if profile.transport is LLMTransport.GEMINI:
            return GeminiAdapter(profile)
        raise ValueError(f"Unsupported LLM transport: {profile.transport}")

    def _resources(
        self, profile: LLMModelProfile
    ) -> tuple[LLMAdapter, threading.BoundedSemaphore]:
        with self._lock:
            adapter = self._adapters.get(profile.profile_id)
            if adapter is None or adapter.profile != profile:
                if adapter is not None:
                    adapter.close()
                adapter = self._adapter_factory(profile)
                self._adapters[profile.profile_id] = adapter
                self._semaphores[profile.profile_id] = threading.BoundedSemaphore(
                    profile.clamped_concurrency(self._max_concurrency)
                )
            return adapter, self._semaphores[profile.profile_id]

    def close(self) -> None:
        """Release native cache resources and provider sessions."""

        with self._lock:
            adapters = tuple(self._adapters.values())
            self._adapters.clear()
            self._semaphores.clear()
        for adapter in adapters:
            adapter.close()

    def complete(
        self,
        profile: LLMModelProfile,
        request: LLMRequest,
        *,
        max_attempts: int = 4,
        cancelled: Optional[Callable[[], bool]] = None,
        use_cache: bool = True,
    ) -> LLMResult:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if use_cache:
            cached = self._response_cache.lookup(profile, request)
            if cached is not None:
                log_gateway_cache_hit(profile, request, cached)
                return cached
        adapter, semaphore = self._resources(profile)
        last_error: Optional[LLMCallError] = None
        for attempt in range(1, max_attempts + 1):
            if cancelled is not None and cancelled():
                raise InterruptedError("LLM request cancelled")
            try:
                with semaphore:
                    if cancelled is not None and cancelled():
                        raise InterruptedError("LLM request cancelled")
                    log_handle = begin_gateway_request(profile, request, attempt=attempt)
                    try:
                        result = adapter.complete(request)
                    except BaseException as exc:
                        duration_ms = finish_gateway_request(log_handle, error=exc)
                        if isinstance(exc, LLMCallError):
                            exc.duration_ms = duration_ms
                        raise
                    duration_ms = finish_gateway_request(log_handle, result=result)
                    result = replace(result, duration_ms=duration_ms)
                    if use_cache:
                        self._response_cache.store(profile, request, result)
                    return result
            except LLMCallError as exc:
                exc.attempts = attempt
                last_error = exc
                # Invalid provider responses get one bounded retry. Repeating a
                # reasoning-heavy empty completion four times is expensive and
                # rarely useful, while one retry recovers transient empty bodies.
                if is_output_limit_finish_reason(exc.finish_reason):
                    # Repeating an already exhausted output budget cannot recover.
                    # Enhanced translation owns semantic cap escalation and input splitting.
                    attempt_limit = 1
                elif exc.category is LLMErrorCategory.INVALID_RESPONSE:
                    attempt_limit = min(max_attempts, 2)
                else:
                    attempt_limit = max_attempts
                if not exc.retryable or attempt >= attempt_limit:
                    raise
                backoff = min(30.0, 2 ** (attempt - 1)) * (
                    0.75 + self._random() * 0.5
                )
                delay = max(backoff, exc.retry_after_seconds or 0.0)
                logger.warning(
                    "LLM transient error for profile %s; retry %s/%s in %.1fs: %s",
                    profile.name,
                    attempt + 1,
                    attempt_limit,
                    delay,
                    exc,
                )
                self._sleep(delay)
        assert last_error is not None
        raise last_error


__all__ = ["LLMGateway"]
