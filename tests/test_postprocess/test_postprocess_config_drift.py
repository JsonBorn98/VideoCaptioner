"""后处理配置漂移与 QA 报告 / 状态溯源（票 07，ADR-0022）：只验证外部行为。

按 spec「Testing Decisions」主 seam B：注入初版字幕任务、脚本化假网关、
临时目录上的文件系统资产库、取消函数、可选恢复决定回调；断言恢复摘要
的漂移清单、QA 报告 / 后处理状态 / manifest 的恢复来源标注，以及
manifest 只存哈希不存明文——不断言内部函数调用顺序或私有状态。
"""

from __future__ import annotations

import json
from pathlib import Path

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg  # noqa: F401 — _write_srt 辅助
from videocaptioner.core.postprocess.config import PostprocessConfig
from videocaptioner.core.postprocess.models import PostprocessLayoutMode, PostprocessTask
from videocaptioner.core.postprocess.runner import run_postprocess_task
from videocaptioner.core.postprocess.workspace import FilesystemAssetStore
from videocaptioner.core.recovery import RecoveryDecision

from .test_repair_execution import _profile, _ScriptedGateway

# 拆分输入：折算 60 字 > 绝对上限 20 → 一个观看长度问题；chunk=20 拆成 3 片。
_LONG_TEXT = "超长" * 30


def _ts(value: int) -> str:
    total, ms = divmod(value, 1000)
    hours, rest = divmod(total, 60)
    minutes, seconds = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}"


def _write_srt(path: Path, pairs: tuple[tuple[str, str], ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for index, (text, translated) in enumerate(pairs):
        start = index * 4000
        end = start + 4000
        lines.append(f"{index + 1}")
        lines.append(f"{_ts(start)} --> {_ts(end)}")
        lines.append(f"{text}\n{translated}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _repair_config(**overrides) -> PostprocessConfig:
    # 与 test_recovery_checkpoints._repair_config 同一形状：规则步骤全关，
    # 只走观看修复（拆分脚本一次归并）。
    return PostprocessConfig(
        trim_trailing_punct=False,
        remove_placeholders=False,
        normalize_quotes=False,
        fix_gaps=False,
        tail_compensation=False,
        compress_fast_subtitles=False,
        speed_optimize=False,
        **overrides,
    )


def _make_task(source: Path, output: Path, config: PostprocessConfig) -> PostprocessTask:
    config.utility_llm_profile = _profile()
    return PostprocessTask(
        str(source),
        postprocessed_subtitle_path=str(output),
        layout_mode=PostprocessLayoutMode.ORIGINAL_ON_TOP,
        config_snapshot=config,
        source_language="zh",
        target_language="en",
        thread_num=4,
    )


def _split_script() -> str:
    from .test_repair_execution import _repairs_for, _response

    return _response(
        _repairs_for(
            {
                "repair_subjects": [
                    {
                        "segments": [
                            {
                                "id": 0,
                                "text": _LONG_TEXT,
                                "translated": "短",
                                "problem_ids": ["length:original:0"],
                            }
                        ]
                    }
                ]
            },
            chunk=20,
        )
    )


class _InterruptingGateway(_ScriptedGateway):
    """第 ``stop_at`` 次请求抛 InterruptedError（主动停止路径）。"""

    def __init__(self, scripts, stop_at: int):
        super().__init__(list(scripts))
        self._stop_at = stop_at
        self.count = 0

    def complete(self, profile, request, *, cancelled=None):
        self.count += 1
        if self.count == self._stop_at:
            raise InterruptedError("user stop")
        return super().complete(profile, request, cancelled=cancelled)


def _interrupt_once(tmp_path: Path, name: str = "run", *, config: PostprocessConfig | None = None):
    """跑一次「主动停止」的运行：阶段末检查点落盘、轮末未写。"""
    run_dir = tmp_path / name
    source = run_dir / "input.srt"
    _write_srt(source, ((_LONG_TEXT, "短"),))
    result = run_postprocess_task(
        _make_task(source, run_dir / "out.srt", config or _repair_config()),
        gateway=_InterruptingGateway([_split_script()], stop_at=1),
        assets=FilesystemAssetStore(),
    )
    assert result.task.status == "cancelled"
    return source, result.task.asset_discovery.task_dir


def _task_dir_files(task_dir: Path) -> dict[str, dict]:
    state = json.loads((task_dir / "postprocess-state.json").read_text(encoding="utf-8"))
    manifest = json.loads((task_dir / "manifest.json").read_text(encoding="utf-8"))
    return {"state": state, "manifest": manifest}


# ---------------------------------------------------------------------------
# 验收 1：换后处理配置方案 / 翻译角色配置重跑——仍复用、摘要列漂移项。
# ---------------------------------------------------------------------------


def test_changed_postprocess_config_lists_drift_and_still_resumes(tmp_path):
    """改后处理配置方案值重跑：仍复用检查点，恢复摘要列出漂移项。"""
    source, task_dir = _interrupt_once(tmp_path)
    # 同一任务形状、改一个方案值（qa_report 开启）：阶段末仍复用。
    changed = _repair_config(qa_report=True)
    decisions = []

    def decision(summary):
        decisions.append(summary)
        return RecoveryDecision.CONTINUE

    resumed = run_postprocess_task(
        _make_task(source, source.parent / "out2.srt", changed),
        gateway=_ScriptedGateway([_split_script()]),
        assets=FilesystemAssetStore(),
        recovery_decision=decision,
    )
    assert resumed.succeeded
    assert resumed.recovery_summary is not None
    drift = resumed.recovery_summary.configuration_drift
    assert len(drift) == 1
    assert "后处理配置方案" in drift[0]
    assert len(decisions) == 1
    assert decisions[0].configuration_drift == drift
    # 漂移不作废检查点（ADR-0022）：阶段末装载，请求只发生在修复循环。
    assert resumed.recovery_summary.completed["phase"] == 1


def test_no_drift_when_config_unchanged(tmp_path):
    """配置未变重跑：恢复摘要无漂移项，溯源 drifted_keys 为空。"""
    source, task_dir = _interrupt_once(tmp_path)
    resumed = run_postprocess_task(
        _make_task(source, source.parent / "out2.srt", _repair_config()),
        gateway=_ScriptedGateway([_split_script()]),
        assets=FilesystemAssetStore(),
    )
    assert resumed.succeeded
    assert resumed.recovery_summary is not None
    assert resumed.recovery_summary.configuration_drift == ()


def test_changed_translation_snapshot_lists_translation_drift(tmp_path):
    """换翻译执行快照（翻译方式 / 提示词）重跑：快照侧漂移项列出。"""
    from videocaptioner.core.postprocess.translation import TranslationExecutionSnapshot

    source, task_dir = _interrupt_once(tmp_path, "snap")
    task = _make_task(source, source.parent / "out2.srt", _repair_config())
    # 中断运行无快照（空方式）→ 本次带快照：翻译方式漂移。
    task.bind_translation_snapshot(
        TranslationExecutionSnapshot(
            method="enhanced_llm",
            boundary_context_radius=3,
            source_language="zh",
            target_language="en",
        )
    )
    resumed = run_postprocess_task(
        task,
        gateway=_ScriptedGateway([_split_script()]),
        assets=FilesystemAssetStore(),
    )
    assert resumed.succeeded
    assert resumed.recovery_summary is not None
    drift = resumed.recovery_summary.configuration_drift
    assert any("翻译方式" in item for item in drift)
    # 提示词漂移需要真实提示词：快照带提示词、检查点无快照（空提示词哈希）。
    source2, _task_dir2 = _interrupt_once(tmp_path, "snap2")
    task2 = _make_task(source2, source2.parent / "out2.srt", _repair_config())
    task2.bind_translation_snapshot(
        TranslationExecutionSnapshot(
            method="enhanced_llm",
            main_prompt="主翻译提示词甲",
            review_prompt="高级校对提示词甲",
            source_language="zh",
            target_language="en",
        )
    )
    resumed2 = run_postprocess_task(
        task2,
        gateway=_ScriptedGateway([_split_script()]),
        assets=FilesystemAssetStore(),
    )
    assert resumed2.succeeded
    drift2 = resumed2.recovery_summary.configuration_drift
    assert any("主翻译提示词" in item for item in drift2)
    assert any("高级校对提示词" in item for item in drift2)


def test_unrecorded_fingerprint_reports_single_drift_item(tmp_path):
    """旧 manifest 无冻结摘要重跑：单条未记录摘要提示，drifted_keys 为空。"""
    source, task_dir = _interrupt_once(tmp_path, "unrecorded")
    manifest_path = task_dir / "recovery-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("config_fingerprint")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    resumed = run_postprocess_task(
        _make_task(source, source.parent / "out2.srt", _repair_config()),
        gateway=_ScriptedGateway([_split_script()]),
        assets=FilesystemAssetStore(),
    )
    assert resumed.succeeded
    assert resumed.recovery_summary is not None
    from videocaptioner.core.recovery import UNRECORDED_CONFIG_DIGEST_DRIFT

    assert resumed.recovery_summary.configuration_drift == (UNRECORDED_CONFIG_DIGEST_DRIFT,)


def test_no_reusable_level_refreshes_fingerprint_before_next_resume(tmp_path):
    """无可复用级别的重跑先刷新冻结摘要：下次恢复不报幻影漂移。

    第一次运行在任何检查点落盘前取消（无 completed）；第二次换配置
    重跑落盘检查点后中断；第三次恢复时冻结摘要已是第二次的配置。
    """
    run_dir = tmp_path / "run"
    source = run_dir / "input.srt"
    _write_srt(source, ((_LONG_TEXT, "短"),))
    # 早期取消（修复循环之前、检查点之前）：manifest 建立但无完成级别。
    stopped_early = run_postprocess_task(
        _make_task(source, run_dir / "out.srt", _repair_config()),
        gateway=_InterruptingGateway([_split_script()], stop_at=1),
        assets=FilesystemAssetStore(),
        cancelled=lambda: True,
    )
    assert stopped_early.task.status == "cancelled"

    # 第二次换配置重跑：落盘阶段末检查点后中断。
    changed = _repair_config(qa_report=True)
    stopped_again = run_postprocess_task(
        _make_task(source, run_dir / "out2.srt", changed),
        gateway=_InterruptingGateway([_split_script()], stop_at=1),
        assets=FilesystemAssetStore(),
    )
    assert stopped_again.task.status == "cancelled"
    manifest_path = stopped_again.task.asset_discovery.task_dir / "recovery-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["completed"]["phase"] is True

    # 第三次同配置恢复：无幻影漂移（摘要已刷新为第二次的配置）。
    resumed = run_postprocess_task(
        _make_task(source, run_dir / "out3.srt", changed),
        gateway=_ScriptedGateway([_split_script()]),
        assets=FilesystemAssetStore(),
    )
    assert resumed.succeeded
    assert resumed.recovery_summary is not None
    assert resumed.recovery_summary.configuration_drift == ()


# ---------------------------------------------------------------------------
# 验收 2：QA 报告与后处理状态带恢复来源；不中断运行不带。
# ---------------------------------------------------------------------------


def _resumed_with_qa(tmp_path: Path, name: str = "qa", *, changed_config=None):
    """一次「中断 → 恢复（qa_report 开）」的双跑，返回恢复运行结果。"""
    source, task_dir = _interrupt_once(tmp_path, name)
    resumed = run_postprocess_task(
        _make_task(source, source.parent / "out2.srt", changed_config or _repair_config(qa_report=True)),
        gateway=_ScriptedGateway([_split_script()]),
        assets=FilesystemAssetStore(),
    )
    assert resumed.succeeded
    return source, resumed


def test_qa_report_contains_recovery_section_with_drift(tmp_path):
    """恢复运行的 QA 报告：恢复来源一节含来源、复用级别与受漂移影响标注。"""
    source, resumed = _resumed_with_qa(tmp_path)
    task_dir = resumed.task.asset_discovery.task_dir
    qa = (task_dir / "qa-report.md").read_text(encoding="utf-8")
    assert "## 恢复来源" in qa
    assert "检查点时间" in qa
    assert "阶段末检查点 1" in qa
    # 漂移项（换配置：后处理配置方案漂移）与受影响成果标注各在。
    assert "后处理配置方案" in qa
    assert "阶段末结果" in qa and "旧的后处理配置方案" in qa


def test_postprocess_state_contains_recovery_and_drift(tmp_path):
    """恢复运行的后处理状态：含恢复来源与漂移清单；manifest 同带一份。"""
    source, resumed = _resumed_with_qa(tmp_path, "state")
    files = _task_dir_files(resumed.task.asset_discovery.task_dir)
    recovery = files["state"]["recovery"]
    assert recovery["checkpoint_time"]
    # 恢复时已完成：阶段末装载（轮末检查点无——中断发生在阶段末后、轮末前）。
    assert recovery["completed"] == {"phase": 1, "rounds": 0}
    assert recovery["configuration_drift"]
    assert recovery["drifted_keys"] == ["postprocess_profile"]
    assert files["manifest"]["postprocess_recovery"] == recovery


def test_uninterrupted_run_has_no_recovery_in_state_manifest_qa(tmp_path):
    """不中断运行：QA 报告无恢复来源节，状态与 manifest 不带该键。"""
    run_dir = tmp_path / "fresh"
    source = run_dir / "input.srt"
    _write_srt(source, ((_LONG_TEXT, "短"),))
    result = run_postprocess_task(
        _make_task(source, run_dir / "out.srt", _repair_config(qa_report=True)),
        gateway=_ScriptedGateway([_split_script()]),
        assets=FilesystemAssetStore(),
    )
    assert result.succeeded
    assert result.recovery_summary is None
    files = _task_dir_files(result.task.asset_discovery.task_dir)
    assert "recovery" not in files["state"]
    assert "postprocess_recovery" not in files["manifest"]
    qa = (result.task.asset_discovery.task_dir / "qa-report.md").read_text(encoding="utf-8")
    assert "恢复来源" not in qa


# ---------------------------------------------------------------------------
# 验收 3：manifest 只存方案冻结值与提示词的哈希，不存 prompt 文本或方案完整值。
# ---------------------------------------------------------------------------


def test_manifest_stores_hashes_not_texts_or_full_config(tmp_path):
    source, task_dir = _interrupt_once(tmp_path, "hash")
    manifest = json.loads((task_dir / "recovery-manifest.json").read_text(encoding="utf-8"))
    json.dumps(manifest, ensure_ascii=False)
    fingerprint = manifest["config_fingerprint"]
    assert fingerprint["main_prompt"].startswith("sha256:")
    assert fingerprint["review_prompt"].startswith("sha256:")
    assert fingerprint["postprocess_profile"]["config_hash"].startswith("sha256:")
    # 提示词原文（快照侧带真实提示词的中断运行）与连接机密都不落盘。
    from videocaptioner.core.postprocess.translation import TranslationExecutionSnapshot


    task = _make_task(source, source.parent / "out3.srt", _repair_config())
    task.bind_translation_snapshot(
        TranslationExecutionSnapshot(
            method="enhanced_llm",
            main_prompt="MAIN SECRET PROMPT TEXT",
            review_prompt="REVIEW SECRET PROMPT TEXT",
            source_language="zh",
            target_language="en",
        )
    )
    # 增强 LLM 快照无 review profile → report_only 流程仍写阶段末检查点后取消。
    stopped = run_postprocess_task(
        task,
        gateway=None,
        assets=FilesystemAssetStore(),
        cancelled=_CancelAfterEarly(),
    )
    assert stopped.task.status == "cancelled"
    manifest2 = json.loads(
        (stopped.task.asset_discovery.task_dir / "recovery-manifest.json").read_text(encoding="utf-8")
    )
    text2 = json.dumps(manifest2, ensure_ascii=False)
    fp2 = manifest2["config_fingerprint"]
    assert fp2["main_prompt"].startswith("sha256:")
    assert fp2["review_prompt"].startswith("sha256:")
    assert "MAIN SECRET PROMPT TEXT" not in text2
    assert "REVIEW SECRET PROMPT TEXT" not in text2
    # 方案冻结值不进 manifest：config_hash 是整方案 payload 的哈希。
    assert "trim_trailing_punct" not in text2
    assert "secret" not in text2  # _profile() 的 api_key


class _CancelAfterEarly:
    """前两次调用 False（早期取消检查 / with 块首检查），之后 True（交付前）。"""

    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> bool:
        self.count += 1
        return self.count > 2


# ---------------------------------------------------------------------------
# 验收 4：阶段末检查点在配置漂移下仍可继续；漂移明确指出阶段末来自旧方案。
# ---------------------------------------------------------------------------


def test_phase_checkpoint_continues_under_config_drift_with_annotation(tmp_path):
    """阶段末检查点在配置漂移下仍可继续，QA 报告指出阶段末结果来自旧方案。"""
    source, task_dir = _interrupt_once(tmp_path, "drift-continue")
    decisions = []

    def decision(summary):
        decisions.append(summary)
        return RecoveryDecision.CONTINUE

    gateway = _ScriptedGateway([_split_script()])
    resumed = run_postprocess_task(
        _make_task(source, source.parent / "out2.srt", _repair_config(qa_report=True)),
        gateway=gateway,
        assets=FilesystemAssetStore(),
        recovery_decision=decision,
    )
    assert resumed.succeeded
    # 漂移下仍继续：决策回调收到漂移清单并返回继续；检查点未被作废。
    assert len(decisions) == 1
    assert decisions[0].completed["phase"] == 1
    # 阶段零重跑（漂移不作废检查点）：网关只收到修复请求。
    assert len(gateway.requests) == 1
    qa = (resumed.task.asset_discovery.task_dir / "qa-report.md").read_text(encoding="utf-8")
    assert "阶段末结果" in qa
    assert "旧的后处理配置方案" in qa


# ---------------------------------------------------------------------------
# 验收 5：复用 05 的漂移比对与恢复摘要类型，不出现第二套漂移实现。
# ---------------------------------------------------------------------------


def test_drift_implementation_is_shared_not_duplicated():
    """漂移比对与恢复摘要走共享实现：checkpoint 模块引用 recovery.config_drift。"""
    import inspect

    from videocaptioner.core.postprocess import checkpoint as checkpoint_module
    from videocaptioner.core.postprocess.report import QualityReport
    from videocaptioner.core.recovery import (
        RecoveryProvenance,
        RecoverySummary,
        config_drift,
        drifted_keys,
    )

    source = inspect.getsource(checkpoint_module)
    # 共享实现被引用（而非本地重写比对逻辑）。
    assert "config_drift(" in source
    assert "def _config_drift" not in source
    # 恢复摘要 / 溯源类型来自共享模块：build_recovery_summary 产出共享
    # RecoverySummary；QualityReport.recovery 消费共享 RecoveryProvenance。
    summary = checkpoint_module.build_recovery_summary(
        manifest={"updated_at": "2026-09-17T00:00:00Z", "config_fingerprint": None},
        identity={"input_fingerprint": "x"},
        rounds=2,
        phase_completed=True,
    )
    assert isinstance(summary, RecoverySummary)
    assert summary.module == "subtitle_postprocess"
    provenance = RecoveryProvenance(checkpoint_time="t", completed={"phase": 1})
    report = QualityReport()
    report.recovery = provenance
    assert isinstance(report.recovery, RecoveryProvenance)
    assert callable(config_drift) and callable(drifted_keys)
