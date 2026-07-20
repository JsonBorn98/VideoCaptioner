"""Concurrency-limited, retrying dispatch for model profiles."""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from typing import Optional

from videocaptioner.core.utils.logger import setup_logger

from .adapters import (
    AnthropicMessagesAdapter,
    GeminiAdapter,
    LLMAdapter,
    OpenAICompatibleAdapter,
)
from .models import LLMCallError, LLMModelProfile, LLMRequest, LLMResult, LLMTransport
from .request_logger import begin_gateway_request, finish_gateway_request

logger = setup_logger("llm_gateway")


class LLMGateway:
    def __init__(
        self,
        adapter_factory: Optional[Callable[[LLMModelProfile], LLMAdapter]] = None,
        sleep: Callable[[float], None] = time.sleep,
        random_source: Callable[[], float] = random.random,
    ) -> None:
        self._adapter_factory = adapter_factory or self._default_adapter
        self._sleep = sleep
        self._random = random_source
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
                    profile.max_concurrency
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
    ) -> LLMResult:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
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
                        finish_gateway_request(log_handle, error=exc)
                        raise
                    finish_gateway_request(log_handle, result=result)
                    return result
            except LLMCallError as exc:
                exc.attempts = attempt
                last_error = exc
                if not exc.retryable or attempt >= max_attempts:
                    raise
                backoff = min(30.0, 2 ** (attempt - 1)) * (
                    0.75 + self._random() * 0.5
                )
                delay = max(backoff, exc.retry_after_seconds or 0.0)
                logger.warning(
                    "LLM transient error for profile %s; retry %s/%s in %.1fs: %s",
                    profile.name,
                    attempt + 1,
                    max_attempts,
                    delay,
                    exc,
                )
                self._sleep(delay)
        assert last_error is not None
        raise last_error


__all__ = ["LLMGateway"]
