"""Offscreen Qt 呈现基线（票 01 UI probe）：完整任务 + 受控网关 + 主动停止。

接缝：真实 ``PostprocessThread``（QThread）→ 真实 ``run_postprocess_task``
修复路径 → 受控网关（真实 ``LLMGateway`` 语义 + 离线合成 adapter）；
offscreen QApplication 事件循环驱动，事件屏障等待首个主修复请求开始后
请求停止。探测的是 QThread 信号到 GUI 事件循环的呈现（presentation），
不代表 widget 刷新；probe 报告 ``presentation_scope`` 有准确标注。

AppData 由调用方隔离（子进程环境变量）；禁止真实 provider（离线 socket
守卫 + 合成 adapter）。子进程包裹限制总运行与清理。

现状缺陷按 ``baseline_gaps`` 分类记录（facts，不设窄时间窗断言阻止优化票；
取消/静默治理票落地时更新分类与门槛，数值只进验收矩阵）：
- 停止无法抢占在途请求，终态等待在途请求自然返回（P05 取消缺口）。
- 在途请求期间没有任何进度信号（「正在修复观看问题（第 1 轮…）」的量化）。
硬断言只保留交付门禁：输入保护、停止不交付部分成果、活动字幕回退初版、
下游阻断、线程退出、终态信号正确、无 watchdog/错误泄漏。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_PROBE_TIMEOUT_SECONDS = 120.0


def _probe_env(tmp_path: Path) -> dict:
    """子进程环境：隔离 AppData（调用方设置）+ offscreen Qt + UTF-8 stdio。

    Windows 默认 GBK：中文路径 / 日志经 ``text=True``（locale 解码）会
    ``UnicodeDecodeError``；显式 ``encoding='utf-8', errors='replace'`` 与
    ``PYTHONIOENCODING=utf-8`` 配对，probe 主程序输出本就全 UTF-8。
    """
    env = os.environ.copy()
    env["VIDEOCAPTIONER_APPDATA_PATH"] = str((tmp_path / "appdata").resolve())
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run_python(
    code: str, tmp_path: Path, *, drop_appdata: bool = False
) -> subprocess.CompletedProcess:
    """在受限于总超时的子进程里执行探针代码（Qt 状态不进入 pytest 进程）。"""
    env = _probe_env(tmp_path)
    if drop_appdata:
        env.pop("VIDEOCAPTIONER_APPDATA_PATH", None)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=_PROBE_TIMEOUT_SECONDS,
        check=False,
    )


def _cli_env(tmp_path: Path) -> dict:
    """CLI 子进程环境：offscreen Qt + UTF-8 stdio，AppData 由 CLI 自建。"""
    env = os.environ.copy()
    env.pop("VIDEOCAPTIONER_APPDATA_PATH", None)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONIOENCODING"] = "utf-8"
    del tmp_path
    return env


def _run_cli(root: Path) -> subprocess.CompletedProcess:
    """运行 CLI 子进程：UTF-8 解码与 ``PYTHONIOENCODING`` 显式配对。"""
    return subprocess.run(
        [sys.executable, "-m", "scripts.postprocess_ui_probe", "--output", str(root)],
        cwd=str(REPO_ROOT),
        env=_cli_env(root),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=_PROBE_TIMEOUT_SECONDS,
        check=False,
    )


def _last_json_line(stdout: str) -> dict:
    lines = [line for line in stdout.strip().splitlines() if line.strip()]
    assert lines, "probe printed no output"
    return json.loads(lines[-1])


def test_stop_during_first_main_repair_measures_gui_silence_and_cancel_wait(tmp_path):
    code = (
        "import json, pathlib\n"
        "from scripts.postprocess_ui_probe import run_ui_probe\n"
        f"report = run_ui_probe(pathlib.Path({str(tmp_path / 'probe')!r}))\n"
        "print(json.dumps(report, ensure_ascii=True, default=str))\n"
    )
    completed = _run_python(code, tmp_path)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = _last_json_line(completed.stdout)

    # 基线标识与冻结条件。
    assert report["probe_id"] == "postprocess-ui-probe-v1"
    assert report["scenario"] == "stop-during-first-main-repair"
    assert "NOT widget refresh" in report["presentation_scope"]
    assert report["appdata"]["root"] == str((tmp_path / "appdata").resolve())
    assert report["input"]["segments"] == 60
    assert report["input"]["problem_segments"] == 20
    assert report["translation_snapshot"]["method"] == "enhanced_llm"
    assert "api_key" not in report["roles"]["main"]
    assert "base_url" not in report["roles"]["main"]
    assert report["real_model_validation"].startswith("not executed")
    assert report["controlled_response"]["socket"] == "none"

    # 终态、输出 / 活动字幕 / 下游门禁（取消路径的交付不变式）。
    outcome = report["outcome"]
    assert outcome["terminal_signal"] == "cancelled"
    assert outcome["task_status"] == "cancelled"
    assert outcome["hard_cap_exceeded"] is False
    assert outcome["repair_flow"]["mode"] == "main_review"  # 真实增强两段修复路径
    # 停止探针固定并发 1（票 04）：测「在途请求期间停止」的呈现与响应；
    # 修复并发（多批重叠 / 完成顺序 / 保护闸）由票 04 修复并发测试覆盖。
    assert outcome["repair_flow"]["requests"] == 1  # 恰好首个主修复请求在途
    gating = outcome["gating"]
    assert gating["source_unchanged"] is True
    assert gating["no_postprocessed_output"] is True  # 停止不交付部分后处理成果
    assert gating["active_is_initial"] is True
    assert gating["downstream_blocked"] is True  # 主动停止阻断下游
    assert gating["thread_exited"] is True
    assert gating["terminal_signal_cancelled"] is True
    assert report["gateway"]["requests_main"] == 1
    assert report["gateway"]["requests_review"] == 0

    # 现状缺陷按分类记录（不设窄时间窗断言阻止优化票；优化票升级此分类）。
    gaps = report["baseline_gaps"]
    assert any("in-flight" in gap for gap in gaps)  # 停止不能抢占在途请求（现状）
    assert any("in flight" in gap for gap in gaps)  # 请求期间无进度信号（现状）
    timeline = report["timeline"]
    assert timeline["stop_during_inflight"] is True
    assert timeline["inflight_role"] == "main"
    assert timeline["terminal_after_inflight_end"] is True
    assert timeline["stop_to_terminal_signal_s"] is not None
    assert timeline["stop_to_terminal_signal_s"] >= 0  # 数值留给矩阵，不设上限断言

    # 取消不交付过程资产（D28）：manifest 可保留，下游产物不得存在
    # （仅有 output.srt 门禁不能掩盖过程资产交付）。
    assert gating["no_delivered_process_assets"] is True
    assert report["outcome"]["delivered_process_assets"] == {
        "qa_report": False,
        "postprocess_state": False,
        "speed_changes": False,
    }

    # GUI 事件循环与真实 progress 信号静默（数值只做健康下限，不做 SLA）。
    gui = report["gui"]
    assert gui["progress_emission_count"] >= 5
    assert gui["delivery_pairing_complete"] is True
    assert gui["heartbeat"]["count"] >= 30
    # Heartbeat 单位明确为秒 / 毫秒双字段；门槛只检事件循环未被 worker
    # 阻塞（中位 ~10ms 计时器），不做 SLA。
    assert gui["heartbeat"]["median_s"] is not None
    assert gui["heartbeat"]["median_ms"] < 60
    assert gui["heartbeat"]["max_ms"] < 600
    assert gui["silent_during_inflight_request"] is True  # 现状缺陷分类来源
    assert gui["last_emission_to_terminal_s"] is not None
    assert gui["delivery_max_latency_ms"] is not None
    # 静默集中在模型请求窗口，而不是本地阶段（请求窗口 >= 1s 对照刷新目标）。
    assert report["controlled_response"]["main_delay"] >= 1.0
    assert gui["progress_max_gap_s"] is not None
    # progress 事件时间相对任务起点，不是 perf_counter 绝对值。
    assert all(
        entry["at_s_from_task_start"] >= 0
        for entry in gui["progress_events"]
    )


def test_probe_refuses_existing_directory_without_touching_it(tmp_path):
    from scripts.postprocess_ui_probe import run_ui_probe

    existing = tmp_path / "existing"
    existing.mkdir()
    marker = existing / "input.srt"
    marker.write_bytes(b"original asset")
    with pytest.raises(FileExistsError):
        run_ui_probe(existing)
    assert marker.read_bytes() == b"original asset"


def test_probe_requires_caller_isolated_appdata(tmp_path):
    code = (
        "from pathlib import Path\n"
        "from scripts.postprocess_ui_probe import run_ui_probe\n"
        "try:\n"
        f"    run_ui_probe(Path({str(tmp_path / 'probe')!r}))\n"
        "except RuntimeError as exc:\n"
        "    print('GUARD:', exc)\n"
        "else:\n"
        "    raise SystemExit('probe ran without isolated AppData')\n"
    )
    completed = _run_python(code, tmp_path, drop_appdata=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "VIDEOCAPTIONER_APPDATA_PATH" in completed.stdout


def test_cli_writes_report_and_summary_json(tmp_path):
    root = tmp_path / "report-root"
    completed = _run_cli(root)
    assert completed.returncode == 0, completed.stdout + completed.stderr

    report = json.loads((root / "report.json").read_text(encoding="utf-8"))
    assert report["probe_id"] == "postprocess-ui-probe-v1"
    assert report["outcome"]["terminal_signal"] == "cancelled"
    assert report["appdata"]["root"] == str((root / "appdata").resolve())

    summary = _last_json_line(completed.stdout)
    assert summary["probe_id"] == "postprocess-ui-probe-v1"
    assert summary["terminal_signal"] == "cancelled"
    assert summary["stop_to_terminal_signal_s"] is not None

    # 拒绝复用既有输出目录，不改动已有报告。
    before = (root / "report.json").read_bytes()
    again = _run_cli(root)
    assert again.returncode != 0
    assert (root / "report.json").read_bytes() == before


def test_cli_main_refuses_existing_output_in_process(tmp_path):
    from scripts.postprocess_ui_probe import main

    existing = tmp_path / "existing"
    existing.mkdir()
    marker = existing / "report.json"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        main(["--output", str(existing)])
    assert marker.read_text(encoding="utf-8") == "keep"
