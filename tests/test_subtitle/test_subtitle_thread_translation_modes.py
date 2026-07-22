import json
from pathlib import Path

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.entities import (
    SubtitleConfig,
    SubtitleTask,
    TranslatorServiceEnum,
)
from videocaptioner.core.llm.models import (
    LLMModelProfile,
    LLMResult,
    LLMTransport,
    ProviderDialect,
)
from videocaptioner.core.translate.enhanced.models import (
    AuthoritativeGlossary,
    EnhancedTranslationError,
    EnhancedTranslationResult,
    TranslationAuditMode,
    TranslationAuditReport,
    TranslationContextBrief,
    TranslationExecutionMode,
)
from videocaptioner.core.translate.enhanced.runner import (
    EnhancedTranslationArtifacts,
    EnhancedTranslationRun,
)
from videocaptioner.core.translate.types import (
    TargetLanguage,
    TranslationMode,
)
from videocaptioner.ui.thread import subtitle_thread as thread_module
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


def _source(text: str = "A uniquely identifiable source line.") -> ASRData:
    return ASRData([ASRDataSeg(text, 0, 1000)])


def _task(tmp_path, config: SubtitleConfig, *, text: str = "Source line") -> SubtitleTask:
    return SubtitleTask(
        subtitle_path=str(tmp_path / "source.srt"),
        video_path="",
        input_data=_source(text),
        output_path=str(tmp_path / "【初版字幕】episode.srt"),
        workflow_base_name="episode",
        need_next_task=False,
        subtitle_config=config,
    )


def _config(mode: TranslationMode) -> SubtitleConfig:
    return SubtitleConfig(
        need_split=False,
        need_optimize=False,
        need_translate=True,
        translation_mode=mode,
        translator_service=(
            TranslatorServiceEnum.BING
            if mode is TranslationMode.NON_LLM
            else TranslatorServiceEnum.OPENAI
        ),
        target_language=TargetLanguage.SIMPLIFIED_CHINESE,
        batch_size=10,
        thread_num=1,
    )


def _signals(thread: SubtitleThread):
    values = {"finished": [], "error": [], "cancelled": []}
    thread.finished.connect(lambda *args: values["finished"].append(args))
    thread.error.connect(values["error"].append)
    thread.cancelled.connect(lambda: values["cancelled"].append(True))
    return values


def _enhanced_run(tmp_path, source: ASRData, translated_text: str):
    translated = ASRData.from_json(source.to_json())
    translated.segments[0].translated_text = translated_text
    glossary = AuthoritativeGlossary(
        source_language="auto",
        target_language=TargetLanguage.SIMPLIFIED_CHINESE.value,
        subtitle_fingerprint="sha256:test",
    )
    report = TranslationAuditReport()
    result = EnhancedTranslationResult(
        translations={1: translated_text},
        brief=TranslationContextBrief(outline="A test"),
        glossary=glossary,
        audit_report=report,
    )
    return EnhancedTranslationRun(
        subtitle_data=translated,
        result=result,
        artifacts=EnhancedTranslationArtifacts(
            glossary_path=tmp_path / "【项目术语表】episode.vcglossary.json",
            audit_report_path=tmp_path / "【翻译审计】episode.md",
        ),
    )


def test_enhanced_mode_uses_runner_sets_artifacts_and_saves_initial_subtitle(
    tmp_path, monkeypatch, qapp
):
    config = _config(TranslationMode.ENHANCED_LLM)
    config.main_llm_profile = _profile("main")
    config.review_llm_profile = _profile("review")
    config.main_translation_prompt = "main prompt"
    config.review_translation_prompt = "review prompt"
    task = _task(tmp_path, config, text="Enhanced source line")
    captured = {}

    def fake_run(source, enhanced_config, **kwargs):
        captured["source"] = source
        captured["config"] = enhanced_config
        captured["kwargs"] = kwargs
        return _enhanced_run(tmp_path, source, "增强译文")

    monkeypatch.setattr(thread_module, "run_enhanced_translation", fake_run)
    monkeypatch.setattr(
        thread_module,
        "create_translator_from_config",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("enhanced mode must not create the legacy translator")
        ),
    )
    thread = SubtitleThread(task)
    signals = _signals(thread)

    thread.run()

    assert not signals["error"]
    assert len(signals["finished"]) == 1
    assert Path(task.output_path or "").is_file()
    assert task.result_data is not None
    assert task.result_data.segments[0].translated_text == "增强译文"
    assert task.glossary_path == str(tmp_path / "【项目术语表】episode.vcglossary.json")
    assert task.translation_audit_report_path == str(tmp_path / "【翻译审计】episode.md")
    assert task.translation_audit_report is not None
    assert captured["config"].main_role.profile == config.main_llm_profile
    assert captured["config"].review_role.profile == config.review_llm_profile
    assert captured["config"].main_role.user_prompt == "main prompt"
    assert captured["config"].review_role.user_prompt == "review prompt"
    assert captured["kwargs"]["cancellation"] is thread.cancellation
    assert captured["kwargs"]["confirm_audit"] is None


def test_standalone_manual_audit_passes_blocking_confirmation_callback(
    tmp_path, monkeypatch, qapp
):
    config = _config(TranslationMode.ENHANCED_LLM)
    config.main_llm_profile = _profile("main")
    config.review_llm_profile = _profile("review")
    config.translation_audit_mode = TranslationAuditMode.REVIEW_AND_CONFIRM
    config.translation_execution_mode = TranslationExecutionMode.GUI_STANDALONE
    task = _task(tmp_path, config)
    captured = {}

    def fake_run(source, enhanced_config, **kwargs):
        captured.update(kwargs)
        return _enhanced_run(tmp_path, source, "人工确认后的译文")

    monkeypatch.setattr(thread_module, "run_enhanced_translation", fake_run)
    thread = SubtitleThread(task)

    result = thread._run_enhanced_translation(task.input_data, config)

    assert result.segments[0].translated_text == "人工确认后的译文"
    assert callable(captured["confirm_audit"])


class _NullCache:
    def get(self, key, default=None):
        return default

    def set(self, key, value, **kwargs):
        return None

    def delete(self, key):
        return None


class _SingleLLMGateway:
    def __init__(self):
        self.calls = []

    def complete(self, profile, request, **kwargs):
        self.calls.append((profile, request))
        return LLMResult(
            text=json.dumps(
                {"1": {"native_translation": "单模型反思译文"}},
                ensure_ascii=False,
            )
        )


def test_single_llm_profile_uses_gateway_and_keeps_reflect_mode(
    tmp_path, monkeypatch, qapp
):
    config = _config(TranslationMode.SINGLE_LLM)
    config.main_llm_profile = _profile("main")
    config.main_translation_prompt = "single role prompt"
    config.need_reflect = True
    task = _task(tmp_path, config, text="Single LLM unique source")
    gateway = _SingleLLMGateway()

    monkeypatch.setattr(
        "videocaptioner.core.translate.base.get_translate_cache", lambda: _NullCache()
    )
    monkeypatch.setattr(
        "videocaptioner.core.translate.llm_translator.LLMGateway", lambda: gateway
    )
    monkeypatch.setattr(
        thread_module,
        "run_enhanced_translation",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("single LLM mode must not use enhanced translation")
        ),
    )
    thread = SubtitleThread(task)
    signals = _signals(thread)

    thread.run()

    assert not signals["error"]
    assert len(signals["finished"]) == 1
    assert task.result_data is not None
    assert task.result_data.segments[0].translated_text == "单模型反思译文"
    assert len(gateway.calls) == 1
    profile, request = gateway.calls[0]
    assert profile == config.main_llm_profile
    assert request.metadata == {"stage": "single_llm_translation", "role": "main"}
    assert "single role prompt" in request.messages[0].content
    assert "native_translation" in request.messages[0].content


class _NonLLMTranslator:
    failed_count = 0

    def __init__(self):
        self.stopped = False

    def translate_subtitle(self, source):
        translated = ASRData.from_json(source.to_json())
        translated.segments[0].translated_text = "非 LLM 译文"
        return translated

    def stop(self):
        self.stopped = True


def test_non_llm_mode_never_enters_enhanced_runner(tmp_path, monkeypatch, qapp):
    config = _config(TranslationMode.NON_LLM)
    task = _task(tmp_path, config)
    translator = _NonLLMTranslator()
    monkeypatch.setattr(
        thread_module,
        "run_enhanced_translation",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("non-LLM mode must not use enhanced translation")
        ),
    )
    monkeypatch.setattr(
        thread_module, "create_translator_from_config", lambda *args, **kwargs: translator
    )
    thread = SubtitleThread(task)
    signals = _signals(thread)

    thread.run()

    assert not signals["error"]
    assert len(signals["finished"]) == 1
    assert translator.stopped is True
    assert task.result_data is not None
    assert task.result_data.segments[0].translated_text == "非 LLM 译文"


def test_enhanced_failure_does_not_save_or_emit_finished(tmp_path, monkeypatch, qapp):
    config = _config(TranslationMode.ENHANCED_LLM)
    config.main_llm_profile = _profile("main")
    config.review_llm_profile = _profile("review")
    task = _task(tmp_path, config)
    monkeypatch.setattr(
        thread_module,
        "run_enhanced_translation",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            EnhancedTranslationError(
                "audit failed",
                stage="audit",
                category="transient",
                retryable=True,
                attempts=4,
            )
        ),
    )
    monkeypatch.setattr(
        thread_module.TaskFactory,
        "save_stage_subtitle",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("failed enhanced translation must not save an initial subtitle")
        ),
    )
    thread = SubtitleThread(task)
    signals = _signals(thread)

    thread.run()

    assert signals["error"] == ["audit failed"]
    assert signals["finished"] == []
    assert task.result_data is None
    assert not Path(task.output_path or "").exists()


def test_cooperative_cancel_emits_only_cancelled(tmp_path, monkeypatch, qapp):
    config = _config(TranslationMode.ENHANCED_LLM)
    config.main_llm_profile = _profile("main")
    config.review_llm_profile = _profile("review")
    task = _task(tmp_path, config)

    def cancel_during_enhanced(*args, **kwargs):
        cancellation = kwargs["cancellation"]
        cancellation.cancel()
        raise InterruptedError("cancelled")

    monkeypatch.setattr(thread_module, "run_enhanced_translation", cancel_during_enhanced)
    monkeypatch.setattr(
        thread_module.TaskFactory,
        "save_stage_subtitle",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("cancelled translation must not save an initial subtitle")
        ),
    )
    thread = SubtitleThread(task)
    signals = _signals(thread)

    thread.run()

    assert signals == {"finished": [], "error": [], "cancelled": [True]}
    assert task.result_data is None
    assert not Path(task.output_path or "").exists()
