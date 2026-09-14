"""真实传输边界探针回归（票 01：后处理性能基准 · 传输探针）。

按 spec「测试决策 · 真实传输边界」：真实 ``LLMGateway`` +
``OpenAICompatibleAdapter``（真实 openai SDK / httpx / loopback TCP）打到
受控 loopback HTTP 服务，触发取消并测量停止响应。假网关证明不了 HTTP
在途取消、adapter 级重试与信号量等待，只有真实传输能回答。

断言口径（与探针模块一致）：
- ``adapter_attempts``：``adapter.complete`` 调用数（网关请求日志证据）。
- ``http_attempts``：服务端实收 HTTP 请求数；两者差 = SDK 隐式重试乘积。
- 取消锚定在真实进入等待阶段的证据上（在途请求 / 网关退避开始），
  不靠 sleep 猜时序；探针揭示的「不及时取消」是 observed baseline gap，
  不是基准设施失败。票 06 改变行为时同步更新这些基线断言与验收矩阵。
- 清理有界且被证明：join 以 ``is_alive`` 验证，``gateway.close()`` 必调，
  release 后 handler 必须结束作答；清理失败是设施失败（ok=False）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.postprocess_transport_probe import (  # noqa: E402
    PROBE_KINDS,
    PROBE_SCHEMA,
    run_probe,
)


def _by_request(report):
    return {entry["request"]: entry for entry in report["requests"]}


# ---- 公共入口与 schema ----


def test_run_probe_rejects_unknown_kind():
    with pytest.raises(ValueError, match="kind"):
        run_probe("not-a-probe")


@pytest.mark.parametrize("kind", PROBE_KINDS)
def test_probe_report_schema_and_bounded_cleanup(kind):
    report = run_probe(kind)

    assert report["schema"] == PROBE_SCHEMA
    assert report["probe"] == kind
    # 基准设施自身不出错；揭示的缺口进 baseline_gaps，不影响 ok。
    assert report["ok"] is True
    assert report["errors"] == []
    assert report["logical_requests"] >= 1
    # 服务端证据 + 网关请求日志证据都在，取消锚定在真实进入事件上。
    assert report["http_attempts"] >= 1
    assert report["http_attempt_log"]
    assert report["adapter_attempts"] >= 1
    assert report["wait_entry_evidence"]["entry_observed"] is True
    assert report["stop"]["cancel_at_s"] is not None
    assert isinstance(report["stop"]["prompt"], bool)
    assert report["forced_release"]["reason"]
    # 受控服务永不自然返回：hold 应答只来自显式释放（或兜底 cap）。
    assert report["responses"]["service_natural_returns"] == 0
    # 挂起守卫（有界清理），不是性能门槛。
    assert 0 < report["duration_seconds"] < 45
    for entry in report["requests"]:
        assert entry["call_started_at_s"] >= 0.0
        assert entry["ended_at_s"] is not None
        assert entry["ended_at_s"] > entry["call_started_at_s"]
        assert entry["outcome"] in {"success", "interrupted", "error"}
    # 清理被证明而非假设：server 线程退出、handler 作答完毕、worker 全退出。
    cleanup = report["cleanup"]
    assert cleanup["server_thread_exited"] is True
    assert cleanup["handler_answers_drained"] is True
    assert all(item["joined"] is True for item in cleanup["worker_joins"])


# ---- queue：并发闸排队 + 两个真实在途挂起（大 timeout + 计划释放）----


def test_queue_probe_gate_caps_inflight_and_cancel_is_prompt():
    report = run_probe("queue")

    # 真实并发闸生效：4 个逻辑请求、闸 2 → 服务端只观察到 2 个在途 HTTP。
    assert report["logical_requests"] == 4
    assert report["http_attempts"] == 2
    assert report["adapter_attempts"] == 2
    assert report["inflight_peak"] == 2
    # 进入证据：取消前 2 个真实在途 HTTP；2 个 worker 进入调用但被闸挡住。
    evidence = report["wait_entry_evidence"]
    assert evidence["http_attempts_at_entry"] >= 2
    assert evidence["workers_entered_calls"] == 4
    assert evidence["http_attempts_at_cancel"] == 2
    assert evidence["queued_waiting_for_gate"] == 2
    # 大 timeout（30s）从未触发：等待只被计划释放刺激结束。
    assert evidence["read_timeout_seconds"] == 30.0
    entries = _by_request(report)
    # 票 06 落地：取消把在途等待也解除（请求级尽力通道）——4 个 worker
    # 全部 interrupted，在途请求不再等 2s 释放后拿到迟到成功响应。
    assert sorted(entry["outcome"] for entry in entries.values()) == [
        "interrupted",
        "interrupted",
        "interrupted",
        "interrupted",
    ]
    # 票 06 落地：排队与在途等待取消均及时（≤0.30s 冻结门槛，
    # 早于计划释放）——不再有「取消后仍等 2s 服务释放」的现状。
    assert report["stop"]["prompt"] is True
    assert report["stop"]["max_stop_responsiveness_seconds"] <= 0.3
    cancel_at = report["stop"]["cancel_at_s"]
    release_at = report["controlled_release"]["at_s"]
    assert release_at is not None and release_at > cancel_at
    # 在途请求的迟到响应不得交付：取消后无任何成功交付。
    assert report["responses"]["before_cancel"] == []
    assert report["responses"]["after_cancel"] == []
    # 停止在取消后、释放前生效：4 个 worker 全部先于释放到达终态
    # （interrupted），未发送的排队请求被闸口取消路径拦下。
    assert report["stop"]["completed_without_release"] is True
    assert report["stop"]["stopped_between_cancel_and_release"] is True
    # 30s timeout 未自然结束：无任何请求靠 timeout 完成。
    assert all(
        hit["ended_s"] is not None and hit["ended_s"] < 10.0
        for hit in report["http_attempt_log"]
    )


# ---- network：SDK 超时 + 隐式重试乘积 + 网关退避边界 ----


def test_network_probe_sdk_retries_are_visible_and_stop_is_prompt():
    report = run_probe("network")

    assert report["logical_requests"] == 1
    # 票 06 落地：SDK max_retries=0，1 次 adapter 尝试 = 1 次 HTTP——
    # 隐式重试乘积不再逃出网关预算（基线是 1 次 adapter = 3 次 HTTP）。
    assert report["adapter_attempts"] == 1
    assert report["http_attempts"] == 1
    attempt = report["adapter_attempt_log"][0]
    assert attempt["status"] == "error"
    assert attempt["category"] == "transient"
    entry = _by_request(report)[1]
    assert entry["outcome"] == "interrupted"
    # 服务未向调用方返回任何结果：取消前后都没有成功交付。
    assert report["responses"]["before_cancel"] == []
    assert report["responses"]["after_cancel"] == []
    # 取消锚定在首个在途 HTTP 上，先于网关退避开始。
    assert report["wait_entry_evidence"]["first_http_at_s"] is not None
    assert (
        report["wait_entry_evidence"]["first_http_at_s"]
        < report["stop"]["cancel_at_s"]
    )
    # 票 06：取消在退避内及时生效（≤0.30s），不再经历 SDK 重试 + 整段退避。
    assert report["stop"]["prompt"] is True
    assert report["stop"]["max_stop_responsiveness_seconds"] <= 0.3
    assert report["stop"]["completed_without_release"] is True
    assert not report["baseline_gaps"] or not any(
        "ignores an already-observed stop" in gap for gap in report["baseline_gaps"]
    )


# ---- backoff：429 + Retry-After 退避 ----


def test_backoff_probe_retry_after_honored_and_sleep_cancellable():
    report = run_probe("backoff")

    assert report["logical_requests"] == 1
    # 票 06 落地：SDK max_retries=0 —— 429 由网关退避重试，不再由 SDK
    # 按 Retry-After 头隐式重发（基线是 1 次 adapter = 3 次 429 HTTP）。
    assert report["adapter_attempts"] == 1
    assert report["http_attempts"] == 1
    assert all(hit["status"] == 429 for hit in report["http_attempt_log"])
    # Retry-After 1.2s 仍被尊重：网关退避 = max(2^0 × 1.0, 1.2) = 1.2s。
    sleep = report["sleeps"][0]
    assert sleep["requested_seconds"] == 1.2
    # 取消锚定在网关退避开始之后：取消落在退避等待中，
    # 票 06：退避等待可唤醒——worker 终态早于退避请求时长；取消路径
    # 提前返回后 sleeper 线程在后台自然结束（ended_at_s 可为 null，
    # 不把「后台线程收尾晚于取消」误判成等待未唤醒）。
    cancel_at = report["stop"]["cancel_at_s"]
    assert report["wait_entry_evidence"]["entered_gateway_backoff"] is True
    assert report["wait_entry_evidence"]["backoff_started_at_s"] == sleep["started_at_s"]
    assert sleep["started_at_s"] < cancel_at
    entry = _by_request(report)[1]
    assert entry["outcome"] == "interrupted"
    # 终态早于退避自然结束：取消唤醒了等待。
    if sleep["ended_at_s"] is not None:
        assert entry["ended_at_s"] < sleep["ended_at_s"]
    else:
        assert entry["ended_at_s"] - sleep["started_at_s"] < sleep["requested_seconds"]
    assert report["stop"]["prompt"] is True
    assert report["stop"]["completed_without_release"] is True
    assert report["stop"]["max_stop_responsiveness_seconds"] <= 0.3
    assert report["responses"]["before_cancel"] == []
    assert report["responses"]["after_cancel"] == []


# ---- CLI：供主基准子进程调用 ----


def test_cli_subprocess_writes_report_and_refuses_overwrite(tmp_path):
    output = tmp_path / "transport" / "queue.json"
    env = os.environ.copy()
    env["VIDEOCAPTIONER_APPDATA_PATH"] = str(tmp_path / "appdata")
    command = [
        sys.executable,
        "-m",
        "scripts.postprocess_transport_probe",
        "--kind",
        "queue",
        "--output",
        str(output),
    ]
    done = subprocess.run(
        command, cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["schema"] == PROBE_SCHEMA
    assert report["probe"] == "queue"
    assert report["ok"] is True
    assert report["cleanup"]["server_thread_exited"] is True
    assert report["cleanup"]["handler_answers_drained"] is True
    assert all(item["joined"] is True for item in report["cleanup"]["worker_joins"])

    # 输出文件禁止覆盖：同路径再跑必须拒绝，原文件保持不变。
    before = output.read_bytes()
    again = subprocess.run(
        command, cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120
    )
    assert again.returncode != 0
    assert output.read_bytes() == before

    # 未知 kind 直接拒绝，不产生输出文件。
    refused = tmp_path / "refused.json"
    bad = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.postprocess_transport_probe",
            "--kind",
            "nope",
            "--output",
            str(refused),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert bad.returncode != 0
    assert not refused.exists()
