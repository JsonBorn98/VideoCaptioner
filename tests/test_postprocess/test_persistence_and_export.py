"""过程持久化、成功后导出与状态展示（票 08）。

切面是 ``run_postprocess_task`` / ``export_process_assets`` /
``build_postprocess_stage_summary``：用临时输入目录验证外部行为
（D21/D28），不锁定内部函数或文件写入实现细节。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from videocaptioner.core.postprocess.config import PostprocessConfig
from videocaptioner.core.postprocess.models import PostprocessLayoutMode, PostprocessTask
from videocaptioner.core.postprocess.repair import RegionRollback, RepairSummary
from videocaptioner.core.postprocess.report import QualityReport
from videocaptioner.core.postprocess.runner import run_postprocess_task
from videocaptioner.core.postprocess.summary import build_postprocess_stage_summary
from videocaptioner.core.postprocess.viewing import ViewingProblem
from videocaptioner.core.postprocess.workspace import (
    ProcessAssetExportError,
    export_process_assets,
)


def _write_srt(path: Path, text: str = "Hello there.", translated: str = "你好。") -> None:
    path.write_text(
        f"1\n00:00:00,000 --> 00:00:02,000\n{text}\n{translated}\n",
        encoding="utf-8",
    )


def _config(**overrides) -> PostprocessConfig:
    values = {"trim_trailing_punct": False, "speed_semantic_repair": False}
    values.update(overrides)
    return PostprocessConfig(**values)


def _task(
    tmp_path: Path,
    *,
    name: str = "demo",
    config: PostprocessConfig | None = None,
    source: Path | None = None,
) -> PostprocessTask:
    if source is None:
        source = tmp_path / f"{name}.srt"
        if not source.exists():
            _write_srt(source)
    return PostprocessTask(
        str(source),
        postprocessed_subtitle_path=str(tmp_path / f"{name}-out.srt"),
        workflow_base_name=name,
        source_language="en",
        target_language="zh",
        layout_mode=PostprocessLayoutMode.ORIGINAL_ON_TOP,
        config_snapshot=config or _config(),
    )


def _manifest(result) -> dict:
    discovery = result.task.asset_discovery
    assert discovery is not None
    return json.loads(Path(discovery.manifest_path).read_text(encoding="utf-8"))


# ---- 过程报告与状态写入过程目录，不散落普通输出目录 ----


def test_success_persists_qa_report_and_state_into_workspace(tmp_path):
    task = _task(tmp_path, config=_config(qa_report=True))

    result = run_postprocess_task(task)

    assert result.succeeded
    discovery = result.task.asset_discovery
    assert discovery is not None
    qa_path = discovery.task_dir / "qa-report.md"
    state_path = discovery.task_dir / "postprocess-state.json"
    assert qa_path.is_file()
    assert state_path.is_file()
    assert "字幕质量 QA 报告" in qa_path.read_text(encoding="utf-8")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["schema"] == "videocaptioner.postprocess_state"
    assert state["status"] == "completed"
    assert state["unresolved_viewing_problems"] == 0
    assert result.task.persisted_outputs["qa_report"] == str(qa_path)
    assert result.task.persisted_outputs["postprocess_state"] == str(state_path)
    # 普通输出目录不因后处理产生过程报告。
    assert list(tmp_path.glob("*.qa.md")) == []
    assert list(tmp_path.glob("*speed-changes*")) == []


def test_speed_result_persists_speed_changes_into_workspace(tmp_path):
    task = _task(tmp_path, config=_config(speed_optimize=True))

    result = run_postprocess_task(task)

    assert result.succeeded
    discovery = result.task.asset_discovery
    assert discovery is not None
    speed_path = discovery.task_dir / "speed-changes.json"
    assert speed_path.is_file()
    payload = json.loads(speed_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert result.task.persisted_outputs["speed_changes"] == str(speed_path)


def test_manifest_registers_downstream_outputs_without_secrets(tmp_path):
    task = _task(tmp_path, config=_config(qa_report=True))

    result = run_postprocess_task(task)

    manifest = _manifest(result)
    assets = manifest["assets"]
    assert assets["qa_report"] == "qa-report.md"
    assert assets["postprocess_state"] == "postprocess-state.json"
    assert manifest["translation_method"] == ""
    dumped = json.dumps(manifest) + json.dumps(
        json.loads(
            (result.task.asset_discovery.task_dir / "postprocess-state.json").read_text(
                encoding="utf-8"
            )
        )
    )
    for forbidden in ("api_key", "sk-", "prompt", "response", "reasoning"):
        assert forbidden not in dumped


def test_upstream_assets_from_full_workflow_land_in_workspace(tmp_path):
    glossary = tmp_path / "【项目术语表】demo.vcglossary.json"
    glossary.write_text(
        '{"schema": "videocaptioner.project_glossary", "terms": []}\n', encoding="utf-8"
    )
    audit = tmp_path / "【翻译审计】demo.md"
    audit.write_text("# 翻译审计\n无问题。\n", encoding="utf-8")
    task = _task(tmp_path, config=_config(qa_report=True))
    task.explicit_assets = {
        "glossary": str(glossary),
        "audit": str(audit),
    }

    result = run_postprocess_task(task)

    assert result.succeeded
    discovery = result.task.asset_discovery
    assert discovery.asset_path("glossary") == discovery.task_dir / "glossary.vcglossary.json"
    assert discovery.asset_path("audit") == discovery.task_dir / "translation-audit.md"
    manifest = _manifest(result)
    assert manifest["assets"]["glossary"] == "glossary.vcglossary.json"
    assert manifest["assets"]["audit"] == "translation-audit.md"
    assert list(tmp_path.glob("*.vcglossary.json")) == [glossary]


def test_rerun_without_qa_report_delists_stale_downstream_output(tmp_path):
    first = run_postprocess_task(_task(tmp_path, config=_config(qa_report=True)))
    assert first.succeeded
    task_dir = first.task.asset_discovery.task_dir

    second = run_postprocess_task(_task(tmp_path, config=_config()))

    assert second.succeeded
    assert second.task.asset_discovery.task_dir == task_dir
    manifest = _manifest(second)
    assert "qa_report" not in manifest["assets"]
    assert not (task_dir / "qa-report.md").exists()
    assert manifest["assets"]["postprocess_state"] == "postprocess-state.json"


def test_analyze_mode_persists_report_and_state(tmp_path):
    task = _task(tmp_path, config=_config(speed_mode="analyze", qa_report=True))

    result = run_postprocess_task(task)

    assert result.succeeded
    assert result.task.status == "completed"
    discovery = result.task.asset_discovery
    assert (discovery.task_dir / "qa-report.md").is_file()
    assert (discovery.task_dir / "postprocess-state.json").is_file()
    state = json.loads(
        (discovery.task_dir / "postprocess-state.json").read_text(encoding="utf-8")
    )
    assert state["status"] == "completed"


# ---- 模块未成功完成时不提供导出 ----


def test_module_failure_writes_no_downstream_outputs_and_blocks_export(tmp_path):
    source = tmp_path / "demo.srt"
    _write_srt(source)
    task = PostprocessTask(
        str(source),
        postprocessed_subtitle_path=str(source),  # 输出碰撞触发模块级失败
        workflow_base_name="demo",
        source_language="en",
        target_language="zh",
        layout_mode=PostprocessLayoutMode.ORIGINAL_ON_TOP,
        config_snapshot=_config(qa_report=True),
    )

    result = run_postprocess_task(task)

    assert not result.succeeded
    assert result.task.status == "fallback"
    assert not result.continue_downstream
    discovery = result.task.asset_discovery
    assert discovery is not None
    assert not (discovery.task_dir / "qa-report.md").exists()
    assert not (discovery.task_dir / "postprocess-state.json").exists()
    with pytest.raises(ProcessAssetExportError, match="未成功完成"):
        export_process_assets(task, tmp_path / "exported")


def test_cancelled_task_blocks_export(tmp_path):
    source = tmp_path / "demo.srt"
    _write_srt(source)
    task = PostprocessTask(
        str(source),
        postprocessed_subtitle_path=str(tmp_path / "demo-out.srt"),
        workflow_base_name="demo",
        source_language="en",
        target_language="zh",
        layout_mode=PostprocessLayoutMode.ORIGINAL_ON_TOP,
        config_snapshot=_config(qa_report=True),
    )

    result = run_postprocess_task(task, cancelled=lambda: True)

    assert result.task.status == "cancelled"
    with pytest.raises(ProcessAssetExportError, match="未成功完成"):
        export_process_assets(task, tmp_path / "exported")


# ---- 成功后按 manifest 导出：稳定文件名、可选 manifest 副本 ----


def test_export_copies_assets_with_stable_names_and_manifest(tmp_path):
    glossary = tmp_path / "【项目术语表】demo.vcglossary.json"
    glossary.write_text(
        '{"schema": "videocaptioner.project_glossary", "terms": []}\n', encoding="utf-8"
    )
    task = _task(tmp_path, config=_config(qa_report=True))
    task.explicit_assets = {"glossary": str(glossary)}
    result = run_postprocess_task(task)
    assert result.succeeded
    destination = tmp_path / "delivery"

    copied = export_process_assets(task, destination)

    names = {path.name for path in copied}
    assert {"glossary.vcglossary.json", "qa-report.md", "postprocess-state.json", "manifest.json"} <= names
    assert (destination / "manifest.json").is_file()
    assert (destination / "qa-report.md").read_text(encoding="utf-8") == (
        result.task.asset_discovery.task_dir / "qa-report.md"
    ).read_text(encoding="utf-8")
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["assets"]["qa_report"] == "qa-report.md"
    # 导出是复制：过程目录源文件仍在。
    assert (result.task.asset_discovery.task_dir / "qa-report.md").is_file()


def test_export_selects_kinds_per_manifest(tmp_path):
    task = _task(tmp_path, config=_config(qa_report=True))
    result = run_postprocess_task(task)
    assert result.succeeded
    destination = tmp_path / "selected"

    copied = export_process_assets(
        task, destination, kinds=["qa_report"], include_manifest=False
    )

    assert [path.name for path in copied] == ["qa-report.md"]
    assert not (destination / "postprocess-state.json").exists()
    assert not (destination / "manifest.json").exists()


def test_export_rejects_unknown_kind_with_available_listing(tmp_path):
    task = _task(tmp_path, config=_config(qa_report=True))
    result = run_postprocess_task(task)
    assert result.succeeded

    with pytest.raises(ProcessAssetExportError, match="ghost"):
        export_process_assets(task, tmp_path / "exported", kinds=["ghost"])


def test_export_failure_reports_only_export_failure(tmp_path):
    task = _task(tmp_path, config=_config(qa_report=True))
    result = run_postprocess_task(task)
    assert result.succeeded
    blocker = tmp_path / "blocked"
    blocker.write_text("occupied", encoding="utf-8")  # 目标被文件占用，mkdir 失败
    state_before = (
        result.task.asset_discovery.task_dir / "postprocess-state.json"
    ).read_bytes()

    with pytest.raises(ProcessAssetExportError, match="导出|复制"):
        export_process_assets(task, blocker)

    # 导出失败不改变已完成的核心结果与过程目录。
    assert result.task.status == "completed"
    assert result.task.asset_discovery is not None
    assert (
        result.task.asset_discovery.task_dir / "postprocess-state.json"
    ).read_bytes() == state_before


# ---- 上游资产收集：完整 workflow 接线（含 checkpoint）----


def test_collect_upstream_assets_picks_readable_kinds_only(tmp_path):
    from types import SimpleNamespace

    from videocaptioner.core.postprocess.assets import collect_upstream_assets

    glossary = tmp_path / "glossary.vcglossary.json"
    glossary.write_text('{"terms": []}\n', encoding="utf-8")
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text('{"cursor": 1}\n', encoding="utf-8")
    source = SimpleNamespace(
        glossary_path=str(glossary),
        translation_audit_report_path=str(tmp_path / "missing-audit.md"),
        translation_checkpoint_path=str(checkpoint),
    )

    assets = collect_upstream_assets(source)

    # 存在且可读的进 explicit_assets；文件缺失的 audit 不进。
    assert assets == {
        "glossary": str(glossary),
        "checkpoint": str(checkpoint),
    }


def test_collect_upstream_assets_skips_absent_attributes(tmp_path):
    from types import SimpleNamespace

    from videocaptioner.core.postprocess.assets import collect_upstream_assets

    assets = collect_upstream_assets(SimpleNamespace())

    assert assets == {}


# ---- 状态展示：未解决问题、回退警告、活动字幕 ----


def _summary_result(task: PostprocessTask, report: QualityReport) -> object:
    from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
    from videocaptioner.core.postprocess.models import PostprocessResult

    data = ASRData([ASRDataSeg("原文。", 0, 1000, "译文。")])
    return PostprocessResult(
        task=task,
        input_data=data,
        output_data=data,
        report=report,
        layout=None,  # type: ignore[arg-type]
        layout_confidence=1.0,
    )


def test_stage_summary_lists_unresolved_rollback_and_active_subtitle(tmp_path):
    task = _task(tmp_path)
    task.postprocessed_subtitle_path = str(tmp_path / "demo-out.srt")
    task.active_subtitle_path = task.postprocessed_subtitle_path
    report = QualityReport()
    report.viewing_problems = [
        ViewingProblem("p1", "original", 0, "超长文本", 30.0, 20.0, 16.0, "超出绝对上限"),
        ViewingProblem("p2", "original", 1, "另一段", 25.0, 20.0, 16.0, "超出绝对上限", resolved=True),
    ]
    report.viewing_repair = RepairSummary(
        rollbacks=[RegionRollback((0,), "业务修复重试耗尽")],
        translation_method="enhanced_llm",
        flow_mode="main_review",
    )

    summary = build_postprocess_stage_summary(_summary_result(task, report))

    counts = dict(summary.counts)
    assert counts["未解决问题"] == 1
    assert counts["回退区域"] == 1
    assert "活动字幕=后处理字幕" in (summary.status or "")


def test_stage_summary_marks_initial_subtitle_active_after_fallback(tmp_path):
    task = _task(tmp_path)
    task.postprocessed_subtitle_path = None
    task.active_subtitle_path = task.initial_subtitle_path

    summary = build_postprocess_stage_summary(_summary_result(task, QualityReport()))

    assert "活动字幕=初版字幕" in (summary.status or "")
