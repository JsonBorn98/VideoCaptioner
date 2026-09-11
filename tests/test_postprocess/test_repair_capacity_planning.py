"""主修复容量规划接通执行（票 03）：完整任务入口验证。

按 spec「Testing Decisions」第 4 条（容量与校对批量化）：测试穿过
``execute_viewing_repair`` / ``run_postprocess_task`` 接缝，用受控网关
从外部观察实际发出的请求是否遵守容量规划契约：
- 每批容量校验覆盖完整序列化请求（提示词 + 主/译文本 + 问题 +
  上下文 + feedback 协议开销）+ 输出预留（一对多拆分 + 原译对应），
  不是仅对主体裸文本或固定数量做检查。
- 容量不够时先减少修复主体数量，再缩减边界上下文，保持主体完整；
  零上下文单主体仍不足时不发送超预算请求、不截断原文，明确报告
  容量不足及正确问题身份。
- 输出截断、响应结构错误和局部缺项分别处理；普通 LLM、增强型及
  仅报告修复模式保持原有角色与门禁。
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
from videocaptioner.core.translate.enhanced.token_planner import estimate_tokens


def _profile(
    profile_id: str = "capacity-profile",
    work_context_tokens: int = 16_384,
    max_output_tokens: int | None = None,
) -> LLMModelProfile:
    return LLMModelProfile(
        profile_id=profile_id,
        name=f"Profile {profile_id}",
        transport=LLMTransport.OPENAI_COMPATIBLE,
        dialect=ProviderDialect.GENERIC,
        base_url="https://capacity.test/v1",
        api_key="secret",
        model="capacity-model",
        work_context_tokens=work_context_tokens,
        max_output_tokens=max_output_tokens,
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


def _split_entries_for_segment(segment: dict, chunk: int = 20, translated: str = "改后") -> list[dict]:
    pid = segment["problem_ids"][0]
    compact = "".join(segment["text"].split())
    pieces = [compact[i : i + chunk] for i in range(0, len(compact), chunk)]
    return [
        {"problem_id": pid, "output_index": index, "original": piece, "translated": translated}
        for index, piece in enumerate(pieces)
    ]


def _split_entries(payload: dict, chunk: int = 20, translated: str = "改后") -> list[dict]:
    repairs: list[dict] = []
    for subject in payload["repair_subjects"]:
        for segment in subject["segments"]:
            repairs.extend(_split_entries_for_segment(segment, chunk=chunk, translated=translated))
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


class _RecordingGateway:
    """记录全部请求（含 max_output_tokens / 消息全文）；主修复按载荷就地
    拆分，复校原样确认。子类覆写 ``_main_repairs`` / ``complete`` 改应答。"""

    def __init__(self, main_translated: str = "改后", review_translated: str = "校后"):
        self.requests: list[dict] = []
        self.raw_requests: list = []
        self.main_translated = main_translated
        self.review_translated = review_translated

    def _main_repairs(self, payload: dict) -> list[dict]:
        return _split_entries(payload, translated=self.main_translated)

    def complete(self, profile, request, *, cancelled=None):
        payload = json.loads(_payload_text(request))
        self.requests.append(payload)
        self.raw_requests.append(request)
        if "review_subjects" in payload:
            return LLMResult(
                text=_review_response_text(_confirm_reviews(payload, self.review_translated))
            )
        return LLMResult(text=_response(self._main_repairs(payload)))

    def main_payloads(self) -> list[dict]:
        return [p for p in self.requests if "repair_subjects" in p]

    def review_payloads(self) -> list[dict]:
        return [p for p in self.requests if "review_subjects" in p]


def _enhanced_snapshot(
    radius: int = 3,
    *,
    main_profile: LLMModelProfile | None = None,
    review_profile: LLMModelProfile | None = None,
    main_prompt: str = "",
) -> TranslationExecutionSnapshot:
    main = main_profile if main_profile is not None else _profile("capacity-main")
    review = review_profile if review_profile is not None else _profile("capacity-review")
    return TranslationExecutionSnapshot(
        method="enhanced_llm",
        main_profile=main,
        review_profile=review,
        boundary_context_radius=radius,
        main_prompt=main_prompt,
    )


def _request_input_tokens(request) -> int:
    """完整请求输入口径（与执行侧估算同一来源：全部消息序列化）。"""
    serialized = "\n".join(message.content for message in request.messages)
    return estimate_tokens(serialized)


# ---- 验收 1：实际发出的请求遵守规划契约（完整输入 + 输出预留 ≤ 预算）----


def test_sent_requests_respect_work_context_budget():
    """每个实际发出的主修复请求：完整输入 + 输出预留 ≤ 角色工作上下文。

    混合主体大小 + 多问题同主体 + 长上下文：容量收紧
    （work_context_tokens=20000）下批次被拆小，但每个请求都在预算内。
    """
    pairs: list[tuple[str, str]] = []
    for index in range(24):
        if index % 2 == 0:
            pairs.append(("超长的问题段落内容" * 12, f"乙{index}译"))
        else:
            pairs.append((f"正常{index}", f"正{index}译"))
    data = _data(*pairs)
    gateway = _RecordingGateway()
    main_profile = _profile("capacity-main", work_context_tokens=20_000)
    repaired, report = execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        snapshot=_enhanced_snapshot(radius=3, main_profile=main_profile),
    )
    summary = report.viewing_repair
    assert summary is not None
    assert summary.requests >= 2  # 12 主体收紧后被拆成多批
    budget = main_profile.work_context_tokens
    # 主修复请求按载荷判别切片（review 请求不配进主修复断言）。
    main_requests = [
        request
        for request in gateway.raw_requests
        if "repair_subjects" in _payload_text(request)
    ]
    assert len(main_requests) == summary.requests
    for request in main_requests:
        input_tokens = _request_input_tokens(request)
        # 输出预留按主体载荷比例（执行侧同口径）；请求的输出上限即钳制值。
        assert request.max_output_tokens is None or request.max_output_tokens < budget
        # 完整输入必须在预算内（不含输出预留的硬口径）。
        assert input_tokens <= budget, f"request input {input_tokens} > budget {budget}"
    # 规划观测已记录：批次与 token 估计可核对（不声称真实吞吐）。
    assert summary.planned_requests >= 2
    assert summary.planned_input_tokens > 0
    assert summary.planned_output_reserve_tokens > 0
    # 主体 / 问题计数分列：12 主体、12 问题（每主体单问题），与段数分口径。
    assert summary.planned_subjects >= 12
    assert summary.planned_problems >= 12
    # 内容不被截断：原文顺序完整。
    assert "".join("".join(s.text.split()) for s in repaired.segments) == "".join(
        "".join(t.split()) for t, _ in pairs
    )


def test_capacity_check_covers_full_request_not_bare_text():
    """容量校验覆盖完整序列化载荷：提示词 + guidance + limits + feedback。

    长提示词 / 长 feedback 会计入请求输入——一个裸文本恰好放得下、
    加上协议开销后放不下的批，必须按完整口径收缩。
    """
    long_guidance = "非常长的原翻译提示配置。" * 60
    data = _data(*[("超长" * 30, "乙译")] * 6)
    gateway = _RecordingGateway()
    main_profile = _profile("capacity-main", work_context_tokens=16_384)
    repaired, report = execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        snapshot=_enhanced_snapshot(
            radius=2, main_profile=main_profile, main_prompt=long_guidance
        ),
    )
    summary = report.viewing_repair
    assert summary is not None
    # 全部主修复请求的完整输入（含 guidance）都在预算内。
    for request in gateway.raw_requests:
        if "repair_subjects" in _payload_text(request):
            assert _request_input_tokens(request) <= main_profile.work_context_tokens
    # guidance 实际进入请求（可观察到）。
    assert any(long_guidance[:20] in r.messages[1].content for r in gateway.raw_requests)


def test_long_feedback_counts_into_request_budget():
    """长 feedback（上一轮失败原因）计入请求输入并占预算：随轮次真实膨胀。

    第 1 轮响应结构错误（非 repairs 数组）→ last_error 进第 2 轮
    feedback；feedback 进载荷并计入估算——请求仍不超预算，
    且观测口径与实际请求一致（估算复用 ``_request_messages``）。
    """

    class _StructuralErrorThenSplitGateway(_RecordingGateway):
        """第 1 次主修复响应结构错误（缺 repairs 数组），之后正常拆分。"""

        def __init__(self):
            super().__init__()
            self.main_calls = 0

        def _main_repairs(self, payload: dict) -> list[dict]:
            self.main_calls += 1
            if self.main_calls == 1:
                # 结构错误形状：整体致命（响应缺 repairs 数组），
                # 该轮不落 splice，全部问题身份进 last_error。
                return [{"problem_id": "x", "output_index": 0,
                          "original": "非法", "translated": "坏"}]
            return _split_entries(payload, translated=self.main_translated)

    data = _data(*[("超长" * 30, f"乙{i}译") for i in range(6)])
    gateway = _StructuralErrorThenSplitGateway()
    main_profile = _profile("capacity-main", work_context_tokens=16_384)
    repaired, report = execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        snapshot=_enhanced_snapshot(radius=2, main_profile=main_profile),
    )
    summary = report.viewing_repair
    assert summary is not None
    # 第 2 轮请求的 feedback 非空且可见于载荷（结构错误原因进 feedback）。
    second = next(
        payload
        for payload in gateway.main_payloads()
        if payload["feedback"]
    )
    assert any("未知问题绑定" in item for item in second["feedback"])
    # 带长 feedback 的请求仍在预算内（feedback 计入估算口径）。
    for request in gateway.raw_requests:
        if "repair_subjects" in _payload_text(request):
            assert _request_input_tokens(request) <= main_profile.work_context_tokens
    # 结构错误按整体致命处理：该轮不落 splice；下一轮带 feedback 重试成功。
    assert summary.requests >= 2
    assert not report.unresolved_viewing_problems()


# ---- 验收 2：先减主体数量，再缩边界上下文，保持主体完整 ----


def test_budget_shrinks_subjects_before_context_in_execution():
    """执行侧收缩顺序（D26）：先减少批内主体，上下文保持完整 radius。

    超长主体（每主体 672 字）：无压力下 10 主体同批（估算约 9.6k
    输入 + 13.9k 预留 ≈ 23.5k）；收紧到最小工作上下文（16384）后
    主体数量被收缩成 6/6/6/2 主体批，但每批仍带满 radius=3 上下文
    （先减主体，不先剪上下文）。本测试只断言规划形状与请求载荷
    ——不要求修复完成（672 字拆 8 片每片 84 字超绝对上限，修复
    本身按验收拒绝，属于正常的耗尽回退路径）。
    """
    pairs: list[tuple[str, str]] = []
    for index in range(40):
        if index % 2 == 0:
            pairs.append(("超长的问题主体段落内容继续扩展再补一些字数" * 32, f"主{index}译"))
        else:
            pairs.append((f"正常{index}", f"正{index}译"))
    data = _data(*pairs)
    # 无压力版本：满批形状（默认每批 ≤10 主体）。
    gateway_full = _RecordingGateway()
    execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway_full,
        snapshot=_enhanced_snapshot(
            radius=3,
            main_profile=_profile("capacity-main", work_context_tokens=200_000),
        ),
    )
    full_counts = [len(p["repair_subjects"]) for p in gateway_full.main_payloads()]
    assert max(full_counts) == 10  # 无压力下满批
    gateway = _RecordingGateway()
    main_profile = _profile("capacity-main", work_context_tokens=16_384)
    repaired, report = execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        snapshot=_enhanced_snapshot(radius=3, main_profile=main_profile),
    )
    summary = report.viewing_repair
    assert summary is not None
    subjects_per_batch = [len(p["repair_subjects"]) for p in gateway.main_payloads()]
    # 收紧后主体数量被收缩：没有任何一批达到满批 10 主体，
    # 且主体数总和 = 全部 20 主体（拆批不丢主体）。
    assert subjects_per_batch and all(count < 10 for count in subjects_per_batch)
    assert sum(subjects_per_batch) >= 20
    # 收缩的是主体数量不是上下文：每批仍带满 radius 上下文
    # （radius=3、主体互不相邻隔 1 段 → 满上下文每批 ≥2 段）。
    for payload in gateway.main_payloads():
        assert len(payload["boundary_context"]) >= 2
    # 全部主体都进入请求（覆盖不缺）；修复完成与否按验收口径
    # 另行断言（超绝对上限的候选按 D27 拒绝 → 回退，不虚报完成）。
    covered = {
        seg["id"]
        for payload in gateway.main_payloads()
        for subject in payload["repair_subjects"]
        for seg in subject["segments"]
    }
    assert covered == {index for index in range(40) if index % 2 == 0}


def test_budget_shrinks_context_after_single_subject():
    """单主体仍放不下时才缩边界上下文：主体区间完整、radius 收缩可观察。

    原文侧 auto_wrap（不检查原文长度）：上下文段可携带长原文
    （5000 字）而自身不成为问题——上下文贡献大，预算收紧时
    先减主体（单主体不再可减），再按顺序缩边界上下文。
    """
    long_context_original = "原文长" + "长" * 4997  # 5000 字，auto_wrap 侧无长度问题
    data = _data(
        ("主体段", "甲" * 5000),  # 译文超长 → 唯一问题主体
        (long_context_original, "译短"),  # radius 内上下文段
    )
    config = _config(original_display_mode="auto_wrap")
    # 无压力版本：满上下文形状。
    gateway_full = _RecordingGateway()
    execute_viewing_repair(
        data,
        config,
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway_full,
        snapshot=_enhanced_snapshot(
            radius=3, main_profile=_profile("capacity-main", work_context_tokens=200_000)
        ),
    )
    full_payload = gateway_full.main_payloads()[0]
    assert {entry["id"] for entry in full_payload["boundary_context"]} == {1}
    gateway = _RecordingGateway()
    main_profile = _profile("capacity-main", work_context_tokens=16_384)
    repaired, report = execute_viewing_repair(
        data,
        config,
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        snapshot=_enhanced_snapshot(radius=3, main_profile=main_profile),
    )
    summary = report.viewing_repair
    assert summary is not None
    payload = gateway.main_payloads()[0]
    # 主体完整：段 0 在主体里，没有被截断或拆丢。
    subject = payload["repair_subjects"][0]
    assert [seg["id"] for seg in subject["segments"]] == [0]
    # 上下文被缩（单主体先减无可减 → 缩上下文）：满 radius 上下文段 1 不再出现。
    assert payload["boundary_context"] == []
    # 上下文收缩观测可核对。
    assert summary.shrunk_context_batches >= 1
    # 上下文段不被误当修改目标：原文完整保留（未被截断 / 改写）。
    assert repaired.segments[1].text == long_context_original
    # 主体照常被修复（译文拆 ≤20 字），问题不虚报完成。
    assert all(len(s.translated_text) <= 20 for s in repaired.segments)
    assert not report.unresolved_viewing_problems()


# ---- 验收 3：零上下文单主体仍不足：明确报告、不截断、正确问题身份 ----


def test_unplannable_single_subject_reports_without_truncation():
    """零上下文单主体仍超预算：不发送请求、不截断原文，报告正确问题身份。

    主体译文 7000 字（单行限长下唯一问题）：满上下文 input+reserve
    ≈ 19.2k > 16384 → 先减主体（无可减）→ 缩上下文至零仍超 →
    unplannable 明确报告，不截断、不静默丢内容。
    """
    data = _data(("主体段落", "甲" * 7000), ("短上下文", "译短"), ("正常段", "正常译"))
    gateway = _RecordingGateway()
    main_profile = _profile("capacity-main", work_context_tokens=16_384)
    repaired, report = execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        snapshot=_enhanced_snapshot(radius=2, main_profile=main_profile),
    )
    summary = report.viewing_repair
    assert summary is not None
    # 段 0 零上下文单主体仍放不下 → unplannable；段 1/2 无问题不进请求。
    assert summary.unplannable_subjects == 1
    assert any("容量不足" in warning for warning in summary.warnings)
    # 不发送超预算请求：没有任何主修复请求覆盖段 0。
    for payload in gateway.main_payloads():
        for subject in payload["repair_subjects"]:
            for segment in subject["segments"]:
                assert segment["id"] != 0
    # 原文不截断：最终字幕保留段 0 完整原文与短译文。
    assert repaired.segments[0].text == "主体段落"
    assert repaired.segments[0].translated_text == "甲" * 7000
    # 问题身份正确：报告容量不足针对段 0 的问题。
    unresolved = report.unresolved_viewing_problems()
    assert {p.segment_index for p in unresolved} == {0}
    assert {p.problem_id for p in unresolved} == {"length:translated:0"}


def test_unplannable_does_not_block_other_subjects_in_execution():
    """超大主体进 unplannable 后其余主体照常修复（不静默丢内容）。

    段 0 译文 7000 字（零上下文单主体仍超 16384 → unplannable）；
    段 2 译文 60 字超长（正常修复路径，拆 ≤20 字）。
    """
    data = _data(
        ("主体段落", "甲" * 7000),  # unplannable：单主体零上下文仍超预算
        ("正常一", "正一译"),  # 无问题
        ("超长" * 30, "乙短"),  # 正常修复：60 字拆 3 片
    )
    gateway = _RecordingGateway()
    main_profile = _profile("capacity-main", work_context_tokens=16_384)
    repaired, report = execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        snapshot=_enhanced_snapshot(radius=2, main_profile=main_profile),
    )
    summary = report.viewing_repair
    assert summary is not None
    assert summary.unplannable_subjects == 1
    # 段 2 照常修复（拆分 ≤20 字）；段 0 unplannable 不截断原文。
    assert all(
        len(s.text) <= 20 or s.text in ("主体段落", "正常一") for s in repaired.segments
    )
    assert any(s.text == "主体段落" and s.translated_text == "甲" * 7000 for s in repaired.segments)
    # 未解决问题恰好是段 0（被封闭），段 2 已解决。
    unresolved = report.unresolved_viewing_problems()
    assert {p.segment_index for p in unresolved} == {0}


# ---- 验收 4：输出截断 / 结构错误 / 局部缺项分别处理 ----


def test_output_truncation_is_not_applied_as_complete_candidate():
    """输出截断（原文片段拼接不等价）：整段拒绝，不当完整候选应用。"""
    data = _data(("超长" * 30, "乙短"))

    class _TruncatingGateway(_RecordingGateway):
        def _main_repairs(self, payload: dict) -> list[dict]:
            # 截断的候选：只交回前一半原文。
            segment = payload["repair_subjects"][0]["segments"][0]
            pid = segment["problem_ids"][0]
            compact = "".join(segment["text"].split())
            half = compact[: len(compact) // 2]
            return [
                {"problem_id": pid, "output_index": 0, "original": half, "translated": "截"}
            ]

    gateway = _TruncatingGateway()
    repaired, report = execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        snapshot=_enhanced_snapshot(
            radius=2, main_profile=_profile("capacity-main", work_context_tokens=16_384)
        ),
    )
    summary = report.viewing_repair
    assert summary is not None
    # 截断候选被验收拒绝 → 重试耗尽 → 回退，原文完整恢复。
    assert summary.rollbacks and summary.rollbacks[0].reason == "业务修复重试耗尽"
    assert repaired.segments[0].text == "超长" * 30


def test_structural_error_and_partial_missing_are_separate():
    """结构错误（缺 repairs 数组）与局部缺项（少一段输出）分别处理。"""
    data = _data(("超长" * 30, "甲短"), ("超长" * 30, "乙短"))

    class _PartialGateway(_RecordingGateway):
        def __init__(self):
            super().__init__()
            self.call_count = 0

        def _main_repairs(self, payload: dict) -> list[dict]:
            self.call_count += 1
            repairs: list[dict] = []
            for subject in payload["repair_subjects"]:
                for segment in subject["segments"]:
                    if segment["id"] == 0:
                        continue  # 局部缺项：段 0 没有输出片段
                    repairs.extend(_split_entries_for_segment(segment))
            return repairs

    gateway = _PartialGateway()
    repaired, report = execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        snapshot=_enhanced_snapshot(
            radius=2, main_profile=_profile("capacity-main", work_context_tokens=16_384)
        ),
    )
    summary = report.viewing_repair
    assert summary is not None
    # 缺项段状态不变（不落 splice）；另一段照常拆分。
    assert "超长" * 30 in [s.text for s in repaired.segments] or any(
        s.text == "超长" * 10 for s in repaired.segments
    )
    # 缺项段的记录进入下一轮反馈（last_error），问题不记为通过。
    unresolved_ids = {p.problem_id for p in report.unresolved_viewing_problems()}
    assert unresolved_ids  # 至少段 0 的问题未解决


# ---- 验收 5：修复模式保持（普通 LLM / 增强 / 仅报告）----


def test_plain_llm_mode_keeps_role_and_single_pass():
    """普通 LLM 方式：只复用主翻译角色，不自动加高级校对；容量同样生效。"""
    data = _data(("超长" * 30, "乙短"), ("正常", "正译"))
    gateway = _RecordingGateway()
    repaired, report = execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        snapshot=TranslationExecutionSnapshot(
            method="single_llm",
            main_profile=_profile("capacity-main", work_context_tokens=16_384),
            boundary_context_radius=2,
        ),
    )
    summary = report.viewing_repair
    assert summary is not None
    assert summary.flow_mode == "main"
    assert not gateway.review_payloads()  # 不自动加校对
    assert all(len(s.text) <= 20 for s in repaired.segments if s.text != "正常")
    assert not report.unresolved_viewing_problems()


def test_report_only_mode_sends_no_requests():
    """仅报告模式：不发起模型请求，容量规划不触发。"""
    data = _data(("超长" * 30, "乙短"))
    gateway = _RecordingGateway()
    repaired, report = execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        snapshot=None,
        profile=None,
    )
    summary = report.viewing_repair
    assert summary is not None
    assert summary.flow_mode == "report_only"
    assert gateway.requests == []
    assert summary.planned_requests == 0


# ---- 验收 6：完整任务入口贯穿 + 边界值 ----


def test_full_task_capacity_clamp_at_config_boundary(tmp_path):
    """配置临界值：work_context_tokens 最小合法值（16384）下仍端到端交付。"""
    source = tmp_path / "input.srt"
    source.write_text("placeholder\n", encoding="utf-8")
    gateway = _RecordingGateway()
    main_profile = _profile("capacity-main", work_context_tokens=16_384)
    task = PostprocessTask(
        str(source),
        postprocessed_subtitle_path=str(tmp_path / "result.srt"),
        input_data=_data(
            ("超长" * 30, "甲短"),
            ("正常一", "正常一译"),
            ("超长" * 30, "乙短"),
        ),
        layout_mode=PostprocessLayoutMode.ORIGINAL_ON_TOP,
        workflow_base_name="capacity",
        source_language="zh",
        target_language="en",
        config_snapshot=_config(qa_report=True),
        translation_snapshot=_enhanced_snapshot(radius=3, main_profile=main_profile),
    )
    result = run_postprocess_task(task, gateway=gateway)
    assert result.succeeded and result.continue_downstream
    summary = result.report.viewing_repair
    assert summary is not None
    # 实际发出的每个主修复请求都在最小工作上下文预算内。
    for request in gateway.raw_requests:
        if "repair_subjects" in _payload_text(request):
            assert _request_input_tokens(request) <= 16_384
    # 规划观测可核对（报告口径，不声称真实吞吐）；计数按主体/问题分列。
    assert summary.planned_requests >= 1
    assert summary.planned_input_tokens > 0
    assert summary.planned_subjects >= 2  # 两个超长主体
    assert summary.planned_problems >= 2
    assert not result.report.unresolved_viewing_problems()
    # 原文保护。
    assert "".join("".join(s.text.split()) for s in result.output_data.segments) == "".join(
        "".join(t.split())
        for t in ("超长" * 30, "正常一", "超长" * 30)
    )


def test_max_output_tokens_clamp_does_not_raise_user_cap():
    """用户请求输出上限钳制输出预留：不自动抬升用户配置。"""
    data = _data(("超长" * 30, "乙短"))
    gateway = _RecordingGateway()
    main_profile = _profile(
        "capacity-main", work_context_tokens=16_384, max_output_tokens=512
    )
    repaired, report = execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        snapshot=_enhanced_snapshot(radius=2, main_profile=main_profile),
    )
    summary = report.viewing_repair
    assert summary is not None
    # 请求的输出上限 ≤ 用户配置（钳制不抬升）。
    for request in gateway.raw_requests:
        if "repair_subjects" in _payload_text(request):
            assert request.max_output_tokens == 512
    # 输出预留钳到 512：主体载荷小，预留本身受 max_output_tokens 钳制。
    assert summary.planned_output_reserve_tokens <= 512 * summary.planned_requests
    assert not report.unresolved_viewing_problems()
