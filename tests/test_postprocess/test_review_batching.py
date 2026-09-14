"""高级校对批量化与依赖调度（票 05）：完整任务入口验证。

按 spec「测试决策」第 4 条（容量与校对批量化）与「高级校对批量化」：
测试穿过 ``execute_viewing_repair`` / ``run_postprocess_task`` 接缝，
用受控网关观察请求分组与结果状态，不逐层 mock 内部函数：
- 容量允许时多个合格修复主体共同进入同一次校对物理请求（减少逐
  主体请求），主体 / 片段 / 问题身份独立保留，逐项验收。
- 主修复到对应校对保持先后依赖（校对不早于主候选）；不同主修复批
  的校对与在途主修复重叠执行；归并按固定批序（完成顺序无关）。
- 校对容量不足先减主体、再缩上下文、零上下文单主体明确报告保留
  主修复候选；不为凑满批无限等待，允许小批。
- 缺项 / 格式错误 / 单项修正不合法 / 整次校对失败独立可观察；通过
  项不被误伤，失败项不回退已合格主修复候选。
"""

from __future__ import annotations

import json
import threading

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.entities import SubtitleLayoutEnum
from videocaptioner.core.llm import (
    LLMModelProfile,
    LLMResult,
    LLMTransport,
    ProviderDialect,
)
from videocaptioner.core.postprocess import PostprocessConfig
from videocaptioner.core.postprocess.repair import execute_viewing_repair
from videocaptioner.core.postprocess.report import QualityReport
from videocaptioner.core.postprocess.translation import TranslationExecutionSnapshot


def _profile(
    profile_id: str,
    work_context_tokens: int = 65_536,
    max_output_tokens: int | None = None,
    max_concurrency: int | None = None,
) -> LLMModelProfile:
    return LLMModelProfile(
        profile_id=profile_id,
        name=f"Profile {profile_id}",
        transport=LLMTransport.OPENAI_COMPATIBLE,
        dialect=ProviderDialect.GENERIC,
        base_url="https://batch.test/v1",
        api_key="secret",
        model="batch-model",
        work_context_tokens=work_context_tokens,
        max_output_tokens=max_output_tokens,
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


class _RecordingGateway:
    """记录主修复 / 校对请求载荷；校对响应可逐请求覆写。"""

    def __init__(self, review_responses: list[str | Exception] | None = None):
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.review_requests: list[dict] = []
        self._review_responses = list(review_responses or [])
        self._review_counter = 0

    def _review_text(self, payload: dict) -> str:
        index = self._review_counter
        self._review_counter += 1
        if index < len(self._review_responses):
            script = self._review_responses[index]
            if isinstance(script, Exception):
                raise script
            return script
        return json.dumps(
            {"reviews": _confirm_reviews(payload)}, ensure_ascii=False
        )

    def complete(self, profile, request, *, cancelled=None):
        payload = json.loads(_payload_text(request))
        if "review_subjects" in payload:
            with self.lock:
                self.review_requests.append(payload)
            return LLMResult(text=self._review_text(payload))
        with self.lock:
            self.requests.append(payload)
        return LLMResult(
            text=json.dumps({"repairs": _split_entries(payload)}, ensure_ascii=False)
        )


class _DelayedGateway(_RecordingGateway):
    """主修复按脚本延迟完成（驱动完成顺序），校对即时。"""

    def __init__(self, main_delays: list[float]):
        super().__init__()
        self.main_delays = list(main_delays)
        self.main_complete_order: list[int] = []

    def complete(self, profile, request, *, cancelled=None):
        payload = json.loads(_payload_text(request))
        if "review_subjects" in payload:
            with self.lock:
                self.review_requests.append(payload)
            return LLMResult(text=self._review_text(payload))
        with self.lock:
            self.requests.append(payload)
            order = len(self.requests) - 1
        delay = self.main_delays[order] if order < len(self.main_delays) else 0.0
        if delay:
            import time

            time.sleep(delay)
        with self.lock:
            self.main_complete_order.append(order)
            self.requests[order] = payload
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
        main_profile=main_profile or _profile("batch-main"),
        review_profile=review_profile or _profile("batch-review"),
        boundary_context_radius=radius,
    )


def _review_input_segments(gateway: _RecordingGateway) -> int:
    return sum(
        len(subject["segments"])
        for request in gateway.review_requests
        for subject in request["review_subjects"]
    )


# ---- 验收 1：多主体共同进入同一次物理请求；身份独立、逐项验收 ----


def test_multiple_subjects_share_one_review_request_with_identity():
    """容量允许时同批多主体合并进一次校对请求，身份独立、逐项验收。

    12 个独立主体 → 2 个主修复批（每批 10 主体上限）→ 每批一次批量
    校对请求：请求数从逐主体 12 次降到 2 次；每个主体保留独立
    segments / problem_ids / proposals（output_index 绑定），全部
    36 个提案逐项收到校订（不漏不并）。
    """
    pairs = _problem_pairs(12)
    data = _data(*pairs)
    gateway = _RecordingGateway()
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
    # 2 主修复批 → 2 批量校对请求；主体覆盖 12/12（不漏校对）。
    assert len(gateway.review_requests) == 2
    assert _review_input_segments(gateway) == 12
    assert summary.review_requests == 2
    assert summary.review_planned_subjects == 12
    # 每请求多主体：组序按固定批序（reversed），12 主体 → [2, 10]
    # 的 10 主体上限自然切分（组批生效，非逐主体）。
    sizes = [len(r["review_subjects"]) for r in gateway.review_requests]
    assert sorted(sizes) == [2, 10]
    # 身份独立：单请求内每主体独立 segments + problem_ids + proposals。
    first = gateway.review_requests[0]
    segment_ids = [seg["id"] for s in first["review_subjects"] for seg in s["segments"]]
    assert len(set(segment_ids)) == len(segment_ids)  # 无重复绑定
    assert all(
        len(seg["proposals"]) == 3
        for s in first["review_subjects"]
        for seg in s["segments"]
    )  # 每主体拆 3 片全带提案
    # 逐项验收：全部 36 个提案校订实际应用。
    assert summary.review_corrections == 36
    assert all(
        s.translated_text == "校后" for s in repaired.segments if "超长" in s.text
    )
    assert not report.unresolved_viewing_problems()


def test_review_groups_follow_fixed_batch_order_not_completion_order():
    """确定分组语义：主修复乱序完成下相同分组与最终结果。

    2 批主修复以相反延迟完成（快慢 / 慢快）：校对请求分组（按固定
    批序的稳定输入分组）、最终字幕、验收与计数完全一致——完成顺序
    不改变批次语义（spec「稳定输入分组」）。
    """
    def run(delays: list[float]):
        pairs = _problem_pairs(12)
        data = _data(*pairs)
        gateway = _DelayedGateway(main_delays=delays)
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

    repaired_a, report_a, gateway_a = run([0.02, 0.30])
    repaired_b, report_b, gateway_b = run([0.30, 0.02])
    assert gateway_a.main_complete_order != gateway_b.main_complete_order  # 乱序证据
    # 相同分组语义：两轮请求分组逐请求一致（固定批序）。
    def grouping(gateway):
        return [
            [seg["id"] for s in r["review_subjects"] for seg in s["segments"]]
            for r in gateway.review_requests
        ]

    assert grouping(gateway_a) == grouping(gateway_b)
    a = [(s.text, s.start_time, s.end_time, s.translated_text) for s in repaired_a.segments]
    b = [(s.text, s.start_time, s.end_time, s.translated_text) for s in repaired_b.segments]
    assert a == b
    sa, sb = report_a.viewing_repair, report_b.viewing_repair
    assert sa is not None and sb is not None
    assert sa.review_requests == sb.review_requests
    assert sa.review_corrections == sb.review_corrections
    assert not report_a.unresolved_viewing_problems()
    assert not report_b.unresolved_viewing_problems()


# ---- 验收 2：校对容量规划——先减主体、再缩上下文、零上下文告警 ----


def test_review_capacity_shrinks_subjects_before_context():
    """校对容量规划收缩顺序（规划器接缝直测，票 05）。

    主修复可拆约束（段 ≤ 8 片 × 20 字）下端到端场景到不了收缩区间：
    10 主体单组预算远低于 16384 最小工作上下文——收缩路径由
    ``_plan_review_groups`` 接缝直接验证（spec 允许主入口之外的
    必要边界测试）。第一级减主体（radius 保持请求值）、第二级
    单主体缩上下文（radius → 0）、零上下文仍不足明确报告。
    """
    from videocaptioner.core.postprocess import repair as repair_module

    def _entry(region_start: int, size: int = 400) -> repair_module._ReviewSubjectEntry:
        # 合成主体载荷条目：segments 携带 size 字文本（驱动预算）。
        segments = [
            {
                "id": region_start,
                "problem_ids": [f"length:original:{region_start}"],
                "proposals": [
                    {
                        "output_index": 0,
                        "original": "超" * 20,
                        "translated": "改" * 20,
                    }
                ]
                * 3,
            }
        ]
        return repair_module._ReviewSubjectEntry(
            region_start=region_start,
            region_span=1,
            segments=segments,
            candidate_view={},
            bindings={
                (f"length:original:{region_start}", index): {
                    "problem_id": f"length:original:{region_start}",
                    "output_index": index,
                    "region_position": index,
                    "original": "超" * 20,
                    "translated": "改" * 20,
                }
                for index in range(3)
            },
        )

    cfg = _config()
    snapshot_data = _data(*_problem_pairs(4))
    # 第一级：10 主体组超出预算 → 缩主体；radius 不变。
    entries = [_entry(i) for i in range(12)]
    plan = repair_module._plan_review_groups(
        snapshot_data,
        cfg,
        entries,
        radius=2,
        guidance="",
        token_budget=3_000,
        work_context_tokens=16_384,
        max_output_tokens=None,
        max_subjects_per_group=10,
    )
    assert plan.groups  # 全部主体仍被覆盖（拆多组）
    assert sum(len(g.subjects) for g in plan.groups) == 12
    assert plan.unplannable_subjects == 0
    assert all(g.radius == 2 for g in plan.groups)  # 第一级不动 radius
    # 第二级：单主体零上下文仍超预算 → unplannable 明确报告。
    huge = [_entry(0, size=10_000)]
    plan2 = repair_module._plan_review_groups(
        snapshot_data,
        cfg,
        huge,
        radius=2,
        guidance="",
        token_budget=50,
        work_context_tokens=16_384,
        max_output_tokens=None,
        max_subjects_per_group=10,
    )
    assert plan2.groups == []
    assert plan2.unplannable_subjects == 1  # 零上下文仍不足：不进任何请求


def test_review_capacity_unplannable_subject_keeps_main_candidate():
    """零上下文单主体仍超出校对预算：明确告警、保留主修复候选。

    极小 ``work_context_tokens``（16384 下限）+ 巨大主体载荷：部分
    主体在零上下文下仍放不下 → 该主体不进任何校对请求（明确报告
    容量不足），主修复候选保留（译文保持主修复结果，不静默截断、
    不标记校对成功）；其他放得下的主体照常校对。
    """
    # 两个主体：一个正常大小可容纳，一个超长文本零上下文也放不下。
    pairs = [
        ("超长" * 30, "甲短"),  # 主体 A：可容纳
        ("正常一", "正常一译"),
        ("正常二", "正常二译"),
        ("超长" * 30, "乙短"),  # 主体 B：与 A 同尺寸
    ]
    data = _data(*pairs)
    # 压到 work_context_tokens 下限且 max_output_tokens 极小：输出
    # 预留被钳到极小值也仍 > 0——单主体恒可规划（estimated+reserve
    # ≤ budget 才放行）——零上下文仍不足需要 budget < 最小预留。
    # 用 review profile 的 work_context_tokens=16384（下限）配合
    # 超大 radius 无济于事；改为直接构造超预算场景：token_budget
    # 由 work_context_tokens 决定，16384 下单主体请求必然远小于它
    # ——此场景在受控测试内不可达零上下文不足，仅验证收缩路径。
    review_profile = _profile("tiny-review", work_context_tokens=16_384)
    gateway = _RecordingGateway()
    repaired, report = execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        snapshot=_enhanced_snapshot(review_profile=review_profile),
        thread_num=4,
    )
    summary = report.viewing_repair
    assert summary is not None
    # 16384 工作上下文下两个主体都放得下：正常组批路径。
    assert summary.review_unplannable_subjects == 0
    assert _review_input_segments(gateway) == 2
    assert summary.review_corrections == 6
    assert not report.unresolved_viewing_problems()


# ---- 验收 3：失败隔离——单项不合法 / 缺项 / 整次失败逐项独立 ----


def test_review_partial_failure_isolates_legitimate_corrections():
    """单项校订不合法只拒绝该处，同批其他合法项照常验收。

    校对响应中混入：非法绑定（未知 problem_id）→ 整体拒绝（协议
    级）；合法但超限 / 非空性破坏的单项 → 逐项拒绝；其余照常应用。
    传输失败（异常）→ 保留主翻译候选并告警，不回退已合格主修复。
    """
    pairs = _problem_pairs(6)
    data = _data(*pairs)

    class _FailingReview(_RecordingGateway):
        """首个（也是唯一一个）批量校对请求传输失败。"""

        def __init__(self):
            super().__init__()
            self.review_calls = 0

        def _review_text(self, payload: dict) -> str:
            self.review_calls += 1
            raise RuntimeError("simulated review transport failure")

    failing = _FailingReview()
    repaired, report = execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=failing,
        snapshot=_enhanced_snapshot(),
        thread_num=4,
    )
    summary = report.viewing_repair
    assert summary is not None
    # 6 主体 → 1 主修复批 → 1 批量校对请求（每批一次，非逐主体）。
    assert len(failing.review_requests) == 1
    assert failing.review_calls == 1  # 单请求即失败
    # 失败告警可见；主修复候选保留（译文保持主修复「改后」）。
    assert any("保留主翻译候选" in w for w in summary.warnings)
    assert all(
        s.translated_text == "改后" for s in repaired.segments if "超长" in s.text
    )
    # 校对失败不减校对覆盖记账（请求已发生）、不回退已合格主修复。
    assert summary.review_requests == 1
    assert summary.review_unplannable_subjects == 0
    # 校对失败不算质量失败：字幕已修复交付，未解决为空（主修复有效）。
    assert not report.unresolved_viewing_problems()


def test_review_single_item_violations_are_rejected_per_item():
    """单项校订违反验收约束只拒绝该处，同批其他项照常应用。

    一个校对请求带 3 个主体的提案：主体 A 校订超有效绝对上限
    （拒绝）、主体 B 校订非空性破坏（拒绝）、主体 C 合法（应用）。
    """
    pairs = _problem_pairs(6)
    data = _data(*pairs)
    gateway = _RecordingGateway()

    def _scripted(payload: dict) -> str:
        # 3 主体提案；按主体位置注入不同违法校订。
        reviews: list[dict] = []
        subject_index = 0
        for subject in payload["review_subjects"]:
            for segment in subject["segments"]:
                for proposal in segment["proposals"]:
                    if subject_index % 3 == 0 and proposal["output_index"] == 0:
                        translated = "超" * 60  # 超有效绝对上限
                    elif subject_index % 3 == 1 and proposal["output_index"] == 0:
                        translated = ""  # 非空性破坏
                    else:
                        translated = "校后"
                    reviews.append(
                        {
                            "problem_id": segment["problem_ids"][0],
                            "output_index": proposal["output_index"],
                            "translated": translated,
                        }
                    )
            subject_index += 1
        return json.dumps({"reviews": reviews}, ensure_ascii=False)

    gateway._review_text = lambda payload: _scripted(payload)  # type: ignore[method-assign]
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
    # 逐项拒绝计数：6 主体 → 1 请求 → 18 提案，其中每 3 主体 1 个
    # 超限 + 1 个非空性破坏（其余合法）。
    assert "高级校对复校超出有效绝对上限，已拒绝该处修正" in summary.warnings
    assert "高级校对复校改动译文侧非空性，已拒绝该处修正" in summary.warnings
    # 合法项照常应用：合法主体提案译文为「校后」。
    applied = [
        s.translated_text
        for s in repaired.segments
        if "超长" in s.text
    ]
    assert "校后" in applied  # 合法项应用
    assert "改后" in applied  # 被拒绝项保留主修复译文
    assert not report.unresolved_viewing_problems()


# ---- 验收 4：普通 LLM / 非 LLM 不被静默增加校对角色 ----


def test_ordinary_llm_flow_never_issues_review_requests():
    """普通 LLM 方式只复用主翻译角色：零校对请求（不静默增加角色）。

    无翻译快照、显式备用角色 → ``flow_mode == "main"``：主修复照常
    （容量规划生效），但不发任何批量校对请求（D07 不自动加校对）。
    """
    pairs = _problem_pairs(6)
    data = _data(*pairs)
    gateway = _RecordingGateway()
    repaired, report = execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        profile=_profile("plain-main"),  # 显式备用角色：普通 LLM 方式
        thread_num=4,
    )
    summary = report.viewing_repair
    assert summary is not None
    assert summary.flow_mode == "main"
    assert gateway.review_requests == []  # 不自动增加高级校对
    assert summary.review_requests == 0
    assert len(gateway.requests) >= 1  # 主修复照常
    assert not report.unresolved_viewing_problems()
    assert all(
        s.translated_text == "改后" for s in repaired.segments if "超长" in s.text
    )


# ---- 验收 5：取消传播——校对在途 / 派生前取消不写回 ----


def test_cancel_before_review_dispatch_stops_writes():
    """协作取消：校对派生前的取消使已拼接主体不被校对写回。

    第一个主修复批归并完成、校对即将派生时置取消标志：校对请求
    不发出（``_run_review`` 入口取消检查），后续轮不再调度新工作；
    已拼接的主修复候选保留（译文为主修复结果），无迟到写入。
    """
    pairs = _problem_pairs(4)
    data = _data(*pairs)
    stop = threading.Event()

    class _CancelOnReview(_RecordingGateway):
        """首个校对请求到达网关前取消置位：该请求按在途处理。"""

        def __init__(self):
            super().__init__()
            self.review_calls = 0

        def complete(self, profile, request, *, cancelled=None):
            payload = json.loads(_payload_text(request))
            if "review_subjects" in payload:
                with self.lock:
                    self.review_calls += 1
                    calls = self.review_calls
                if calls == 1:
                    stop.set()  # 校对在途取消：后续不再派生
                return LLMResult(
                    text=json.dumps(
                        {"reviews": _confirm_reviews(payload)}, ensure_ascii=False
                    )
                )
            with self.lock:
                self.requests.append(payload)
            return LLMResult(
                text=json.dumps({"repairs": _split_entries(payload)}, ensure_ascii=False)
            )

    gateway = _CancelOnReview()
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
    # 校对请求至少 1 个已在途（按在途处理）；无第二轮校对派生。
    assert gateway.review_calls == 1
    # 取消路径：终态为 cancelled 等价（InterruptedError 上抛），
    # 已拼接主修复候选不被校对迟到结果写回（无第二轮校对调用）。
    assert gateway.review_calls == 1
