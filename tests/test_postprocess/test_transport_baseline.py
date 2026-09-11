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


def test_queue_probe_gate_caps_inflight_and_cancel_is_not_prompt():
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
    assert sorted(entry["outcome"] for entry in entries.values()) == [
        "interrupted",
        "interrupted",
        "success",
        "success",
    ]
    # 基线（现状）：取消不能及时结束排队 / 在途等待。
    assert report["stop"]["prompt"] is False
    assert report["stop"]["max_stop_responsiveness_seconds"] > 0.3
    cancel_at = report["stop"]["cancel_at_s"]
    release_at = report["controlled_release"]["at_s"]
    assert release_at is not None and release_at > cancel_at
    # 在途请求在取消后才拿到成功响应：迟到响应照常交付（传输层现状）。
    assert report["responses"]["before_cancel"] == []
    after = report["responses"]["after_cancel"]
    assert len(after) == 2
    for request_id in after:
        entry = entries[request_id]
        assert entry["outcome"] == "success"
        assert entry["result_text"] == "probe-ok"
        assert entry["ended_at_s"] > cancel_at
        assert entry["ended_at_s"] >= release_at
    # queue 的应答由计划释放刺激产生：停止本身没有先完成任何等待。
    assert report["responses"]["service_released_writes"] == 2
    assert report["stop"]["completed_without_release"] is False
    # 未来门槛：停止应在取消后、释放前生效（现状 False，票 06 应翻正）。
    assert report["stop"]["stopped_between_cancel_and_release"] is False
    # 30s timeout 未自然结束：无任何请求靠 timeout 完成。
    assert all(
        hit["ended_s"] is not None and hit["ended_s"] < 10.0
        for hit in report["http_attempt_log"]
    )
    assert report["baseline_gaps"]


# ---- network：SDK 超时 + 隐式重试乘积 + 网关退避边界 ----


def test_network_probe_measures_sdk_retry_multiplication_and_stop_latency():
    report = run_probe("network")

    assert report["logical_requests"] == 1
    # SDK 隐式重试乘积（实测基线）：1 次 adapter 尝试 = 3 次 HTTP。
    assert report["adapter_attempts"] == 1
    assert report["http_attempts"] == 3
    attempt = report["adapter_attempt_log"][0]
    assert attempt["status"] == "error"
    assert attempt["category"] == "transient"
    entry = _by_request(report)[1]
    assert entry["outcome"] == "interrupted"
    # 服务未向调用方返回任何结果：取消前后都没有成功交付。
    assert report["responses"]["before_cancel"] == []
    assert report["responses"]["after_cancel"] == []
    # 服务未自然结束时本地取消仍完成（经传输超时 + 退避边界），但不及时。
    assert report["stop"]["completed_without_release"] is True
    assert report["stop"]["prompt"] is False
    # 取消锚定在首个在途 HTTP 上，先于网关退避开始。
    assert report["wait_entry_evidence"]["first_http_at_s"] is not None
    assert (
        report["wait_entry_evidence"]["first_http_at_s"]
        < report["stop"]["cancel_at_s"]
    )
    latency = report["stop"]["max_stop_responsiveness_seconds"]
    # 覆盖 read timeout(1s)×3 次 HTTP + 网关退避 1s，远超及时阈值。
    assert latency > 1.0
    assert latency < 15.0  # 有界（挂起守卫，非性能门槛）
    # 网关退避 1.0s（超时无 Retry-After：min(30, 2^0) × 1.0 抖动固定）。
    assert report["sleeps"] and report["sleeps"][0]["requested_seconds"] == 1.0
    sleep = report["sleeps"][0]
    # 取消先于退避开始（已置位），网关仍进入新的退避等待——直接证据。
    assert report["stop"]["cancel_at_s"] < sleep["started_at_s"]
    # worker 在退避结束后才停止。
    assert entry["ended_at_s"] >= sleep["ended_at_s"]
    assert report["baseline_gaps"]


# ---- backoff：429 + Retry-After 退避 ----


def test_backoff_probe_retry_after_honored_and_sleep_uncancellable():
    report = run_probe("backoff")

    assert report["logical_requests"] == 1
    assert report["adapter_attempts"] == 1
    assert report["http_attempts"] == 3
    assert all(hit["status"] == 429 for hit in report["http_attempt_log"])
    # Retry-After 1.2s 未被缩短：SDK 按头间隔重发（间隔 >= 1.0s）。
    gaps = [hit["gap_since_previous_s"] for hit in report["http_attempt_log"][1:]]
    assert len(gaps) == 2
    assert all(gap >= 1.0 for gap in gaps)
    # 网关退避 = max(2^0 × 1.0, Retry-After 1.2) = 1.2s，真实 sleep。
    sleep = report["sleeps"][0]
    assert sleep["requested_seconds"] == 1.2
    assert sleep["actual_seconds"] >= 1.1
    # 取消锚定在网关退避开始之后：取消落在退避 sleep 中间，
    # worker 仍等满整段退避才停止。
    cancel_at = report["stop"]["cancel_at_s"]
    assert report["wait_entry_evidence"]["entered_gateway_backoff"] is True
    assert report["wait_entry_evidence"]["backoff_started_at_s"] == sleep["started_at_s"]
    assert sleep["started_at_s"] < cancel_at < sleep["ended_at_s"]
    entry = _by_request(report)[1]
    assert entry["outcome"] == "interrupted"
    assert entry["ended_at_s"] >= sleep["ended_at_s"]
    assert report["stop"]["prompt"] is False
    assert report["stop"]["completed_without_release"] is True
    assert 0.5 < report["stop"]["max_stop_responsiveness_seconds"] < 5.0
    assert report["responses"]["before_cancel"] == []
    assert report["responses"]["after_cancel"] == []
    assert report["baseline_gaps"]


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
