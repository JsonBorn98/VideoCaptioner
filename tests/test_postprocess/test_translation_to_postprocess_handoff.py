"""独立两阶段调用的过程资产交接（交付票 09 人工验收回归）。

场景：单独运行强化翻译产出初版字幕，之后拿该字幕独立运行后处理。
D21/D23 要求翻译过程资产进入输入旁的 ``videocaptioner-workspace``，
后处理按 manifest 验证复用；本文件钉住两阶段之间的交接契约，
不锁定翻译编排或修复循环的内部实现。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import videocaptioner.core.translate.enhanced.runner as enhanced_runner_module
from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.entities import SubtitleLayoutEnum
from videocaptioner.core.llm import LLMModelProfile, LLMTransport, ProviderDialect
from videocaptioner.core.postprocess.config import PostprocessConfig
from videocaptioner.core.postprocess.models import PostprocessLayoutMode, PostprocessTask
from videocaptioner.core.postprocess.runner import run_postprocess_task
from videocaptioner.core.subtitle.io import save_canonical_srt
from videocaptioner.core.translate.enhanced.models import (
    AuthoritativeGlossary,
    EnhancedTranslationConfig,
    EnhancedTranslationResult,
    TranslationAuditReport,
    TranslationContextBrief,
    TranslationExecutionMode,
    TranslationRoleSnapshot,
)


def _profile() -> LLMModelProfile:
    return LLMModelProfile(
        profile_id="handoff-profile",
        name="Handoff Profile",
        transport=LLMTransport.OPENAI_COMPATIBLE,
        dialect=ProviderDialect.GENERIC,
        base_url="https://handoff.test/v1",
        api_key="secret",
        model="handoff-model",
        work_context_tokens=16_384,
    )


class _SuccessfulOrchestrator:
    """成功路径的增强翻译编排器：术语表 + 全量译文 + 空审计。"""

    def __init__(self, _config, **_kwargs):
        pass

    def run(self, cues, **kwargs):
        glossary = AuthoritativeGlossary(
            source_language="en",
            target_language="zh",
            subtitle_fingerprint="sha256:" + "0" * 64,
        )
        kwargs["on_glossary"](glossary)
        translations = {cue.cue_id: f"译文{cue.cue_id}" for cue in cues}
        kwargs["on_translations"](translations)
        return EnhancedTranslationResult(
            translations=translations,
            brief=TranslationContextBrief(),
            glossary=glossary,
            audit_report=TranslationAuditReport(),
        )


@pytest.fixture(autouse=True)
def _successful_enhanced_translation(monkeypatch):
    monkeypatch.setattr(
        enhanced_runner_module, "EnhancedTranslationOrchestrator", _SuccessfulOrchestrator
    )


def _translate(tmp_path: Path):
    """独立运行强化翻译（CLI 口径）：产物落在字幕输出目录旁。"""

    return enhanced_runner_module.run_enhanced_translation(
        ASRData([ASRDataSeg("Hello world.", 0, 2000)]),
        EnhancedTranslationConfig(
            main_role=TranslationRoleSnapshot("main", _profile()),
            review_role=TranslationRoleSnapshot("review", _profile()),
            source_language="en",
            target_language="zh",
            execution_mode=TranslationExecutionMode.CLI,
        ),
        output_dir=tmp_path,
        base_name="clip",
    )


def _save_initial(run, tmp_path: Path) -> Path:
    return save_canonical_srt(
        run.subtitle_data,
        tmp_path / "【初版字幕】clip.srt",
        layout=SubtitleLayoutEnum.ORIGINAL_ON_TOP,
    )


def test_independent_translation_keeps_process_assets_out_of_plain_output(tmp_path):
    run = _translate(tmp_path)
    _save_initial(run, tmp_path)

    # D21：过程资产集中在专用目录，不散落普通输出目录。
    # 【初版字幕】是阶段自动保存的合法 SRT 成果，不属于过程资产。
    scattered = {
        prefix
        for prefix in ("【项目术语表】", "【翻译审计】", "【增强翻译检查点】")
        if list(tmp_path.glob(f"{prefix}*"))
    }
    assert scattered == set()
    workspace = tmp_path / "videocaptioner-workspace"
    assert workspace.is_dir()
    assert list(workspace.rglob("manifest.json"))
    assert list(workspace.rglob("translation-checkpoint.json"))


def test_standalone_postprocess_discovers_translation_stage_assets(tmp_path):
    initial = _save_initial(_translate(tmp_path), tmp_path)

    result = run_postprocess_task(
        PostprocessTask(
            str(initial),
            postprocessed_subtitle_path=str(tmp_path / "【后处理字幕】clip.srt"),
            workflow_base_name="clip",
            source_language="en",
            target_language="zh",
            translation_method="enhanced",
            layout_mode=PostprocessLayoutMode.ORIGINAL_ON_TOP,
            config_snapshot=PostprocessConfig(
                trim_trailing_punct=False, speed_semantic_repair=False
            ),
        )
    )

    assert result.succeeded
    discovery = result.task.asset_discovery
    assert discovery is not None
    # 交付票人工验收症状：独立后处理找不到翻译阶段的检查点 / 执行快照。
    assert discovery.asset_path("checkpoint") is not None
    assert "checkpoint" not in discovery.missing
    assert "translation_snapshot" not in discovery.missing
    assert result.task.translation_snapshot is not None
    # 用户看到的警告原文：缺失项里含检查点（「没有找到中间检查文件」）。
    assert not any("过程资产缺失" in warning and "checkpoint" in warning
                   for warning in result.warnings)


def test_standalone_postprocess_finds_assets_when_languages_do_not_match(tmp_path):
    """GUI 独立后处理常不带语言；翻译侧则是 auto + 简体中文。"""

    run = enhanced_runner_module.run_enhanced_translation(
        ASRData([ASRDataSeg("Hello world.", 0, 2000)]),
        EnhancedTranslationConfig(
            main_role=TranslationRoleSnapshot("main", _profile()),
            review_role=TranslationRoleSnapshot("review", _profile()),
            source_language="auto",
            target_language="简体中文",
            execution_mode=TranslationExecutionMode.CLI,
        ),
        output_dir=tmp_path,
        base_name="clip",
    )
    initial = _save_initial(run, tmp_path)

    result = run_postprocess_task(
        PostprocessTask(
            str(initial),
            postprocessed_subtitle_path=str(tmp_path / "【后处理字幕】clip.srt"),
            workflow_base_name="clip",
            layout_mode=PostprocessLayoutMode.ORIGINAL_ON_TOP,
            config_snapshot=PostprocessConfig(
                trim_trailing_punct=False, speed_semantic_repair=False
            ),
        )
    )

    assert result.succeeded
    discovery = result.task.asset_discovery
    assert discovery is not None
    assert discovery.asset_path("checkpoint") is not None
    assert "checkpoint" not in discovery.missing
    assert "translation_snapshot" not in discovery.missing
    assert result.task.translation_snapshot is not None
    assert not any(
        "过程资产缺失" in warning and "checkpoint" in warning for warning in result.warnings
    )
