import json
from pathlib import Path

import pytest

import videocaptioner.core.translate.enhanced.runner as runner_module
from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.translate.enhanced.glossary import subtitle_fingerprint
from videocaptioner.core.translate.enhanced.models import (
    AuthoritativeGlossary,
    EnhancedTranslationError,
)


def _written_checkpoint(tmp_path: Path) -> Path:
    matches = list((tmp_path / "videocaptioner-workspace").rglob("translation-checkpoint.json"))
    assert matches, "expected translation checkpoint in process workspace"
    return matches[0]


def test_audit_failure_preserves_completed_main_translation_checkpoint(
    tmp_path, monkeypatch
):
    class FailingAuditOrchestrator:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, cues, **kwargs):
            assert [cue.text for cue in cues] == ["Hello"]
            kwargs["on_translations"]({1: "你好"})
            raise EnhancedTranslationError(
                "audit failed",
                stage="audit",
                category="configuration",
                retryable=False,
            )

    monkeypatch.setattr(
        runner_module, "EnhancedTranslationOrchestrator", FailingAuditOrchestrator
    )
    source = ASRData([ASRDataSeg("Hello", 0, 1000)])

    with pytest.raises(EnhancedTranslationError, match="主翻译结果已保存到检查点"):
        runner_module.run_enhanced_translation(
            source,
            object(),
            output_dir=tmp_path,
            base_name="episode",
        )

    checkpoint = _written_checkpoint(tmp_path)
    document = json.loads(checkpoint.read_text("utf-8"))
    assert document["1"]["original_subtitle"] == "Hello"
    assert document["1"]["translated_subtitle"] == "你好"


def _two_cues() -> ASRData:
    return ASRData(
        [
            ASRDataSeg("Hello", 0, 1000),
            ASRDataSeg("World", 1000, 2000),
        ]
    )


def test_failed_later_translation_batch_keeps_only_completed_checkpoint(
    tmp_path, monkeypatch
):
    class FirstBatchThenFailOrchestrator:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, cues, **kwargs):
            assert [cue.text for cue in cues] == ["Hello", "World"]
            kwargs["on_translations"]({1: "你好"})
            raise EnhancedTranslationError(
                "translation failed",
                stage="translation",
                category="transient",
                retryable=True,
            )

    monkeypatch.setattr(
        runner_module, "EnhancedTranslationOrchestrator", FirstBatchThenFailOrchestrator
    )

    with pytest.raises(EnhancedTranslationError, match="主翻译结果已保存到检查点"):
        runner_module.run_enhanced_translation(
            _two_cues(),
            object(),
            output_dir=tmp_path,
            base_name="episode",
        )

    checkpoint = _written_checkpoint(tmp_path)
    document = json.loads(checkpoint.read_text("utf-8"))
    assert document["1"]["original_subtitle"] == "Hello"
    assert document["1"]["translated_subtitle"] == "你好"
    assert document["2"]["original_subtitle"] == "World"
    assert document["2"]["translated_subtitle"] == ""


def test_cancel_after_first_translation_batch_keeps_completed_checkpoint(
    tmp_path, monkeypatch
):
    class CancelAfterFirstBatchOrchestrator:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, cues, **kwargs):
            kwargs["on_translations"]({1: "你好"})
            raise InterruptedError("enhanced translation cancelled")

    monkeypatch.setattr(
        runner_module,
        "EnhancedTranslationOrchestrator",
        CancelAfterFirstBatchOrchestrator,
    )

    with pytest.raises(InterruptedError, match="enhanced translation cancelled"):
        runner_module.run_enhanced_translation(
            _two_cues(),
            object(),
            output_dir=tmp_path,
            base_name="episode",
        )

    checkpoint = _written_checkpoint(tmp_path)
    document = json.loads(checkpoint.read_text("utf-8"))
    assert document["1"]["translated_subtitle"] == "你好"
    assert document["2"]["translated_subtitle"] == ""


def test_translation_checkpoint_merges_out_of_order_batches_by_subtitle_id(
    tmp_path, monkeypatch
):
    class OutOfOrderThenFailOrchestrator:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, cues, **kwargs):
            kwargs["on_translations"]({2: "世界"})
            kwargs["on_translations"]({1: "你好"})
            raise EnhancedTranslationError(
                "audit failed",
                stage="audit",
                category="configuration",
                retryable=False,
            )

    monkeypatch.setattr(
        runner_module, "EnhancedTranslationOrchestrator", OutOfOrderThenFailOrchestrator
    )

    with pytest.raises(EnhancedTranslationError, match="主翻译结果已保存到检查点"):
        runner_module.run_enhanced_translation(
            _two_cues(),
            object(),
            output_dir=tmp_path,
            base_name="episode",
        )

    document = json.loads(_written_checkpoint(tmp_path).read_text("utf-8"))
    assert document["1"]["translated_subtitle"] == "你好"
    assert document["2"]["translated_subtitle"] == "世界"


def test_resume_skips_completed_glossary_and_translates_only_missing_cues(
    tmp_path, monkeypatch
):
    class InterruptedRun:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, cues, **kwargs):
            glossary = AuthoritativeGlossary(
                source_language="英语",
                target_language="简体中文",
                subtitle_fingerprint=subtitle_fingerprint(cues),
            )
            kwargs["on_glossary"](glossary)
            kwargs["on_translations"]({1: "你好"})
            raise EnhancedTranslationError(
                "translation failed",
                stage="translation",
                category="transient",
                retryable=True,
            )

    monkeypatch.setattr(runner_module, "EnhancedTranslationOrchestrator", InterruptedRun)
    with pytest.raises(EnhancedTranslationError):
        runner_module.run_enhanced_translation(
            _two_cues(),
            object(),
            output_dir=tmp_path,
            base_name="episode",
        )

    captured = {}

    class ResumedRun:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, cues, **kwargs):
            captured["imported_glossary"] = kwargs["imported_glossary"]
            captured["resume_translations"] = kwargs["resume_translations"]
            kwargs["on_translations"]({2: "世界"})
            return _enhanced_result(cues, {1: "你好", 2: "世界"}, kwargs["imported_glossary"])

    monkeypatch.setattr(runner_module, "EnhancedTranslationOrchestrator", ResumedRun)
    run = runner_module.run_enhanced_translation(
        _two_cues(),
        object(),
        output_dir=tmp_path,
        base_name="episode",
    )

    assert captured["imported_glossary"] is not None
    assert captured["resume_translations"] == {1: "你好"}
    assert [segment.translated_text for segment in run.subtitle_data.segments] == ["你好", "世界"]
    assert run.result.audit_report.warnings == ()
    assert run.recovery_summary is not None
    assert run.recovery_summary.completed == {"glossary": 1, "translation_segments": 1}
    assert not list((tmp_path / "videocaptioner-workspace").rglob("recovery-manifest.json"))


def test_start_fresh_resets_manifest_and_does_not_reuse_checkpoint(tmp_path, monkeypatch):
    class InterruptedRun:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, cues, **kwargs):
            kwargs["on_glossary"](
                AuthoritativeGlossary(
                    source_language="英语",
                    target_language="简体中文",
                    subtitle_fingerprint=subtitle_fingerprint(cues),
                )
            )
            kwargs["on_translations"]({1: "旧译文"})
            raise EnhancedTranslationError(
                "failed", stage="translation", category="transient", retryable=True
            )

    monkeypatch.setattr(runner_module, "EnhancedTranslationOrchestrator", InterruptedRun)
    with pytest.raises(EnhancedTranslationError):
        runner_module.run_enhanced_translation(
            _two_cues(), object(), output_dir=tmp_path, base_name="episode"
        )

    captured = {}

    class FreshRun:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, cues, **kwargs):
            captured["glossary"] = kwargs["imported_glossary"]
            captured["translations"] = kwargs["resume_translations"]
            glossary = AuthoritativeGlossary(
                source_language="英语",
                target_language="简体中文",
                subtitle_fingerprint=subtitle_fingerprint(cues),
            )
            kwargs["on_glossary"](glossary)
            kwargs["on_translations"]({1: "你好", 2: "世界"})
            return _enhanced_result(cues, {1: "你好", 2: "世界"}, glossary)

    monkeypatch.setattr(runner_module, "EnhancedTranslationOrchestrator", FreshRun)
    runner_module.run_enhanced_translation(
        _two_cues(),
        object(),
        output_dir=tmp_path,
        base_name="episode",
        recovery_decision=lambda _summary: "start_fresh",
    )

    assert captured == {"glossary": None, "translations": {}}


def test_different_source_text_does_not_offer_recovery(tmp_path, monkeypatch):
    class InterruptedRun:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, cues, **kwargs):
            kwargs["on_translations"]({1: "你好"})
            raise EnhancedTranslationError(
                "failed", stage="translation", category="transient", retryable=True
            )

    monkeypatch.setattr(runner_module, "EnhancedTranslationOrchestrator", InterruptedRun)
    with pytest.raises(EnhancedTranslationError):
        runner_module.run_enhanced_translation(
            _two_cues(), object(), output_dir=tmp_path, base_name="episode"
        )

    class FreshRun:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, cues, **kwargs):
            assert kwargs["resume_translations"] == {}
            glossary = AuthoritativeGlossary(
                source_language="英语",
                target_language="简体中文",
                subtitle_fingerprint=subtitle_fingerprint(cues),
            )
            kwargs["on_glossary"](glossary)
            kwargs["on_translations"]({1: "早上好", 2: "世界"})
            return _enhanced_result(cues, {1: "早上好", 2: "世界"}, glossary)

    called = False

    def decision(_summary):
        nonlocal called
        called = True
        return "continue"

    monkeypatch.setattr(runner_module, "EnhancedTranslationOrchestrator", FreshRun)
    runner_module.run_enhanced_translation(
        ASRData([ASRDataSeg("Good morning", 0, 1000), ASRDataSeg("World", 1000, 2000)]),
        object(),
        output_dir=tmp_path,
        base_name="episode",
        recovery_decision=decision,
    )

    assert called is False


def test_corrupted_checkpoint_warns_without_offering_empty_recovery(tmp_path, monkeypatch):
    class InterruptedRun:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, cues, **kwargs):
            kwargs["on_translations"]({1: "你好"})
            raise EnhancedTranslationError(
                "failed", stage="translation", category="transient", retryable=True
            )

    monkeypatch.setattr(runner_module, "EnhancedTranslationOrchestrator", InterruptedRun)
    with pytest.raises(EnhancedTranslationError):
        runner_module.run_enhanced_translation(
            _two_cues(), object(), output_dir=tmp_path, base_name="episode"
        )

    _written_checkpoint(tmp_path).write_text("not json", encoding="utf-8")
    captured = {}

    class FreshRun:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, cues, **kwargs):
            captured["translations"] = kwargs["resume_translations"]
            glossary = AuthoritativeGlossary(
                source_language="英语",
                target_language="简体中文",
                subtitle_fingerprint=subtitle_fingerprint(cues),
            )
            kwargs["on_glossary"](glossary)
            kwargs["on_translations"]({1: "你好", 2: "世界"})
            return _enhanced_result(cues, {1: "你好", 2: "世界"}, glossary)

    called = False

    def decision(_summary):
        nonlocal called
        called = True
        return "continue"

    monkeypatch.setattr(runner_module, "EnhancedTranslationOrchestrator", FreshRun)
    result = runner_module.run_enhanced_translation(
        _two_cues(),
        object(),
        output_dir=tmp_path,
        base_name="episode",
        recovery_decision=decision,
    )

    assert captured["translations"] == {}
    assert called is False
    assert result.result.audit_report.warnings == ()


def _enhanced_result(cues, translations, glossary):
    from videocaptioner.core.translate.enhanced.models import (
        EnhancedTranslationResult,
        TranslationAuditReport,
        TranslationContextBrief,
    )

    return EnhancedTranslationResult(
        translations=translations,
        brief=TranslationContextBrief(),
        glossary=glossary,
        audit_report=TranslationAuditReport(),
    )
