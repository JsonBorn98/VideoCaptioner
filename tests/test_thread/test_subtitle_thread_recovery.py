"""GUI 恢复提示：翻译（票 08）。

字幕翻译线程的「需要恢复决定」信号与阻塞等待、执行模式接线，
以及无检查点时的既有体验不变。线程与视图之间只经信号与提交方法
交流；测试用 fake runner 持有回调本身，不发真实模型请求。
"""

from __future__ import annotations

import threading

from PyQt5.QtCore import QEventLoop, QTimer

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.entities import SubtitleConfig, SubtitleTask, TranslatorServiceEnum
from videocaptioner.core.llm.models import LLMModelProfile, LLMTransport, ProviderDialect
from videocaptioner.core.recovery import RecoveryDecision, RecoverySummary
from videocaptioner.core.translate.enhanced.models import (
    AuthoritativeGlossary,
    EnhancedTranslationResult,
    TranslationAuditReport,
    TranslationContextBrief,
    TranslationExecutionMode,
)
from videocaptioner.core.translate.enhanced.runner import (
    EnhancedTranslationArtifacts,
    EnhancedTranslationRun,
)
from videocaptioner.core.translate.types import TargetLanguage, TranslationMode
from videocaptioner.ui.thread import subtitle_thread as subtitle_thread_module
from videocaptioner.ui.thread.subtitle_thread import SubtitleThread

_TIMEOUT_MS = 15_000


def _source() -> ASRData:
    return ASRData([ASRDataSeg("Hello", 0, 1000)])


def _profile(profile_id: str) -> LLMModelProfile:
    return LLMModelProfile(
        profile_id=profile_id,
        name=profile_id.title(),
        transport=LLMTransport.OPENAI_COMPATIBLE,
        dialect=ProviderDialect.GENERIC,
        base_url=f"https://{profile_id}.test/v1",
        api_key="secret",
        model=f"{profile_id}-model",
        max_concurrency=1,
    )


def _subtitle_config(execution_mode: TranslationExecutionMode) -> SubtitleConfig:
    return SubtitleConfig(
        need_split=False,
        need_optimize=False,
        need_translate=True,
        translation_mode=TranslationMode.ENHANCED_LLM,
        translator_service=TranslatorServiceEnum.OPENAI,
        target_language=TargetLanguage.SIMPLIFIED_CHINESE,
        utility_llm_profile=None,
        main_llm_profile=_profile("main"),
        review_llm_profile=_profile("review"),
        translation_execution_mode=execution_mode,
    )


def _task(execution_mode: TranslationExecutionMode) -> SubtitleTask:
    return SubtitleTask(
        subtitle_path="source.srt",
        video_path="",
        input_data=_source(),
        output_path="【初版字幕】episode.srt",
        workflow_base_name="episode",
        need_next_task=False,
        subtitle_config=_subtitle_config(execution_mode),
    )


def _summary(**overrides) -> RecoverySummary:
    payload = dict(
        module="translation",
        identity={"source_fingerprint": "sha256:test"},
        completed={
            "analysis": 1,
            "glossary": 1,
            "translation_segments": 3,
            "audit_batches": 2,
        },
        checkpoint_time="2026-09-17T00:00:00Z",
        configuration_drift=("主翻译提示词：检查点 sha256:old，当前 sha256:new",),
    )
    payload.update(overrides)
    return RecoverySummary(**payload)


def _enhanced_run(source: ASRData) -> EnhancedTranslationRun:
    translated = ASRData.from_json(source.to_json())
    translated.segments[0].translated_text = "译文"
    glossary = AuthoritativeGlossary(
        source_language="auto",
        target_language=TargetLanguage.SIMPLIFIED_CHINESE.value,
        subtitle_fingerprint="sha256:test",
    )
    result = EnhancedTranslationResult(
        translations={1: "译文"},
        brief=TranslationContextBrief(outline="A test"),
        glossary=glossary,
        audit_report=TranslationAuditReport(),
    )
    return EnhancedTranslationRun(
        subtitle_data=translated,
        result=result,
        artifacts=EnhancedTranslationArtifacts(
            glossary_path="【项目术语表】episode.vcglossary.json",
            audit_report_path="【翻译审计】episode.md",
        ),
    )


class _FakeSplitter:
    def __init__(self, *args, **kwargs):
        self.rule_fallback_segments = 0

    def split_subtitle(self, data):
        return data

    def stop(self):
        return None


class _FakeOptimizer:
    def __init__(self, *args, **kwargs):
        self.failed_batches = 0
        self.maxed_batches = 0

    def optimize_subtitle(self, data):
        return data

    def stop(self):
        return None


class _GuardGateway:
    """注入用假网关：恢复路径测试不发真请求、不关闭真实资源。"""

    def __init__(self, *args, **kwargs):
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


def _patch_consumers(monkeypatch, captured: dict, *, invoke_callback: bool) -> None:
    """Fake enhanced runner：捕获回调并按脚本调用（或不调用 = 无检查点）。"""

    monkeypatch.setattr(subtitle_thread_module, "SubtitleSplitter", _FakeSplitter)
    monkeypatch.setattr(subtitle_thread_module, "SubtitleOptimizer", _FakeOptimizer)

    def run(source, config, **kwargs):
        captured["recovery_decision"] = kwargs.get("recovery_decision")
        callback = kwargs.get("recovery_decision")
        if callback is not None and invoke_callback:
            captured["summary"] = callback(_summary())
        return _enhanced_run(source)

    monkeypatch.setattr(subtitle_thread_module, "run_enhanced_translation", run)


def _run_thread_with_recovery_slot(
    monkeypatch,
    qapp,
    execution_mode: TranslationExecutionMode,
    *,
    invoke_callback: bool,
    decision: str = "continue",
) -> tuple[SubtitleThread, dict]:
    """起线程并跑事件循环；信号到达时提交决定，线程完成退出循环。"""

    captured: dict = {}
    _patch_consumers(monkeypatch, captured, invoke_callback=invoke_callback)
    thread = SubtitleThread(_task(execution_mode), gateway=_GuardGateway())
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


def test_recovery_signal_blocks_until_submitted_for_gui_modes(qapp, monkeypatch):
    """有匹配检查点时 GUI 任务发「需要恢复决定」信号并阻塞至提交。"""

    for mode in (
        TranslationExecutionMode.GUI_STANDALONE,
        TranslationExecutionMode.GUI_WORKFLOW,
    ):
        _thread, captured = _run_thread_with_recovery_slot(
            monkeypatch, qapp, mode, invoke_callback=True
        )
        assert captured["recovery_decision"] is not None
        assert captured["summary"] is RecoveryDecision.CONTINUE


def test_recovery_start_fresh_decision_runs_the_module(qapp, monkeypatch):
    """提交「从头开始」后模块按选择运行：线程正常完成、不报错。"""

    _thread, captured = _run_thread_with_recovery_slot(
        monkeypatch,
        qapp,
        TranslationExecutionMode.GUI_STANDALONE,
        invoke_callback=True,
        decision="start_fresh",
    )
    assert captured["recovery_decision"] is not None
    assert captured["summary"] is RecoveryDecision.START_FRESH


def test_recovery_callback_absent_for_batch_and_cli_modes(qapp, monkeypatch):
    """BATCH 与 CLI 模式不接回调（默认继续路径），信号不会被 emit。"""

    for mode in (TranslationExecutionMode.BATCH, TranslationExecutionMode.CLI):
        captured: dict = {}
        _patch_consumers(monkeypatch, captured, invoke_callback=True)
        thread = SubtitleThread(_task(mode), gateway=_GuardGateway())
        emitted: list[RecoverySummary] = []
        thread.recovery_decision_required.connect(emitted.append)
        errors: list[str] = []
        thread.finished.connect(lambda *_: None)
        thread.error.connect(errors.append)

        thread.run()

        assert captured["recovery_decision"] is None
        assert emitted == []
        assert errors == []


def test_no_signal_without_checkpoint(qapp, monkeypatch):
    """无匹配检查点时 runner 不调用回调：信号不 emit、体验不变。"""

    captured: dict = {}
    _patch_consumers(monkeypatch, captured, invoke_callback=False)
    thread = SubtitleThread(
        _task(TranslationExecutionMode.GUI_STANDALONE), gateway=_GuardGateway()
    )
    emitted: list[RecoverySummary] = []
    thread.recovery_decision_required.connect(emitted.append)
    errors: list[str] = []
    thread.error.connect(errors.append)

    thread.run()

    assert emitted == []
    assert errors == []
    assert captured["recovery_decision"] is not None  # 回调仍接线


def test_stop_wakes_blocked_recovery_wait(qapp, monkeypatch):
    """用户停在恢复等待时点停止：stop() 唤醒阻塞等待并按取消退出。"""

    import time

    captured: dict = {}
    _patch_consumers(monkeypatch, captured, invoke_callback=False)
    thread = SubtitleThread(
        _task(TranslationExecutionMode.GUI_STANDALONE), gateway=_GuardGateway()
    )
    emitted: list[RecoverySummary] = []
    thread.recovery_decision_required.connect(emitted.append)

    worker = threading.Thread(target=_expect_interrupted, args=(thread,), daemon=True)
    worker.start()
    time.sleep(0.3)

    # stop()：cancel + notify 恢复条件变量。若 stop() 不 notify，
    # blocker 要等满 0.2s 轮询超时才靠 cancelled 标志退出。
    thread.stop()
    worker.join(timeout=5)
    assert not worker.is_alive(), "stop() did not wake the blocked recovery wait"
    qapp.processEvents()
    assert emitted, "recovery signal was not emitted before the stop"


def _expect_interrupted(thread: SubtitleThread) -> None:
    try:
        thread._confirm_recovery(_summary())
        raise AssertionError("cancelled recovery wait should raise InterruptedError")
    except InterruptedError:
        return
