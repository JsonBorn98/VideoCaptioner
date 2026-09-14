"""进度与诊断事件（票 07，spec「进度与诊断」）：完整任务接缝验证。

沿 ``execute_viewing_repair`` / ``run_postprocess_task`` 接缝收集结构化
事件（``on_event`` 回调），从外部断言阶段 / 轮次 / 批次 / 验收口径与
等待刷新——不逐层 mock 内部函数，不依赖 token streaming：
- 摘要口径：模型返回 / 批次完成不等于问题解决；只有归并验收通过的
  计数进入「已通过」，分母（open_problems）逐轮显式记录，不藏在
  伪单调百分比里。
- 长模型调用期间：等待事件以 ≤0.50s 间隔持续刷新（01 冻结门槛），
  携带窗口 / 在途 / 排队与单次尝试窗口；不缩短受控延迟伪造达标。
- 停止：取消后事件流终止（进度不复活），终态事件 status=cancelled。
- 关联：请求 metadata / 请求日志 started+终态行可按 task / round /
  batch / attempt 关联；内容日志关闭时不落 prompt / 响应正文。
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from videocaptioner.core.entities import SubtitleLayoutEnum
from videocaptioner.core.llm import LLMResult
from videocaptioner.core.postprocess import diagnostics
from videocaptioner.core.postprocess.repair import execute_viewing_repair
from videocaptioner.core.postprocess.report import QualityReport

from .test_repair_concurrency import (
    _BarrierGateway,
    _config,
    _data,
    _enhanced_snapshot,
    _payload_text,
    _problem_pairs,
    _profile,
    _split_entries,
)


class _DelayGateway:
    """受控延迟网关：主修复按序号延迟，复校即时确认。

    等待刷新验证不用短 sleep 判断时序：延迟窗口（0.8s）远大于事件
    节流间隔（0.2s），断言「窗口内收到 ≥2 个等待事件 + 相邻间隔
    ≤0.50s」是门槛验收，不是竞态猜测。
    """

    def __init__(self, main_delay: float = 0.8):
        self.main_delay = main_delay
        self.lock = threading.Lock()
        self.metadata: list[dict] = []
        self.main_calls = 0

    def complete(self, profile, request, *, cancelled=None):
        payload = json.loads(_payload_text(request))
        with self.lock:
            self.metadata.append(
                {
                    "stage": dict(request.metadata),
                    "role": "review" if "review_subjects" in payload else "main",
                }
            )
        if "review_subjects" in payload:
            from .test_repair_concurrency import _confirm_reviews

            return LLMResult(
                text=json.dumps({"reviews": _confirm_reviews(payload)}, ensure_ascii=False)
            )
        with self.lock:
            self.main_calls += 1
            call = self.main_calls
        deadline = time.perf_counter() + (self.main_delay if call == 1 else 0.0)
        while time.perf_counter() < deadline:
            if cancelled is not None and cancelled():
                raise RuntimeError("delay gateway request cancelled in flight")
            time.sleep(min(0.01, max(0.0, deadline - time.perf_counter())))
        return LLMResult(text=json.dumps({"repairs": _split_entries(payload)}, ensure_ascii=False))


class _EventCollector:
    """线程安全事件收集器（on_event 回调的测试侧）。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.events: list[dict] = []

    def __call__(self, event: dict) -> None:
        with self.lock:
            self.events.append(dict(event))

    def by_kind(self, kind: str) -> list[dict]:
        with self.lock:
            return [event for event in self.events if event.get("kind") == kind]


# ---- 验收 1：摘要口径 —— 验收通过 ≠ 模型返回 ----


def test_round_batch_and_terminal_events_report_acceptance_not_returns():
    """轮 / 批 / 终态事件：分母逐轮显式，验收通过与请求次数分开。"""
    pairs = _problem_pairs(12)
    data = _data(*pairs)
    gateway = _BarrierGateway(barrier_size=2)
    collector = _EventCollector()
    repaired, report = execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        snapshot=_enhanced_snapshot(),
        thread_num=4,
        on_event=collector,
    )
    del repaired
    rounds = collector.by_kind("round")
    assert len(rounds) == 1  # 一轮解决
    assert rounds[0]["counts"]["open_problems"] == 12  # 分母显式（12 个问题主体）
    assert rounds[0]["percent"] is not None  # 兼容简单百分比消费者

    batches = collector.by_kind("batch")
    assert len(batches) == 2  # 12 主体 → 2 批（10 + 2）
    # 归并顺序 = reversed(plan.batches) 固定序：order 0 = 末批（2 主体）先归并。
    # 归并验收通过的口径：每批 accepted 计数，模型返回不直接算解决。
    assert [event["counts"]["accepted_in_batch"] for event in batches] == [2, 10]
    assert all(event["round"] == 1 for event in batches)
    assert [event["batch"] for event in batches] == [0, 1]
    assert [event["counts"]["accepted_total"] for event in batches] == [2, 12]

    terminal = collector.by_kind("terminal")
    assert len(terminal) == 1  # 无重复完成
    assert terminal[0]["status"] == "completed"
    # 请求次数与解决计数分列（2 次请求解决 12 个问题：返回 ≠ 解决的口径基础）。
    assert terminal[0]["counts"]["requests"] == 2
    assert terminal[0]["counts"]["resolved"] == 12
    summary = report.viewing_repair
    assert summary is not None and summary.resolved_problem_count == 12


def test_denominator_changes_are_explicit_across_rounds():
    """跨轮分母变化显式：首轮无接受 → 次轮重扫，事件逐轮带 open 数。"""
    pairs = _problem_pairs(12)
    data = _data(*pairs)
    gateway = _FailingThenValidGateway()
    collector = _EventCollector()
    repaired, report = execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        snapshot=_enhanced_snapshot(),
        thread_num=4,
        on_event=collector,
    )
    del repaired
    rounds = collector.by_kind("round")
    assert len(rounds) == 2
    # 两轮的 open 数都显式记录；首轮无验收通过时不伪造进度百分比。
    assert rounds[0]["counts"]["open_problems"] == 12
    assert rounds[1]["counts"]["open_problems"] == 12
    first_round_batches = [event for event in collector.by_kind("batch") if event["round"] == 1]
    assert all(event["counts"]["accepted_in_batch"] == 0 for event in first_round_batches)
    assert not report.unresolved_viewing_problems()  # 次轮全部解决
    terminal = collector.by_kind("terminal")
    # 轮 2 成功后，轮 3 顶部扫描无未解决才 break：确认扫描轮同样计入
    # 轮次口径（与 summary.rounds 一致，不因事件通道改变循环语义）。
    assert terminal[0]["counts"]["rounds"] == 3


class _FailingThenValidGateway(_BarrierGateway):
    """首轮主修复响应缺少输出片段（业务失败），次轮合法。"""

    def __init__(self):
        super().__init__(barrier_size=2)
        self._round = 0
        self._calls = 0

    def complete(self, profile, request, *, cancelled=None):
        payload = json.loads(_payload_text(request))
        if "review_subjects" in payload:
            return LLMResult(text=json.dumps({"reviews": []}, ensure_ascii=False))
        with self.lock:
            self._calls += 1
            first_round = self._round == 0
        if first_round:
            with self.lock:
                if self._calls >= 2:
                    self._round = 1
            # 首轮：响应缺少 repairs 数组 → 整体业务失败，无验收通过。
            return LLMResult(text=json.dumps({"repairs": []}, ensure_ascii=False))
        return LLMResult(text=json.dumps({"repairs": _split_entries(payload)}, ensure_ascii=False))


# ---- 验收 2：长模型调用期间的等待刷新（01 门槛 ≤0.50s）----


def test_waiting_events_refresh_during_long_model_calls():
    """首个主修复请求在途期间：等待事件持续刷新且间隔 ≤0.50s。

    窗口 1（单批单请求）也必须刷新——历史痛点正是串行慢修复整轮
    只显示一次文案。受控延迟 0.8s 不缩短；断言窗口内 ≥2 个等待事件。
    """
    pairs = _problem_pairs(4)  # 单批 4 主体 → window 1
    data = _data(*pairs)
    gateway = _DelayGateway(main_delay=0.8)
    collector = _EventCollector()
    start = time.perf_counter()
    execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        snapshot=_enhanced_snapshot(),
        thread_num=4,
        on_event=collector,
    )
    waiting = collector.by_kind("waiting")
    assert len(waiting) >= 2  # 0.8s 窗口 / 0.2s 节流 → ≥2 次刷新
    first = waiting[0]
    assert first["role"] == "main"
    assert first["counts"]["window"] == 1
    assert first["counts"]["inflight"] == 1
    assert first["counts"]["queued"] == 0
    assert first["request_window_s"] > 0  # 单次尝试窗口（等待期限）
    assert first["counts"]["wait_elapsed_ms"] >= 0
    # 相邻等待事件间隔 ≤0.50s（01 冻结状态刷新门槛，2.5 倍余量节流）。
    emitted_at = [event["at_s"] for event in waiting]
    assert all(later - earlier <= 0.50 for earlier, later in zip(emitted_at, emitted_at[1:]))
    # 首个等待事件不晚于轮开始后 0.50s。
    assert emitted_at[0] - start <= 0.50 + 0.30  # 轮前规划成本余量
    del start


def test_waiting_events_also_cover_review_window():
    """校对在途等待同样刷新（role=review），不等整段静默。"""
    pairs = _problem_pairs(4)
    data = _data(*pairs)
    gateway = _ReviewDelayGateway(review_delay=0.8)
    collector = _EventCollector()
    execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        snapshot=_enhanced_snapshot(),
        thread_num=4,
        on_event=collector,
    )
    review_waits = [event for event in collector.by_kind("waiting") if event["role"] == "review"]
    assert len(review_waits) >= 2  # 0.8s 校对窗口内持续刷新


class _ReviewDelayGateway(_DelayGateway):
    """复校延迟 0.8s（主修复即时）：校对窗口的等待刷新探针。"""

    def __init__(self, review_delay: float = 0.8):
        super().__init__(main_delay=0.0)
        self.review_delay = review_delay
        self._review_calls = 0
        self._review_lock = threading.Lock()

    def complete(self, profile, request, *, cancelled=None):
        payload = json.loads(_payload_text(request))
        if "review_subjects" not in payload:
            return LLMResult(
                text=json.dumps({"repairs": _split_entries(payload)}, ensure_ascii=False)
            )
        from .test_repair_concurrency import _confirm_reviews

        with self._review_lock:
            self._review_calls += 1
            call = self._review_calls
        deadline = time.perf_counter() + (self.review_delay if call == 1 else 0.0)
        while time.perf_counter() < deadline:
            if cancelled is not None and cancelled():
                raise RuntimeError("review delay gateway cancelled in flight")
            time.sleep(min(0.01, max(0.0, deadline - time.perf_counter())))
        return LLMResult(
            text=json.dumps({"reviews": _confirm_reviews(payload)}, ensure_ascii=False)
        )


# ---- 验收 3：停止后事件流终止（进度不复活）----


def test_cancel_stops_event_stream_and_terminal_is_cancelled():
    """在途请求期间取消：无后续等待 / 批事件，终态 status=cancelled。"""
    pairs = _problem_pairs(4)
    data = _data(*pairs)
    gateway = _DelayGateway(main_delay=0.8)
    collector = _EventCollector()
    stop = threading.Event()

    def _cancelled() -> bool:
        # 首个等待事件到达后置位停止：等待刷新先可观察，再取消。
        if not stop.is_set() and len(collector.by_kind("waiting")) >= 1:
            stop.set()
        return stop.is_set()

    with pytest.raises(InterruptedError):
        execute_viewing_repair(
            data,
            _config(),
            QualityReport(),
            SubtitleLayoutEnum.ORIGINAL_ON_TOP,
            gateway=gateway,
            snapshot=_enhanced_snapshot(),
            thread_num=4,
            cancelled=_cancelled,
            on_event=collector,
        )
    # 取消后不再发新事件（进度不复活）：无归并验收事件。
    assert collector.by_kind("batch") == []
    # 取消终态由调用方（runner）统一发射（票 07 审查修复：修复层与
    # 任务层各发一次会重复「修复已停止」终态）；修复层只上抛。完整
    # 任务入口的取消终态由 runner 测试口径覆盖（无重复完成）。
    assert collector.by_kind("terminal") == []


# ---- 验收 4：关联字段 —— 事件 / metadata / 请求日志 ----


def test_requests_carry_task_round_batch_metadata():
    """请求 metadata 携带 task_id / round / batch：关闭内容日志仍可关联。"""
    pairs = _problem_pairs(4)
    data = _data(*pairs)
    gateway = _DelayGateway(main_delay=0.0)
    execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        snapshot=_enhanced_snapshot(),
        thread_num=4,
        task_id="task-07",
    )
    assert gateway.metadata
    for entry in gateway.metadata:
        stage_meta = entry["stage"]
        assert stage_meta.get("task_id") == "task-07"
        assert stage_meta.get("round")
        assert stage_meta.get("batch") is not None
        assert stage_meta.get("stage") in ("viewing_repair", "viewing_repair_review")
        assert stage_meta.get("role") == "utility"


def test_request_log_started_and_terminal_pair_by_request_id(tmp_path, monkeypatch):
    """请求日志：started 行先落盘，终态行配对；未闭合可识别、无内容。

    真实 ``LLMGateway`` + 受控 adapter：begin 在尝试开始即写
    status=started 行（异常退出时只有开始无终态 = 未闭合，不伪造），
    finish 写 success/error 行；两行按 request_id 关联，携带
    task_id / round / batch（内容日志关闭：无 prompt / 响应正文）。
    """
    from videocaptioner.core.llm import request_logger
    from videocaptioner.core.llm.gateway import LLMGateway

    log_path = tmp_path / "llm_requests.jsonl"
    monkeypatch.setattr(request_logger, "LLM_LOG_FILE", log_path)
    monkeypatch.setattr(request_logger, "is_llm_content_logging_enabled", lambda: False)

    class _Adapter:
        def __init__(self, profile):
            self.profile = profile

        def complete(self, request, *, cancelled=None):
            payload = json.loads(_payload_text(request))
            if "review_subjects" in payload:
                from .test_repair_concurrency import _confirm_reviews

                return LLMResult(
                    text=json.dumps({"reviews": _confirm_reviews(payload)}, ensure_ascii=False)
                )
            return LLMResult(
                text=json.dumps({"repairs": _split_entries(payload)}, ensure_ascii=False)
            )

        def close(self):
            pass

    runtime = LLMGateway(
        adapter_factory=_Adapter,
        response_cache=_NullCache(),
        sleep=time.sleep,
        random_source=lambda: 0.5,
    )

    class _WrappedGateway:
        def complete(self, profile, request, **kwargs):
            return runtime.complete(profile, request, **kwargs)

        def close(self):
            runtime.close()

    pairs = _problem_pairs(4)
    data = _data(*pairs)
    repaired, report = execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=_WrappedGateway(),
        snapshot=_enhanced_snapshot(
            main_profile=_profile("logged-main"),
            review_profile=_profile("logged-review"),
        ),
        thread_num=4,
        task_id="task-logged",
    )
    del repaired, report
    lines = [json.loads(line) for line in log_path.read_text("utf-8").splitlines()]
    started = [entry for entry in lines if entry["status"] == "started"]
    finished = [entry for entry in lines if entry["status"] in ("success", "error", "cache_hit")]
    assert started and finished
    # 一次尝试 = started + 终态两行，request_id 一一配对。
    started_ids = {entry["request_id"] for entry in started}
    finished_ids = {entry["request_id"] for entry in finished}
    assert started_ids == finished_ids
    assert len(started) == len(finished)
    # 关联字段在内容日志关闭时仍可读取。
    for entry in lines:
        assert entry.get("task_id") == "task-logged"
        assert entry.get("round")
        assert entry.get("batch") is not None
        assert entry["attempt"] == 1
        assert "request" not in entry  # 无 prompt 正文
        assert "response" not in entry  # 无响应正文


class _NullCache:
    """无查找无写入：conftest 已全局禁用缓存，这里显式隔离。"""

    def lookup(self, *_args):
        return None

    def store(self, *_args):
        pass


# ---- 验收 5：渲染 —— 简单消费者与详情消费者共用同一事实 ----


def test_event_rendering_supports_summary_and_detail_consumers():
    """事件可渲染：单行 verbose / spinner 消息 + 展开详情多行。"""
    event = diagnostics.waiting_event(
        round_index=2,
        wait_elapsed_ms=3200,
        inflight=2,
        queued=1,
        window=4,
        request_window_s=128.0,
        role="main",
    )
    line = diagnostics.render_event_line(event)
    assert "waiting" in line
    assert "round=2" in line or "R2" in line
    detail = diagnostics.render_detail(
        {
            "message": event["message"],
            "round": 2,
            "window": 4,
            "inflight": 2,
            "queued": 1,
            "wait_elapsed_ms": 3200,
            "request_window_s": 128.0,
            "accepted_total": 6,
            "open_problems": 12,
        }
    )
    assert "窗口" in detail
    assert "在途" in detail and "排队" in detail
    assert "已通过" in detail
    assert "未解决" in detail
    assert "3.2" in detail  # 等待时长秒
    assert "128" in detail  # 单次尝试窗口（等待期限）


# ---- 验收 6：并发等待分槽呈现 —— 主修复与高级校对并列，不互相覆盖 ----


def test_concurrent_waiting_events_render_both_roles_side_by_side():
    """并发双角色等待：主修复与高级校对并列显示，不互相覆盖（票 08 点验修复）。

    票 04/05 落地后并发成为常态：主修复请求与校对窗口请求各有
    ``WaitRefresher``（0.2s 各自发射）。单槽「最新一条」存储让两类
    消息以 ~10 次/秒交替覆盖（实机闪烁）：主修复已等 230.3s 被
    校对 12.0s 覆盖、又被主修复覆盖……分槽按 role 存储/渲染
    （spec「展开详情展示主修复与高级校对」），detail 同时呈现两行。
    """
    fields: dict = {}
    # 主修复等待与校对等待交替到达（并发真实顺序）。
    for event in (
        diagnostics.waiting_event(
            round_index=1, wait_elapsed_ms=230_300, inflight=1, queued=0,
            window=1, request_window_s=300.0, role="main",
        ),
        diagnostics.waiting_event(
            round_index=1, wait_elapsed_ms=12_000, inflight=3, queued=0,
            window=3, request_window_s=128.0, role="review",
        ),
        # 主修复的下一次刷新：只更新 main 槽，review 槽保留。
        diagnostics.waiting_event(
            round_index=1, wait_elapsed_ms=230_500, inflight=1, queued=0,
            window=1, request_window_s=300.0, role="main",
        ),
    ):
        fields = diagnostics.merge_waiting_event(fields, event)
    detail = diagnostics.render_detail(fields)
    # 两角色并列（不闪烁）：主修复等待时长与校对等待时长同屏。
    assert "主修复" in detail and "高级校对" in detail, detail
    assert "230.3" in detail or "230.5" in detail, detail  # 主修复最新等待
    assert "12.0" in detail, detail  # 校对等待未被主修复覆盖
    # 各角色独立口径：主修复在途 1 / 校对在途 3 同屏。
    assert "在途 1" in detail and "在途 3" in detail, detail


def test_waiting_slot_clears_when_role_goes_silent():
    """角色请求返回后：该槽不再显示，另一角色照常（无陈旧闪烁）。"""
    fields: dict = {}
    for event in (
        diagnostics.waiting_event(
            round_index=1, wait_elapsed_ms=1_000, inflight=1, queued=0,
            window=1, request_window_s=300.0, role="main",
        ),
        diagnostics.waiting_event(
            round_index=1, wait_elapsed_ms=500, inflight=2, queued=0,
            window=2, request_window_s=128.0, role="review",
        ),
    ):
        fields = diagnostics.merge_waiting_event(fields, event)
    # 主修复请求返回（无新 main waiting）：仅校对在途等待。
    fields = diagnostics.drop_waiting_role(fields, "main")
    detail = diagnostics.render_detail(fields)
    assert "主修复" not in detail, detail
    assert "高级校对" in detail, detail
    assert "0.5" in detail, detail
