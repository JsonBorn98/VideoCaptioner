"""主修复受控并发与配置贯通（票 04，ADR-0018）：完整任务入口验证。

按 spec「测试决策」第 3 条（并发与依赖）与「有界并发与入口配置」：
测试穿过 ``execute_viewing_repair`` / ``run_postprocess_task`` 接缝，
用受控网关从外部观察请求重叠与结果状态，不用易抖动短 sleep 判断重叠
（用受控屏障 + 进入计数），不逐层 mock 内部函数：
- 有足够独立批且有效额度 > 1 时，多个主修复请求真实重叠；并发为 1
  时仍正确运行；暖缓存命中不冒充并发（缓存命中不出 adapter）。
- GUI 独立 / 编排 / CLI 的现有并发设置传递到实际执行，任务开始冻结；
  不悄悄退回隐藏网关默认值，不新增重复旋钮。
- 同 profile 请求共享有效保护上限，显式夹钳生效；主修复 / 复校 /
  重试的嵌套调度不使上限相乘。
- 任意响应完成顺序得到 02 定义的相同字幕、报告与回退（确定性归并
  在并发路径下保持）；协作取消后迟到候选不写回、不继续调度新工作。
"""

from __future__ import annotations

import json
import threading
import time

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.entities import SubtitleLayoutEnum
from videocaptioner.core.llm import (
    LLMModelProfile,
    LLMResult,
    LLMTransport,
    ProviderDialect,
)
from videocaptioner.core.postprocess import PostprocessConfig
from videocaptioner.core.postprocess.models import PostprocessLayoutMode, PostprocessTask
from videocaptioner.core.postprocess.repair import execute_viewing_repair
from videocaptioner.core.postprocess.report import QualityReport
from videocaptioner.core.postprocess.runner import run_postprocess_task
from videocaptioner.core.postprocess.translation import TranslationExecutionSnapshot


def _profile(
    profile_id: str = "concurrent-profile",
    max_concurrency: int | None = None,
) -> LLMModelProfile:
    return LLMModelProfile(
        profile_id=profile_id,
        name=f"Profile {profile_id}",
        transport=LLMTransport.OPENAI_COMPATIBLE,
        dialect=ProviderDialect.GENERIC,
        base_url="https://concurrent.test/v1",
        api_key="secret",
        model="concurrent-model",
        work_context_tokens=16_384,
        max_concurrency=max_concurrency,
    )


def _data(*pairs: tuple[str, str]) -> ASRData:
    return ASRData(
        [ASRDataSeg(text, i * 4000, i * 4000 + 4000, tr) for i, (text, tr) in enumerate(pairs)]
    )


def _config(**overrides) -> PostprocessConfig:
    return PostprocessConfig(trim_trailing_punct=False, **overrides)


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
    """``count`` 个相互独立的问题主体（偶数段超长），中间留安全上下文段。"""
    pairs: list[tuple[str, str]] = []
    for index in range(count * spacing):
        if index % spacing == 0:
            pairs.append(("超长" * 30, f"甲{index}短"))
        else:
            pairs.append((f"正常{index}", f"正{index}译"))
    return pairs


class _BarrierGateway:
    """受控屏障网关：主修复请求在屏障处等待首个窗口成员到齐。

    重叠判定不用短 sleep：每个主修复请求先登记进入，``release`` 前
    ``complete`` 阻塞在 ``threading.Barrier``（窗口大小）——并发为 1 时
    立即放行，不伪造重叠。滑动窗口下超过窗口数的后续请求（前面成员
    完成后才进入）直接放行，不因窗口收缩后的尾批误判为无重叠。
    复校原样确认。
    """

    def __init__(self, *, barrier_size: int, review_translated: str = "校后"):
        self.barrier = threading.Barrier(barrier_size, timeout=10)
        self.review_translated = review_translated
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.entered: list[float] = []  # 进入时刻（进入序号顺序追加）
        self.completed_at: list[tuple[int, float]] = []  # (完成进入序号, 时刻)
        self.review_requests: list[dict] = []
        self._counter = 0

    def _next_entry(self) -> int:
        with self.lock:
            self._counter += 1
            return self._counter

    def complete(self, profile, request, *, cancelled=None):
        payload = json.loads(_payload_text(request))
        if "review_subjects" in payload:
            with self.lock:
                self.review_requests.append(payload)
            return LLMResult(
                text=json.dumps(
                    {"reviews": _confirm_reviews(payload, self.review_translated)},
                    ensure_ascii=False,
                )
            )
        entry = self._next_entry()
        with self.lock:
            self.requests.append(payload)
            first_window = entry <= self.barrier.parties
        if first_window:
            # 首个窗口成员到齐才放行：真实重叠证明（非时序推断）。
            self.barrier.wait()
        with self.lock:
            self.completed_at.append((entry, threading.currentThread().ident))
        return LLMResult(
            text=json.dumps({"repairs": _split_entries(payload)}, ensure_ascii=False)
        )


class _OrderedGateway:
    """记录请求并允许主修复响应乱序返回（完成顺序与请求顺序解耦）。"""

    def __init__(self, main_delays: list[float] | None = None):
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.main_start_order: list[int] = []
        self.main_complete_order: list[int] = []
        self.main_delays = main_delays or []

    def complete(self, profile, request, *, cancelled=None):
        payload = json.loads(_payload_text(request))
        if "review_subjects" in payload:
            return LLMResult(
                text=json.dumps({"reviews": _confirm_reviews(payload)}, ensure_ascii=False)
            )
        with self.lock:
            self.requests.append(payload)
            order = len(self.requests) - 1
        delay = self.main_delays[order] if order < len(self.main_delays) else 0.0
        if delay:
            import time

            time.sleep(delay)
        with self.lock:
            self.main_complete_order.append(order)
        return LLMResult(
            text=json.dumps({"repairs": _split_entries(payload)}, ensure_ascii=False)
        )


def _enhanced_snapshot(
    main_profile: LLMModelProfile | None = None,
    review_profile: LLMModelProfile | None = None,
    radius: int = 2,
) -> TranslationExecutionSnapshot:
    return TranslationExecutionSnapshot(
        method="enhanced_llm",
        main_profile=main_profile or _profile("concurrent-main"),
        review_profile=review_profile or _profile("concurrent-review"),
        boundary_context_radius=radius,
    )


# ---- 验收 1：受控屏障证明真实重叠；并发为 1 仍正确 ----


def test_concurrent_main_requests_actually_overlap():
    """额度 > 1 且有独立批：多个主修复请求在窗口内真实重叠。

    12 个独立问题主体 → 每批 10 主体 → 同轮 2 批；线程数 4 → 窗口 2。
    网关屏障在两批都已进入 ``complete`` 后才放行——屏障不满足即超时，
    证明重叠不靠短 sleep 时序推断。归并后最终字幕与 02 确定性语义一致。
    """
    pairs = _problem_pairs(12)
    data = _data(*pairs)
    gateway = _BarrierGateway(barrier_size=2)
    repaired, report = execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        snapshot=_enhanced_snapshot(),
        thread_num=4,
    )
    summary = report.viewing_repair
    assert summary is not None
    # 两批同轮并发发出：请求计数、窗口与在途观测一致。
    assert summary.requests == 2
    assert summary.thread_num == 4
    assert summary.concurrency_gate == 4
    assert summary.effective_concurrency == 2  # 批数 < 闸：窗口收缩到批数
    assert summary.max_inflight >= 2
    assert len(gateway.requests) == 2
    # 屏障通过 = 两批重叠在途（否则 BarrierTimeoutError 直接失败）。
    # 原文保护 + 全部主体拆分 + 无未解决问题（02 语义保持）。
    assert "".join("".join(s.text.split()) for s in repaired.segments) == "".join(
        "".join(t.split()) for t, _ in pairs
    )
    assert not report.unresolved_viewing_problems()
    # 全部问题主体拆分为 ≤20 字片段，译文经复校确认。
    assert all(
        len(s.text) <= 20 for s in repaired.segments if s.text.startswith("超长")
    )
    assert all(
        s.translated_text == "校后" for s in repaired.segments if s.text.startswith("超长")
    )


def test_concurrency_one_runs_correctly_without_fake_overlap():
    """并发为 1：窗口 = 1，串行路径仍正确完成修复（不伪造并发）。"""
    pairs = _problem_pairs(12)
    data = _data(*pairs)
    gateway = _BarrierGateway(barrier_size=1)  # 窗口 1：屏障立即可过
    repaired, report = execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        snapshot=_enhanced_snapshot(),
        thread_num=1,
    )
    summary = report.viewing_repair
    assert summary is not None
    assert summary.thread_num == 1
    assert summary.concurrency_gate == 1
    assert summary.effective_concurrency == 1
    assert summary.max_inflight >= 1
    assert not report.unresolved_viewing_problems()
    assert all(
        len(s.text) <= 20 for s in repaired.segments if s.text.startswith("超长")
    )
    assert summary.review_corrections == 36  # 12 主体 × 3 片全部复校确认


def test_few_batches_do_not_wait_for_serial_warmup():
    """少量批次不因固定首批串行预热被强制串行。

    4 个独立主体（单批 4 ≤ 每批 10 上限）→ 1 批 + 并发闸 4 → 单请求；
    多批场景（12 主体 → 2 批）下窗口立即开满 2，没有「首批先串行完成
    才放行第二批」的预热屏障（屏障大小 = 窗口大小已证明）。
    """
    pairs = _problem_pairs(4)
    data = _data(*pairs)
    gateway = _BarrierGateway(barrier_size=1)
    repaired, report = execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        snapshot=_enhanced_snapshot(),
        thread_num=4,
    )
    summary = report.viewing_repair
    assert summary is not None
    assert summary.requests == 1  # 4 主体 → 1 批
    assert summary.effective_concurrency == 1
    assert summary.concurrent_rounds == 0  # 单批无并发轮
    assert not report.unresolved_viewing_problems()
    # 单批 4 主体全拆分（一对多 + 独立主体同批）。
    assert all(
        len(s.text) <= 20 for s in repaired.segments if s.text.startswith("超长")
    )


# ---- 验收 2：完成顺序无关 —— 并发下确定性归并保持（02 语义）----


def test_completion_order_does_not_change_results_under_concurrency():
    """乱序完成（后发先至 / 先发后至）产生相同最终字幕与报告。

    并发路径下 2 批主修复以不同延迟完成：延迟组合 A（快慢）与 B（慢快）
    的最终字幕逐段一致、验收 / 回退 / 计数一致——固定批序归并与完成
    顺序解耦（ADR-0021 在并发执行下保持）。
    """
    def run(delays: list[float]):
        pairs = _problem_pairs(12)
        data = _data(*pairs)
        gateway = _OrderedGateway(main_delays=delays)
        repaired, report = execute_viewing_repair(
            data,
            _config(),
            QualityReport(),
            SubtitleLayoutEnum.ORIGINAL_ON_TOP,
            gateway=gateway,
            snapshot=_enhanced_snapshot(),
            thread_num=4,
        )
        return repaired, report, gateway

    repaired_a, report_a, gateway_a = run([0.02, 0.30])  # 批 1 先完成
    repaired_b, report_b, gateway_b = run([0.30, 0.02])  # 批 2 先完成（乱序）
    # 乱序证据：B 的完成顺序确实与请求顺序相反。
    assert gateway_b.main_complete_order != gateway_a.main_complete_order
    a = [(s.text, s.start_time, s.end_time, s.translated_text) for s in repaired_a.segments]
    b = [(s.text, s.start_time, s.end_time, s.translated_text) for s in repaired_b.segments]
    assert a == b  # 相同字幕、时间轴与译文
    sa, sb = report_a.viewing_repair, report_b.viewing_repair
    assert sa is not None and sb is not None
    assert sa.requests == sb.requests
    assert sa.spliced_fragments == sb.spliced_fragments
    assert sa.resolved_problem_count == sb.resolved_problem_count
    assert sa.review_corrections == sb.review_corrections
    assert not report_a.unresolved_viewing_problems()
    assert not report_b.unresolved_viewing_problems()


# ---- 验收 3：同 profile 保护上限；嵌套调度不相乘 ----


def test_profile_clamp_lowers_window():
    """显式 profile 保护上限生效：thread_num 4 / clamp 2 → 窗口 2。

    同 profile 的主修复与复校共享该闸（ADR-0018：``clamped_concurrency``
    唯一钳制口径，main/review 各自吃满，不嵌套相乘）。
    """
    pairs = _problem_pairs(22)  # → 3 批
    data = _data(*pairs)
    clamped = _profile("clamped-main", max_concurrency=2)
    review = _profile("clamped-review", max_concurrency=2)
    gateway = _BarrierGateway(barrier_size=2)  # 3 批 / 闸 2：滑动窗口 2
    repaired, report = execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        snapshot=_enhanced_snapshot(main_profile=clamped, review_profile=review),
        thread_num=4,
    )
    summary = report.viewing_repair
    assert summary is not None
    assert summary.thread_num == 4
    assert summary.concurrency_gate == 2  # 夹钳生效，不取任务值 4
    assert summary.effective_concurrency == 2
    # 3 批两滑动窗（10+10 主体 / 2 主体）：全部请求经屏障成对重叠。
    assert summary.requests == 3
    assert not report.unresolved_viewing_problems()
    # 复校与主修复同 profile 闸：复校请求数 = 主体数（每主体一次，票 05 前保持）。
    assert len(gateway.review_requests) == 22


def test_higher_thread_num_does_not_exceed_profile_clamp():
    """thread_num 调大不被隐藏上限压回（ADR-0018 回归），且 clamp 只降不升。"""
    profile_none = _profile("noclamp")
    assert profile_none.clamped_concurrency(7) == 7  # None 不夹
    profile_clamp = _profile("clamp3", max_concurrency=3)
    assert profile_clamp.clamped_concurrency(7) == 3
    assert profile_clamp.clamped_concurrency(2) == 2  # 不抬升任务值


# ---- 验收 4：配置贯通 —— GUI / 编排 / CLI 传递到实际执行并冻结 ----


def test_full_task_entry_carries_frozen_thread_num(tmp_path):
    """完整任务入口：``thread_num`` 冻结进任务并到达修复执行。

    ``run_postprocess_task`` 从 ``task.thread_num`` 读取（GUI 编排由
    ``TaskFactory`` 冻结 ``cfg.thread_num``；CLI 由配置 + 参数冻结），
    修复摘要与过程状态记录实际生效值，不悄悄回退隐藏网关默认。
    """
    source = tmp_path / "input.srt"
    source.write_text("placeholder\n", encoding="utf-8")
    gateway = _BarrierGateway(barrier_size=2)
    task = PostprocessTask(
        str(source),
        postprocessed_subtitle_path=str(tmp_path / "result.srt"),
        input_data=_data(*_problem_pairs(12)),
        layout_mode=PostprocessLayoutMode.ORIGINAL_ON_TOP,
        workflow_base_name="concurrent",
        source_language="zh",
        target_language="en",
        config_snapshot=_config(qa_report=True),
        thread_num=4,
        translation_snapshot=_enhanced_snapshot(),
    )
    result = run_postprocess_task(task, gateway=gateway)
    assert result.succeeded and result.continue_downstream
    summary = result.report.viewing_repair
    assert summary is not None
    assert summary.thread_num == 4
    assert summary.concurrency_gate == 4
    assert summary.effective_concurrency == 2
    assert summary.requests == 2
    # 过程状态载荷记录冻结值与实际生效闸（可观察有效值）。
    discovery = task.asset_discovery
    assert discovery is not None
    state_path = discovery.asset_path("postprocess_state")
    assert state_path is not None and state_path.is_file()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    repair_state = state["viewing_repair"]
    assert repair_state["thread_num"] == 4
    assert repair_state["concurrency_gate"] == 4
    assert repair_state["effective_concurrency"] == 2
    assert repair_state["max_inflight"] >= 2
    # 归并质量门槛：原文保护、全拆分、无未解决。
    assert not result.report.unresolved_viewing_problems()
    assert all(
        len(s.text) <= 20 for s in result.output_data.segments if s.text.startswith("超长")
    )


def test_default_thread_num_is_authoritative_ten():
    """未提供任务并发时冻结权威默认 10（ADR-0018），不悄悄回退隐藏值。

    旧调用方（``thread_num=None``）与非法值（0 / 负数 / 非整）都取
    权威默认并记录，核心不抛错、不改用户配置。
    """
    data = _data(*_problem_pairs(4))
    for thread_num in (None, 0, -3, "four"):
        gateway = _BarrierGateway(barrier_size=1)
        repaired, report = execute_viewing_repair(
            data,
            _config(),
            QualityReport(),
            SubtitleLayoutEnum.ORIGINAL_ON_TOP,
            gateway=gateway,
            snapshot=_enhanced_snapshot(),
            thread_num=thread_num,
        )
        summary = report.viewing_repair
        assert summary is not None
        assert summary.thread_num == 10
        assert summary.concurrency_gate == 10
        assert summary.requests == 1
        assert not report.unresolved_viewing_problems()


def test_cli_postprocess_freezes_thread_num_from_config(tmp_path):
    """CLI 入口：``subtitle.thread_num`` 配置冻结进任务（与 GUI 同一旋钮）。

    CLI ``postprocess`` 命令从共享配置解析（显式参数 > ``subtitle.thread_num``
    > 默认 10）；不新增后处理专用并发旋钮。
    """
    from argparse import Namespace

    from videocaptioner.cli.commands import postprocess as postprocess_command
    from videocaptioner.core import postprocess as postprocess_package

    captured = {}

    def fake_run_postprocess_task(task, **kwargs):
        captured["thread_num"] = task.thread_num
        captured["gateway"] = kwargs.get("gateway")

        class Result:
            succeeded = True
            used_fallback = False
            warnings = ()
            output_data = ASRData([ASRDataSeg("你好", 0, 1000, "hello")])
            layout = None
            continue_downstream = True

            @property
            def report(self_inner):
                class _Report:
                    speed = None
                    viewing_repair = None
                    viewing_problems = []

                    def unresolved_viewing_problems(self_inner):
                        return []

                return _Report()

            @property
            def task(self_inner):
                return task

        task.status = "completed"
        task.active_subtitle_path = str(tmp_path / "【后处理字幕】cli.srt")
        task.postprocessed_subtitle_path = task.active_subtitle_path
        return Result()

    original = postprocess_package.run_postprocess_task
    postprocess_package.run_postprocess_task = fake_run_postprocess_task
    try:
        source = tmp_path / "cli.srt"
        source.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        args = Namespace(
            input=str(source),
            output=None,
            layout="source-only",
            profile="balanced",
            speed_profile=None,
            media=None,
            speed_media=None,
            quiet=True,
            verbose=False,
            thread_num=None,
        )
        config = {
            "subtitle": {"thread_num": 6},
            "llm": {"profile_id": "main-profile"},
            # 不发起工具角色请求（核心入口已被 fake）：避免依赖本机方案库。
            "postprocess": {"speed_optimize": False, "qa_report": True},
        }
        result = postprocess_command.run(args, config)
        assert result == 0
        assert captured["thread_num"] == 6  # 配置值冻结，不是隐藏默认 10
        # 显式参数优先于配置。
        args.thread_num = 3
        assert postprocess_command.run(args, config) == 0
        assert captured["thread_num"] == 3
        # 两者皆缺 → 权威默认 10。
        args.thread_num = None
        del config["subtitle"]
        assert postprocess_command.run(args, config) == 0
        assert captured["thread_num"] == 10
    finally:
        postprocess_package.run_postprocess_task = original


# ---- 验收 5：取消传播 —— 不继续调度 / 归并前重确认 ----


def test_cancel_during_inflight_stops_merge_of_late_candidates():
    """协作取消：在途等待期间取消 → 已返回候选不写回、不发新工作。

    第一个主修复请求进入网关后置取消标志：并发路径下窗口内其他请求
    在发送前检查取消（不再发起），已发出请求按在途处理；归并前
    ``_raise_if_cancelled`` 阻止迟到候选写回共享字幕（停止后的写入竞态）。
    """
    pairs = _problem_pairs(22)  # → 3 批 / 窗口 2
    data = _data(*pairs)
    stop = threading.Event()

    class _CancelGateway(_OrderedGateway):
        """首个主修复请求返回后取消置位：其余在途 / 归并全部停止。

        取消锚定在真实请求开始后（请求 1 立即返回 → 标志置位）；
        协调器在归并前重确认取消（迟到候选不写回），窗口内后续
        worker 在发送前检查取消不再发起新请求。
        """

        def __init__(self):
            super().__init__()
            self._main_calls = 0
            self._lock = threading.Lock()

        def complete(self, profile, request, *, cancelled=None):
            payload = json.loads(_payload_text(request))
            if "review_subjects" in payload:
                return LLMResult(
                    text=json.dumps({"reviews": _confirm_reviews(payload)}, ensure_ascii=False)
                )
            with self._lock:
                self._main_calls += 1
                first = self._main_calls == 1
            result = super().complete(profile, request, cancelled=cancelled)
            if first:
                stop.set()  # 首个请求返回瞬间取消：后续全部停止
            return result

    gateway = _CancelGateway()
    report = QualityReport()
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
        raise AssertionError("expected InterruptedError")
    except InterruptedError:
        pass
    summary = report.viewing_repair
    assert summary is not None
    # 请求已发出（窗口内至少 1 个），但没有任何批次归并写回。
    assert summary.requests >= 1
    assert summary.spliced_fragments == 0
    assert summary.resolved_problem_count == 0


def test_transport_failure_in_one_batch_keeps_other_batches_merging():
    """并发路径局部传输失败：失败批不消耗业务重试，其余批照常归并。"""
    pairs = _problem_pairs(22)  # → 3 批
    data = _data(*pairs)

    class _OneFailingGateway(_OrderedGateway):
        def __init__(self):
            super().__init__()
            self._failed = False
            self.lock2 = threading.Lock()

        def complete(self, profile, request, *, cancelled=None):
            payload = json.loads(_payload_text(request))
            if "review_subjects" in payload:
                return LLMResult(
                    text=json.dumps({"reviews": _confirm_reviews(payload)}, ensure_ascii=False)
                )
            with self.lock2:
                first = not self._failed
                self._failed = True
            if first:
                raise RuntimeError("simulated transport failure")
            return super().complete(profile, request, cancelled=cancelled)

    gateway = _OneFailingGateway()
    repaired, report = execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        snapshot=_enhanced_snapshot(),
        thread_num=4,
    )
    summary = report.viewing_repair
    assert summary is not None
    # 一批传输失败不影响同轮其他批归并（失败批进入下一轮重试语义）。
    assert summary.spliced_fragments > 0
    # 传输失败轮不消耗业务重试：失败批下一轮正常请求（未被回退耗尽）。
    assert not report.unresolved_viewing_problems()
    assert all(
        len(s.text) <= 20 for s in repaired.segments if s.text.startswith("超长")
    )


# ---- 验收 6：暖缓存不冒充并发提速（spec：缓存命中不冒充真实模型并发）----


def test_warm_cache_real_gateway_zero_attempts(tmp_path):
    """真实 ``LLMGateway`` + 受控 adapter + 暖缓存：复跑零 adapter 尝试。

    穿过真实网关（含 per-profile 信号量与响应缓存路径）证明缓存命中
    不是 adapter 尝试：第一次任务产生缓存源，第二次任务 logical 请求
    照常发出、全部命中缓存、adapter ``complete`` 零调用（修复执行的
    验收 / 归并不因缓存跳过，提速不冒充模型并发）。
    pytest conftest 全局关闭缓存：本测试在受控范围内临时启用并复原
    （与基准 ``run_case`` 同一管理模式），缓存隔离在 tmp 磁盘目录。
    """
    from diskcache import Cache

    from videocaptioner.core.llm.gateway import LLMGateway
    from videocaptioner.core.llm.response_cache import GatewayResponseCache
    from videocaptioner.core.utils import cache as cache_control

    response_cache = GatewayResponseCache(cache=Cache(str(tmp_path / "cache")))
    adapter_attempts = {"main": 0, "review": 0}
    adapter_lock = threading.Lock()

    class _CountingAdapter:
        def __init__(self, profile):
            self.profile = profile

        def complete(self, request):
            payload = json.loads(_payload_text(request))
            role = "review" if "review_subjects" in payload else "main"
            with adapter_lock:
                adapter_attempts[role] += 1
            import time

            time.sleep(0.02 if role == "main" else 0.005)
            if role == "review":
                text = json.dumps(
                    {"reviews": _confirm_reviews(payload)}, ensure_ascii=False
                )
            else:
                text = json.dumps({"repairs": _split_entries(payload)}, ensure_ascii=False)
            return LLMResult(text=text)

        def close(self):
            pass

    class _CountingGateway:
        """真实网关外包一层：只注入 adapter 工厂与缓存，不转发语义。"""

        def __init__(self):
            self.runtime = LLMGateway(
                adapter_factory=_CountingAdapter,
                response_cache=response_cache,
                sleep=time.sleep,
                random_source=lambda: 0.5,
            )

        def complete(self, profile, request, **kwargs):
            return self.runtime.complete(profile, request, **kwargs)

        def close(self):
            self.runtime.close()

    data = _data(*_problem_pairs(12))
    gateway = _CountingGateway()
    was_enabled = cache_control.is_cache_enabled()
    cache_control.enable_cache()
    try:

        def run_once():
            return execute_viewing_repair(
                data,
                _config(),
                QualityReport(),
                SubtitleLayoutEnum.ORIGINAL_ON_TOP,
                gateway=gateway,
                snapshot=_enhanced_snapshot(),
                thread_num=4,
            )

        repaired_cold, report_cold = run_once()
        assert not report_cold.unresolved_viewing_problems()
        assert adapter_attempts["main"] == 2  # 冷：2 批真实 adapter 调用
        assert adapter_attempts["review"] == 12

        repaired_warm, report_warm = run_once()
        summary = report_warm.viewing_repair
        assert summary is not None
        # 暖：logical 请求照常发生（请求计数进 summary），但零新增 adapter 尝试。
        assert summary.requests >= 2
        assert adapter_attempts["main"] == 2  # 无新增
        assert adapter_attempts["review"] == 12  # 无新增
        # 缓存命中复跑产生逐段一致的修复结果（确定性归并对缓存命中同样成立）。
        a = [
            (s.text, s.start_time, s.end_time, s.translated_text)
            for s in repaired_cold.segments
        ]
        b = [
            (s.text, s.start_time, s.end_time, s.translated_text)
            for s in repaired_warm.segments
        ]
        assert a == b
        assert not report_warm.unresolved_viewing_problems()
    finally:
        if not was_enabled:
            cache_control.disable_cache()
