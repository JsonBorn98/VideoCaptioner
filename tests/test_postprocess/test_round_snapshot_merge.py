"""修复轮次快照与确定性归并（票 02，ADR-0021）：完整任务入口验证。

按 spec「Testing Decisions」与 P08：测试穿过 ``run_postprocess_task`` /
``execute_viewing_repair`` 接缝，用受控网关从外部观察请求载荷与结果状态：
- 同轮各批主修复请求的主体与边界上下文来自统一修复轮次快照；
  相邻批的拆分不改变其他请求载荷（回退在轮内只发生在归并阶段，
  下一轮才读取归并后的字幕，天然不污染载荷）。
- 高级校对读取本轮底稿叠加对应主修复候选的明确版本，
  不读取邻批（含同批更高段序主体）刚完成的归并结果。
- 候选按固定段序归并：响应数组顺序不同产生相同最终字幕、
  问题验收、计数与区域回退（不要求真实模型两次生成逐字相同）。
不逐层 mock 内部函数，不锁定内部方法调用顺序。
"""

from __future__ import annotations

import json

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


def _profile(profile_id: str = "round-profile") -> LLMModelProfile:
    return LLMModelProfile(
        profile_id=profile_id,
        name=f"Profile {profile_id}",
        transport=LLMTransport.OPENAI_COMPATIBLE,
        dialect=ProviderDialect.GENERIC,
        base_url="https://round.test/v1",
        api_key="secret",
        model="round-model",
        work_context_tokens=16_384,
    )


def _data(*pairs: tuple[str, str]) -> ASRData:
    return ASRData(
        [ASRDataSeg(text, i * 4000, i * 4000 + 4000, tr) for i, (text, tr) in enumerate(pairs)]
    )


def _config(**overrides) -> PostprocessConfig:
    return PostprocessConfig(trim_trailing_punct=False, **overrides)


def _response(repairs: list[dict]) -> str:
    return json.dumps({"repairs": repairs}, ensure_ascii=False)


def _review_response_text(reviews: list[dict]) -> str:
    return json.dumps({"reviews": reviews}, ensure_ascii=False)


def _payload_text(request) -> str:
    user = next(m.content for m in request.messages if m.role == "user")
    return user.split("<input>", 1)[1].split("</input>", 1)[0]


def _split_entries(payload: dict, chunk: int = 20, translated: str = "改后") -> list[dict]:
    """按请求载荷构造合法主修复响应：每段原文（紧凑字符）切成 ≤chunk 字片段。"""
    repairs: list[dict] = []
    for subject in payload["repair_subjects"]:
        for segment in subject["segments"]:
            repairs.extend(_split_entries_for_segment(segment, chunk=chunk, translated=translated))
    return repairs


def _confirm_reviews(payload: dict, translated: str = "校后") -> list[dict]:
    """按复校请求载荷构造原样确认的 reviews 响应（译文改为 ``translated``）。"""
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


def _split_entries_for_segment(segment: dict, chunk: int = 20, translated: str = "改后") -> list[dict]:
    """按单个段载荷构造合法拆分候选（供失败网关复用既有拆分循环）。"""
    pid = segment["problem_ids"][0]
    compact = "".join(segment["text"].split())
    pieces = [compact[i : i + chunk] for i in range(0, len(compact), chunk)]
    return [
        {"problem_id": pid, "output_index": index, "original": piece, "translated": translated}
        for index, piece in enumerate(pieces)
    ]


class _RecordingGateway:
    """记录全部请求载荷；主修复经 ``_main_repairs`` 钩子（默认按载荷就地
    拆分），复校原样确认。子类只覆写 ``_main_repairs`` 即可改变主修复
    应答，不必复制 complete 的解析 / 记录 / 复校分支。"""

    def __init__(self, main_translated: str = "改后", review_translated: str = "校后"):
        self.requests: list[dict] = []
        self.main_translated = main_translated
        self.review_translated = review_translated

    def _main_repairs(self, payload: dict) -> list[dict]:
        return _split_entries(payload, translated=self.main_translated)

    def complete(self, profile, request, *, cancelled=None):
        payload = json.loads(_payload_text(request))
        self.requests.append(payload)
        if "review_subjects" in payload:
            return LLMResult(
                text=_review_response_text(_confirm_reviews(payload, self.review_translated))
            )
        return LLMResult(text=_response(self._main_repairs(payload)))

    def main_payloads(self) -> list[dict]:
        return [p for p in self.requests if "repair_subjects" in p]

    def review_payloads(self) -> list[dict]:
        return [p for p in self.requests if "review_subjects" in p]


def _enhanced_snapshot(radius: int = 3) -> TranslationExecutionSnapshot:
    return TranslationExecutionSnapshot(
        method="enhanced_llm",
        main_profile=_profile("round-main"),
        review_profile=_profile("round-review"),
        boundary_context_radius=radius,
    )


# ---- 验收 1：同轮各批请求读取统一轮次快照，邻批拆分不改变其他请求载荷 ----


def test_same_round_batches_share_one_round_snapshot():
    """同一轮内多批主修复请求的主体与边界上下文来自同一轮次快照。

    25 段输入、12 个独立问题主体（偶数段超长）→ 每批最多 10 主体 →
    同轮两批。高段序批（主体 20/22）先归并；低段序批（主体 0-18）的
    载荷后构建。统一快照语义下，低段序批的边界上下文必须仍读到
    高段序主体的初版未拆分形态，而不是其已归并的拆分碎片。
    """
    pairs: list[tuple[str, str]] = []
    for index in range(25):
        if index % 2 == 0:
            pairs.append(("超长" * 30, f"乙{index}短"))  # 问题主体（初版译文标记）
        else:
            pairs.append((f"正常{index}", f"正{index}译"))
    data = _data(*pairs)
    gateway = _RecordingGateway()
    repaired, report = execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        snapshot=_enhanced_snapshot(radius=3),
    )
    summary = report.viewing_repair
    assert summary is not None
    # 12 主体 → 同轮 2 批主修复；第二轮扫描无问题即终止。
    assert summary.requests == 2
    mains = gateway.main_payloads()
    assert len(mains) == 2
    # 高段序批（主体 20/22）先请求；低段序批（主体 0-18）后请求。
    low = next(
        p for p in mains if any(seg["id"] == 0 for s in p["repair_subjects"] for seg in s["segments"])
    )
    low_context = {entry["id"]: entry["translated"] for entry in low["boundary_context"]}
    # 低段序批上下文覆盖段 19-21（主体 18 的下文）：段 20 必须是轮次快照的
    # 初版「乙20短」，不是高段序批已归并的拆分碎片（译文「改后」）。
    assert 20 in low_context
    assert low_context[20] == "乙20短"
    # 归并后最终字幕：原文顺序完整、全部主体拆分、无未解决问题。
    assert "".join("".join(s.text.split()) for s in repaired.segments) == "".join(
        "".join(t.split()) for t, _ in pairs
    )
    assert all(len(s.text) <= 20 for s in repaired.segments)
    assert not report.unresolved_viewing_problems()
    # 拆分片段译文经复校确认为「校后」；未修主体保持原译文（「正N译」）。
    # 全部偶数段都拆分（≤20 字），奇数段原样保留。
    assert all(
        s.translated_text == "校后" for s in repaired.segments if "超长" in s.text
    )
    assert all(
        s.translated_text.startswith("正") for s in repaired.segments if "正常" in s.text
    )


# ---- 验收 2：高级校对读取轮次快照底稿，不读邻批刚完成的归并 ----


def test_review_reads_round_base_not_neighbor_merge():
    """高级校对边界上下文 = 本轮底稿（轮次快照），不含同轮邻主体的归并结果。

    同轮两主体（段 0 低、段 3 高）按固定段序归并：高段序主体先拼接。
    低段序主体的高级校对复校在拼接后执行，其下文边界上下文若读活
    state，会看到高段序主体的拆分碎片；快照语义下必须仍是初版段 3。
    """
    data = _data(
        ("超长" * 30, "甲短"),  # 主体 A（低段序）
        ("正常一", "正常一译"),
        ("正常二", "正常二译"),
        ("超长" * 30, "乙短"),  # 主体 B（高段序）
    )
    gateway = _RecordingGateway()
    repaired, report = execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        snapshot=_enhanced_snapshot(radius=3),
    )
    summary = report.viewing_repair
    assert summary is not None
    assert summary.flow_mode == "main_review"
    reviews = gateway.review_payloads()
    assert len(reviews) == 2  # 同轮两主体各复校一次
    # 高段序主体（段 3）的复校：上文覆盖段 0-2（底稿初版）。
    high = next(r for r in reviews if any(seg["id"] == 3 for s in r["review_subjects"] for seg in s["segments"]))
    high_context = {entry["id"]: entry["translated"] for entry in high["boundary_context"]}
    assert high_context[0] == "甲短"
    # 低段序主体（段 0）的复校：下文覆盖段 1-3。段 3 必须是轮次快照的
    # 初版「乙短」，不是高段序主体已归并的拆分碎片（译文「校后」）。
    low = next(r for r in reviews if any(seg["id"] == 0 for s in r["review_subjects"] for seg in s["segments"]))
    low_context = {entry["id"]: entry["translated"] for entry in low["boundary_context"]}
    assert 3 in low_context
    assert low_context[3] == "乙短"
    # 复校校订实际应用：拆分片段译文为「校后」，未修主体保持原译文。
    assert all("超长" in s.text and s.translated_text == "校后" for s in repaired.segments[:3])
    assert [s.translated_text for s in repaired.segments[3:5]] == ["正常一译", "正常二译"]
    assert all("超长" in s.text and s.translated_text == "校后" for s in repaired.segments[5:])
    assert summary.review_corrections == 6
    assert not report.unresolved_viewing_problems()


# ---- 验收 2b：多问题对应同段——身份 / 记账 / 归并一并验证 ----


def test_multiple_problems_on_same_segment_keep_identity_and_accounting():
    """同一段两个问题身份（原文侧 + 译文侧超长）：一次拆分同时解决两侧。

    归并路径按问题身份列表记账：一次候选验收同时撤销两个身份的
    last_error、两个身份都进 accepted；回退时两个身份一起撤销。
    主体载荷把两个 problem_ids 都带进请求（不只第一个）。
    """
    # 段 0：原文超长（60 字 > 20）且译文超长（24 字 > 20）→ 同段两个问题。
    data = _data(("超长" * 30, "超译" * 12))
    gateway = _RecordingGateway()
    repaired, report = execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        snapshot=_enhanced_snapshot(radius=2),
    )
    summary = report.viewing_repair
    assert summary is not None
    assert not report.unresolved_viewing_problems()
    # 请求把同段的两个问题身份都带入主体载荷（不是只带第一个）。
    main = gateway.main_payloads()[0]
    segment = main["repair_subjects"][0]["segments"][0]
    assert set(segment["problem_ids"]) == {"length:original:0", "length:translated:0"}
    # 一次拆分（3 片）同时解决两个问题：两身份都计入解决数。
    assert summary.resolved_problem_count == 2
    assert summary.spliced_fragments == 3
    # 拆分后两侧都合规：原文 ≤20、译文 ≤20。
    assert all(len(s.text) <= 20 for s in repaired.segments)
    assert all(len(s.translated_text) <= 20 for s in repaired.segments)
    # 复校覆盖拆分片段（3 片全带 proposals），校订实际应用。
    review = gateway.review_payloads()[0]
    proposals = review["review_subjects"][0]["segments"][0]["proposals"]
    assert len(proposals) == 3
    assert summary.review_corrections == 3


def test_multiple_problems_rollback_revokes_both_identities():
    """同段双问题的区域回退：两个身份一起撤销、不残留已接受计数。"""
    data = _data(("超长" * 30, "超译" * 12), ("正常", "正常译"))

    class _NeverSplitGateway(_RecordingGateway):
        def _main_repairs(self, payload: dict) -> list[dict]:
            # 永远交回不拆分候选：原文拼接不等价 → 整段拒绝 → 耗尽回退。
            repairs = []
            for subject in payload["repair_subjects"]:
                for segment in subject["segments"]:
                    repairs.append(
                        {
                            "problem_id": segment["problem_ids"][0],
                            "output_index": 0,
                            "original": "非法截断",
                            "translated": "坏",
                        }
                    )
            return repairs

    gateway = _NeverSplitGateway()
    repaired, report = execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        snapshot=_enhanced_snapshot(radius=2),
    )
    summary = report.viewing_repair
    assert summary is not None
    # 耗尽回退：段 0 区域（双问题）恢复初版，段 1 保留。
    assert summary.rollbacks and summary.rollbacks[0].reason == "业务修复重试耗尽"
    assert summary.rollbacks[0].initial_indices == (0,)
    # 回退撤销双身份的接受计数：终态以重新扫描为准，两问题都未解决。
    assert summary.resolved_problem_count == 0
    unresolved = report.unresolved_viewing_problems()
    assert {p.problem_id for p in unresolved} == {"length:original:0", "length:translated:0"}
    # 回退区域恢复初版未拆分形态。
    assert [s.text for s in repaired.segments] == ["超长" * 30, "正常"]
    assert [s.translated_text for s in repaired.segments] == ["超译" * 12, "正常译"]


# ---- 验收 3：候选归并按固定段序——响应数组顺序无关 ----


class _PermutingGateway(_RecordingGateway):
    """同一响应集合、不同 repairs 数组顺序（升序 / 降序交错）。"""

    def __init__(self, order: str):
        super().__init__()
        self.order = order

    def _main_repairs(self, payload: dict) -> list[dict]:
        repairs = _split_entries(payload)
        if self.order == "reverse":
            repairs = list(reversed(repairs))
        elif self.order == "interleave":
            half = len(repairs) // 2
            repairs = [x for pair in zip(repairs[half:], repairs[:half]) for x in pair] + repairs[
                2 * half:
            ]
        return repairs


def _run_ordering(order: str):
    data = _data(
        ("超长" * 30, "甲短"),  # 主体 1（段 0）
        ("正常一", "正常一译"),
        ("正常二", "正常二译"),
        ("超长" * 30, "乙短"),  # 主体 2（段 3，与段 4 相邻合并）
        ("超长" * 30, "丙短"),  # 主体 2 的第二输入段（段 4）
    )
    gateway = _PermutingGateway(order)
    repaired, report = execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        snapshot=_enhanced_snapshot(radius=2),
    )
    return gateway, repaired, report


def test_candidate_order_does_not_change_final_subtitle_or_accounting():
    """不同候选提交顺序（响应数组排列）产生相同最终字幕、验收、计数与回退。"""
    gateway_a, repaired_a, report_a = _run_ordering("forward")
    gateway_b, repaired_b, report_b = _run_ordering("reverse")
    gateway_c, repaired_c, report_c = _run_ordering("interleave")

    assert len(gateway_a.requests) == len(gateway_b.requests) == len(gateway_c.requests)
    # 最终字幕逐段一致（文本 / 时间 / 译文）：固定段序归并的唯一结果。
    a = [(s.text, s.start_time, s.end_time, s.translated_text) for s in repaired_a.segments]
    for other in (repaired_b, repaired_c):
        assert [(s.text, s.start_time, s.end_time, s.translated_text) for s in other.segments] == a
    # 固定段序归并的确定性布局：主体 [3,5) 先归并（含相邻合并的段 3/4），
    # 主体 [0,1) 后归并；各输入段的片段按 output_index 顺序就位。
    expected_texts = ["超长" * 10] * 3 + ["正常一", "正常二"] + ["超长" * 10] * 6
    assert [s.text for s in repaired_a.segments] == expected_texts
    assert [s.translated_text for s in repaired_a.segments] == ["校后"] * 3 + [
        "正常一译",
        "正常二译",
    ] + ["校后"] * 6
    # 时间轴：单调、每段为正、原 cue 边界保持（0/4000/8000/12000/16000/20000）。
    starts = [s.start_time for s in repaired_a.segments]
    assert starts == sorted(starts)
    assert all(s.end_time > s.start_time for s in repaired_a.segments)
    assert [s.start_time for s in repaired_a.segments][0] == 0
    assert {s.start_time for s in repaired_a.segments} >= {0, 4000, 8000, 12000, 16000}
    # 问题验收与计数一致。
    sa, sb, sc = (r.viewing_repair for r in (report_a, report_b, report_c))
    assert sa is not None and sb is not None and sc is not None
    for other in (sb, sc):
        assert other.requests == sa.requests
        assert other.spliced_fragments == sa.spliced_fragments
        assert other.resolved_problem_count == sa.resolved_problem_count
        assert other.rollbacks == sa.rollbacks
        assert other.review_corrections == sa.review_corrections
    assert not report_a.unresolved_viewing_problems()


# ---- 验收 4：完整任务入口贯穿——字幕与报告可验证，下游门禁保持 ----


def test_full_task_entry_produces_verifiable_subtitle_and_report(tmp_path):
    """轮次快照与归并贯穿完整任务入口：字幕、QA 报告、状态与门禁可验证。"""
    source = tmp_path / "input.srt"
    source.write_text("placeholder\n", encoding="utf-8")
    gateway = _RecordingGateway()
    task = PostprocessTask(
        str(source),
        postprocessed_subtitle_path=str(tmp_path / "result.srt"),
        input_data=_data(
            ("超长" * 30, "甲短"),
            ("正常一", "正常一译"),
            ("正常二", "正常二译"),
            ("超长" * 30, "乙短"),
        ),
        layout_mode=PostprocessLayoutMode.ORIGINAL_ON_TOP,
        workflow_base_name="round",
        source_language="zh",
        target_language="en",
        config_snapshot=_config(qa_report=True),
        translation_snapshot=_enhanced_snapshot(radius=3),
    )
    result = run_postprocess_task(task, gateway=gateway)
    assert result.succeeded and result.continue_downstream
    assert result.task.status == "completed"
    # 原文保护：非空白字符按序拼接与输入一致。
    assert "".join("".join(s.text.split()) for s in result.output_data.segments) == "".join(
        "".join(t.split())
        for t in ("超长" * 30, "正常一", "正常二", "超长" * 30)
    )
    # 时间轴单调、起点对齐、无越界。
    starts = [s.start_time for s in result.output_data.segments]
    assert starts == sorted(starts)
    assert result.output_data.segments[0].start_time == 0
    assert all(s.end_time > s.start_time for s in result.output_data.segments)
    # 高级校对实际校订已应用（受控应答「校后」）。
    assert all(
        s.translated_text == "校后" for s in result.output_data.segments if "超长" in s.text
    )
    # QA 报告与过程状态交付，状态载荷记录修复方式。
    discovery = task.asset_discovery
    assert discovery is not None
    state_path = discovery.asset_path("postprocess_state")
    assert state_path is not None and state_path.is_file()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "completed"
    assert state["viewing_repair"]["flow_mode"] == "main_review"
    qa_path = discovery.asset_path("qa_report")
    assert qa_path is not None and "修复方式" in qa_path.read_text(encoding="utf-8")
    # 归并后全片重检通过；修复摘要可核对。
    assert not result.report.unresolved_viewing_problems()
    assert result.report.viewing_repair is not None
    assert result.report.viewing_repair.review_corrections == 6


def test_partial_failure_keeps_other_candidates_and_rejects_only_failed_region(tmp_path):
    """局部失败：非法候选只回退对应区域，同轮其他合法候选照常归并交付。"""
    source = tmp_path / "input.srt"
    source.write_text("placeholder\n", encoding="utf-8")

    class _FailingGateway(_RecordingGateway):
        def _main_repairs(self, payload: dict) -> list[dict]:
            repairs: list[dict] = []
            for subject in payload["repair_subjects"]:
                for segment in subject["segments"]:
                    pid = segment["problem_ids"][0]
                    if segment["id"] == 0:
                        # 非法候选：原文片段拼接不等价（丢字）→ 整段拒绝。
                        repairs.append(
                            {"problem_id": pid, "output_index": 0,
                             "original": "超长" * 10, "translated": "坏"}
                        )
                    else:
                        repairs.extend(_split_entries_for_segment(segment))
            return repairs

    gateway = _FailingGateway()
    task = PostprocessTask(
        str(source),
        postprocessed_subtitle_path=str(tmp_path / "result.srt"),
        input_data=_data(("超长" * 30, "甲短"), ("超长" * 30, "乙短")),
        layout_mode=PostprocessLayoutMode.ORIGINAL_ON_TOP,
        workflow_base_name="partial",
        source_language="zh",
        target_language="en",
        config_snapshot=_config(qa_report=True),
        translation_snapshot=_enhanced_snapshot(radius=3),
    )
    result = run_postprocess_task(task, gateway=gateway)
    assert result.succeeded and result.continue_downstream
    summary = result.report.viewing_repair
    assert summary is not None
    assert summary.rollbacks and summary.rollbacks[0].reason == "业务修复重试耗尽"
    texts = [s.text for s in result.output_data.segments]
    assert "超长" * 30 in texts  # 失败区域恢复初版未拆分
    assert texts.count("超长" * 10) == 3  # 段 1 成功拆分保留（3 片）
    assert result.report.unresolved_viewing_problems()  # 失败区域不记为通过
    qa_path = task.asset_discovery.asset_path("qa_report")
    assert qa_path is not None and "批量修复局部回退" in qa_path.read_text(encoding="utf-8")


# ---- 验收 5：既有防重复 / 防震荡守卫在归并路径下保持（D27）----


def test_duplicate_candidate_guard_survives_merge(monkeypatch):
    """归并路径保留 D27 守卫：同段同指纹候选再次通过验收 → 立即回退。"""
    from videocaptioner.core.postprocess import repair as repair_module
    from videocaptioner.core.postprocess.viewing import ViewingProblem

    class _AlwaysProblematicScan:
        def __init__(self, segment_index: int, text: str):
            self._index = segment_index
            self._text = text

        def __call__(self, asr_data, cfg, layout):
            return [
                ViewingProblem(
                    problem_id=f"length:original:{self._index}",
                    side="original",
                    segment_index=self._index,
                    text=self._text,
                    weighted_length=999.0,
                    absolute_limit=20,
                    target_limit=16,
                    reason="fake scan keeps flagging the segment",
                )
            ]

    # 轮 1 接受等价候选并记录指纹；轮 2 再交同一候选 → 指纹相同 → 守卫触发。
    monkeypatch.setattr(
        repair_module, "scan_viewing_lengths", _AlwaysProblematicScan(1, "正常")
    )
    same_piece_repairs = [
        {"problem_id": "length:original:1", "output_index": 0,
         "original": "正常", "translated": "短"}
    ]

    class _SamePieceGateway(_RecordingGateway):
        def _main_repairs(self, payload: dict) -> list[dict]:
            return same_piece_repairs

    gateway = _SamePieceGateway()
    repaired, report = execute_viewing_repair(
        _data(("超长" * 30, "短"), ("正常", "正常译")),
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        snapshot=_enhanced_snapshot(radius=2),
    )
    summary = report.viewing_repair
    assert summary is not None
    assert summary.requests == 2  # 轮 1 记指纹；轮 2 同指纹 → 立即回退
    assert summary.rollbacks and summary.rollbacks[0].reason == "重复修复候选"
    assert any("重复修复候选" in warning for warning in summary.warnings)
    # 回退恢复初版段：整个区域回到未拆分形状。
    assert [s.text for s in repaired.segments][1] == "正常"
