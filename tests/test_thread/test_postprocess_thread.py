from pathlib import Path
from threading import Event

from PyQt5.QtCore import Qt

from videocaptioner.core.postprocess.config import PostprocessConfig
from videocaptioner.core.postprocess.models import PostprocessTask
from videocaptioner.ui.thread.postprocess_thread import PostprocessThread


def _subtitle(path: Path) -> None:
    path.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n译文。\n",
        encoding="utf-8",
    )


def test_postprocess_thread_emits_separate_active_output(tmp_path):
    source = tmp_path / "【初版字幕】sample.srt"
    output = tmp_path / "【后处理字幕】sample.srt"
    _subtitle(source)
    task = PostprocessTask(
        str(source),
        postprocessed_subtitle_path=str(output),
        config_snapshot=PostprocessConfig(trim_trailing_punct=True),
    )
    finished = []
    errors = []
    thread = PostprocessThread(task)
    thread.finished.connect(lambda video, path: finished.append((video, path)))
    thread.error.connect(errors.append)

    thread.run()

    assert not errors
    assert finished == [("", str(output))]
    assert source.exists()
    assert output.exists()
    assert "译文。" in source.read_text(encoding="utf-8")
    assert "译文。" not in output.read_text(encoding="utf-8")


def test_postprocess_thread_unexpected_failure_continues_with_initial(
    tmp_path, monkeypatch
):
    source = tmp_path / "【初版字幕】sample.srt"
    _subtitle(source)
    task = PostprocessTask(
        str(source),
        config_snapshot=PostprocessConfig(trim_trailing_punct=True),
    )
    monkeypatch.setattr(
        "videocaptioner.ui.thread.postprocess_thread.run_postprocess_task",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("stage unavailable")),
    )
    finished = []
    errors = []
    thread = PostprocessThread(task)
    thread.finished.connect(lambda video, path: finished.append((video, path)))
    thread.error.connect(errors.append)

    thread.run()

    assert not errors
    assert finished == [("", str(source))]
    assert task.status == "fallback"
    assert task.active_subtitle_path == str(source)


def test_postprocess_thread_cancel_suppresses_late_finished_signal(tmp_path, monkeypatch):
    source = tmp_path / "【初版字幕】sample.srt"
    _subtitle(source)
    task = PostprocessTask(str(source), config_snapshot=PostprocessConfig())
    entered = Event()
    release = Event()

    def slow_runner(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return object()

    monkeypatch.setattr(
        "videocaptioner.ui.thread.postprocess_thread.run_postprocess_task", slow_runner
    )
    finished = []
    cancelled = []
    thread = PostprocessThread(task)
    thread.finished.connect(lambda *_: finished.append(True))
    thread.cancelled.connect(lambda: cancelled.append(True), Qt.DirectConnection)

    thread.start()
    assert entered.wait(5)
    thread.stop()
    release.set()
    assert thread.wait(5000)

    assert cancelled == [True]
    assert not finished
    assert task.status == "cancelled"
