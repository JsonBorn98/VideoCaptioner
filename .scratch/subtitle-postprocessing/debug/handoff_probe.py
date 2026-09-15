"""Offline handoff diagnosis; production code and existing tests stay unchanged.

Run from repository root:
  uv run python .scratch/subtitle-postprocessing/debug/handoff_probe.py
"""
from __future__ import annotations

import json
import runpy
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
fixtures = runpy.run_path(str(ROOT / "tests/test_postprocess/test_translation_to_postprocess_handoff.py"))

from videocaptioner.core.postprocess.config import PostprocessConfig
from videocaptioner.core.postprocess.models import PostprocessLayoutMode, PostprocessTask
from videocaptioner.core.postprocess.runner import run_postprocess_task
from videocaptioner.core.postprocess.translation import (
    METHOD_ENHANCED_LLM,
    TranslationExecutionSnapshot,
    load_translation_snapshot_file,
    resolve_repair_flow,
)
from videocaptioner.core.postprocess.workspace import (
    ASSET_FILENAMES,
    FilesystemAssetStore,
    fingerprint_subtitle,
)
from videocaptioner.core.subtitle.io import import_subtitle
from videocaptioner.core.entities import SubtitleLayoutEnum


def emit(label, **fields):
    print(json.dumps({"probe": label, **fields}, ensure_ascii=False))


def task_for(initial, **changes):
    args = dict(
        source_subtitle_path=str(initial),
        workflow_base_name="clip",
        source_language="en",
        target_language="zh",
        translation_method=METHOD_ENHANCED_LLM,
        layout_mode=PostprocessLayoutMode.ORIGINAL_ON_TOP,
        config_snapshot=PostprocessConfig(trim_trailing_punct=False, speed_semantic_repair=False),
    )
    args.update(changes)
    return PostprocessTask(**args)


def discover(initial, **changes):
    task = task_for(initial, **changes)
    data = import_subtitle(initial, layout_hint=SubtitleLayoutEnum.ORIGINAL_ON_TOP).data
    task.subtitle_fingerprint = fingerprint_subtitle(data)
    FilesystemAssetStore().discover(task)
    return task


with TemporaryDirectory(prefix="vc-handoff-diagnosis-") as tmp, patch.object(
    fixtures["enhanced_runner_module"], "EnhancedTranslationOrchestrator", fixtures["_SuccessfulOrchestrator"]
):
    root = Path(tmp)
    run = fixtures["_translate"](root)
    initial = fixtures["_save_initial"](run, root)
    emit("translation_only", files=sorted(p.name for p in root.iterdir()),
         workspace_exists=(root / "videocaptioner-workspace").exists(),
         manifest_count=len(list(root.rglob("manifest.json"))))
    assert run.artifacts.translation_checkpoint_path.is_file()
    assert not list(root.rglob("manifest.json"))

    result = run_postprocess_task(task_for(initial))
    baseline = result.task.asset_discovery
    emit("independent_postprocess", succeeded=result.succeeded, status=result.task.status,
         error=result.task.error, warnings=list(result.warnings),
         missing=list(baseline.missing), repair_flow=result.report.viewing_repair.flow_mode,
         manifest_assets=json.loads(baseline.manifest_path.read_text(encoding="utf-8"))["assets"])
    assert "checkpoint" in baseline.missing
    assert result.report.viewing_repair.flow_mode == "report_only"

    # A longer cue shows that the warning can leave viewing problems unresolved.
    from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
    long_data = ASRData([ASRDataSeg("Hello world.", 0, 2000, "这是一条用于验证缺少翻译资产时无法执行观看问题自动修复的很长很长的译文。")])
    long_result = run_postprocess_task(task_for(initial, input_data=long_data))
    emit("long_subtitle_without_snapshot", succeeded=long_result.succeeded,
         repair_flow=long_result.report.viewing_repair.flow_mode,
         unresolved=len(long_result.report.unresolved_viewing_problems()),
         translated_unchanged=long_result.output_data.segments[0].translated_text == long_data.segments[0].translated_text)
    assert long_result.succeeded
    assert long_result.report.unresolved_viewing_problems()
    assert long_result.output_data.segments[0].translated_text == long_data.segments[0].translated_text

    paths = {"glossary": run.artifacts.glossary_path, "audit": run.artifacts.audit_report_path,
             "checkpoint": run.artifacts.translation_checkpoint_path}
    for kind, source in paths.items():
        shutil.copy2(source, baseline.task_dir / ASSET_FILENAMES[kind])
    moved = discover(initial)
    emit("copy_without_manifest_registration", missing=list(moved.asset_discovery.missing))
    assert moved.asset_discovery.asset_path("checkpoint") is None

    registered = discover(initial, explicit_assets={k: str(v) for k, v in paths.items()})
    emit("register_three_assets", verified=[k for k, _ in registered.asset_discovery.verified_assets],
         missing=list(registered.asset_discovery.missing),
         repair_flow=resolve_repair_flow(registered.translation_snapshot).mode)
    assert registered.asset_discovery.asset_path("checkpoint") is not None
    assert registered.asset_discovery.asset_path("translation_snapshot") is None

    profile = fixtures["_profile"]()
    snapshot = TranslationExecutionSnapshot(method=METHOD_ENHANCED_LLM,
        main_profile=profile, review_profile=profile, source_language="en", target_language="zh")
    discover(initial, translation_snapshot=snapshot)
    independent = discover(initial)
    snapshot_path = independent.asset_discovery.asset_path("translation_snapshot")
    restored = load_translation_snapshot_file(snapshot_path)
    flow = resolve_repair_flow(restored, profile_resolver=lambda *_: profile)
    emit("register_snapshot_then_rediscover", missing=list(independent.asset_discovery.missing),
         restored_method=restored.method, repair_flow=flow.mode)
    assert flow.mode == "main_review"
    assert "checkpoint" not in independent.asset_discovery.missing

    blank = discover(initial, source_language="", target_language="")
    emit("same_input_blank_language_pair", language_dir=blank.asset_discovery.task_dir.name,
         same_task_dir=blank.asset_discovery.task_dir == independent.asset_discovery.task_dir,
         missing=list(blank.asset_discovery.missing))
    assert blank.asset_discovery.asset_path("checkpoint") is None

    auto = discover(initial, source_language="auto")
    emit("same_input_auto_source_language", language_dir=auto.asset_discovery.task_dir.name,
         missing=list(auto.asset_discovery.missing))
    assert auto.asset_discovery.asset_path("checkpoint") is None

    implicit_name = discover(initial, workflow_base_name="")
    emit("derive_name_from_stage_prefix", same_task_dir=implicit_name.asset_discovery.task_dir == independent.asset_discovery.task_dir)
    assert implicit_name.asset_discovery.task_dir == independent.asset_discovery.task_dir

    emit("fingerprint_roundtrip", matches=fingerprint_subtitle(run.subtitle_data) == independent.subtitle_fingerprint)
    assert fingerprint_subtitle(run.subtitle_data) == independent.subtitle_fingerprint

    from videocaptioner.core.postprocess.repair import select_repair_flow
    fallback = select_repair_flow(None, profile)
    emit("missing_snapshot_with_utility_profile", repair_flow=fallback.mode,
         review_profile_present=fallback.review_profile is not None, reason=fallback.reason)
    assert fallback.mode == "main"
    assert fallback.review_profile is None

print("All diagnostic predictions confirmed; no network calls or production edits.")
