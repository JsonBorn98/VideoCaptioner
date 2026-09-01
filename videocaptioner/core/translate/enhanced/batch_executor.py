"""Sliding-window batch execution for enhanced translation stages."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Callable, Optional, Sequence, TypeVar

from .models import CancellationToken

T = TypeVar("T")
R = TypeVar("R")

_CANCEL_POLL_SECONDS = 0.05


def execute_batches(
    batches: Sequence[T],
    execute: Callable[[T], R],
    *,
    concurrency: int,
    cancellation: CancellationToken,
    on_complete: Optional[Callable[[R], None]] = None,
) -> list[R]:
    """Run independent batches with a sliding concurrency window.

    When there are more batches than ``concurrency``, the first batch runs
    serially so a stable provider prefix can warm implicit caches. Otherwise
    every batch is eligible for the window immediately. ``on_complete`` fires
    on the coordinating thread after each successful batch so callers can keep
    finished work if later batches are cancelled.
    """

    if type(concurrency) is not int or concurrency < 1:
        raise ValueError("concurrency must be a positive integer")
    cancellation.raise_if_cancelled()
    if not batches:
        return []

    results: list[Optional[R]] = [None] * len(batches)

    def record(index: int, value: R) -> None:
        results[index] = value
        if on_complete is not None:
            on_complete(value)

    start = 0
    if len(batches) > concurrency:
        record(0, execute(batches[0]))
        start = 1
        cancellation.raise_if_cancelled()

    remaining = list(enumerate(batches[start:], start=start))
    if not remaining:
        return [item for item in results if item is not None]

    workers = min(concurrency, len(remaining))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        pending: dict[Future[tuple[int, R]], int] = {}
        iterator = iter(remaining)

        def submit_next() -> bool:
            try:
                index, batch = next(iterator)
            except StopIteration:
                return False
            pending[
                executor.submit(_run_indexed, execute, index, batch, cancellation)
            ] = index
            return True

        for _ in range(workers):
            submit_next()
        try:
            while pending:
                cancellation.raise_if_cancelled()
                completed, _ = wait(
                    tuple(pending),
                    timeout=_CANCEL_POLL_SECONDS,
                    return_when=FIRST_COMPLETED,
                )
                if not completed:
                    continue
                finished: list[tuple[int, R]] = []
                errors: list[BaseException] = []
                for future in completed:
                    pending.pop(future)
                    try:
                        finished.append(future.result())
                    except BaseException as exc:
                        errors.append(exc)
                for index, value in finished:
                    record(index, value)
                if errors:
                    raise errors[0]
                for _ in finished:
                    cancellation.raise_if_cancelled()
                    submit_next()
        except BaseException:
            for future in pending:
                future.cancel()
            raise

    return [item for item in results if item is not None]


def _run_indexed(
    execute: Callable[[T], R],
    index: int,
    batch: T,
    cancellation: CancellationToken,
) -> tuple[int, R]:
    cancellation.raise_if_cancelled()
    return index, execute(batch)
