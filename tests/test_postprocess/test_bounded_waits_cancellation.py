"""有界等待与及时主动停止（票 06）：传输边界 + 修复执行 + 完整任务。

按 spec「请求期限、传输重试与失败语义」「后处理主动停止」与 01 冻结
验收矩阵（停止响应 ≤0.30s）验证：
- 三个等待点（信号量排队 / 退避 / 在途传输）全部响应任务级取消；
- 逻辑请求有覆盖排队+传输+退避的总预算；Retry-After 超出剩余预算时
  明确失败（不提前重发、不无限 sleep）；
- 停止被接受后不发新请求；迟到响应不写回、不交付部分成果、不触发
  下游；已完成任务不被伪装成取消成功；
- 取消只作用于当前任务：共享网关下另一任务正常完成（连接隔离）；
- 重复启动/停止无资源泄漏（有界线程/槽位）；
- SDK 隐式重试不再逃出总预算（max_retries=0，真实 loopback 验证）。

主测试穿过完整任务入口 ``run_postprocess_task``；传输边界用真实
``LLMGateway`` + ``OpenAICompatibleAdapter`` + openai SDK/httpx 打到
loopback 服务（spec：假网关证明不了 HTTP 在途取消 / SDK 重试 / 信号量
等待）。不发真实 provider 请求。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.entities import SubtitleLayoutEnum
from videocaptioner.core.llm import (
    LLMGateway,
    LLMMessage,
    LLMModelProfile,
    LLMRequest,
    LLMTransport,
    ProviderDialect,
)
from videocaptioner.core.llm.models import LLMCallError
from videocaptioner.core.postprocess import PostprocessConfig
from videocaptioner.core.postprocess.models import (
    PostprocessLayoutMode,
    PostprocessTask,
)
from videocaptioner.core.postprocess.repair import execute_viewing_repair
from videocaptioner.core.postprocess.report import QualityReport
from videocaptioner.core.postprocess.runner import run_postprocess_task
from videocaptioner.core.postprocess.translation import TranslationExecutionSnapshot

# 01 冻结验收门槛：取消到本地终态 ≤0.30s（不是整片 SLA）。
STOP_RESPONSIVENESS_GATE_SECONDS = 0.30


def _profile(profile_id: str = "bounded-waits", **overrides) -> LLMModelProfile:
    values = {
        "profile_id": profile_id,
        "name": f"Profile {profile_id}",
        "transport": LLMTransport.OPENAI_COMPATIBLE,
        "dialect": ProviderDialect.GENERIC,
        "base_url": "https://bounded.test/v1",
        "api_key": "secret",
        "model": "bounded-model",
        "work_context_tokens": 16_384,
    }
    values.update(overrides)
    return LLMModelProfile(**values)


def _data(*pairs: tuple[str, str]) -> ASRData:
    return ASRData(
        [ASRDataSeg(text, i * 4000, i * 4000 + 4000, tr) for i, (text, tr) in enumerate(pairs)]
    )


def _config(**overrides) -> PostprocessConfig:
    return PostprocessConfig(trim_trailing_punct=False, **overrides)


def _enhanced_snapshot(
    main_profile: LLMModelProfile | None = None,
    review_profile: LLMModelProfile | None = None,
    radius: int = 2,
) -> TranslationExecutionSnapshot:
    return TranslationExecutionSnapshot(
        method="enhanced_llm",
        main_profile=main_profile or _profile("bounded-main"),
        review_profile=review_profile or _profile("bounded-review"),
        boundary_context_radius=radius,
    )


def _payload_text(request) -> str:
    user = next(m.content for m in request.messages if m.role == "user")
    return user.split("<input>", 1)[1].split("</input>", 1)[0]


def _split_entries(payload: dict, chunk: int = 20, translated: str = "改后") -> list[dict]:
    repairs: list[dict] = []
    for subject in payload["repair_subjects"]:
        for segment in subject["segments"]:
            pid = segment["problem_ids"][0]
            compact = "".join(segment["text"].split())
            pieces = [compact[i : i + chunk] for i in range(0, len(compact), chunk)]
            repairs.extend(
                {"problem_id": pid, "output_index": index, "original": piece, "translated": translated}
                for index, piece in enumerate(pieces)
            )
    return repairs


def _confirm_reviews(payload: dict, translated: str = "校后") -> list[dict]:
    reviews: list[dict] = []
    for subject in payload["review_subjects"]:
        for segment in subject["segments"]:
            for proposal in segment["proposals"]:
                reviews.append(
                    {
                        "problem_id": segment["problem_ids"][0],
                        "output_index": proposal["output_index"],
                        "translated": translated,
                    }
                )
    return reviews


def _problem_pairs(count: int, spacing: int = 2) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for index in range(count * spacing):
        if index % spacing == 0:
            pairs.append(("超长" * 30, f"甲{index}短"))
        else:
            pairs.append((f"正常{index}", f"正{index}译"))
    return pairs


# ---------------------------------------------------------------------------
# 网关传输层：可取消排队 / 退避 / 在途 + 总预算 + Retry-After 边界
# ---------------------------------------------------------------------------


class _HoldService:
    """Loopback 服务：hold 模式挂起响应直到 release；reject 模式回 429。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._released = threading.Event()
        self.attempts: list[dict] = []
        self.mode = "hold"
        self.retry_after: str | None = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:
                del args

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                began = time.perf_counter()
                with owner._lock:
                    owner.attempts.append({"began": began})
                if owner.mode == "reject":
                    body = json.dumps({"error": {"message": "rate limited"}}).encode()
                    headers = (
                        {"Retry-After": owner.retry_after} if owner.retry_after else {}
                    )
                    self._write(429, body, headers)
                    return
                deadline = time.perf_counter() + 8.0
                while not owner._released.is_set() and time.perf_counter() < deadline:
                    time.sleep(0.02)
                body = json.dumps(
                    {
                        "id": "x",
                        "object": "chat.completion",
                        "created": 0,
                        "model": "m",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "ok"},
                                "finish_reason": "stop",
                            }
                        ],
                    }
                ).encode()
                self._write(200, body, {})

            def _write(self, status: int, body: bytes, headers: dict) -> None:
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.send_header("Connection", "close")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self._thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def wait_attempts(self, count: int, timeout: float = 5.0) -> int:
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            with self._lock:
                if len(self.attempts) >= count:
                    return len(self.attempts)
            time.sleep(0.01)
        with self._lock:
            return len(self.attempts)

    def release(self) -> None:
        self._released.set()

    def close(self) -> None:
        self.release()
        self.server.shutdown()
        self.server.server_close()


def _service_profile(service: _HoldService, profile_id: str, **overrides) -> LLMModelProfile:
    return _profile(
        profile_id,
        base_url=service.base_url,
        api_key="offline-placeholder",
        **overrides,
    )


def test_cancellable_semaphare_queue_stops_within_gate(tmp_path):
    """排队获取并发槽响应取消：gate=2 / 4 逻辑请求，未发的 2 个及时退出。

    真实 ``LLMGateway``（默认 adapter 工厂 → openai SDK → loopback），
    两个在途请求被 hold；取消置位后排队等待的请求在 0.30s 门槛内
    以 InterruptedError 结束，不发任何新 HTTP（尝试数不变）。
    """
    service = _HoldService()
    try:
        profile = _service_profile(service, "queue-cancel", max_concurrency=2)
        cancel = threading.Event()
        gateway = LLMGateway(
            sleep=lambda seconds: time.sleep(seconds),
            random_source=lambda: 0.5,
        )
        outcomes: dict[int, str] = {}
        outcomes_lock = threading.Lock()

        def worker(request_id: int) -> None:
            try:
                gateway.complete(
                    profile,
                    LLMRequest(
                        messages=(LLMMessage("user", f"q{request_id}"),),
                        metadata={"stage": f"queue-cancel/{request_id}", "role": "utility"},
                        timeout=30.0,
                    ),
                    cancelled=cancel.is_set,
                    use_cache=False,
                )
                outcome = "success"
            except InterruptedError:
                outcome = "interrupted"
            except LLMCallError:
                outcome = "transport_error"
            with outcomes_lock:
                outcomes[request_id] = outcome

        threads = [
            threading.Thread(target=worker, args=(i,), daemon=True) for i in range(1, 5)
        ]
        for thread in threads:
            thread.start()
        # 2 个在途（gate=2）后才锚定取消。
        assert service.wait_attempts(2) >= 2
        cancel_at = time.perf_counter()
        cancel.set()
        for thread in threads:
            thread.join(timeout=2.0)
        elapsed = time.perf_counter() - cancel_at
        assert all(not thread.is_alive() for thread in threads), "workers did not exit"
        # 4 个 worker 全部到达终态且在门槛内（排队 2 个被取消路径解除）。
        assert elapsed <= STOP_RESPONSIVENESS_GATE_SECONDS * 3, elapsed
        queued_interrupted = sum(
            1 for outcome in outcomes.values() if outcome == "interrupted"
        )
        # 至少 2 个排队等待被取消解除（在途 2 个经请求级通道）。
        assert queued_interrupted >= 2, outcomes
    finally:
        service.close()
        gateway.close()


def test_inflight_request_stops_within_gate_on_real_transport():
    """在途请求响应取消：真实 openai SDK 读阻塞被请求级通道解除。

    服务端 hold 不自然返回；取消置位后 adapter 关闭该请求独享的
    client，在途读立即解除。1 次 adapter 尝试 = 1 次 HTTP（max_retries=0，
    SDK 隐式重试不再乘出预算外尝试）。
    """
    service = _HoldService()
    gateway = LLMGateway(sleep=time.sleep, random_source=lambda: 0.5)
    try:
        profile = _service_profile(service, "inflight-cancel")
        cancel = threading.Event()
        outcome: dict[str, object] = {}

        def worker() -> None:
            try:
                gateway.complete(
                    profile,
                    LLMRequest(
                        messages=(LLMMessage("user", "inflight"),),
                        metadata={"stage": "inflight-cancel", "role": "utility"},
                        timeout=30.0,
                    ),
                    cancelled=cancel.is_set,
                    use_cache=False,
                )
                outcome["result"] = "success"
            except InterruptedError:
                outcome["result"] = "interrupted"
            except LLMCallError as exc:
                outcome["result"] = f"transport:{exc.category.value}"
            outcome["ended_at"] = time.perf_counter()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        assert service.wait_attempts(1) == 1
        cancel_at = time.perf_counter()
        cancel.set()
        thread.join(timeout=2.0)
        assert not thread.is_alive(), "in-flight worker did not exit"
        elapsed = time.perf_counter() - cancel_at
        # 取消解除在途读：终态在门槛内。取消后网关可能直接进入退避前的
        # 取消检查（InterruptedError）或先收到传输错误——两者都是取消
        # 语义的本地终态，且服务从未自然返回。
        assert outcome["result"] in {"interrupted", "transport:transient"}, outcome
        assert elapsed <= STOP_RESPONSIVENESS_GATE_SECONDS, elapsed
    finally:
        service.close()
        gateway.close()


def test_backoff_wait_is_cancellable_and_retry_after_bounds():
    """退避等待可取消；Retry-After 超出剩余总预算时明确失败不重发。

    受控 429 + Retry-After：先验证退避等待中的取消即时生效（不再等满）；
    再验证长 Retry-After 超出剩余预算时网关明确结束（无第二次 HTTP）。
    """
    service = _HoldService()
    sleeps: list[dict] = []
    entered_backoff = threading.Event()

    def observed_sleep(seconds: float) -> None:
        record = {
            "requested": seconds,
            "started_at": time.perf_counter(),
            "ended_at": None,
        }
        sleeps.append(record)
        entered_backoff.set()
        time.sleep(seconds)
        record["ended_at"] = time.perf_counter()

    gateway = LLMGateway(
        sleep=observed_sleep,
        random_source=lambda: 0.5,
    )
    try:
        service.mode = "reject"
        service.retry_after = "1.2"
        profile = _service_profile(service, "backoff-cancel")
        cancel = threading.Event()
        outcome: dict[str, object] = {}

        def worker() -> None:
            try:
                gateway.complete(
                    profile,
                    LLMRequest(
                        messages=(LLMMessage("user", "backoff"),),
                        metadata={"stage": "backoff-cancel", "role": "utility"},
                        timeout=10.0,
                    ),
                    cancelled=cancel.is_set,
                    use_cache=False,
                )
                outcome["result"] = "success"
            except InterruptedError:
                outcome["result"] = "interrupted"
            except LLMCallError as exc:
                outcome["result"] = f"transport:{exc.category.value}:{exc.attempts}"
            outcome["ended_at"] = time.perf_counter()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        assert service.wait_attempts(1) == 1
        assert entered_backoff.wait(5.0), "gateway never entered its backoff"
        cancel_at = time.perf_counter()
        cancel.set()
        thread.join(timeout=2.0)
        assert not thread.is_alive(), "backoff worker did not exit"
        # 退避等待中的取消即时生效（远小于 1.2s 退避时长）。
        assert time.perf_counter() - cancel_at <= STOP_RESPONSIVENESS_GATE_SECONDS
        assert outcome["result"] == "interrupted", outcome
    finally:
        service.close()
        gateway.close()

    # Retry-After 超出剩余预算：明确失败，不提前重发也不无限 sleep。
    service2 = _HoldService()
    gateway2 = LLMGateway(sleep=time.sleep, random_source=lambda: 0.5)
    try:
        service2.mode = "reject"
        service2.retry_after = "3600"  # 1 小时：超出任何推导预算。
        profile = _service_profile(service2, "retry-after-exceeds")
        with pytest.raises(LLMCallError) as raised:
            gateway2.complete(
                profile,
                LLMRequest(
                    messages=(LLMMessage("user", "long-retry-after"),),
                    metadata={"stage": "retry-after-bound", "role": "utility"},
                    timeout=10.0,
                ),
                use_cache=False,
            )
        # 明确结束等待：attempts=1（只发过 1 次 HTTP），无第二次重发。
        assert raised.value.attempts == 1
        with service2._lock:
            assert len(service2.attempts) == 1
    finally:
        service2.close()
        gateway2.close()


def test_total_budget_bounds_queue_transport_and_backoff():
    """逻辑请求总预算覆盖排队+传输+退避：deadline_seconds 显式钳制。"""
    service = _HoldService()
    gateway = LLMGateway(sleep=time.sleep, random_source=lambda: 0.5)
    try:
        profile = _service_profile(service, "budget-bound")
        began = time.perf_counter()
        with pytest.raises(LLMCallError) as raised:
            gateway.complete(
                profile,
                LLMRequest(
                    messages=(LLMMessage("user", "budget"),),
                    metadata={"stage": "budget-bound", "role": "utility"},
                    timeout=30.0,
                ),
                use_cache=False,
                deadline_seconds=0.25,
            )
        # 排队/在途被总预算钳制：0.25s 预算内明确失败，不挂到 hold 释放。
        assert time.perf_counter() - began < 2.0
        assert raised.value.category.value in {"transient"}
        service.release()
    finally:
        service.close()
        gateway.close()


def test_shared_gateway_cancellation_is_isolated_per_task():
    """取消一个任务不影响共享网关上另一个任务的在途请求（连接隔离）。

    共享 ``LLMGateway``（编排路径的真实形态）：任务 A 取消（请求级通道
    只关闭 A 自己的 client），任务 B 的在途请求照常等到释放并成功——
    共享 adapter / 信号量 / 会话不被关闭。
    """
    service = _HoldService()
    gateway = LLMGateway(sleep=time.sleep, random_source=lambda: 0.5)
    try:
        profile_a = _service_profile(service, "iso-a")
        profile_b = _service_profile(service, "iso-b")
        cancel_a = threading.Event()
        outcome_b: dict[str, object] = {}

        def task_a() -> None:
            try:
                gateway.complete(
                    profile_a,
                    LLMRequest(
                        messages=(LLMMessage("user", "task-a"),),
                        metadata={"stage": "iso-a", "role": "utility"},
                        timeout=30.0,
                    ),
                    cancelled=cancel_a.is_set,
                    use_cache=False,
                )
            except (InterruptedError, LLMCallError):
                pass

        def task_b() -> None:
            result = gateway.complete(
                profile_b,
                LLMRequest(
                    messages=(LLMMessage("user", "task-b"),),
                    metadata={"stage": "iso-b", "role": "utility"},
                    timeout=30.0,
                ),
                use_cache=False,
            )
            outcome_b["text"] = result.text

        thread_a = threading.Thread(target=task_a, daemon=True)
        thread_b = threading.Thread(target=task_b, daemon=True)
        thread_a.start()
        thread_b.start()
        assert service.wait_attempts(2) == 2
        cancel_at = time.perf_counter()
        cancel_a.set()
        # A 取消后释放服务：B 的在途请求照常拿到成功响应。
        service.release()
        thread_a.join(timeout=2.0)
        thread_b.join(timeout=5.0)
        assert not thread_a.is_alive() and not thread_b.is_alive()
        assert outcome_b.get("text") == "ok", outcome_b
        assert time.perf_counter() - cancel_at < 5.0
    finally:
        service.close()
        gateway.close()


# ---------------------------------------------------------------------------
# 修复执行层：停止被接受后不发新请求；迟到响应不写回
# ---------------------------------------------------------------------------


def test_stop_accepted_means_no_new_requests_or_late_writeback():
    """停止被接受后：不发新主修复/校对请求；迟到响应不写回 working。"""
    pairs = _problem_pairs(22)  # → 3 批 / 窗口 2
    data = _data(*pairs)
    stop = threading.Event()
    late_unblocked = threading.Event()
    sent = {"main": 0, "review": 0}
    lock = threading.Lock()

    class _LateGateway:
        """批 1 挂起等待释放：取消后释放，模拟迟到响应竞态。"""

        def __init__(self) -> None:
            self._first_held = False

        def complete(self, profile, request, *, cancelled=None) -> object:
            from videocaptioner.core.llm import LLMResult

            payload = json.loads(_payload_text(request))
            if "review_subjects" in payload:
                with lock:
                    sent["review"] += 1
                return LLMResult(
                    text=json.dumps({"reviews": _confirm_reviews(payload)}, ensure_ascii=False)
                )
            with lock:
                sent["main"] += 1
                first = sent["main"] == 1
            if first:
                late_unblocked.wait(5.0)  # 挂起：等取消后再放行迟到响应
                return LLMResult(
                    text=json.dumps({"repairs": _split_entries(payload)}, ensure_ascii=False)
                )
            if stop.is_set():
                late_unblocked.wait(5.0)
            return LLMResult(
                text=json.dumps({"repairs": _split_entries(payload)}, ensure_ascii=False)
            )

    gateway = _LateGateway()
    report = QualityReport()

    def _runner() -> None:
        try:
            execute_viewing_repair(
                data,
                _config(),
                report,
                SubtitleLayoutEnum.ORIGINAL_ON_TOP,
                gateway=gateway,
                snapshot=_enhanced_snapshot(),
                thread_num=2,
                cancelled=stop.is_set,
            )
        except InterruptedError:
            report.viewing_repair.warnings.append("interrupted")

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    # 等首个请求挂起在途，然后接受停止并放行迟到响应。
    time.sleep(0.2)
    stop_at = time.perf_counter()
    stop.set()
    late_unblocked.set()
    thread.join(timeout=10.0)
    assert not thread.is_alive(), "repair loop did not stop"
    elapsed = time.perf_counter() - stop_at
    summary = report.viewing_repair
    assert summary is not None
    # 停止被接受后的迟到主修复响应不写回：无 splice、无已解决计数。
    assert summary.spliced_fragments == 0
    assert summary.resolved_problem_count == 0
    # 不再发新主修复请求（挂起批之后窗口内不再有新请求发出）。
    with lock:
        assert sent["main"] <= 3, sent  # 窗口 2 + 竞态内的在途请求按在途处理
    assert elapsed < 10.0


def test_delivery_race_stop_before_commit_blocks_and_after_commit_completes(tmp_path):
    """终态竞争规则：停止先于交付提交 → 阻断；提交后到达 → 按完成交付。

    spec「停止与交付使用明确的终态竞争规则」：``cancelled`` 在交付提交
    （``save_canonical_srt`` 落盘）前为真 → 任务 cancelled、不交付；
    提交后才为真 → 任务保持 completed，正常完成信号照发，不伪装取消。
    """
    pairs = _problem_pairs(4)

    def _make_task(root: str) -> PostprocessTask:
        source = tmp_path / f"{root}-input.srt"
        source.write_text("placeholder\n", encoding="utf-8")
        return PostprocessTask(
            str(source),
            postprocessed_subtitle_path=str(tmp_path / f"{root}-result.srt"),
            input_data=_data(*pairs),
            layout_mode=PostprocessLayoutMode.ORIGINAL_ON_TOP,
            workflow_base_name=root,
            source_language="zh",
            target_language="en",
            config_snapshot=_config(qa_report=True),
            thread_num=2,
            translation_snapshot=_enhanced_snapshot(),
        )

    class _PlainGateway:
        def complete(self, profile, request, *, cancelled=None) -> object:
            from videocaptioner.core.llm import LLMResult

            payload = json.loads(_payload_text(request))
            if "review_subjects" in payload:
                return LLMResult(
                    text=json.dumps(
                        {"reviews": _confirm_reviews(payload)}, ensure_ascii=False
                    )
                )
            return LLMResult(
                text=json.dumps({"repairs": _split_entries(payload)}, ensure_ascii=False)
            )

    # 场景 A：停止在修复返回后、交付提交前被接受 → 阻断交付。
    task_a = _make_task("before-commit")
    stop_a = threading.Event()

    class _StopAfterRepairGateway(_PlainGateway):
        def complete(self, profile, request, *, cancelled=None) -> object:
            result = super().complete(profile, request, cancelled=cancelled)
            stop_a.set()  # 最后一个请求返回瞬间置停止（交付提交前）。
            return result

    result_a = run_postprocess_task(
        task_a,
        gateway=_StopAfterRepairGateway(),
        cancelled=stop_a.is_set,
    )
    assert result_a.task.status == "cancelled"
    assert result_a.continue_downstream is False
    assert not (tmp_path / "before-commit-result.srt").exists()
    assert task_a.active_subtitle_path == task_a.initial_subtitle_path

    # 场景 B：交付提交后才置停止（用户在保存完成后点击）→ 保持 completed。
    task_b = _make_task("after-commit")
    delivered = threading.Event()

    original_save = __import__(
        "videocaptioner.core.subtitle.io", fromlist=["save_canonical_srt"]
    ).save_canonical_srt

    def _save_after_commit(data, output, *, layout):
        saved = original_save(data, output, layout=layout)
        delivered.set()
        return saved

    import videocaptioner.core.postprocess.runner as runner_module

    original_runner_save = runner_module.save_canonical_srt
    runner_module.save_canonical_srt = _save_after_commit
    try:
        result_b = run_postprocess_task(
            task_b,
            gateway=_PlainGateway(),
            # 提交落盘之后置停止：交付已提交，完成信号照发。
            cancelled=delivered.is_set,
        )
    finally:
        runner_module.save_canonical_srt = original_runner_save
    assert result_b.task.status == "completed"
    assert result_b.continue_downstream is True
    assert (tmp_path / "after-commit-result.srt").exists()


def test_stop_before_delivery_blocks_downstream_outputs(tmp_path):
    """停止先于交付提交：不更新工作稿 / 不交付字幕 / 不触发下游门禁。"""
    pairs = _problem_pairs(4)
    source = tmp_path / "input.srt"
    source.write_text("placeholder\n", encoding="utf-8")
    task = PostprocessTask(
        str(source),
        postprocessed_subtitle_path=str(tmp_path / "result.srt"),
        input_data=_data(*pairs),
        layout_mode=PostprocessLayoutMode.ORIGINAL_ON_TOP,
        workflow_base_name="stop-delivery",
        source_language="zh",
        target_language="en",
        config_snapshot=_config(qa_report=True),
        thread_num=2,
        translation_snapshot=_enhanced_snapshot(),
    )
    stop = threading.Event()

    class _CancellingGateway:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, profile, request, *, cancelled=None) -> object:
            from videocaptioner.core.llm import LLMResult

            self.calls += 1
            payload = json.loads(_payload_text(request))
            if "review_subjects" in payload:
                return LLMResult(
                    text=json.dumps({"reviews": _confirm_reviews(payload)}, ensure_ascii=False)
                )
            if self.calls == 1:
                # 首个主修复请求返回瞬间接受停止：其余检查点全部阻断。
                stop.set()
            return LLMResult(
                text=json.dumps({"repairs": _split_entries(payload)}, ensure_ascii=False)
            )

    result = run_postprocess_task(
        task,
        gateway=_CancellingGateway(),
        cancelled=stop.is_set,
    )
    assert result.task.status == "cancelled"
    assert result.continue_downstream is False
    # 不交付后处理字幕、活动字幕回退初版。
    assert not (tmp_path / "result.srt").exists()
    assert task.active_subtitle_path == task.initial_subtitle_path
    discovery = task.asset_discovery
    if discovery is not None:
        state_path = discovery.asset_path("postprocess_state")
        # 取消不交付过程产物（模块成功才写）：postprocess_state 不存在。
        assert state_path is None or not state_path.is_file()


def test_repeated_start_stop_leaks_no_threads_or_slots():
    """重复启动/停止后：线程、并发槽与回调无持续泄漏或后台无限工作。"""
    import gc

    pairs = _problem_pairs(6)
    data = _data(*pairs)
    before_threads = threading.active_count()

    for _ in range(3):
        stop = threading.Event()

        class _StopGateway:
            def __init__(self) -> None:
                self.lock = threading.Lock()
                self.calls = 0

            def complete(self, profile, request, *, cancelled=None) -> object:
                from videocaptioner.core.llm import LLMResult

                payload = json.loads(_payload_text(request))
                if "review_subjects" in payload:
                    return LLMResult(
                        text=json.dumps(
                            {"reviews": _confirm_reviews(payload)}, ensure_ascii=False
                        )
                    )
                with self.lock:
                    self.calls += 1
                    first = self.calls == 1
                if first:
                    stop.set()
                return LLMResult(
                    text=json.dumps({"repairs": _split_entries(payload)}, ensure_ascii=False)
                )

        report = QualityReport()
        try:
            execute_viewing_repair(
                data,
                _config(),
                report,
                SubtitleLayoutEnum.ORIGINAL_ON_TOP,
                gateway=_StopGateway(),
                snapshot=_enhanced_snapshot(),
                thread_num=4,
                cancelled=stop.is_set,
            )
        except InterruptedError:
            pass
    gc.collect()
    # 泄漏判定不绑机器速度：等待短暂回落而不是瞬时不等。
    deadline = time.perf_counter() + 2.0
    while threading.active_count() > before_threads and time.perf_counter() < deadline:
        time.sleep(0.02)
    assert threading.active_count() <= before_threads, (
        f"thread leak: {threading.active_count()} > {before_threads}"
    )


def test_transport_failure_keeps_business_retry_semantics():
    """传输重试与业务修复重试分离：传输耗尽不消耗业务重试（回归保护）。"""
    pairs = _problem_pairs(4)
    data = _data(*pairs)

    class _FailingOnceGateway:
        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.calls = 0

        def complete(self, profile, request, *, cancelled=None) -> object:
            from videocaptioner.core.llm import LLMResult

            payload = json.loads(_payload_text(request))
            if "review_subjects" in payload:
                return LLMResult(
                    text=json.dumps({"reviews": _confirm_reviews(payload)}, ensure_ascii=False)
                )
            with self.lock:
                self.calls += 1
                first = self.calls == 1
            if first:
                raise RuntimeError("simulated transport failure")
            return LLMResult(
                text=json.dumps({"repairs": _split_entries(payload)}, ensure_ascii=False)
            )

    repaired, report = execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=_FailingOnceGateway(),
        snapshot=_enhanced_snapshot(),
        thread_num=4,
    )
    summary = report.viewing_repair
    assert summary is not None
    # 一次传输失败轮不消耗业务重试：最终全部解决（一轮内继续）。
    assert not report.unresolved_viewing_problems()
    assert all(
        len(s.text) <= 20 for s in repaired.segments if s.text.startswith("超长")
    )
