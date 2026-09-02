import json

import pytest

import videocaptioner.core.translate.enhanced.runner as runner_module
from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.translate.enhanced.models import EnhancedTranslationError


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

    checkpoint = tmp_path / "【增强翻译检查点】episode.json"
    assert checkpoint.is_file()
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

    checkpoint = tmp_path / "【增强翻译检查点】episode.json"
    assert checkpoint.is_file()
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

    checkpoint = tmp_path / "【增强翻译检查点】episode.json"
    assert checkpoint.is_file()
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

    document = json.loads(
        (tmp_path / "【增强翻译检查点】episode.json").read_text("utf-8")
    )
    assert document["1"]["translated_subtitle"] == "你好"
    assert document["2"]["translated_subtitle"] == "世界"
