import threading

import pytest

from videocaptioner.ui.thread.modelscope_download_thread import (
    DownloadCancelled,
    create_progress_callback_class,
)


def test_modelscope_progress_aggregates_parallel_shards_without_zero_ping_pong():
    events: list[tuple[int, str]] = []
    callback_class = create_progress_callback_class(
        lambda percentage, message: events.append((percentage, message))
    )

    shard_a = callback_class("model-00001-of-00002.safetensors", 1000)
    shard_b = callback_class("model-00002-of-00002.safetensors", 1000)

    shard_a.update(1)
    shard_b.update(1)
    shard_a.update(99)
    shard_b.update(99)
    shard_a.update(900)
    shard_b.update(900)

    percentages = [percentage for percentage, _ in events]

    assert percentages == sorted(percentages)
    assert percentages.count(0) == 1
    assert percentages[-1] == 99
    assert any("总进度" in message for _, message in events)


def test_modelscope_progress_callback_stops_when_cancelled():
    cancel_event = threading.Event()
    callback_class = create_progress_callback_class(
        lambda _percentage, _message: None,
        cancel_event=cancel_event,
    )
    callback = callback_class("model.safetensors", 1000)

    cancel_event.set()

    with pytest.raises(DownloadCancelled):
        callback.update(1)
