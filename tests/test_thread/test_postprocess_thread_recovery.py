"""GUI 恢复提示：后处理（票 09）。

后处理线程的「需要恢复决定」信号与阻塞等待、interactive_recovery
接线（页面 True / 批量与流水线默认 False 不接回调），以及无检查点时
的既有体验不变。线程与视图之间只经信号与提交方法交流；测试用
fake runner 持有回调本身，不发真实模型请求。
"""

from __future__ import annotations

import threading

from PyQt5.QtCore import QEventLoop, QTimer

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.entities import SubtitleLayoutEnum
from videocaptioner.core.postprocess.config import PostprocessConfig
from videocaptioner.core.postprocess.models import PostprocessTask
from videocaptioner.core.postprocess.report import QualityReport
from videocaptioner.core.recovery import RecoveryDecision, RecoverySummary
from videocaptioner.ui.thread import postprocess_thread as postprocess_thread_module
from videocaptioner.ui.thread.postprocess_thread import PostprocessThread

_TIMEOUT_MS = 15_000


def _task(tmp_path) -> PostprocessTask:
    source = tmp_path / "【初版字幕】episode.srt"
    source.write_text("1\n00:00:00,000 --> 00:00:02,000\n译文。\n", encoding="utf-8")
    return PostprocessTask(
        str(source),
        postprocessed_subtitle_path=str(tmp_path / "【后处理字幕】episode.srt"),
        config_snapshot=PostprocessConfig(trim_trailing_punct=True),
    )


def _summary(**overrides) -> RecoverySummary:
    payload = dict(
        module="postprocess",
        identity={"input_fingerprint": "sha256:test"},
        completed={"phase": 1, "rounds": 2},
        checkpoint_time="2026-09-17T00:00:00Z",
        configuration_drift=("后处理配置方案：检查点 sha256:old，当前 sha256:new",),
    )
    payload.update(overrides)
    return RecoverySummary(**payload)


def _fake_result(task: PostprocessTask, recovery_summary=None):
    """已回退形状的最小结果：跳过保存 / sidecar 路径，run() 可走完。"""

    from videocaptioner.core.postprocess.models import PostprocessResult

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
        recovery_summary=recovery_summary,
    )


def _patch_runner(monkeypatch, captured: dict, *, invoke_callback: bool) -> None:
    """Fake runner：捕获回调并按脚本调用（或不调用 = 无检查点）。

    与真实 runner 同口径：决策为继续才在结果上携带 recovery_summary。
    """

    def runner(task, **kwargs):
        captured["recovery_decision"] = kwargs.get("recovery_decision")
        callback = kwargs.get("recovery_decision")
        decision = None
        if callback is not None and invoke_callback:
            decision = callback(_summary())
        captured["decision_result"] = decision
        resumed = decision is None or decision is RecoveryDecision.CONTINUE
        return _fake_result(
            task, recovery_summary=_summary() if resumed and invoke_callback else None
        )

    monkeypatch.setattr(postprocess_thread_module, "run_postprocess_task", runner)


def _run_thread_with_recovery_slot(
    monkeypatch,
    qapp,
    tmp_path,
    *,
    interactive_recovery: bool,
    invoke_callback: bool = True,
    decision: str = "continue",
) -> tuple[PostprocessThread, dict]:
    """起线程并跑事件循环；信号到达时提交决定，线程完成退出循环。"""

    captured: dict = {}
    _patch_runner(monkeypatch, captured, invoke_callback=invoke_callback)
    thread = PostprocessThread(
        _task(tmp_path), interactive_recovery=interactive_recovery
    )
    errors: list[str] = []
    thread.error.connect(errors.append)

    loop = QEventLoop()
    finished = threading.Event()
    timed_out = threading.Event()

    def on_recovery(summary: RecoverySummary) -> None:
        # GUI 线程收到信号后按用户选择提交（「继续」或「从头开始」），
        # 验证「发信号 → 阻塞 → 提交 → 按选择运行」的握手。
        thread.submit_recovery_decision(decision)

    thread.recovery_decision_required.connect(on_recovery)
    thread.finished.connect(lambda *_args: (finished.set(), loop.quit()))

    def on_timeout() -> None:
        timed_out.set()
        thread.terminate()
        loop.quit()

    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(on_timeout)
    timer.start(_TIMEOUT_MS)
    thread.start()
    loop.exec_()
    timer.stop()

    assert not timed_out.is_set(), "recovery handshake did not finish before the timeout"
    assert finished.is_set(), "thread finished without emitting finished"
    assert errors == []
    return thread, captured


def test_recovery_signal_blocks_until_submitted_for_interactive_thread(
    qapp, monkeypatch, tmp_path
):
    """有匹配检查点时页面线程发「需要恢复决定」信号并阻塞至提交。"""

    _thread, captured = _run_thread_with_recovery_slot(
        monkeypatch, qapp, tmp_path, interactive_recovery=True
    )
    assert captured["recovery_decision"] is not None
    assert captured["decision_result"] is RecoveryDecision.CONTINUE


def test_recovery_start_fresh_decision_runs_the_module(qapp, monkeypatch, tmp_path):
    """提交「从头开始」后模块按选择运行：线程正常完成、不报错。"""

    _thread, captured = _run_thread_with_recovery_slot(
        monkeypatch,
        qapp,
        tmp_path,
        interactive_recovery=True,
        decision="start_fresh",
    )
    assert captured["recovery_decision"] is not None
    assert captured["decision_result"] is RecoveryDecision.START_FRESH


def test_recovery_callback_absent_for_unattended_default(qapp, monkeypatch, tmp_path):
    """批量 / 流水线默认（interactive_recovery=False）不接回调：默认继续。"""

    captured: dict = {}
    _patch_runner(monkeypatch, captured, invoke_callback=True)
    thread = PostprocessThread(_task(tmp_path))
    emitted: list[RecoverySummary] = []
    thread.recovery_decision_required.connect(emitted.append)
    errors: list[str] = []
    finished: list[tuple] = []
    thread.finished.connect(lambda *args: finished.append(args))
    thread.error.connect(errors.append)

    thread.run()

    assert captured["recovery_decision"] is None
    assert captured["decision_result"] is None
    assert emitted == []
    assert errors == []
    assert finished, "unattended run should complete without the dialog"


def test_no_signal_without_checkpoint(qapp, monkeypatch, tmp_path):
    """无匹配检查点时 runner 不调用回调：信号不 emit、体验不变。"""

    captured: dict = {}
    _patch_runner(monkeypatch, captured, invoke_callback=False)
    thread = PostprocessThread(_task(tmp_path), interactive_recovery=True)
    emitted: list[RecoverySummary] = []
    thread.recovery_decision_required.connect(emitted.append)
    errors: list[str] = []
    finished: list[tuple] = []
    thread.finished.connect(lambda *args: finished.append(args))
    thread.error.connect(errors.append)

    thread.run()

    assert emitted == []
    assert errors == []
    assert captured["recovery_decision"] is not None  # 回调仍接线
    assert finished


def test_stop_wakes_blocked_recovery_wait(qapp, monkeypatch, tmp_path):
    """用户停在恢复等待时点停止：stop() 唤醒阻塞等待并按取消退出。"""

    import time

    captured: dict = {}
    _patch_runner(monkeypatch, captured, invoke_callback=False)
    thread = PostprocessThread(_task(tmp_path), interactive_recovery=True)
    emitted: list[RecoverySummary] = []
    thread.recovery_decision_required.connect(emitted.append)

    worker = threading.Thread(target=_expect_interrupted, args=(thread,), daemon=True)
    worker.start()
    time.sleep(0.3)

    # stop()：requestInterruption + notify 恢复条件变量。若 stop() 不
    # notify，blocker 要等满 0.2s 轮询超时才靠中断标志退出。
    thread.stop()
    worker.join(timeout=5)
    assert not worker.is_alive(), "stop() did not wake the blocked recovery wait"
    qapp.processEvents()
    assert emitted, "recovery signal was not emitted before the stop"


def _expect_interrupted(thread: PostprocessThread) -> None:
    try:
        thread._confirm_recovery(_summary())
        raise AssertionError("cancelled recovery wait should raise InterruptedError")
    except InterruptedError:
        return


def test_stop_during_recovery_wait_emits_cancelled_not_fallback(
    qapp, monkeypatch, tmp_path
):
    """恢复等待中的停止（票 09 spec 审查修复）：线程按取消终态收场。

    停止语义（P05 / spec 用户故事 31）：cancelled 信号发出、不交付
    部分成果、下游阻断——不是 fallback「回退到初版字幕」继续下游。
    """

    import time

    from PyQt5.QtCore import Qt

    def hanging_runner(task, **kwargs):
        callback = kwargs.get("recovery_decision")
        assert callback is not None
        return callback(_summary())  # 阻塞在恢复等待里直到 stop()

    monkeypatch.setattr(
        postprocess_thread_module, "run_postprocess_task", hanging_runner
    )
    thread = PostprocessThread(_task(tmp_path), interactive_recovery=True)
    cancelled: list[bool] = []
    finished: list[tuple] = []
    warnings: list[str] = []
    thread.cancelled.connect(lambda: cancelled.append(True), Qt.DirectConnection)
    thread.finished.connect(lambda *args: finished.append(args))
    thread.warning.connect(warnings.append)

    thread.start()
    # 等 worker 进入恢复等待（信号送达需事件循环转一圈）。
    deadline = time.perf_counter() + 5
    while time.perf_counter() < deadline and not cancelled and not finished:
        qapp.processEvents()
        if not thread.isRunning():
            break
        time.sleep(0.02)
    assert not finished, "thread should still be blocked in the recovery wait"
    thread.stop()
    assert thread.wait(5000)

    assert cancelled == [True], "stop during recovery wait must emit cancelled"
    assert not finished, "cancelled run must not emit finished (blocks downstream)"
    assert thread.task.status == "cancelled"
    assert warnings == [], "no fallback warning should be published"


def test_recovery_summary_accessor_feeds_batch_row_annotation(
    qapp, monkeypatch, tmp_path
):
    """recovery_summary() 访问器：续跑运行带摘要、未续跑为 None（批量行标注消费）。"""

    # 续跑运行（决策为继续 / 默认继续）：结果携带恢复摘要。
    captured: dict = {}
    _patch_runner(monkeypatch, captured, invoke_callback=True)
    thread = PostprocessThread(_task(tmp_path))  # 批量默认：不接回调
    thread.recovery_decision_required.connect(lambda *_: None)
    thread.error.connect(lambda *_: None)
    thread.run()
    # fake runner：未接回调 → 决策为默认继续 → 带 recovery_summary。
    assert thread.recovery_summary() is not None

    # 未续跑运行（无检查点）：runner 不带摘要，访问器返回 None。
    captured_fresh: dict = {}
    _patch_runner(monkeypatch, captured_fresh, invoke_callback=False)
    fresh_thread = PostprocessThread(_task(tmp_path), interactive_recovery=True)
    fresh_thread.recovery_decision_required.connect(lambda *_: None)
    fresh_thread.error.connect(lambda *_: None)
    fresh_thread.run()
    assert fresh_thread.recovery_summary() is None
