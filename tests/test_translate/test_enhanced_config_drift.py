"""配置漂移与恢复来源（票 05，ADR-0022）的验收测试。

漂移只作记录、不作废检查点：恢复摘要逐项比对冻结配置指纹，成果
（审计报告、执行快照、workspace manifest）带上恢复来源与受影响标注。
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

import videocaptioner.core.translate.enhanced.runner as runner_module
from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.recovery import UNRECORDED_CONFIG_DIGEST_DRIFT
from videocaptioner.core.translate.enhanced.glossary import subtitle_fingerprint
from videocaptioner.core.translate.enhanced.models import (
    AuthoritativeGlossary,
    EnhancedTranslationError,
)


def _two_cues() -> ASRData:
    return ASRData(
        [
            ASRDataSeg("Hello", 0, 1000),
            ASRDataSeg("World", 1000, 2000),
        ]
    )


def _staging_dir(tmp_path: Path) -> Path:
    """暂存目录：源字幕指纹 + 语言对身份目录（含恢复 manifest 与检查点）。"""
    matches = list(
        (tmp_path / "videocaptioner-workspace").rglob("recovery-manifest.json")
    )
    assert matches, "expected recovery manifest in staging directory"
    return matches[0].parent


def _recovery_manifest(tmp_path: Path) -> Path:
    matches = list(
        (tmp_path / "videocaptioner-workspace").rglob("recovery-manifest.json")
    )
    assert matches, "expected recovery manifest in staging directory"
    return matches[0]


def _published_dir(tmp_path: Path) -> Path:
    matches = list((tmp_path / "videocaptioner-workspace").rglob("manifest.json"))
    assert matches, "expected published task directory"
    return matches[0].parent


def _published_manifest(tmp_path: Path) -> dict:
    matches = list((tmp_path / "videocaptioner-workspace").rglob("manifest.json"))
    assert matches, "expected published task directory"
    return json.loads(matches[0].read_text(encoding="utf-8"))


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


def _glossary_for(cues) -> AuthoritativeGlossary:
    return AuthoritativeGlossary(
        source_language="英语",
        target_language="简体中文",
        subtitle_fingerprint=subtitle_fingerprint(cues),
    )


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


def _interrupt_with_checkpoint(tmp_path, monkeypatch, config) -> None:
    """制造一次翻译级中断：on_translations 已落检查点后抛可重试错误。"""

    class InterruptedRun:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, cues, **kwargs):
            kwargs["on_translations"](
                {cue.cue_id: f"译文{cue.cue_id}" for cue in cues}
            )
            raise EnhancedTranslationError(
                "translation failed",
                stage="translation",
                category="transient",
                retryable=True,
            )

    monkeypatch.setattr(runner_module, "EnhancedTranslationOrchestrator", InterruptedRun)
    with pytest.raises(EnhancedTranslationError, match="已保存到检查点"):
        runner_module.run_enhanced_translation(
            _two_cues(),
            config,
            output_dir=tmp_path,
            base_name="episode",
        )


def _install_completing_orchestrator(monkeypatch, captured) -> None:
    """假编排器：记录恢复进度，登记术语与译文后返回完整成果。"""

    class CompletingRun:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, cues, **kwargs):
            captured["resume_translations"] = dict(kwargs["resume_translations"])
            glossary = _glossary_for(cues)
            kwargs["on_glossary"](glossary)
            translations = {cue.cue_id: f"译文{cue.cue_id}" for cue in cues}
            kwargs["on_translations"](translations)
            return _enhanced_result(cues, translations, glossary)

    monkeypatch.setattr(runner_module, "EnhancedTranslationOrchestrator", CompletingRun)


def test_changed_prompt_rerun_still_reuses_checkpoint_and_lists_drift(
    tmp_path, monkeypatch
):
    """改提示词重跑：检查点照旧复用，摘要与决策回调各列一条主翻译提示词漂移。"""
    config = _enhanced_config_with_profiles()
    _interrupt_with_checkpoint(tmp_path, monkeypatch, config)

    changed = replace(
        config,
        main_role=replace(config.main_role, user_prompt="NEW PROMPT"),
    )
    captured = {}
    decisions = []

    def decision(summary):
        decisions.append(summary)
        return "continue"

    _install_completing_orchestrator(monkeypatch, captured)
    run = runner_module.run_enhanced_translation(
        _two_cues(),
        changed,
        output_dir=tmp_path,
        base_name="episode",
        recovery_decision=decision,
    )

    assert captured["resume_translations"] != {}
    assert run.recovery_summary is not None
    drift = run.recovery_summary.configuration_drift
    assert len(drift) == 1
    assert "主翻译提示词" in drift[0]
    assert "sha256:" in drift[0]
    assert len(decisions) == 1
    assert decisions[0].configuration_drift == drift


def test_no_drift_when_config_unchanged(tmp_path, monkeypatch):
    """配置未变重跑：恢复摘要无漂移项，恢复来源的 drifted_keys 为空。"""
    config = _enhanced_config_with_profiles()
    _interrupt_with_checkpoint(tmp_path, monkeypatch, config)

    captured = {}
    _install_completing_orchestrator(monkeypatch, captured)
    run = runner_module.run_enhanced_translation(
        _two_cues(),
        config,
        output_dir=tmp_path,
        base_name="episode",
    )

    assert run.recovery_summary is not None
    assert run.recovery_summary.configuration_drift == ()
    assert run.recovery_provenance is not None
    assert run.recovery_provenance.drifted_keys == ()


def test_changed_batch_size_and_radius_list_drift_items(tmp_path, monkeypatch):
    """改批处理上限与边界语境段范围重跑：两条漂移项，drifted_keys 按序列出。"""
    config = _enhanced_config_with_profiles()
    _interrupt_with_checkpoint(tmp_path, monkeypatch, config)

    changed = replace(config, batch_size=5, boundary_context_radius=1)
    captured = {}
    _install_completing_orchestrator(monkeypatch, captured)
    run = runner_module.run_enhanced_translation(
        _two_cues(),
        changed,
        output_dir=tmp_path,
        base_name="episode",
    )

    assert run.recovery_summary is not None
    drift = run.recovery_summary.configuration_drift
    assert len(drift) == 2
    assert any("翻译批处理上限" in item for item in drift)
    assert any("边界语境段范围" in item for item in drift)
    assert run.recovery_provenance is not None
    assert run.recovery_provenance.drifted_keys == (
        "batch_size",
        "boundary_context_radius",
    )


def test_changed_profile_lists_profile_drift(tmp_path, monkeypatch):
    """换主翻译模型配置方案重跑：一条主翻译模型配置方案漂移项。"""
    from videocaptioner.core.llm.models import (
        LLMModelProfile,
        LLMTransport,
        ProviderDialect,
    )
    from videocaptioner.core.translate.enhanced.models import TranslationRoleSnapshot

    config = _enhanced_config_with_profiles()
    _interrupt_with_checkpoint(tmp_path, monkeypatch, config)

    changed_profile = LLMModelProfile(
        profile_id="main",
        name="main",
        transport=LLMTransport.OPENAI_COMPATIBLE,
        dialect=ProviderDialect.GENERIC,
        base_url="https://main.test/v1",
        api_key="secret",
        model="main-model-v2",
        work_context_tokens=16_384,
    )
    changed = replace(
        config,
        main_role=TranslationRoleSnapshot(
            "main", changed_profile, config.main_role.user_prompt
        ),
    )
    captured = {}
    _install_completing_orchestrator(monkeypatch, captured)
    run = runner_module.run_enhanced_translation(
        _two_cues(),
        changed,
        output_dir=tmp_path,
        base_name="episode",
    )

    assert run.recovery_summary is not None
    drift = run.recovery_summary.configuration_drift
    assert len(drift) == 1
    assert "主翻译模型配置方案" in drift[0]
    assert run.recovery_provenance is not None
    assert run.recovery_provenance.drifted_keys == ("main_profile",)


def test_unrecorded_fingerprint_reports_single_drift_item(tmp_path, monkeypatch):
    """旧 manifest 无冻结摘要重跑：单条未记录摘要提示，drifted_keys 为空。"""
    config = _enhanced_config_with_profiles()
    _interrupt_with_checkpoint(tmp_path, monkeypatch, config)

    manifest_path = _recovery_manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("config_fingerprint")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    captured = {}
    _install_completing_orchestrator(monkeypatch, captured)
    run = runner_module.run_enhanced_translation(
        _two_cues(),
        config,
        output_dir=tmp_path,
        base_name="episode",
    )

    assert run.recovery_summary is not None
    assert run.recovery_summary.configuration_drift == (
        UNRECORDED_CONFIG_DIGEST_DRIFT,
    )
    assert run.recovery_provenance is not None
    assert run.recovery_provenance.drifted_keys == ()


def test_manifest_stores_prompt_hash_not_text(tmp_path, monkeypatch):
    """中断后的 manifest 只存提示词摘要：不含提示词原文，也不含连接机密。"""
    config = _enhanced_config_with_profiles()
    _interrupt_with_checkpoint(tmp_path, monkeypatch, config)

    manifest_path = _recovery_manifest(tmp_path)
    text = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(text)
    fingerprint = manifest["config_fingerprint"]
    assert fingerprint["main_prompt"].startswith("sha256:")
    assert fingerprint["review_prompt"].startswith("sha256:")
    assert "MAIN USER PROMPT" not in text
    assert "REVIEW USER PROMPT" not in text
    assert "secret" not in text


def test_resumed_run_marks_report_snapshot_and_manifest_with_recovery(
    tmp_path, monkeypatch
):
    """恢复运行完成：报告、执行快照与 workspace manifest 三处各带一份恢复来源。"""
    config = _enhanced_config_with_profiles()
    _interrupt_with_checkpoint(tmp_path, monkeypatch, config)

    captured = {}
    _install_completing_orchestrator(monkeypatch, captured)
    run = runner_module.run_enhanced_translation(
        _two_cues(),
        config,
        output_dir=tmp_path,
        base_name="episode",
    )

    manifest = _published_manifest(tmp_path)
    recovery = manifest["recovery"]
    assert recovery["checkpoint_time"]
    assert isinstance(recovery["completed"], dict)
    assert recovery["configuration_drift"] == []

    snapshot = json.loads(
        (_published_dir(tmp_path) / "translation-snapshot.json").read_text(
            encoding="utf-8"
        )
    )
    assert snapshot["recovery"]["checkpoint_time"] == recovery["checkpoint_time"]

    markdown = run.artifacts.audit_report_path.read_text(encoding="utf-8")
    assert "## 恢复来源" in markdown
    assert "检查点时间" in markdown

    assert run.recovery_provenance is not None
    assert run.result.audit_report.recovery is not None
    assert run.result.audit_report.recovery.configuration_drift == ()


def test_uninterrupted_run_has_no_recovery_provenance(tmp_path, monkeypatch):
    """不中断运行：无恢复来源，成果里完全不带 recovery 键与恢复来源一节。"""
    config = _enhanced_config_with_profiles()
    captured = {}
    _install_completing_orchestrator(monkeypatch, captured)
    run = runner_module.run_enhanced_translation(
        _two_cues(),
        config,
        output_dir=tmp_path,
        base_name="episode",
    )

    assert run.recovery_provenance is None
    assert run.result.audit_report.recovery is None

    manifest = _published_manifest(tmp_path)
    assert "recovery" not in manifest
    snapshot = json.loads(
        (_published_dir(tmp_path) / "translation-snapshot.json").read_text(
            encoding="utf-8"
        )
    )
    assert "recovery" not in snapshot
    markdown = run.artifacts.audit_report_path.read_text(encoding="utf-8")
    assert "恢复来源" not in markdown


def test_resumed_drift_run_annotates_affected_outputs_in_audit_markdown(
    tmp_path, monkeypatch
):
    """带漂移的恢复运行：审计报告标注漂移项与受影响成果（如旧提示词）。"""
    config = _enhanced_config_with_profiles()
    _interrupt_with_checkpoint(tmp_path, monkeypatch, config)

    changed = replace(
        config,
        main_role=replace(config.main_role, user_prompt="NEW PROMPT"),
        review_role=replace(config.review_role, user_prompt="NEW REVIEW PROMPT"),
    )
    captured = {}
    _install_completing_orchestrator(monkeypatch, captured)
    run = runner_module.run_enhanced_translation(
        _two_cues(),
        changed,
        output_dir=tmp_path,
        base_name="episode",
    )

    markdown = run.artifacts.audit_report_path.read_text(encoding="utf-8")
    assert "## 恢复来源" in markdown
    # 漂移项逐条列出（两条提示词漂移）。
    assert markdown.count("提示词：检查点 sha256:") == 2
    # 受影响成果按漂移项标注：两行分别指向旧的主翻译/高级校对提示词。
    assert "部分译文与全文分析结果来自旧的主翻译提示词" in markdown
    assert "部分术语裁决与审计结果来自旧的高级校对提示词" in markdown
    assert run.recovery_provenance is not None
    assert run.recovery_provenance.drifted_keys == ("main_prompt", "review_prompt")


def test_snapshot_round_trips_recovery_provenance(tmp_path, monkeypatch):
    """快照 to_persisted/from_persisted 对恢复来源读写对称，重载不丢溯源。"""
    from videocaptioner.core.postprocess.translation import (
        TranslationExecutionSnapshot,
    )

    config = _enhanced_config_with_profiles()
    _interrupt_with_checkpoint(tmp_path, monkeypatch, config)

    captured = {}
    _install_completing_orchestrator(monkeypatch, captured)
    run = runner_module.run_enhanced_translation(
        _two_cues(),
        config,
        output_dir=tmp_path,
        base_name="episode",
    )
    assert run.recovery_provenance is not None

    snapshot_payload = json.loads(
        (_published_dir(tmp_path) / "translation-snapshot.json").read_text(
            encoding="utf-8"
        )
    )
    rebuilt = TranslationExecutionSnapshot.from_persisted(snapshot_payload)
    assert rebuilt is not None
    assert rebuilt.recovery == run.recovery_provenance
    # 无该键（不中断运行的快照）时同样不猜测：恢复来源为 None。
    clean = TranslationExecutionSnapshot.from_persisted(
        {key: value for key, value in snapshot_payload.items() if key != "recovery"}
    )
    assert clean is not None and clean.recovery is None


def test_added_or_removed_fingerprint_key_never_claims_no_drift(tmp_path, monkeypatch):
    """比对项增删时报告不自相矛盾：不写「无（无配置漂移）」，给归属说明。"""
    from videocaptioner.core.recovery import RecoveryProvenance
    from videocaptioner.core.translate.enhanced.models import TranslationAuditReport
    from videocaptioner.core.translate.enhanced.report import render_audit_markdown

    # 旧检查点没记录该项 → drift 非空、drifted_keys 为空的矛盾形状。
    provenance = RecoveryProvenance(
        checkpoint_time="2026-09-16T12:00:00Z",
        completed={"glossary": 1},
        configuration_drift=("翻译批处理上限：检查点未记录该项（当前 10）",),
        drifted_keys=(),
    )
    markdown = render_audit_markdown(TranslationAuditReport(recovery=provenance))
    assert "检查点未记录该项" in markdown
    assert "受影响成果：无（无配置漂移）" not in markdown
    assert "受影响成果" in markdown
    # 无漂移时照旧明确为无。
    clean = RecoveryProvenance(checkpoint_time="t")
    clean_markdown = render_audit_markdown(TranslationAuditReport(recovery=clean))
    assert "受影响成果：无（无配置漂移）" in clean_markdown


def test_start_fresh_reruns_with_zero_drift(tmp_path, monkeypatch):
    """start_fresh 用当前配置重写指纹：随后同配置重跑零漂移且仍复用进度。"""
    config = _enhanced_config_with_profiles()
    changed = replace(
        config,
        main_role=replace(config.main_role, user_prompt="NEW PROMPT"),
    )
    _interrupt_with_checkpoint(tmp_path, monkeypatch, config)

    class StartFreshInterruptedRun:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, cues, **kwargs):
            assert kwargs["resume_translations"] == {}
            kwargs["on_translations"]({1: "新译文"})
            raise EnhancedTranslationError(
                "translation failed",
                stage="translation",
                category="transient",
                retryable=True,
            )

    monkeypatch.setattr(
        runner_module, "EnhancedTranslationOrchestrator", StartFreshInterruptedRun
    )
    with pytest.raises(EnhancedTranslationError, match="已保存到检查点"):
        runner_module.run_enhanced_translation(
            _two_cues(),
            changed,
            output_dir=tmp_path,
            base_name="episode",
            recovery_decision=lambda _summary: "start_fresh",
        )

    captured = {}
    _install_completing_orchestrator(monkeypatch, captured)
    run = runner_module.run_enhanced_translation(
        _two_cues(),
        changed,
        output_dir=tmp_path,
        base_name="episode",
    )

    assert captured["resume_translations"] == {1: "新译文"}
    assert run.recovery_summary is not None
    assert run.recovery_summary.configuration_drift == ()
    assert run.recovery_provenance is not None
    assert run.recovery_provenance.drifted_keys == ()
