"""任务级单一 LLM 网关：流水线线程把同一实例传到全部消费点。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.entities import (
    FullProcessTask,
    SubtitleConfig,
    SubtitleLayoutEnum,
    SubtitleTask,
    SynthesisTask,
    TranscribeConfig,
    TranscribeTask,
    TranslatorServiceEnum,
)
from videocaptioner.core.llm.models import (
    LLMModelProfile,
    LLMResult,
    LLMTransport,
    ProviderDialect,
)
from videocaptioner.core.postprocess.config import PostprocessConfig
from videocaptioner.core.postprocess.models import PostprocessResult, PostprocessTask
from videocaptioner.core.postprocess.report import QualityReport
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
from videocaptioner.ui.thread import subtitle_pipeline_thread as pipeline_module
from videocaptioner.ui.thread import subtitle_thread as subtitle_thread_module
from videocaptioner.ui.thread.postprocess_thread import PostprocessThread
from videocaptioner.ui.thread.subtitle_pipeline_thread import SubtitlePipelineThread
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


def _fast_cjk_source() -> ASRData:
    return ASRData(
        [ASRDataSeg("这是一句非常长的中文字幕内容需要很快阅读完毕", 0, 800)]
    )


class _Signal:
    def connect(self, slot):
        self._slot = slot


class TrackingGateway:
    def __init__(self, *args, **kwargs) -> None:
        self.max_concurrency = kwargs.get("max_concurrency")
        self.closed = 0
        self.kwargs = kwargs

    def close(self) -> None:
        self.closed += 1

    def complete(self, profile, request, **kwargs):
        raise AssertionError("pipeline tests must not send real LLM requests")


def _enhanced_run(tmp_path, source: ASRData, translated_text: str) -> EnhancedTranslationRun:
    translated = ASRData.from_json(source.to_json())
    if translated.segments:
        translated.segments[0].translated_text = translated_text
    glossary = AuthoritativeGlossary(
        source_language="auto",
        target_language=TargetLanguage.SIMPLIFIED_CHINESE.value,
        subtitle_fingerprint="sha256:test",
    )
    result = EnhancedTranslationResult(
        translations={index: translated_text for index in range(1, len(translated.segments) + 1)}
        or {1: translated_text},
        brief=TranslationContextBrief(outline="A test"),
        glossary=glossary,
        audit_report=TranslationAuditReport(),
    )
    return EnhancedTranslationRun(
        subtitle_data=translated,
        result=result,
        artifacts=EnhancedTranslationArtifacts(
            glossary_path=tmp_path / "【项目术语表】video.vcglossary.json",
            audit_report_path=tmp_path / "【翻译审计】video.md",
        ),
    )


def _subtitle_config() -> SubtitleConfig:
    return SubtitleConfig(
        need_split=True,
        need_optimize=True,
        need_translate=True,
        translation_mode=TranslationMode.ENHANCED_LLM,
        translator_service=TranslatorServiceEnum.OPENAI,
        target_language=TargetLanguage.SIMPLIFIED_CHINESE,
        thread_num=7,
        batch_size=10,
        utility_llm_profile=_profile("utility"),
        main_llm_profile=_profile("main"),
        review_llm_profile=_profile("review"),
        translation_execution_mode=TranslationExecutionMode.BATCH,
    )


def _patch_transcript_and_synthesis(monkeypatch, tmp_path, source: ASRData) -> None:
    class FakeTranscriptThread:
        def __init__(self, task):
            self.task = task
            self.progress = _Signal()
            self.error = _Signal()

        def run(self):
            self.task.result_data = source
            self.task.output_path = str(tmp_path / "【转录字幕】video.srt")

    class FakeSynthesisThread:
        def __init__(self, task):
            self.task = task
            self.progress = _Signal()
            self.error = _Signal()

        def run(self):
            return None

    monkeypatch.setattr(pipeline_module, "TranscriptThread", FakeTranscriptThread)
    monkeypatch.setattr(pipeline_module, "VideoSynthesisThread", FakeSynthesisThread)
    monkeypatch.setattr(
        pipeline_module.TaskFactory,
        "create_transcribe_task",
        lambda *args, **kwargs: TranscribeTask(
            file_path=str(tmp_path / "video.mp4"),
            output_path=str(tmp_path / "【转录字幕】video.srt"),
            transcribe_config=TranscribeConfig(),
            workflow_base_name="video",
        ),
    )
    monkeypatch.setattr(
        pipeline_module.TaskFactory,
        "create_synthesis_task",
        lambda *args, **kwargs: SynthesisTask(video_path="", subtitle_path=""),
    )


def _patch_subtitle_consumers(monkeypatch, tmp_path, captured: dict) -> None:
    class FakeSplitter:
        def __init__(self, *args, **kwargs):
            captured["splitter"] = kwargs.get("gateway")
            self.rule_fallback_segments = 0

        def split_subtitle(self, data):
            return data

        def stop(self):
            return None

    class FakeOptimizer:
        def __init__(self, *args, **kwargs):
            captured["optimizer"] = kwargs.get("gateway")
            self.failed_batches = 0
            self.maxed_batches = 0

        def optimize_subtitle(self, data):
            return data

        def stop(self):
            return None

    def fake_enhanced(source, config, **kwargs):
        captured["enhanced"] = kwargs.get("gateway")
        return _enhanced_run(tmp_path, source, "增强译文")

    monkeypatch.setattr(subtitle_thread_module, "SubtitleSplitter", FakeSplitter)
    monkeypatch.setattr(subtitle_thread_module, "SubtitleOptimizer", FakeOptimizer)
    monkeypatch.setattr(subtitle_thread_module, "run_enhanced_translation", fake_enhanced)


def test_pipeline_shares_one_gateway_across_split_optimize_translate_and_postprocess(
    tmp_path, monkeypatch, qapp
):
    constructed = []
    captured = {}

    class PipelineGateway(TrackingGateway):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            constructed.append(self)

    def fake_compress(asr_data, cfg, report, gateway=None):
        captured["compress"] = gateway
        return asr_data, report

    def fake_optimize(data, **kwargs):
        captured["speed"] = kwargs.get("semantic_gateway")

        class Result:
            pass

        return data, Result()

    monkeypatch.setattr(pipeline_module, "LLMGateway", PipelineGateway)
    monkeypatch.setattr(subtitle_thread_module, "LLMGateway", PipelineGateway)
    monkeypatch.setattr(
        "videocaptioner.core.postprocess.compress.compress_fast_subtitles",
        fake_compress,
    )
    monkeypatch.setattr(
        "videocaptioner.core.speed.pipeline.optimize_speed",
        fake_optimize,
    )
    _patch_transcript_and_synthesis(monkeypatch, tmp_path, _word_source())
    _patch_subtitle_consumers(monkeypatch, tmp_path, captured)

    video = tmp_path / "video.mp4"
    video.write_bytes(b"")
    postprocess_task = PostprocessTask(
        source_subtitle_path=str(tmp_path / "placeholder.srt"),
        config_snapshot=PostprocessConfig(
            compress_fast_subtitles=True,
            speed_optimize=True,
            trim_trailing_punct=False,
            utility_llm_profile=_profile("utility"),
        ),
    )
    task = FullProcessTask(
        file_path=str(video),
        workflow_base_name="video",
        transcribe_config=TranscribeConfig(),
        subtitle_config=_subtitle_config(),
        postprocess_enabled=True,
        postprocess_task=postprocess_task,
    )
    errors = []
    thread = SubtitlePipelineThread(task)
    thread.error.connect(errors.append)

    thread.run()

    assert errors == []
    assert len(constructed) == 1
    gateway = constructed[0]
    assert gateway.max_concurrency == 7
    assert captured["splitter"] is gateway
    assert captured["optimizer"] is gateway
    assert captured["enhanced"] is gateway
    assert captured["compress"] is gateway
    assert captured["speed"] is gateway
    assert gateway.closed == 1


def test_standalone_subtitle_thread_closes_owned_gateway(tmp_path, monkeypatch, qapp):
    constructed = []
    captured = {}

    class OwnedGateway(TrackingGateway):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            constructed.append(self)

    monkeypatch.setattr(subtitle_thread_module, "LLMGateway", OwnedGateway)
    _patch_subtitle_consumers(monkeypatch, tmp_path, captured)
    task = SubtitleTask(
        subtitle_path=str(tmp_path / "source.srt"),
        video_path="",
        input_data=_word_source(),
        output_path=str(tmp_path / "【初版字幕】episode.srt"),
        workflow_base_name="episode",
        need_next_task=False,
        subtitle_config=_subtitle_config(),
    )
    errors = []
    thread = SubtitleThread(task)
    thread.error.connect(errors.append)

    thread.run()

    assert errors == []
    assert len(constructed) == 1
    gateway = constructed[0]
    assert gateway.max_concurrency == 7
    assert captured["splitter"] is gateway
    assert captured["optimizer"] is gateway
    assert captured["enhanced"] is gateway
    assert gateway.closed == 1


def test_standalone_subtitle_thread_does_not_close_injected_gateway(
    tmp_path, monkeypatch, qapp
):
    captured = {}
    injected = TrackingGateway(max_concurrency=4)
    monkeypatch.setattr(
        subtitle_thread_module,
        "LLMGateway",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("injected subtitle thread must not construct a gateway")
        ),
    )
    _patch_subtitle_consumers(monkeypatch, tmp_path, captured)
    task = SubtitleTask(
        subtitle_path=str(tmp_path / "source.srt"),
        video_path="",
        input_data=_word_source(),
        output_path=str(tmp_path / "【初版字幕】episode.srt"),
        workflow_base_name="episode",
        need_next_task=False,
        subtitle_config=_subtitle_config(),
    )
    thread = SubtitleThread(task, gateway=injected)

    thread.run()

    assert captured["splitter"] is injected
    assert captured["optimizer"] is injected
    assert captured["enhanced"] is injected
    assert injected.closed == 0


def test_standalone_postprocess_self_builds_and_closes_gateway(tmp_path, monkeypatch, qapp):
    owned = []

    class OwnedGateway:
        def __init__(self, *args, **kwargs) -> None:
            owned.append(self)
            self.closed = False

        def complete(self, profile, request, **kwargs):
            return LLMResult(text=json.dumps({"1": "这是一句非常长的"}, ensure_ascii=False))

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("videocaptioner.core.llm.utility.LLMGateway", OwnedGateway)
    source = tmp_path / "【初版字幕】sample.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:00,800\n这是一句非常长的中文字幕内容需要很快阅读完毕\n",
        encoding="utf-8",
    )
    task = PostprocessTask(
        str(source),
        input_data=_fast_cjk_source(),
        config_snapshot=PostprocessConfig(
            compress_fast_subtitles=True,
            trim_trailing_punct=False,
            utility_llm_profile=_profile("utility"),
        ),
    )
    errors = []
    thread = PostprocessThread(task)
    thread.error.connect(errors.append)

    thread.run()

    assert errors == []
    assert len(owned) == 1
    assert owned[0].closed is True


def test_injected_postprocess_gateway_is_never_closed(tmp_path, monkeypatch, qapp):
    class InjectedGateway:
        def __init__(self) -> None:
            self.closed = False
            self.requests = []

        def complete(self, profile, request, **kwargs):
            self.requests.append((profile, request))
            return LLMResult(text=json.dumps({"1": "这是一句非常长的"}, ensure_ascii=False))

        def close(self) -> None:
            self.closed = True

    gateway = InjectedGateway()
    source = tmp_path / "【初版字幕】sample.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:00,800\n这是一句非常长的中文字幕内容需要很快阅读完毕\n",
        encoding="utf-8",
    )
    task = PostprocessTask(
        str(source),
        input_data=_fast_cjk_source(),
        config_snapshot=PostprocessConfig(
            compress_fast_subtitles=True,
            trim_trailing_punct=False,
            utility_llm_profile=_profile("utility"),
        ),
    )
    thread = PostprocessThread(task, gateway=gateway)

    thread.run()

    assert gateway.requests
    assert gateway.closed is False


def _canned_postprocess_result(task: PostprocessTask, *, continue_downstream: bool, status: str):
    data = ASRData([ASRDataSeg("Hello", 0, 1000, "你好")])
    task.status = status
    task.result_data = data
    task.active_subtitle_path = str(
        Path(task.source_subtitle_path).with_name("【后处理字幕】video.srt")
    )
    return PostprocessResult(
        task,
        data,
        data,
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        1.0,
        succeeded=continue_downstream,
        used_fallback=status == "fallback",
        continue_downstream=continue_downstream,
    )


def _run_pipeline_with_canned_postprocess(
    tmp_path, monkeypatch, qapp, *, continue_downstream: bool, status: str
):
    synthesis_calls = []

    class FakePostprocessThread:
        def __init__(self, task, gateway=None):
            self.task = task
            self.result = None
            self.progress = _Signal()
            self.error = _Signal()

        def run(self):
            self.result = _canned_postprocess_result(
                self.task, continue_downstream=continue_downstream, status=status
            )

    class RecordingSynthesisThread:
        def __init__(self, task):
            self.task = task
            self.progress = _Signal()
            self.error = _Signal()

        def run(self):
            synthesis_calls.append(self.task.subtitle_path)

    monkeypatch.setattr(pipeline_module, "PostprocessThread", FakePostprocessThread)
    monkeypatch.setattr(pipeline_module, "LLMGateway", TrackingGateway)
    _patch_transcript_and_synthesis(monkeypatch, tmp_path, _word_source())
    monkeypatch.setattr(pipeline_module, "VideoSynthesisThread", RecordingSynthesisThread)
    _patch_subtitle_consumers(monkeypatch, tmp_path, {})

    video = tmp_path / "video.mp4"
    video.write_bytes(b"")
    postprocess_task = PostprocessTask(
        source_subtitle_path=str(tmp_path / "placeholder.srt"),
        config_snapshot=PostprocessConfig(trim_trailing_punct=False),
    )
    task = FullProcessTask(
        file_path=str(video),
        workflow_base_name="video",
        transcribe_config=TranscribeConfig(),
        subtitle_config=_subtitle_config(),
        postprocess_enabled=True,
        postprocess_task=postprocess_task,
    )
    errors = []
    thread = SubtitlePipelineThread(task)
    thread.error.connect(errors.append)
    thread.run()
    return errors, synthesis_calls


@pytest.mark.parametrize(
    ("continue_downstream", "status", "expect_synthesis"),
    [
        (True, "completed", True),
        (False, "fallback", False),
        (False, "cancelled", False),
        (True, "cancelled", False),
    ],
)
def test_pipeline_gates_synthesis_on_postprocess_continue_flag(
    tmp_path, monkeypatch, qapp, continue_downstream, status, expect_synthesis
):
    errors, synthesis_calls = _run_pipeline_with_canned_postprocess(
        tmp_path,
        monkeypatch,
        qapp,
        continue_downstream=continue_downstream,
        status=status,
    )
    if expect_synthesis:
        assert errors == []
        assert synthesis_calls
    else:
        assert errors
        assert synthesis_calls == []


def test_postprocess_thread_forwards_cancellation_into_task_seam(tmp_path, monkeypatch, qapp):
    from threading import Event

    entered = Event()
    release = Event()
    captured = {}

    def fake_run(task, **kwargs):
        callback = kwargs.get("cancelled")
        captured["callable"] = callable(callback)
        captured["before"] = callback() if callback else None
        entered.set()
        assert release.wait(5)
        captured["after"] = callback() if callback else None
        raise RuntimeError("capture only")

    monkeypatch.setattr(
        "videocaptioner.ui.thread.postprocess_thread.run_postprocess_task",
        fake_run,
    )
    source = tmp_path / "【初版字幕】sample.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n译文。\n", encoding="utf-8"
    )
    thread = PostprocessThread(
        PostprocessTask(str(source), config_snapshot=PostprocessConfig())
    )
    thread.start()
    assert entered.wait(5)
    thread.stop()
    release.set()
    assert thread.wait(5000)
    assert captured["callable"] is True
    assert captured["before"] is False
    assert captured["after"] is True
