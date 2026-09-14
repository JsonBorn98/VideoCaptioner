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

# 一次逻辑请求排队等待的总预算内，信号量轮询的粒度（秒）。取停止响应
# 门槛 0.30s 的 1/10：排队取消在门槛内可见（票 06）。
_GATE_POLL_SECONDS = 0.03


def _derived_deadline_seconds(max_attempts: int, attempt_window: float) -> float:
    """Derive the total wait budget for one logical request (票 06).

    覆盖排队 + 传输尝试 + 重试退避（spec：网络分阶段 timeout 不替代
    端到端期限）：每次尝试给满缩放后的网络窗口，每次重试额外给指数
    退避的结构上界（与 ``complete`` 的 ``min(30, 2**(n-1))`` 同型，
    含抖动余量）。数值是结构推导的上界，不是承诺的完成时限。
    """
    total = float(attempt_window) * max_attempts
    backoff = 0.0
    for attempt in range(1, max_attempts + 1):
        backoff += min(30.0, 2 ** (attempt - 1))
    return total + backoff * 1.0 + _GATE_QUEUE_RESERVE_SECONDS


# 排队预算的固定预留（秒）：信号量等待不占尝试窗口，但必须有界。
_GATE_QUEUE_RESERVE_SECONDS = 30.0


def _deadline_exceeded(
    profile: LLMModelProfile, attempt: int, budget: float
) -> LLMCallError:
    return LLMCallError(
        f"LLM request for profile {profile.name} exceeded its total wait "
        f"budget of {budget:.1f}s (after {attempt} attempt(s)); giving up "
        "instead of waiting without bound",
        category=LLMErrorCategory.TRANSIENT,
        retryable=True,
    )


class _CancellableAcquire:
    """可取消的信号量获取（票 06：排队等待不挂在 ``BoundedSemaphore`` 上）。

    ``BoundedSemaphore.acquire`` 阻塞时无法从外部唤醒；改为非阻塞
    ``acquire(False)`` + 短轮询，每次轮询检查取消与剩余总预算。
    ``__enter__`` 返回是否成功获得槽位；``__exit__`` 释放获得的槽位。
    """

    def __init__(
        self,
        semaphore: threading.BoundedSemaphore,
        cancelled: Optional[Callable[[], bool]],
        remaining: Callable[[], float],
    ) -> None:
        self._semaphore = semaphore
        self._cancelled = cancelled
        self._remaining = remaining
        self._acquired = False

    def __enter__(self) -> bool:
        if self._cancelled is None:
            deadline = time.perf_counter() + max(self._remaining(), 0.0)
            while not self._semaphore.acquire(blocking=False):
                if time.perf_counter() >= deadline:
                    return False
                time.sleep(_GATE_POLL_SECONDS)
            self._acquired = True
            return True
        while True:
            if self._cancelled():
                return False
            if self._semaphore.acquire(blocking=False):
                self._acquired = True
                return True
            if self._remaining() <= 0:
                return False
            time.sleep(_GATE_POLL_SECONDS)

    def __exit__(self, *exc_info: object) -> None:
        if self._acquired:
            self._semaphore.release()


def _cancellable_acquire(
    semaphore: threading.BoundedSemaphore,
    cancelled: Optional[Callable[[], bool]],
    remaining: float,
) -> _CancellableAcquire:
    """``complete`` 用的包装：把剩余预算转成可调用口径并附排队预算。

    排队是总预算之外单独有界的窗口（``_GATE_QUEUE_RESERVE_SECONDS``）：
    传输 / 退避吃总预算，排队等待不吃满传输窗口但仍有自己的上界。
    """
    budget = remaining + _GATE_QUEUE_RESERVE_SECONDS
    clock = {"start": time.perf_counter()}

    def _remaining() -> float:
        return budget - (time.perf_counter() - clock["start"])

    return _CancellableAcquire(semaphore, cancelled, _remaining)


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
        deadline_seconds: Optional[float] = None,
    ) -> LLMResult:
        """Dispatch one logical request with bounded, cancellable waits (票 06).

        一个逻辑请求的有限等待预算（spec「请求期限、传输重试与失败语义」）
        覆盖排队、传输尝试与重试退避；``deadline_seconds`` 是显式总期限，
        缺省按尝试结构推导上界（不依赖网络 timeout 冒充端到端期限）。
        三个等待点全部可取消：信号量排队、退避 sleep、在途传输
        （adapter 请求级尽力通道）；Retry-After 超出剩余预算时明确失败，
        不提前重发也不无限 sleep。
        """
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if use_cache:
            cached = self._response_cache.lookup(profile, request)
            if cached is not None:
                log_gateway_cache_hit(profile, request, cached)
                return cached
        adapter, semaphore = self._resources(profile)
        if deadline_seconds is None:
            deadline_seconds = _derived_deadline_seconds(
                max_attempts, self._request_deadline_hint(request)
            )
        started = time.perf_counter()

        def _remaining() -> float:
            return deadline_seconds - (time.perf_counter() - started)  # type: ignore[operator]

        def _raise_if_cancelled() -> None:
            if cancelled is not None and cancelled():
                raise InterruptedError("LLM request cancelled")

        last_error: Optional[LLMCallError] = None
        for attempt in range(1, max_attempts + 1):
            _raise_if_cancelled()
            if _remaining() <= 0:
                raise _deadline_exceeded(profile, attempt, deadline_seconds)
            try:
                with _cancellable_acquire(
                    semaphore, cancelled, _remaining()
                ) as acquired:
                    if not acquired:
                        _raise_if_cancelled()
                        raise _deadline_exceeded(profile, attempt, deadline_seconds)
                    _raise_if_cancelled()
                    # 每次尝试的网络窗口也受总预算钳制（票 06：传输尝试
                    # 计入端到端期限，网络分阶段 timeout 不替代总预算）。
                    # 剩余预算小于请求窗口时收窄本次尝试的 deadline；
                    # ``LLMRequest`` 冻结，用 ``replace`` 派生钳制副本。
                    attempt_window = min(
                        self._request_deadline_hint(request), max(_remaining(), 0.0)
                    )
                    if request.timeout is None or request.timeout > attempt_window:
                        request = replace(request, timeout=attempt_window)
                    log_handle = begin_gateway_request(profile, request, attempt=attempt)
                    try:
                        if cancelled is None:
                            result = adapter.complete(request)
                        else:
                            result = adapter.complete(request, cancelled=cancelled)
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
                if attempt + 1 > max_attempts:
                    raise
                backoff = min(30.0, 2 ** (attempt - 1)) * (
                    0.75 + self._random() * 0.5
                )
                requested = max(backoff, exc.retry_after_seconds or 0.0)
                remaining = _remaining()
                if requested > remaining:
                    # Retry-After / 退避超出剩余总预算：明确结束等待并报告原因
                    # （spec：不得提前违反 Retry-After 重发，也不无限 sleep）。
                    logger.warning(
                        "LLM retry for profile %s needs %.1fs but only %.1fs of "
                        "the request budget remains; giving up after %s attempt(s): %s",
                        profile.name,
                        requested,
                        max(remaining, 0.0),
                        attempt,
                        exc,
                    )
                    raise
                logger.warning(
                    "LLM transient error for profile %s; retry %s/%s in %.1fs: %s",
                    profile.name,
                    attempt + 1,
                    attempt_limit,
                    requested,
                    exc,
                )
                self._cancellable_sleep(requested, cancelled, _remaining)
        assert last_error is not None
        raise last_error

    @staticmethod
    def _request_deadline_hint(request: LLMRequest) -> float:
        """One attempt's network window for this request (output-scaled)."""
        from .adapters import request_timeout_seconds

        return request_timeout_seconds(
            request.max_output_tokens,
            baseline=request.timeout if request.timeout is not None else 120.0,
        )

    def _cancellable_sleep(
        self,
        seconds: float,
        cancelled: Optional[Callable[[], bool]],
        remaining: Callable[[], float],
    ) -> None:
        """可取消、有界的退避等待（票 06：不在 sleep 前后检查了事）。

        ``self._sleep`` 仍是唯一的等待实现通道，每次退避恰一次完整
        调用（测试 / 探针的注入 seam：退避请求时长与开始/结束可观测）；
        等待本身放在后台线程，调用线程以 ``_GATE_POLL_SECONDS`` 轮询
        取消——置位即立刻返回，sleeper 线程按原时长在后台自然结束
        （每次退避至多遗留一个 daemon 计时线程，``max_attempts`` 有界）。
        ``remaining`` 只作预算口径记录；耗尽由下一轮尝试前的显式判定处理。
        """
        del remaining
        if cancelled is None:
            self._sleep(seconds)
            return
        done = threading.Event()

        def _sleeper() -> None:
            try:
                self._sleep(seconds)
            finally:
                done.set()

        threading.Thread(target=_sleeper, daemon=True).start()
        while not done.wait(_GATE_POLL_SECONDS):
            if cancelled():
                raise InterruptedError("LLM request cancelled")


__all__ = ["LLMGateway"]
