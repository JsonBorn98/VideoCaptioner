"""停止门与停止可达性回归测试。

复现 2026-09-02 12:22 停止失效现场（app.log「正在优化字幕...」出现在停止警告
之后、llm_requests.jsonl 在停止点击后仍出现新的 llm_optimize / llm_split 请求）：
- 取消后 run() 不得继续推进到下一阶段（优化/翻译）发起新请求；
- stop() 必须能触达正在运行的消费者（self.optimizer 此前从未赋值，是死代码）。
"""

import pytest

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.entities import (
    SubtitleConfig,
    SubtitleTask,
    TranslatorServiceEnum,
)
from videocaptioner.core.llm.models import (
    LLMModelProfile,
    LLMTransport,
    ProviderDialect,
)
from videocaptioner.core.translate.enhanced.models import TranslationExecutionMode
from videocaptioner.core.translate.types import TargetLanguage, TranslationMode
from videocaptioner.ui.thread import subtitle_thread as subtitle_thread_module
from videocaptioner.ui.thread.subtitle_thread import SubtitleThread


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


def _word_source() -> ASRData:
    return ASRData(
        [
            ASRDataSeg("Hello", 0, 500),
            ASRDataSeg("world", 500, 1000),
        ]
    )


def _config(**overrides) -> SubtitleConfig:
    config = SubtitleConfig(
        need_split=True,
        need_optimize=True,
        need_translate=False,
        thread_num=2,
        batch_size=5,
        utility_llm_profile=_profile("utility"),
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def _task(tmp_path, config: SubtitleConfig) -> SubtitleTask:
    return SubtitleTask(
        subtitle_path=str(tmp_path / "source.srt"),
        video_path="",
        input_data=_word_source(),
        output_path=str(tmp_path / "【初版字幕】episode.srt"),
        workflow_base_name="episode",
        need_next_task=False,
        subtitle_config=config,
    )


def _make_thread(tmp_path, config: SubtitleConfig):
    thread = SubtitleThread(_task(tmp_path, config))
    cancelled: list = []
    errors: list = []
    thread.cancelled.connect(lambda: cancelled.append(True))
    thread.error.connect(errors.append)
    return thread, cancelled, errors


class _BenignSplitter:
    """快速合并路径的假断句器：不取消、不发请求。"""

    rule_fallback_segments = 0

    def __init__(self, *args, **kwargs):
        pass

    def split_subtitle(self, data):
        return data

    def stop(self):
        return None


class _BenignOptimizer:
    """不取消的假优化器：直接原样返回。"""

    failed_batches = 0
    maxed_batches = 0

    def __init__(self, *args, **kwargs):
        pass

    def optimize_subtitle(self, data):
        return data

    def stop(self):
        return None


def test_cancel_during_split_skips_optimize_and_translate(tmp_path, monkeypatch, qapp):
    """断句阶段取消后，run() 不得进入优化阶段，更不得进入翻译阶段。"""

    class CancellingSplitter(_BenignSplitter):
        def split_subtitle(self, data):
            thread.cancellation.cancel()  # 用户在断句阶段点击停止
            return data

    reached = {"optimizer": False}

    class RecordingOptimizer(_BenignOptimizer):
        def __init__(self, *args, **kwargs):
            reached["optimizer"] = True

    def fail_translate(self, data, config, gateway=None):
        raise AssertionError("translate stage must not run after cancellation")

    monkeypatch.setattr(subtitle_thread_module, "SubtitleSplitter", CancellingSplitter)
    monkeypatch.setattr(subtitle_thread_module, "SubtitleOptimizer", RecordingOptimizer)
    monkeypatch.setattr(SubtitleThread, "_run_enhanced_translation", fail_translate)

    thread, cancelled, errors = _make_thread(tmp_path, _config())

    thread.run()

    assert errors == []
    assert cancelled == [True]
    assert reached["optimizer"] is False


def test_cancel_during_optimize_skips_translate(tmp_path, monkeypatch, qapp):
    """优化阶段取消后，run() 不得进入翻译阶段。"""

    class CancellingOptimizer(_BenignOptimizer):
        def optimize_subtitle(self, data):
            thread.cancellation.cancel()  # 用户在优化阶段点击停止
            return data

    def fail_translate(self, data, config, gateway=None):
        raise AssertionError("translate stage must not run after cancellation")

    monkeypatch.setattr(subtitle_thread_module, "SubtitleSplitter", _BenignSplitter)
    monkeypatch.setattr(subtitle_thread_module, "SubtitleOptimizer", CancellingOptimizer)
    monkeypatch.setattr(SubtitleThread, "_run_enhanced_translation", fail_translate)

    config = _config(
        need_translate=True,
        translation_mode=TranslationMode.ENHANCED_LLM,
        translator_service=TranslatorServiceEnum.OPENAI,
        target_language=TargetLanguage.SIMPLIFIED_CHINESE,
        translation_execution_mode=TranslationExecutionMode.BATCH,
        main_llm_profile=_profile("main"),
        review_llm_profile=_profile("review"),
    )
    thread, cancelled, errors = _make_thread(tmp_path, config)

    thread.run()

    assert errors == []
    assert cancelled == [True]


def test_stop_reaches_running_optimizer_and_cleans_up(tmp_path, monkeypatch, qapp):
    """优化进行中点击停止：stop() 必须触达在跑的优化器，阶段收尾必须清理。"""

    stopped = []

    class StoppableOptimizer:
        failed_batches = 0
        maxed_batches = 0

        def __init__(self, *args, **kwargs):
            self.stopped = 0
            stopped.append(self)

        def optimize_subtitle(self, data):
            thread.stop()  # 用户在优化进行中点击停止
            return data

        def stop(self):
            self.stopped += 1

    monkeypatch.setattr(subtitle_thread_module, "SubtitleSplitter", _BenignSplitter)
    monkeypatch.setattr(subtitle_thread_module, "SubtitleOptimizer", StoppableOptimizer)
    # QThread.wait 在直接调用 run() 的测试里没有托管线程可等，替换为立即返回。
    monkeypatch.setattr(SubtitleThread, "wait", lambda self, msecs=None: True)

    thread, cancelled, errors = _make_thread(tmp_path, _config(need_split=False))

    thread.run()

    assert errors == []
    assert cancelled == [True]
    assert stopped[0].stopped >= 1
