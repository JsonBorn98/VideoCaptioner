"""后处理阶段末 / 轮末恢复检查点（票 06，ADR-0022）：只验证外部行为。

按 spec「Testing Decisions」主 seam B：注入初版字幕任务、脚本化假网关、
临时目录上的文件系统资产库、取消函数、可选恢复决定回调；断言哪些阶段
发了请求、发了多少、过程资产目录里落了什么、最终成果是否与不中断运行
等价——不断言内部函数调用顺序或私有状态。
"""

from __future__ import annotations

import json
from pathlib import Path

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.postprocess.checkpoint import round_checkpoint_payload
from videocaptioner.core.postprocess.config import PostprocessConfig
from videocaptioner.core.postprocess.models import PostprocessLayoutMode, PostprocessTask
from videocaptioner.core.postprocess.repair import RepairSummary
from videocaptioner.core.postprocess.runner import run_postprocess_task
from videocaptioner.core.postprocess.workspace import FilesystemAssetStore
from videocaptioner.core.recovery import (
    RecoveryDecision,
    atomic_write_json,
    write_recovery_manifest,
)

from .test_repair_execution import _config, _profile, _repairs_for, _response, _ScriptedGateway

# 拆分输入：折算 60 字 > 绝对上限 20 → 一个观看长度问题；chunk=20 拆成 3 片。
_LONG_TEXT = "超长" * 30


def _asr_data(pairs: tuple[tuple[str, str], ...]) -> ASRData:
    return ASRData(
        [
            ASRDataSeg(text, index * 4000, index * 4000 + 4000, translated)
            for index, (text, translated) in enumerate(pairs)
        ]
    )


def _repair_summary(**overrides) -> RepairSummary:
    summary = RepairSummary()
    for key, value in overrides.items():
        setattr(summary, key, value)
    return summary


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


def _repair_config() -> PostprocessConfig:
    return _config(
        remove_placeholders=False,
        normalize_quotes=False,
        fix_gaps=False,
        tail_compensation=False,
        compress_fast_subtitles=False,
        speed_optimize=False,
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
    )


def _split_script() -> str:
    """对单段超长输入的合法拆分脚本（60 字 → 3 片 × 20 字）。"""
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


class _CountingGateway(_ScriptedGateway):
    """按请求用途分别计数：压缩重译（阶段请求）vs 观看修复（修复请求）。"""

    def __init__(self, scripts):
        super().__init__(list(scripts))
        self.compress_requests = 0
        self.repair_requests = 0

    def complete(self, profile, request, *, cancelled=None):
        user = next(m.content for m in request.messages if m.role == "user")
        if user.startswith("Compress the following subtitles"):
            self.compress_requests += 1
        else:
            self.repair_requests += 1
        return super().complete(profile, request, cancelled=cancelled)


def test_phase_resume_does_not_replay_compress_requests(tmp_path):
    """验收 2：阶段末中断后恢复不重跑压缩重译请求（阶段请求零重发）。

    输入两段：段 0（4 秒 / 60 字）超速 → 压缩重译压到 20 字且不再触发
    观看修复；段 1（8 秒 / 60 字）不超速不进压缩，但超观看绝对上限 →
    修复轮的拆分对象。压缩重译在 ``run_post_stage`` 内发生——阶段末
    检查点在它之后写入；恢复装载阶段末时跳过整个 post stage，
    压缩请求不重发。
    """

    def _two_segment_srt(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "1",
            f"{_ts(0)} --> {_ts(4000)}",
            f"{_LONG_TEXT}\n短甲",
            "",
            "2",
            f"{_ts(4000)} --> {_ts(12000)}",
            f"{_LONG_TEXT}\n短乙",
            "",
        ]
        path.write_text("\n".join(lines), encoding="utf-8")

    # 修复脚本：只拆段 1（problem_id length:original:1；段 0 已被压缩修好）。
    def _repair_script() -> str:
        return _response(
            _repairs_for(
                {
                    "repair_subjects": [
                        {
                            "segments": [
                                {
                                    "id": 1,
                                    "text": _LONG_TEXT,
                                    "translated": "短乙",
                                    "problem_ids": ["length:original:1"],
                                }
                            ]
                        }
                    ]
                },
                chunk=20,
            )
        )

    # 压缩响应：段 0 → 20 字（≤ target floor(4×11)=44；相似度 0.5）。
    compressed = json.dumps({"1": "超长" * 10}, ensure_ascii=False)

    baseline_dir = tmp_path / "baseline"
    baseline_source = baseline_dir / "baseline.srt"
    _two_segment_srt(baseline_source)
    config = _repair_config()
    config.compress_fast_subtitles = True
    baseline_gateway = _CountingGateway([compressed, _repair_script()])
    baseline = run_postprocess_task(
        _make_task(baseline_source, baseline_dir / "out.srt", config),
        gateway=baseline_gateway,
        assets=FilesystemAssetStore(),
    )
    assert baseline.succeeded
    assert baseline_gateway.compress_requests == 1  # 阶段请求发生过一次
    assert baseline_gateway.repair_requests == 1

    # 中断：压缩完成后（阶段末检查点落盘），修复请求前停止。
    run_dir = tmp_path / "run"
    source = run_dir / "input.srt"
    _two_segment_srt(source)
    run_config = _repair_config()
    run_config.compress_fast_subtitles = True
    stopped = run_postprocess_task(
        _make_task(source, run_dir / "out.srt", run_config),
        gateway=_InterruptingGateway([compressed, _repair_script()], stop_at=2),
        assets=FilesystemAssetStore(),
    )
    assert stopped.task.status == "cancelled"
    assert not (run_dir / "out.srt").exists()
    task_dir = stopped.task.asset_discovery.task_dir
    assert (task_dir / "recovery-phase.json").is_file()

    # 恢复：阶段末装载，压缩请求零重发；修复从阶段末重跑。
    resumed_config = _repair_config()
    resumed_config.compress_fast_subtitles = True
    resumed_gateway = _CountingGateway([_repair_script()])
    resumed = run_postprocess_task(
        _make_task(source, run_dir / "out2.srt", resumed_config),
        gateway=resumed_gateway,
        assets=FilesystemAssetStore(),
    )
    assert resumed.succeeded
    assert resumed.recovery_summary is not None
    # 关键断言：压缩重译零重发（若阶段重跑，这里会是 1）。
    assert resumed_gateway.compress_requests == 0
    assert resumed_gateway.repair_requests == 1
    # 等价：与不中断运行的最终字幕一致。
    assert [(seg.text, seg.translated_text) for seg in resumed.output_data.segments] == [
        (seg.text, seg.translated_text) for seg in baseline.output_data.segments
    ]
    # 段 0 的压缩成果保留在恢复后的工作字幕里（阶段末装载，非重压缩）。
    assert resumed.output_data.segments[0].text == "超长" * 10


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


def test_round_level_resume_continues_attempt_counts(tmp_path):
    """轮末检查点恢复：尝试计数延续，业务重试上限以恢复的计数继续。

    剧本：第 1 轮响应空 repairs（无候选 → 轮末检查点落盘，attempts=1）；
    第 2 轮请求被主动停止。恢复重跑：attempts 从 1 延续，再发 4 次请求
    （第 2-5 次）即触发耗尽回退——若计数被重置，会重发满 5 次。
    不中断基准：连续 5 次空响应后耗尽回退，回退记录一致。
    """
    empty = _response([])

    # 基准：不中断——5 次请求（1 + 4 次业务重试）后耗尽回退。
    baseline_dir = tmp_path / "baseline"
    baseline_source = baseline_dir / "input.srt"
    _write_srt(baseline_source, ((_LONG_TEXT, "短"),))
    baseline = run_postprocess_task(
        _make_task(baseline_source, baseline_dir / "out.srt", _repair_config()),
        gateway=_ScriptedGateway([empty] * 5),
        assets=FilesystemAssetStore(),
    )
    assert baseline.succeeded
    assert baseline.report.viewing_repair.requests == 5
    assert baseline.report.viewing_repair.rollbacks  # 耗尽回退
    assert [(r.reason) for r in baseline.report.viewing_repair.rollbacks] == ["业务修复重试耗尽"]

    # 中断：第 1 轮空响应（轮末检查点：attempts=1，rounds=1），第 2 轮停止。
    run_dir = tmp_path / "run"
    source = run_dir / "input.srt"
    _write_srt(source, ((_LONG_TEXT, "短"),))
    stopped = run_postprocess_task(
        _make_task(source, run_dir / "out.srt", _repair_config()),
        gateway=_InterruptingGateway([empty, empty, empty, empty, empty], stop_at=2),
        assets=FilesystemAssetStore(),
    )
    assert stopped.task.status == "cancelled"
    assert stopped.continue_downstream is False
    assert not (run_dir / "out.srt").exists()  # 停止不交付部分成果
    task_dir = stopped.task.asset_discovery.task_dir
    manifest = json.loads((task_dir / "recovery-manifest.json").read_text(encoding="utf-8"))
    assert manifest["interruption"] == "stopped"
    assert manifest["completed"] == {"phase": True, "rounds": 1}
    round_checkpoint = json.loads((task_dir / "recovery-round.json").read_text(encoding="utf-8"))
    assert round_checkpoint["summary"]["rounds"] == 1
    assert round_checkpoint["attempts"] == {"0|original|length": 1}

    # 恢复：attempts 延续 → 只再发 4 次请求（2-5 次）即耗尽回退。
    resumed_gateway = _ScriptedGateway([empty] * 5)
    resumed = run_postprocess_task(
        _make_task(source, run_dir / "out2.srt", _repair_config()),
        gateway=resumed_gateway,
        assets=FilesystemAssetStore(),
    )
    assert resumed.succeeded
    assert resumed.recovery_summary is not None
    assert resumed.recovery_summary.completed["rounds"] == 1
    # 关键断言：总请求计数延续（1 次中断前 + 4 次恢复后 = 5 = 基准值）。
    assert resumed.report.viewing_repair.requests == 5
    # 恢复运行只发 4 次（脚本网关收到 4 个请求；重置则会发 5 次）。
    assert len(resumed_gateway.requests) == 4
    # 回退记录延续 + 恢复后新增耗尽回退，与基准一致。
    assert resumed.report.viewing_repair.rollbacks[-1].reason == "业务修复重试耗尽"
    # 等价：最终字幕与基准一致（回退到初版快照）。
    assert [(seg.text, seg.translated_text) for seg in resumed.output_data.segments] == [
        (seg.text, seg.translated_text) for seg in baseline.output_data.segments
    ]


def test_success_clears_recovery_files_and_delists(tmp_path):
    run_dir = tmp_path / "run"
    source = run_dir / "input.srt"
    _write_srt(source, ((_LONG_TEXT, "短"),))
    result = run_postprocess_task(
        _make_task(source, run_dir / "out.srt", _repair_config()),
        gateway=_ScriptedGateway([_split_script()]),
        assets=FilesystemAssetStore(),
    )
    assert result.succeeded
    task_dir = result.task.asset_discovery.task_dir
    assert not (task_dir / "recovery-manifest.json").is_file()
    assert not (task_dir / "recovery-phase.json").is_file()
    assert not (task_dir / "recovery-round.json").is_file()
    manifest = json.loads((task_dir / "manifest.json").read_text(encoding="utf-8"))
    assert "recovery_manifest" not in manifest.get("assets", {})
    assert "recovery_phase" not in manifest.get("assets", {})
    assert "recovery_round" not in manifest.get("assets", {})
    assert result.recovery_summary is None  # 不中断运行为 None


# ---------------------------------------------------------------------------
# 验收 3：模块级失败保留检查点；分析模式不写；仅报告流程只写阶段末。
# ---------------------------------------------------------------------------


def test_module_failure_preserves_phase_checkpoint(tmp_path):
    run_dir = tmp_path / "run"
    source = run_dir / "input.srt"
    _write_srt(source, ((_LONG_TEXT, "短"),))
    # 模块级失败：输出路径 == 输入路径 → 保存前 ValueError → fallback。
    result = run_postprocess_task(
        _make_task(source, source, _repair_config()),
        gateway=_ScriptedGateway([_split_script()]),
        assets=FilesystemAssetStore(),
    )
    assert result.task.status == "fallback"
    assert result.continue_downstream is False
    task_dir = result.task.asset_discovery.task_dir
    assert (task_dir / "recovery-manifest.json").is_file()
    assert (task_dir / "recovery-phase.json").is_file()
    manifest = json.loads((task_dir / "recovery-manifest.json").read_text(encoding="utf-8"))
    assert manifest["interruption"] == "failure"
    assert manifest["completed"]["phase"] is True
    workspace_manifest = json.loads((task_dir / "manifest.json").read_text(encoding="utf-8"))
    assert workspace_manifest["assets"].get("recovery_manifest") == "recovery-manifest.json"
    assert workspace_manifest["assets"].get("recovery_phase") == "recovery-phase.json"


def test_analyze_mode_writes_no_recovery_files(tmp_path):
    run_dir = tmp_path / "run"
    source = run_dir / "input.srt"
    _write_srt(source, ((_LONG_TEXT, "短"),))
    config = _repair_config()
    config.speed_mode = "analyze"
    result = run_postprocess_task(
        _make_task(source, run_dir / "out.srt", config),
        gateway=_ScriptedGateway([]),
        assets=FilesystemAssetStore(),
    )
    assert result.succeeded
    task_dir = result.task.asset_discovery.task_dir
    assert not (task_dir / "recovery-manifest.json").is_file()
    assert not (task_dir / "recovery-phase.json").is_file()
    assert not (task_dir / "recovery-round.json").is_file()


# ---------------------------------------------------------------------------
# 验收 5：身份不匹配不发现；损坏 manifest 告警并从头；从头开始重置。
# ---------------------------------------------------------------------------


def _fail_once(tmp_path: Path, name: str = "run"):
    """跑一次「主动停止」的运行，返回 (source_path, config, task_dir)。"""
    run_dir = tmp_path / name
    source = run_dir / "input.srt"
    _write_srt(source, ((_LONG_TEXT, "短"),))
    config = _repair_config()
    result = run_postprocess_task(
        _make_task(source, run_dir / "out.srt", config),
        gateway=_InterruptingGateway([_split_script()], stop_at=1),
        assets=FilesystemAssetStore(),
    )
    assert result.task.status == "cancelled"
    return source, config, result.task.asset_discovery.task_dir


# ---------------------------------------------------------------------------
# 验收 5：身份不匹配不发现；损坏 manifest 告警并从头；从头开始重置。
# ---------------------------------------------------------------------------


def test_different_fingerprint_does_not_resume(tmp_path):
    source, config, _task_dir = _fail_once(tmp_path)
    # 同目录不同输入（输入指纹不同）：不发现检查点，回调不被调用。
    other = source.parent / "other.srt"
    _write_srt(other, (("完全不同的输入文本", "短"),))
    decisions = []

    def decision(summary):
        decisions.append(summary)
        return RecoveryDecision.CONTINUE

    result = run_postprocess_task(
        _make_task(other, other.parent / "out2.srt", _repair_config()),
        gateway=_ScriptedGateway([]),
        assets=FilesystemAssetStore(),
        recovery_decision=decision,
    )
    assert result.succeeded
    assert decisions == []
    assert result.recovery_summary is None


def test_report_only_flow_writes_phase_checkpoint_but_no_round(tmp_path):
    """仅报告流程（无修复轮次）只写阶段末检查点：中断后盘上无轮末文件。"""
    run_dir = tmp_path / "run"
    source = run_dir / "input.srt"
    _write_srt(source, ((_LONG_TEXT, "短"),))
    config = _repair_config()
    task = _make_task(source, run_dir / "out.srt", config)
    task.config_snapshot.utility_llm_profile = None  # 无角色 → report_only
    result = run_postprocess_task(
        task,
        gateway=None,
        assets=FilesystemAssetStore(),
        # 交付前复查取消：处理完成、交付提交前停止 → 检查点保留。
        cancelled=_CancelAfterDiscovery(),
    )
    assert result.task.status == "cancelled"
    task_dir = result.task.asset_discovery.task_dir
    manifest = json.loads((task_dir / "recovery-manifest.json").read_text(encoding="utf-8"))
    assert (task_dir / "recovery-phase.json").is_file()  # 阶段末写入
    assert not (task_dir / "recovery-round.json").is_file()  # 无轮末
    assert manifest["completed"] == {"phase": True, "rounds": 0}


class _CancelAfterDiscovery:
    """前两次调用（早期取消检查、with 块首检查）False，之后 True。

    第三次到达的取消检查点在阶段末检查点写入之后（交付前复查），
    用于验证仅报告流程只写阶段末、不写轮末。
    """

    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> bool:
        self.count += 1
        return self.count > 2


def test_corrupted_manifest_warns_and_starts_fresh(tmp_path):
    source, _config_used, task_dir = _fail_once(tmp_path)
    (task_dir / "recovery-manifest.json").write_text("{ broken json", encoding="utf-8")
    result = run_postprocess_task(
        _make_task(source, source.parent / "out3.srt", _repair_config()),
        gateway=_ScriptedGateway([_split_script()]),
        assets=FilesystemAssetStore(),
    )
    assert result.succeeded
    assert any("不可用" in warning for warning in result.warnings)


def test_phase_rewrite_resets_rounds_and_deletes_stale_round_checkpoint(tmp_path):
    """重写阶段末 = 轮换修复循环基准：rounds 清零、旧轮末文件删除（修复回审）。

    剧本：manifest 登记轮末（rounds=1）但轮末文件损坏 → 该级视为未完成
    并告警 → 从阶段末重跑；重跑到新阶段末时，旧轮末状态属于上一份
    工作字幕，不得静默配到新基准——rounds 清零、旧文件删除。
    """
    source, _config_used, task_dir = _fail_once(tmp_path, "phase-reset")
    # manifest 登记轮末完成、轮末文件完好，但阶段末文件损坏（崩溃残留，
    # R06）——重跑会从阶段末重来：旧轮末属于上一份工作字幕，配不上
    # 新阶段末基准，重写阶段末时必须清零 rounds 并删旧轮末文件。
    round_payload = round_checkpoint_payload(
        working=_asr_data(((_LONG_TEXT, "短"),)),
        snapshot=_asr_data(((_LONG_TEXT, "短"),)),
        origin=[0],
        summary=_repair_summary(rounds=1, requests=1),
        closed_regions=set(),
        attempts={(0, "original", "length"): 1},
        last_error={},
        last_subject={},
        accepted=set(),
        candidate_fps={},
        state_fps={},
        transport_streak=0,
        viewing_problems=[],
    )
    atomic_write_json(task_dir / "recovery-round.json", round_payload)
    (task_dir / "recovery-phase.json").write_text("{ broken phase", encoding="utf-8")
    manifest = json.loads((task_dir / "recovery-manifest.json").read_text(encoding="utf-8"))
    manifest["completed"] = {"phase": True, "rounds": 1}
    write_recovery_manifest(task_dir / "recovery-manifest.json", manifest)
    assert (task_dir / "recovery-round.json").is_file()

    # 重跑：阶段末不可用 → 告警并从阶段末重跑；新阶段末写入时清零
    # rounds 并删除旧轮末（不把上一轮的计数配到新基准上）。
    stopped = run_postprocess_task(
        _make_task(source, source.parent / "out6.srt", _repair_config()),
        gateway=_InterruptingGateway([_split_script()], stop_at=1),
        assets=FilesystemAssetStore(),
    )
    assert stopped.task.status == "cancelled"
    assert any("阶段末检查点不可用" in warning for warning in stopped.warnings)
    task_dir_after = stopped.task.asset_discovery.task_dir
    manifest_after = json.loads(
        (task_dir_after / "recovery-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest_after["completed"] == {"phase": True, "rounds": 0}
    assert not (task_dir_after / "recovery-round.json").is_file()


def test_recovery_decision_start_fresh_resets_manifest(tmp_path):
    source, _config_used, _task_dir = _fail_once(tmp_path)
    seen = []

    def start_fresh(summary):
        seen.append(summary)
        assert summary.completed["phase"] == 1
        return RecoveryDecision.START_FRESH

    result = run_postprocess_task(
        _make_task(source, source.parent / "out4.srt", _repair_config()),
        gateway=_ScriptedGateway([_split_script()]),
        assets=FilesystemAssetStore(),
        recovery_decision=start_fresh,
    )
    assert result.succeeded
    assert len(seen) == 1  # 决策回调恰好调用一次
    assert result.recovery_summary is None  # 从头开始不算恢复运行
    task_dir = result.task.asset_discovery.task_dir
    assert not (task_dir / "recovery-manifest.json").is_file()  # 成功后清理


# ---------------------------------------------------------------------------
# 验收 6：修复循环无新增磁盘 I/O；阶段摘要含恢复行。
# ---------------------------------------------------------------------------


def test_repair_module_has_no_direct_disk_writes():
    """修复循环模块自身不含磁盘写调用（落盘经运行器与资产库）。"""
    import inspect

    from videocaptioner.core.postprocess import repair

    source = inspect.getsource(repair)
    for forbidden in (
        "atomic_write_json",
        "write_recovery_manifest",
        "write_text(",
        "NamedTemporaryFile",
    ):
        assert forbidden not in source, forbidden


def test_stage_summary_contains_resume_line(tmp_path):
    from videocaptioner.core.postprocess.summary import build_postprocess_stage_summary
    from videocaptioner.core.utils.stage_summary import format_stage_summary

    source, _config_used, _task_dir = _fail_once(tmp_path, "resume-line")
    resumed = run_postprocess_task(
        _make_task(source, source.parent / "out5.srt", _repair_config()),
        gateway=_ScriptedGateway([_split_script()]),
        assets=FilesystemAssetStore(),
    )
    assert resumed.succeeded
    assert resumed.recovery_summary is not None
    summary = build_postprocess_stage_summary(resumed)
    line = format_stage_summary(summary)
    assert (
        "从恢复检查点继续" in line
    )  # ---------------------------------------------------------------------------


# 验收 1：第 N 轮归并后中断重跑——前 N 轮零请求，成果与不中断运行等价。
# ---------------------------------------------------------------------------


def test_round_resume_after_failure_saves_requests_and_is_equivalent(tmp_path):
    """阶段末检查点恢复：修复阶段外的请求零重发，成果与不中断运行等价。"""
    baseline_dir = tmp_path / "baseline"
    baseline_source = baseline_dir / "input.srt"
    _write_srt(baseline_source, ((_LONG_TEXT, "短"),))
    baseline = run_postprocess_task(
        _make_task(baseline_source, baseline_dir / "out.srt", _repair_config()),
        gateway=_ScriptedGateway([_split_script()]),
        assets=FilesystemAssetStore(),
    )
    assert baseline.succeeded
    assert len(baseline.output_data.segments) == 3  # 60 字 → 3 片

    # 中断：同一输入，修复请求前停止（阶段末检查点已落盘、无轮末检查点）。
    fail_dir = tmp_path / "interrupted"
    fail_source = fail_dir / "input.srt"
    _write_srt(fail_source, ((_LONG_TEXT, "短"),))
    stopped = run_postprocess_task(
        _make_task(fail_source, fail_dir / "out.srt", _repair_config()),
        gateway=_InterruptingGateway([_split_script()], stop_at=1),
        assets=FilesystemAssetStore(),
    )
    assert stopped.task.status == "cancelled"
    assert stopped.continue_downstream is False
    assert not (fail_dir / "out.srt").exists()  # 停止不交付部分成果
    fail_task_dir = stopped.task.asset_discovery.task_dir
    assert (fail_task_dir / "recovery-phase.json").is_file()
    assert not (fail_task_dir / "recovery-round.json").is_file()  # 尚无轮末

    # 恢复重跑：阶段末检查点装载，后处理阶段不重跑；修复循环从阶段末
    # 重跑（尚无轮末检查点）——网关只收到修复请求，且阶段请求零重发。
    resumed = run_postprocess_task(
        _make_task(fail_source, fail_dir / "out2.srt", _repair_config()),
        gateway=_ScriptedGateway([_split_script()]),
        assets=FilesystemAssetStore(),
    )
    assert resumed.succeeded
    assert resumed.recovery_summary is not None
    assert resumed.recovery_summary.completed["phase"] == 1
    # 等价性：最终后处理字幕与不中断运行一致（同样的拆分 → 同样的片段）。
    assert [(seg.text, seg.translated_text) for seg in resumed.output_data.segments] == [
        (seg.text, seg.translated_text) for seg in baseline.output_data.segments
    ]
    assert resumed.report.segment_count == baseline.report.segment_count
    assert resumed.report.viewing_repair.rounds == baseline.report.viewing_repair.rounds
    assert resumed.report.viewing_repair.requests == baseline.report.viewing_repair.requests
