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


def test_postprocess_thread_forwards_progress_while_runner_is_still_working(
    tmp_path, monkeypatch
):
    """卡在「正在读取初版字幕」：run_postprocess_task 期间没有后续进度。"""

    source = tmp_path / "【初版字幕】sample.srt"
    _subtitle(source)
    task = PostprocessTask(
        str(source),
        config_snapshot=PostprocessConfig(
            trim_trailing_punct=False,
            speed_optimize=False,
            speed_semantic_repair=False,
        ),
    )
    captured = {}
    messages = []

    def capturing_runner(task, **kwargs):
        captured["progress"] = kwargs.get("progress")
        progress = captured["progress"]
        if progress is not None:
            progress(40, "正在修复观看问题")
        from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
        from videocaptioner.core.entities import SubtitleLayoutEnum
        from videocaptioner.core.postprocess.models import PostprocessResult
        from videocaptioner.core.postprocess.report import QualityReport

        data = ASRData([ASRDataSeg("译文", 0, 2000)])
        return PostprocessResult(
            task,
            data,
            data,
            QualityReport(segment_count=1),
            SubtitleLayoutEnum.ONLY_TRANSLATE,
            1.0,
            (),
            True,
            False,
        )

    monkeypatch.setattr(
        "videocaptioner.ui.thread.postprocess_thread.run_postprocess_task",
        capturing_runner,
    )
    thread = PostprocessThread(task)
    thread.progress.connect(lambda _value, msg: messages.append(msg))
    thread.run()

    assert captured.get("progress") is not None
    assert any("正在修复观看问题" in msg for msg in messages)


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
