"""问题主体、上下文与批次规划（票 04）：只验证外部行为。

按 spec「Testing Decisions」：测试穿过规划 seam（``plan_repair_batches``）
观察批次内容与边界身份，不锁定内部函数调用顺序。fake gateway 语义 =
批次以显式结构（主体 / 上下文 / 问题 ID）暴露，供修复执行直接观察。
"""

from __future__ import annotations

from typing import Literal

import pytest

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.postprocess.planning import (
    DEFAULT_BOUNDARY_CONTEXT_RADIUS,
    PlanProblem,
    RepairSubject,
    estimate_plan_tokens,
    merge_subjects,
    plan_repair_batches,
    problems_from_viewing,
)
from videocaptioner.core.postprocess.viewing import ViewingProblem


def _data(*pairs: tuple[str, str]) -> ASRData:
    return ASRData([ASRDataSeg(text, i * 2000, i * 2000 + 2000, translated)
                    for i, (text, translated) in enumerate(pairs)])


def _problem(pid: str, index: int, *,
             kind: Literal["length", "speed", "semantic", "structure"] = "length",
             side: Literal["original", "translated"] = "original",
             resolved=False) -> PlanProblem:
    return PlanProblem(
        problem_id=pid, kind=kind, side=side, segment_index=index,
        reason=f"reason-{pid}", resolved=resolved,
    )


# ---- 统一问题身份（验收 1：长度 / 速度 / 语义 / 结构混合）----


def test_mixed_kinds_keep_stable_ids():
    problems = [
        _problem("length:original:2", 2, kind="length"),
        _problem("speed:translated:2", 2, kind="speed", side="translated"),
        _problem("semantic:original:4", 4, kind="semantic"),
        _problem("structure:original:5", 5, kind="structure"),
    ]
    data = _data(*[("段", "译")] * 8)
    plan = plan_repair_batches(data, problems)
    assert plan.problem_ids() == [p.problem_id for p in problems]
    assert plan.unplannable == []
    # 同段双侧问题保持独立身份（长度在原文侧、速度在译文侧）。
    subjects = [s for b in plan.batches for s in b.subjects]
    merged = [s for s in subjects if s.covers(2)]
    assert len(merged) == 1
    assert "length:original:2" in merged[0].problem_ids
    assert "speed:translated:2" in merged[0].problem_ids


def test_viewing_problems_adapt_with_original_ids_and_reasons():
    viewing = [
        ViewingProblem(
            problem_id="lines:translated:3", side="translated", segment_index=3,
            text="第一行\n第二行", weighted_length=8.0,
            absolute_limit=20.0, target_limit=16.0, reason="单行限长模式下显示侧行数超过单行",
        ),
    ]
    adapted = problems_from_viewing(viewing)
    assert len(adapted) == 1
    assert adapted[0].problem_id == "lines:translated:3"
    assert adapted[0].kind == "structure"  # 行数问题归入结构
    assert adapted[0].reason == "单行限长模式下显示侧行数超过单行"
    assert adapted[0].detail["weighted_length"] == 8.0


def test_empty_problem_id_is_rejected():
    with pytest.raises(ValueError, match="problem_id"):
        _problem("", 0)


# ---- 主体与上下文分开（验收 2：上下文默认不成为修改目标）----


def test_context_is_separate_from_subjects():
    data = _data(*[("段", "译")] * 10)
    plan = plan_repair_batches(data, [_problem("length:original:5", 5)])
    (batch,) = plan.batches
    (subject,) = batch.subjects
    assert subject.start_index == 5 and subject.end_index == 6
    # 上下文区间在主体之外，分开表示，不进入主体。
    assert batch.context.spans == ((2, 5), (6, 9))
    assert not subject.covers(2)  # 主体前最后一段上下文
    assert not subject.covers(8)  # 主体后第一段上下文


def test_context_clips_at_subtitle_edges():
    data = _data(*[("段", "译")] * 4)
    plan = plan_repair_batches(data, [_problem("length:original:0", 0)])
    (batch,) = plan.batches
    assert batch.context.spans == ((1, 4),)  # 开头无上文
    plan = plan_repair_batches(data, [_problem("length:original:3", 3)])
    (batch,) = plan.batches
    assert batch.context.spans == ((0, 3),)  # 结尾无下文


# ---- radius 复用（验收 3：无第二套设置）----


def test_default_radius_is_upstream_three():
    assert DEFAULT_BOUNDARY_CONTEXT_RADIUS == 3


def test_radius_controls_context_expansion():
    data = _data(*[("段", "译")] * 12)
    plan = plan_repair_batches(
        data, [_problem("length:original:6", 6)], boundary_context_radius=1
    )
    (batch,) = plan.batches
    assert batch.context.spans == ((5, 6), (7, 8))
    plan = plan_repair_batches(
        data, [_problem("length:original:6", 6)], boundary_context_radius=0
    )
    (batch,) = plan.batches
    assert batch.context.is_empty()


def test_negative_radius_is_rejected():
    data = _data(*[("段", "译")] * 3)
    with pytest.raises(ValueError, match="boundary_context_radius"):
        plan_repair_batches(data, [_problem("p", 1)], boundary_context_radius=-1)


# ---- 主体合并（验收 4：重叠 / 相邻合并，保留 ID 与原因）----


def test_overlapping_and_adjacent_subjects_merge():
    data = _data(*[("段", "译")] * 10)
    # 主体区间 [3,4) 与 [4,5) 相邻；[4,5) 与 [5,6) 相邻 → 三问题一个主体。
    plan = plan_repair_batches(
        data,
        [_problem("a", 3), _problem("b", 4), _problem("c", 5)],
    )
    (batch,) = plan.batches
    (subject,) = batch.subjects
    assert (subject.start_index, subject.end_index) == (3, 6)
    assert subject.problem_ids == ["a", "b", "c"]
    assert subject.reasons == ["reason-a", "reason-b", "reason-c"]


def test_merge_preserves_ids_and_reasons_via_merge_subjects():
    subjects = merge_subjects(
        [_problem("a", 2), _problem("b", 2), _problem("c", 3)],
        segment_count=6,
    )
    (subject,) = subjects
    # 同段两问题去重保序；相邻段并入同一主体。
    assert subject.problem_ids == ["a", "b", "c"]
    assert subject.reasons == ["reason-a", "reason-b", "reason-c"]
    assert (subject.start_index, subject.end_index) == (2, 4)


def test_problem_index_beyond_segment_count_is_rejected():
    with pytest.raises(ValueError, match="segment_count"):
        merge_subjects([_problem("a", 9)], segment_count=3)


# ---- 仅上下文重叠而主体独立（验收 5：保持独立并共享上下文）----


def test_independent_subjects_with_overlapping_contexts_stay_independent():
    data = _data(*[("段", "译")] * 10)
    # 主体段 2 与段 4：radius=2 时各自上下文在段 3 相遇（仅上下文重叠）。
    plan = plan_repair_batches(
        data, [_problem("a", 2), _problem("b", 4)], boundary_context_radius=2
    )
    # 主体互不相邻（隔 1 段）→ 不合并。
    (batch,) = plan.batches
    assert len(batch.subjects) == 2
    first, second = batch.subjects
    assert first.problem_ids == ["a"] and second.problem_ids == ["b"]
    # 主体保持独立：区间互不重叠。
    assert (first.start_index, first.end_index) == (2, 3)
    assert (second.start_index, second.end_index) == (4, 5)
    # 上下文 = 各主体扩展的并集 − 主体区间：段 4 是主体被剔除，
    # 间隙段 3 成为独立共享 span（first 下文 ∩ second 上文）。
    assert batch.context.spans == ((0, 2), (3, 4), (5, 7))
    # 主体间间隙段 3 在 radius 覆盖内：作为共享上下文出现（D22）。
    radius_tight = plan_repair_batches(
        data, [_problem("a", 2), _problem("b", 4)], boundary_context_radius=1
    )
    (tight_batch,) = radius_tight.batches
    assert tight_batch.context.spans == ((1, 2), (3, 4), (5, 6))
    # 间隙段 3 既是 first 的下文也是 second 的上文（仅上下文重叠）。
    assert 3 in range(tight_batch.context.spans[1][0], tight_batch.context.spans[1][1])
    # 上下文在主体之外，不成为修改目标（不并入任一主体区间）。
    assert not first.covers(3) and not second.covers(3)


def test_gap_segments_between_subjects_become_shared_context():
    """主体间间隙段在 radius 覆盖内时成为共享上下文，不被整批包络丢弃（D16）。"""
    data = _data(*[("段", "译")] * 10)
    # 主体段 2 与段 5（隔 2 段）：radius=3 时段 3、4 在两侧覆盖内。
    plan = plan_repair_batches(
        data, [_problem("a", 2), _problem("b", 5)], boundary_context_radius=3
    )
    (batch,) = plan.batches
    first, second = batch.subjects
    assert (first.start_index, first.end_index) == (2, 3)
    assert (second.start_index, second.end_index) == (5, 6)
    # 各主体扩展并集：段 3/4（first 下文 ∩ second 上文）+ 两侧外沿。
    covered = {
        index
        for start, end in batch.context.spans
        for index in range(start, end)
    }
    assert 3 in covered and 4 in covered  # 间隙段是共享上下文
    assert 0 in covered and 1 in covered  # first 上文
    assert 6 in covered and 7 in covered and 8 in covered  # second 下文
    # 主体段永不进入上下文。
    assert 2 not in covered and 5 not in covered


def test_far_subjects_split_into_batches_without_budget():
    data = _data(*[("段", "译")] * 10)
    plan = plan_repair_batches(data, [_problem("a", 1), _problem("b", 8)])
    # 无预算默认整批提交：一批覆盖全部主体，主体仍按独立身份保留。
    (batch,) = plan.batches
    assert {s.problem_ids[0] for s in batch.subjects} == {"a", "b"}


# ---- 已解决问题不进入请求（D27 口径）----


def test_resolved_problems_are_not_planned():
    data = _data(*[("段", "译")] * 6)
    plan = plan_repair_batches(
        data, [_problem("done", 2, resolved=True), _problem("open", 3)]
    )
    assert plan.problem_ids() == ["open"]
    assert "done" not in plan.problem_ids()


# ---- 容量收缩顺序（验收 6：先减主体，再减上下文）----


def test_budget_shrinks_subject_count_before_context():
    # 主体互不相邻（隔 1 段），单批收缩主体数量时才可拆批。
    data = _data(*[("很长的段落内容" * 3, "同样很长的译文内容" * 3)] * 10)
    problems = [_problem(f"p{i}", i * 2) for i in range(4)]
    full = plan_repair_batches(data, problems)
    assert len(full.batches) == 1  # 无预算整批可容纳
    full_tokens = full.batches[0].estimated_tokens
    # 首主体在段 0：上文钳制在片头（无 span），下文从段 1 起；
    # 主体互不相邻（隔 1 段）→ 间隙段自成共享 span。
    assert full.batches[0].context.spans == ((1, 2), (3, 4), (5, 6), (7, 10))

    # 预算约一半：先减主体数量（拆批），上下文保持完整 radius。
    budget = full_tokens // 2
    plan = plan_repair_batches(data, problems, token_budget=budget)
    assert plan.unplannable == []
    assert plan.problem_ids() == [p.problem_id for p in problems]
    assert len(plan.batches) > 1  # 主体数量被收缩 → 拆成多批
    for batch in plan.batches:
        assert batch.estimated_tokens <= budget
        # 上下文未被收缩：批内仍带完整 radius 上下文。
        assert not batch.context.is_empty()


def test_budget_shrinks_context_after_single_subject():
    data = _data(*[("很长的段落内容" * 3, "同样很长的译文内容" * 3)] * 10)
    # 预算夹在“单主体+满上下文”与“单主体+零上下文”之间。
    radius = 2
    full = plan_repair_batches(
        data, [_problem("p", 5)], boundary_context_radius=radius
    )
    full_tokens = full.batches[0].estimated_tokens
    zero = plan_repair_batches(
        data, [_problem("p", 5)], boundary_context_radius=0
    )
    zero_tokens = zero.batches[0].estimated_tokens
    assert zero_tokens < full_tokens

    budget = (full_tokens + zero_tokens) // 2
    plan = plan_repair_batches(
        data, [_problem("p", 5)], boundary_context_radius=radius, token_budget=budget
    )
    (batch,) = plan.batches
    assert plan.unplannable == []
    # 单主体装下了，但上下文被收缩（radius < 请求值）。
    assert batch.estimated_tokens <= budget
    context_span = batch.context.segment_count()
    assert context_span < 2 * radius  # 少于满 radius 的上下文量
    # 主体内容完整保留（未截断）。
    (subject,) = batch.subjects
    assert (subject.start_index, subject.end_index) == (5, 6)


def test_unplannable_single_subject_is_reported_not_truncated():
    data = _data(*[("非常长的段落" * 10, "同样非常长的译文" * 10)] * 3)
    # 预算小于零上下文单主体估算：明确报告容量不足。
    zero = plan_repair_batches(
        data, [_problem("p", 1)], boundary_context_radius=0
    )
    zero_tokens = zero.batches[0].estimated_tokens
    plan = plan_repair_batches(
        data, [_problem("p", 1)], boundary_context_radius=2,
        token_budget=max(1, zero_tokens // 2),
    )
    assert plan.batches == []
    (subject,) = plan.unplannable
    assert subject.problem_ids == ["p"]
    # 完整文本保留：主体区间覆盖整段，不做任何截断。
    assert (subject.start_index, subject.end_index) == (1, 2)


def test_unplannable_does_not_block_other_subjects():
    # 非均匀段长：段 0/1 巨大、其余正常 → 段 0 装不下，段 6 可规划。
    pairs = [("非常长的段落" * 10, "同样非常长的译文" * 10)] * 2 + [
        ("段", "译")
    ] * 6
    data = ASRData([ASRDataSeg(t, i * 2000, i * 2000 + 2000, tr) for i, (t, tr) in enumerate(pairs)])
    huge_alone = plan_repair_batches(
        data, [_problem("huge", 0)], boundary_context_radius=0
    )
    huge_tokens = huge_alone.batches[0].estimated_tokens
    # 预算容得下正常段主体（含上下文），容不下巨大段零上下文单主体。
    normal = plan_repair_batches(
        data, [_problem("small", 6)], boundary_context_radius=1
    )
    budget = normal.batches[0].estimated_tokens
    assert budget < huge_tokens

    plan = plan_repair_batches(
        data,
        [_problem("huge", 0), _problem("small", 6)],
        boundary_context_radius=1,
        token_budget=budget,
    )
    # 预算容得下 normal：段 6 主体正常成批；段 0 巨大主体（带相邻巨大段 1
    # 合并后零上下文仍超预算）明确进入 unplannable，不阻断其余主体。
    assert [s.problem_ids for s in plan.unplannable] == [["huge"]]
    assert plan.problem_ids() == ["small"]
    (small_batch,) = plan.batches
    assert small_batch.estimated_tokens <= budget
    pairs_far = [("段", "译")] * 6 + [
        ("非常长的段落" * 10, "同样非常长的译文" * 10)
    ]
    data_far = ASRData(
        [ASRDataSeg(t, i * 2000, i * 2000 + 2000, tr) for i, (t, tr) in enumerate(pairs_far)]
    )
    plan_far = plan_repair_batches(
        data_far,
        [_problem("huge", 6), _problem("small", 0)],
        boundary_context_radius=1,
        token_budget=budget,
    )
    assert plan_far.problem_ids() == ["small"]
    assert [s.problem_ids for s in plan_far.unplannable] == [["huge"]]


# ---- 批次可观察性（验收 7：fake gateway 语义）----


def test_batch_payload_observability_shape():
    """批次结构显式携带主体 / 上下文 / 问题 ID，修复执行与测试可直接观察。"""
    data = _data(*[("段", "译")] * 8)
    plan = plan_repair_batches(data, [_problem("a", 3), _problem("b", 4)])
    (batch,) = plan.batches
    # 每个主体：区间 + 保序问题 ID + 一一对应的原因。
    for subject in batch.subjects:
        assert isinstance(subject, RepairSubject)
        assert len(subject.problem_ids) == len(subject.reasons)
        assert subject.size >= 1
    # 批次带估算 token，供预算决策与 fake gateway 断言。
    assert batch.estimated_tokens > 0
    assert estimate_plan_tokens(data, batch.subjects, batch.context) == batch.estimated_tokens


def test_max_subjects_per_batch_splits_without_budget():
    data = _data(*[("段", "译")] * 12)
    plan = plan_repair_batches(
        data,
        [_problem(f"p{i}", i) for i in range(6)],  # 相邻段全并成一个主体链
        max_subjects_per_batch=2,
    )
    # 相邻主体全部合并 → 单主体跨 6 段；每批至多 2 个主体。
    total_subjects = sum(len(b.subjects) for b in plan.batches)
    assert total_subjects == 1  # 链式合并为一个主体
    assert len(plan.batches) == 1
    plan = plan_repair_batches(
        data,
        [_problem(f"p{i}", i * 2) for i in range(6)],  # 互不相邻 → 6 个主体
        max_subjects_per_batch=2,
    )
    assert all(len(b.subjects) <= 2 for b in plan.batches)
    assert plan.problem_ids() == [f"p{i}" for i in range(6)]
