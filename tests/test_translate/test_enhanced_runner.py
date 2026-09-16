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
    assert run.recovery_summary.completed == {
        "analysis": 0,
        "glossary": 1,
        "translation_segments": 1,
    }
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




def _staging_dir(tmp_path: Path) -> Path:
    """暂存目录：源字幕指纹 + 语言对身份目录（含恢复 manifest 与检查点）。"""
    matches = list(
        (tmp_path / "videocaptioner-workspace").rglob("recovery-manifest.json")
    )
    assert matches, "expected recovery manifest in staging directory"
    return matches[0].parent


def _recovery_manifest(tmp_path: Path) -> Path:
    matches = list((tmp_path / "videocaptioner-workspace").rglob("recovery-manifest.json"))
    assert matches, "expected recovery manifest in staging directory"
    return matches[0]


def _brief_and_candidates():
    from videocaptioner.core.translate.enhanced.models import (
        TermCandidate,
        TranslationContextBrief,
    )

    return TranslationContextBrief(outline="Space lecture"), (
        TermCandidate(
            candidate_id="mercury-planet",
            source_term="Mercury",
            sense="the planet",
            occurrence_ids=(1,),
        ),
    )


def test_analysis_checkpoint_written_before_manifest_records_completion(tmp_path, monkeypatch):
    """级别①：先原子写翻译简报文件，manifest 才登记 analysis 完成。"""
    captured = {}

    class AnalysisRecordingRun:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, cues, **kwargs):
            captured["on_analysis"] = kwargs.get("on_analysis")
            translations = {cue.cue_id: f"译文{cue.cue_id}" for cue in cues}
            return _enhanced_result(cues, translations, None)

    monkeypatch.setattr(
        runner_module, "EnhancedTranslationOrchestrator", AnalysisRecordingRun
    )
    source = ASRData([ASRDataSeg("Hello", 0, 1000), ASRDataSeg("World", 1000, 2000)])
    runner_module.run_enhanced_translation(
        source,
        object(),
        output_dir=tmp_path,
        base_name="episode",
    )

    on_analysis = captured["on_analysis"]
    assert on_analysis is not None
    on_analysis(*_brief_and_candidates())

    manifest = json.loads(_recovery_manifest(tmp_path).read_text(encoding="utf-8"))
    assert manifest["completed"]["analysis"] is True
    document = json.loads((_staging_dir(tmp_path) / "context.json").read_text(encoding="utf-8"))
    assert document["brief"]["outline"] == "Space lecture"
    assert document["candidates"][0]["id"] == "mercury-planet"
    assert document["schema"] == "videocaptioner.translation_brief"
    assert document["version"] == 1


def test_resume_analysis_after_term_stage_interruption_skips_whole_analysis(
    tmp_path, monkeypatch
):
    """术语阶段（或更晚）中断后重跑：全文分析零请求，直接进入术语阶段。"""

    class AnalysisThenInterruptedRun:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, cues, **kwargs):
            kwargs["on_analysis"](*_brief_and_candidates())
            raise EnhancedTranslationError(
                "term stage failed",
                stage="term_proposal",
                category="transient",
                retryable=True,
            )

    monkeypatch.setattr(
        runner_module, "EnhancedTranslationOrchestrator", AnalysisThenInterruptedRun
    )
    source = ASRData([ASRDataSeg("Mercury is visible.", 0, 1000)])
    with pytest.raises(EnhancedTranslationError):
        runner_module.run_enhanced_translation(
            source,
            object(),
            output_dir=tmp_path,
            base_name="episode",
        )

    captured = {}

    class ResumedAnalysisRun:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, cues, **kwargs):
            captured["resume_analysis"] = kwargs.get("resume_analysis")
            captured["on_analysis"] = kwargs.get("on_analysis")
            translations = {cue.cue_id: f"译文{cue.cue_id}" for cue in cues}
            kwargs["on_translations"](translations)
            return _enhanced_result(cues, translations, None)

    monkeypatch.setattr(runner_module, "EnhancedTranslationOrchestrator", ResumedAnalysisRun)
    run = runner_module.run_enhanced_translation(
        source,
        object(),
        output_dir=tmp_path,
        base_name="episode",
    )

    resume_analysis = captured["resume_analysis"]
    assert resume_analysis is not None
    brief, candidates = resume_analysis
    assert brief.outline == "Space lecture"
    assert [candidate.source_term for candidate in candidates] == ["Mercury"]
    # 恢复的分析不再写检查点（文件已是最新），也不再发分析请求。
    assert captured["on_analysis"] is None
    assert run.recovery_summary is not None
    assert run.recovery_summary.completed == {
        "analysis": 1,
        "glossary": 0,
        "translation_segments": 0,
    }


def test_resume_analysis_rejects_corrupted_brief_and_reruns_analysis(
    tmp_path, monkeypatch
):
    """翻译简报文件损坏或版本不符时告警、视级别①未完成并重跑全文分析。"""

    class AnalysisThenInterruptedRun:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, cues, **kwargs):
            kwargs["on_analysis"](*_brief_and_candidates())
            raise EnhancedTranslationError(
                "term stage failed",
                stage="term_proposal",
                category="transient",
                retryable=True,
            )

    monkeypatch.setattr(
        runner_module, "EnhancedTranslationOrchestrator", AnalysisThenInterruptedRun
    )
    source = ASRData([ASRDataSeg("Mercury is visible.", 0, 1000)])
    with pytest.raises(EnhancedTranslationError):
        runner_module.run_enhanced_translation(
            source,
            object(),
            output_dir=tmp_path,
            base_name="episode",
        )

    (_staging_dir(tmp_path) / "context.json").write_text("not json", encoding="utf-8")
    captured = {}

    class RerunAnalysisRun:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, cues, **kwargs):
            captured["resume_analysis"] = kwargs.get("resume_analysis")
            captured["on_analysis"] = kwargs.get("on_analysis")
            kwargs["on_analysis"](*_brief_and_candidates())
            translations = {cue.cue_id: f"译文{cue.cue_id}" for cue in cues}
            kwargs["on_translations"](translations)
            return _enhanced_result(cues, translations, None)

    monkeypatch.setattr(runner_module, "EnhancedTranslationOrchestrator", RerunAnalysisRun)
    warnings_seen = []
    original_warning = runner_module.logger.warning

    def collect_warning(message, *args):
        warnings_seen.append(message % args if args else message)
        return original_warning(message, *args)

    monkeypatch.setattr(runner_module.logger, "warning", collect_warning)
    run = runner_module.run_enhanced_translation(
        source,
        object(),
        output_dir=tmp_path,
        base_name="episode",
    )

    assert captured["resume_analysis"] is None
    assert captured["on_analysis"] is not None
    assert run.recovery_summary is None
    assert any("翻译简报文件不可用" in message for message in warnings_seen)
    # 重跑分析后新文件原子替换损坏文件；成功发布后它进入任务过程目录。
    published = run.artifacts.context_path
    assert published is not None and published.is_file()
    document = json.loads(published.read_text(encoding="utf-8"))
    assert document["brief"]["outline"] == "Space lecture"


def test_successful_publish_registers_context_asset(tmp_path, monkeypatch):
    """成功发布后任务过程目录含 context 资产且 manifest 登记；暂存目录删除。"""

    class FullRun:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, cues, **kwargs):
            kwargs["on_analysis"](*_brief_and_candidates())
            glossary = AuthoritativeGlossary(
                source_language="英语",
                target_language="简体中文",
                subtitle_fingerprint=subtitle_fingerprint(cues),
            )
            kwargs["on_glossary"](glossary)
            translations = {cue.cue_id: f"译文{cue.cue_id}" for cue in cues}
            kwargs["on_translations"](translations)
            return _enhanced_result(cues, translations, glossary)

    monkeypatch.setattr(runner_module, "EnhancedTranslationOrchestrator", FullRun)
    source = ASRData([ASRDataSeg("Hello world.", 0, 2000)])
    run = runner_module.run_enhanced_translation(
        source,
        object(),
        output_dir=tmp_path,
        base_name="episode",
    )

    manifests = list((tmp_path / "videocaptioner-workspace").rglob("manifest.json"))
    assert manifests, "expected published task directory"
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["assets"].get("context") == "context.json"
    published = manifests[0].parent / "context.json"
    assert published.is_file()
    document = json.loads(published.read_text(encoding="utf-8"))
    assert document["brief"]["outline"] == "Space lecture"
    assert run.artifacts.context_path == published
    # 暂存目录（含恢复 manifest）随成功发布整体删除。
    assert not list((tmp_path / "videocaptioner-workspace").rglob(".in-progress"))
    assert not list((tmp_path / "videocaptioner-workspace").rglob("recovery-manifest.json"))


def test_resumed_run_publishes_context_and_reports_analysis_recovery(tmp_path, monkeypatch):
    """恢复运行：全文分析零请求；context 资产发布；摘要报告分析级别。"""

    class AnalysisThenInterruptedRun:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, cues, **kwargs):
            kwargs["on_analysis"](*_brief_and_candidates())
            raise EnhancedTranslationError(
                "term stage failed",
                stage="term_proposal",
                category="transient",
                retryable=True,
            )

    monkeypatch.setattr(
        runner_module, "EnhancedTranslationOrchestrator", AnalysisThenInterruptedRun
    )
    source = ASRData([ASRDataSeg("Mercury is visible.", 0, 1000)])
    with pytest.raises(EnhancedTranslationError):
        runner_module.run_enhanced_translation(
            source,
            object(),
            output_dir=tmp_path,
            base_name="episode",
        )

    class ResumedFullRun:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, cues, **kwargs):
            assert kwargs["resume_analysis"] is not None
            glossary = AuthoritativeGlossary(
                source_language="英语",
                target_language="简体中文",
                subtitle_fingerprint=subtitle_fingerprint(cues),
            )
            kwargs["on_glossary"](glossary)
            translations = {cue.cue_id: f"译文{cue.cue_id}" for cue in cues}
            kwargs["on_translations"](translations)
            return _enhanced_result(cues, translations, glossary)

    monkeypatch.setattr(runner_module, "EnhancedTranslationOrchestrator", ResumedFullRun)
    run = runner_module.run_enhanced_translation(
        source,
        object(),
        output_dir=tmp_path,
        base_name="episode",
    )

    assert run.recovery_summary is not None
    assert run.recovery_summary.completed["analysis"] == 1
    manifests = list((tmp_path / "videocaptioner-workspace").rglob("manifest.json"))
    published = manifests[0].parent / "context.json"
    assert published.is_file()
    assert run.artifacts.context_path == published


def _prompt_recording_gateway():
    """假网关：记录每次请求的完整消息对，按阶段脚本化响应。"""

    from videocaptioner.core.llm.models import LLMResult, LLMUsage

    class RecordingGateway:
        def __init__(self):
            self.calls = []
            self.analysis_count = 0

        def complete(self, profile, request, *, cancelled=None):
            import json as _json

            stage = request.metadata["stage"]
            self.calls.append(
                (stage, request.messages[0].content, request.messages[1].content)
            )
            if stage == "analysis_window":
                self.analysis_count += 1
                body = {
                    "brief": {
                        "outline": "Space lecture",
                        "background": "",
                        "themes": [],
                        "style_notes": [],
                        "translation_notes": [],
                    },
                    "candidates": [
                        {
                            "id": "mercury-planet",
                            "source_term": "Mercury",
                            "sense": "the planet",
                            "aliases": [],
                            "occurrence_ids": [1],
                        }
                    ],
                }
            elif stage == "term_proposal":
                body = {"translation": "水星", "reason": "ok"}
            elif stage == "term_review":
                body = {"is_term": True, "decision": "accept", "translation": "", "reason": "ok"}
            elif stage == "translation":
                body = {"translations": [{"id": 1, "text": "可以看到水星。"}]}
            elif stage == "audit":
                body = {"issues": []}
            else:
                raise AssertionError(f"unexpected stage {stage!r}")
            return LLMResult(
                text=_json.dumps(body, ensure_ascii=False),
                usage=LLMUsage(input_tokens=10, output_tokens=2),
            )

    return RecordingGateway()


def _enhanced_config_with_profiles():
    from videocaptioner.core.llm.models import (
        LLMModelProfile,
        LLMTransport,
        ProviderDialect,
    )
    from videocaptioner.core.translate.enhanced.models import (
        EnhancedTranslationConfig,
        TranslationRoleSnapshot,
    )

    def _profile(profile_id):
        return LLMModelProfile(
            profile_id=profile_id,
            name=profile_id,
            transport=LLMTransport.OPENAI_COMPATIBLE,
            dialect=ProviderDialect.GENERIC,
            base_url=f"https://{profile_id}.test/v1",
            api_key="secret",
            model=f"{profile_id}-model",
            work_context_tokens=16_384,
        )

    return EnhancedTranslationConfig(
        main_role=TranslationRoleSnapshot("main", _profile("main"), "MAIN USER PROMPT"),
        review_role=TranslationRoleSnapshot("review", _profile("review"), "REVIEW USER PROMPT"),
        source_language="English",
        target_language="简体中文",
        batch_size=10,
    )


def test_resumed_brief_feeds_identical_prompts_to_uninterrupted_run(
    tmp_path, monkeypatch
):
    """主 seam：以文件恢复的简报与候选喂出的完整翻译提示词逐字一致。"""

    from videocaptioner.core.translate.enhanced.orchestrator import (
        EnhancedTranslationOrchestrator,
    )

    class FailingTermProposalOrchestrator(EnhancedTranslationOrchestrator):
        def _call_json(self, *args, **kwargs):
            if kwargs.get("stage") == "term_proposal":
                raise EnhancedTranslationError(
                    "term proposal interrupted",
                    stage="term_proposal",
                    category="transient",
                    retryable=True,
                )
            return super()._call_json(*args, **kwargs)

    source = ASRData([ASRDataSeg("Mercury is visible.", 0, 1000)])
    config = _enhanced_config_with_profiles()

    # 不中断基线：同一输入与配置完整跑一遍。
    monkeypatch.setattr(
        runner_module, "EnhancedTranslationOrchestrator", EnhancedTranslationOrchestrator
    )
    baseline_gateway = _prompt_recording_gateway()
    runner_module.run_enhanced_translation(
        source,
        config,
        output_dir=tmp_path / "baseline",
        base_name="episode",
        gateway=baseline_gateway,
    )
    baseline_pairs = [
        (stage, prefix, suffix) for stage, prefix, suffix in baseline_gateway.calls
    ]
    assert baseline_gateway.analysis_count == 1

    # 中断：分析完成后、术语提议首个请求抛错。
    monkeypatch.setattr(
        runner_module, "EnhancedTranslationOrchestrator", FailingTermProposalOrchestrator
    )
    interrupted_gateway = _prompt_recording_gateway()
    with pytest.raises(EnhancedTranslationError):
        runner_module.run_enhanced_translation(
            source,
            config,
            output_dir=tmp_path,
            base_name="episode",
            gateway=interrupted_gateway,
        )
    assert interrupted_gateway.analysis_count == 1

    # 恢复：同一输出目录重跑，manifest 发现级别①已完成。
    monkeypatch.setattr(
        runner_module, "EnhancedTranslationOrchestrator", EnhancedTranslationOrchestrator
    )
    resumed_gateway = _prompt_recording_gateway()
    run = runner_module.run_enhanced_translation(
        source,
        config,
        output_dir=tmp_path,
        base_name="episode",
        gateway=resumed_gateway,
    )

    # 全文分析窗口与汇总零请求；术语阶段照常。
    assert resumed_gateway.analysis_count == 0
    resumed_pairs = [
        (stage, prefix, suffix) for stage, prefix, suffix in resumed_gateway.calls
    ]
    baseline_after_analysis = [
        pair for pair in baseline_pairs if pair[0] != "analysis_window"
    ]
    # 逐字一致：恢复运行的分析后请求与不中断运行完全相同。
    assert resumed_pairs == baseline_after_analysis
    assert [pair[0] for pair in resumed_pairs] == [
        "term_proposal",
        "term_review",
        "translation",
        "audit",
    ]
    # 完整翻译提示词含恢复的简报与候选（文件驱动的组装路径）。
    term_call = resumed_pairs[0]
    assert "Space lecture" in term_call[1]
    assert '"source_term":"Mercury"' in term_call[2]
    translation_call = next(pair for pair in resumed_pairs if pair[0] == "translation")
    assert "Space lecture" in translation_call[1]
    assert run.recovery_summary is not None
    assert run.recovery_summary.completed["analysis"] == 1
    # 成果与不中断运行等价。
    assert run.subtitle_data.segments[0].translated_text == "可以看到水星。"
