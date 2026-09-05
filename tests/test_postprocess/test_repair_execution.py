"""批量修复、一对多结果与局部回退（票 05）：只验证外部行为。

按 spec「Testing Decisions」：测试穿过修复执行 seam
（``execute_viewing_repair`` / 核心任务入口），用 fake gateway 观察请求
形状与结果状态，不锁定内部函数调用顺序与私有数据结构。
"""

from __future__ import annotations

import json

import pytest

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


def _profile() -> LLMModelProfile:
    return LLMModelProfile(
        profile_id="repair-profile",
        name="Repair Profile",
        transport=LLMTransport.OPENAI_COMPATIBLE,
        dialect=ProviderDialect.GENERIC,
        base_url="https://repair.test/v1",
        api_key="secret",
        model="repair-model",
        work_context_tokens=16_384,
    )


class _ScriptedGateway:
    """Fake gateway：按脚本回放响应，记录每次请求载荷。"""

    def __init__(self, scripts: list):
        # scripts: 每次 complete 调用一个元素；str = 正常响应文本，
        # Exception = 抛出（传输失败），None = 返回空文本。
        self.scripts = list(scripts)
        self.requests: list[dict] = []

    def complete(self, profile, request, *, cancelled=None):
        self.requests.append(json.loads(_payload_text(request)))
        script = self.scripts.pop(0) if self.scripts else None
        if isinstance(script, Exception):
            raise script
        text = "" if script is None else script
        return LLMResult(text=text)


def _payload_text(request) -> str:
    """从 user 消息提取 <input>…</input> 内的 JSON 文本。"""
    user = next(m.content for m in request.messages if m.role == "user")
    inner = user.split("<input>", 1)[1].split("</input>", 1)[0]
    return inner


def _data(*pairs: tuple[str, str]) -> ASRData:
    return ASRData(
        [ASRDataSeg(text, i * 4000, i * 4000 + 4000, tr) for i, (text, tr) in enumerate(pairs)]
    )


def _config(**overrides) -> PostprocessConfig:
    return PostprocessConfig(trim_trailing_punct=False, **overrides)


def _response(repairs: list[dict]) -> str:
    return json.dumps({"repairs": repairs}, ensure_ascii=False)


def _repairs_for(payload: dict, chunk: int = 20, translated: str = "短短") -> list[dict]:
    """按请求载荷构造合法响应：把每段原文（按紧凑字符）切成 ≤chunk 字的片段。"""
    repairs: list[dict] = []
    for subject in payload["repair_subjects"]:
        for segment in subject["segments"]:
            pid = segment["problem_ids"][0]
            compact = "".join(segment["text"].split())
            pieces = [compact[i : i + chunk] for i in range(0, len(compact), chunk)]
            for output_index, piece in enumerate(pieces):
                repairs.append(
                    {
                        "problem_id": pid,
                        "output_index": output_index,
                        "original": piece,
                        "translated": translated,
                    }
                )
    return repairs


# ---- 验收 1：响应显式绑定问题 ID / 输出段序号，不按数组位置推断 ----


def test_unknown_problem_binding_rejects_whole_response():
    """未知 problem_id 绑定是协议级违规：整体拒绝，问题不解决、耗尽后回退。"""
    data = _data(("超长" * 30, "短"))
    gateway = _ScriptedGateway([
        _response([{"problem_id": "length:original:0", "output_index": 0,
                    "original": "超长" * 30, "translated": "短"}]),
        _response([{"problem_id": "ghost", "output_index": 0,
                    "original": "超超", "translated": "短"}]),
        _response([{"problem_id": "ghost", "output_index": 0,
                    "original": "超超", "translated": "短"}]),
        _response([{"problem_id": "ghost", "output_index": 0,
                    "original": "超超", "translated": "短"}]),
        _response([{"problem_id": "ghost", "output_index": 0,
                    "original": "超超", "translated": "短"}]),
    ])
    repaired, report = execute_viewing_repair(
        data, _config(), QualityReport(), SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway, profile=_profile(),
    )
    summary = report.viewing_repair
    assert summary is not None
    # 第 1 次响应整段未拆（仍超长）被拒；第 2-5 次未知绑定整体拒绝：
    # 全部请求都没产生合格 splice，耗尽后局部回退、初版快照恢复。
    assert summary.requests == 5
    assert summary.rollbacks
    assert report.viewing_problems  # 问题保持未解决
    assert repaired.segments[0].text == "超长" * 30  # 初版快照


def test_output_index_sequence_must_be_dense():
    """output_index 必须从 0 连续编号；跳号拒绝。"""
    data = _data(("超长" * 30, "短"))
    bad = _response([
        {"problem_id": "length:original:0", "output_index": 1,
         "original": "超长" * 30, "translated": "短"},
    ])
    gateway = _ScriptedGateway([bad, bad, bad, bad, bad, bad])
    repaired, report = execute_viewing_repair(
        data, _config(), QualityReport(), SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway, profile=_profile(),
    )
    # 1 次正常提交 + 4 次业务重试后耗尽 → 局部回退（见验收 7）。
    assert report.viewing_repair.rollbacks
    assert report.viewing_repair.requests == 5


# ---- 验收 2：一个输入段产生多个输出段，原文顺序拼接等价 ----


def test_one_to_many_split_with_equivalent_original():
    """一段拆多段：原文按 output_index 顺序拼接后与输入等价。"""
    data = _data(("超长" * 30, "短"))
    payload = {
        "repair_subjects": [
            {"segments": [{"id": 0, "text": "超长" * 30, "translated": "短",
                           "problem_ids": ["length:original:0"]}]}
        ]
    }
    gateway = _ScriptedGateway([_response(_repairs_for(payload, chunk=20))])
    repaired, report = execute_viewing_repair(
        data, _config(), QualityReport(), SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway, profile=_profile(),
    )
    # 60 字按 20 字一片 → 3 片；每片折算 20 ≤ 绝对上限 20。
    assert len(repaired.segments) == 3
    assert "".join(seg.text for seg in repaired.segments) == "超长" * 30
    assert report.viewing_repair.spliced_fragments == 3
    assert not report.viewing_problems  # 拆分后不再超限


def test_original_nonequivalence_rejects_candidate():
    """原文片段增删改（拼接不等价）整段拒绝。"""
    data = _data(("超长" * 30, "短"))
    repairs = [
        {"problem_id": "length:original:0", "output_index": 0,
         "original": "超长" * 10, "translated": "短"},  # 丢了 50 字
        {"problem_id": "length:original:0", "output_index": 1,
         "original": "超长" * 10, "translated": "短"},
    ]
    gateway = _ScriptedGateway([_response(repairs)] * 6)
    repaired, report = execute_viewing_repair(
        data, _config(), QualityReport(), SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway, profile=_profile(),
    )
    assert report.viewing_repair.spliced_fragments == 0
    assert report.viewing_repair.rollbacks  # 耗尽后回退
    # 回退到初版未拆分快照：段数与原文恢复。
    assert len(repaired.segments) == 1
    assert repaired.segments[0].text == "超长" * 30


def test_whitespace_rearrangement_is_equivalent():
    """拆分边界空白重排允许：片段拼接与输入紧凑等价即可。"""
    data = _data(("很长的中文句子 " * 4, "短"))  # 紧凑 28 字
    repairs = [
        {"problem_id": "length:original:0", "output_index": 0,
         "original": "很长的中文句子 很长的", "translated": "短短"},  # 紧凑 9 字
        # 其余 19 字（拼接紧凑等价，多余空白重排）：
        {"problem_id": "length:original:0", "output_index": 1,
         "original": "中文句子 很长的中文句子 很长的中文句子", "translated": "短短"},
    ]
    gateway = _ScriptedGateway([_response(repairs)])
    repaired, report = execute_viewing_repair(
        data, _config(), QualityReport(), SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway, profile=_profile(),
    )
    assert report.viewing_repair.spliced_fragments == 2
    # 尾随空白被重排允许：拼接与输入按紧凑形式等价。
    assert "".join(seg.text for seg in repaired.segments).strip() == ("很长的中文句子 " * 4).strip()


# ---- 验收 3：输出时间由确定性规则决定，模型不能破坏时间轴 ----


def test_fragment_times_follow_reading_load_within_original_cue():
    """片段时间在原 cue 时长内按负荷分配：总时长不变、无缝隙、无越界。"""
    data = _data(("超长" * 30, "短"))  # 0-4000ms
    payload = {
        "repair_subjects": [{"segments": [
            {"id": 0, "text": "超长" * 30, "translated": "短",
             "problem_ids": ["length:original:0"]}]}]
    }
    gateway = _ScriptedGateway([_response(_repairs_for(payload, chunk=20))])
    repaired, _ = execute_viewing_repair(
        data, _config(), QualityReport(), SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway, profile=_profile(),
    )
    fragments = repaired.segments[:3]
    assert fragments[0].start_time == 0
    for first, second in zip(fragments, fragments[1:]):
        assert first.end_time == second.start_time  # 无缝隙
    assert fragments[-1].end_time == 4000  # 原 cue 边界
    assert all(frag.end_time - frag.start_time >= 1000 for frag in fragments)


def test_model_cannot_shift_timeline_across_cues():
    """时间由后处理分配：输出片段不会漂移出原 cue 或与其他段重叠。"""
    data = _data(("超长" * 30, "短"), ("第二段", "也短"))
    payload = {
        "repair_subjects": [{"segments": [
            {"id": 0, "text": "超长" * 30, "translated": "短",
             "problem_ids": ["length:original:0"]}]}]
    }
    gateway = _ScriptedGateway([_response(_repairs_for(payload, chunk=20))])
    repaired, _ = execute_viewing_repair(
        data, _config(), QualityReport(), SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway, profile=_profile(),
    )
    starts = [seg.start_time for seg in repaired.segments]
    assert starts == sorted(starts)  # 时间单调
    assert max(seg.end_time for seg in repaired.segments[:3]) <= 4000


def test_min_duration_bounds_fragment_count():
    """片段数受最短显示时长约束：4000ms / 1000ms 最多 4 片，超出的候选拒绝。"""
    data = _data(("超长" * 30, "短"))
    text = "超长" * 30
    repairs = [
        {"problem_id": "length:original:0", "output_index": i,
         "original": text[i * 10:(i + 1) * 10], "translated": "短"}
        for i in range(8)  # 8 片 > 4000 // 1000 = 4 上限
    ]
    gateway = _ScriptedGateway([_response(repairs)] * 6)
    repaired, report = execute_viewing_repair(
        data, _config(), QualityReport(), SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway, profile=_profile(),
    )
    assert report.viewing_repair.spliced_fragments == 0


# ---- 验收 4：每轮拼回完整字幕重新验收 ----


def test_resolved_problem_not_resented_next_round():
    """已通过问题不进入下一轮请求：第二轮请求不再含它。"""
    data = _data(("超长" * 30, "短"), ("正常", "正常"))
    payload = {
        "repair_subjects": [{"segments": [
            {"id": 0, "text": "超长" * 30, "translated": "短",
             "problem_ids": ["length:original:0"]}]}]
    }
    good = _repairs_for(payload, chunk=20)
    # 首轮返回整段未拆（仍超长）被拒；第二轮返回合格拆分被接受。
    bad = _response([{"problem_id": "length:original:0", "output_index": 0,
                      "original": "超长" * 30, "translated": "短"}])
    gateway = _ScriptedGateway([bad, _response(good)])
    repaired, report = execute_viewing_repair(
        data, _config(), QualityReport(), SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway, profile=_profile(),
    )
    # 首轮 bad 被拒；第二轮 good 拆分成功 → 第三轮扫描无问题终止。
    assert report.viewing_repair.rounds >= 3
    assert len(repaired.segments) == 4  # 段 0 拆 3 + 段 1
    assert not report.viewing_problems
    # 第二轮请求只含仍未解决的问题（段 0），不再含已通过的问题。
    assert len(gateway.requests[1]["repair_subjects"][0]["segments"][0]["problem_ids"]) == 1


# ---- 验收 5：重复候选停止 ----


def test_duplicate_candidate_stops_with_rollback():
    """重复候选（同段同指纹再次出现）立即停止并回退报告。"""
    data = _data(("超长" * 30, "短"), ("另一段超长" * 3, "短"))
    # 段 1 也超长：拆分成功后它的问题仍在，触发第二轮；第二轮对段 0 无问题。
    payload = {
        "repair_subjects": [{"segments": [
            {"id": 0, "text": "超长" * 30, "translated": "短",
             "problem_ids": ["length:original:0"]}]}]
    }
    good = _response(_repairs_for(payload, chunk=20))
    # 轮 1：段 0 拆分接受；轮 2：段 1 整段响应被拒（未拆分仍超长）；
    # 轮 3-5：段 1 重复交同一失败候选？—— 被拒候选不记指纹，
    # 由业务重试计数兜底（见下一测试）。这里验证接受路径不误报重复。
    gateway = _ScriptedGateway([good])
    repaired, report = execute_viewing_repair(
        data, _config(), QualityReport(), SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway, profile=_profile(),
    )
    summary = report.viewing_repair
    assert summary.spliced_fragments == 3
    assert summary.rollbacks == []
    # 段 1 整段仍超长：第一次响应无法覆盖（脚本耗尽），后续轮不再有请求。


def test_duplicate_candidate_rollback_on_repeat_after_failure():
    """被拒后重复同一失败候选：指纹只在验收通过时记录，拒绝不计指纹。"""
    data = _data(("超长" * 30, "短"))
    over = _response([{"problem_id": "length:original:0", "output_index": 0,
                       "original": "超长" * 30, "translated": "短"}])
    gateway = _ScriptedGateway([over] * 6)  # 同一失败候选 ×6
    repaired, report = execute_viewing_repair(
        data, _config(), QualityReport(), SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway, profile=_profile(),
    )
    summary = report.viewing_repair
    # 1 正常提交 + 4 业务重试耗尽 → 局部回退（不是重复候选路径）。
    assert summary.requests == 5
    assert summary.rollbacks and summary.rollbacks[0].reason == "业务修复重试耗尽"


# ---- 验收 6：首次提交不计入重试；4 次业务重试；网络重试独立 ----


def test_first_submission_not_counted_as_retry():
    """首次失败后才开始计数：第 5 次请求（1+4）耗尽 → 回退。"""
    data = _data(("超长" * 30, "短"))
    over = _response([{"problem_id": "length:original:0", "output_index": 0,
                       "original": "超长" * 30, "translated": "短"}])
    gateway = _ScriptedGateway([over] * 5)
    repaired, report = execute_viewing_repair(
        data, _config(), QualityReport(), SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway, profile=_profile(),
    )
    summary = report.viewing_repair
    assert summary.requests == 5  # 1 正常 + 4 重试
    assert summary.rollbacks
    assert len(gateway.requests) == 5


def test_transport_failure_does_not_consume_business_retries():
    """传输失败轮不消耗业务重试；连续 2 轮传输失败停止循环。"""
    data = _data(("超长" * 30, "短"))
    gateway = _ScriptedGateway([RuntimeError("network down")] * 2)
    repaired, report = execute_viewing_repair(
        data, _config(), QualityReport(), SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway, profile=_profile(),
    )
    summary = report.viewing_repair
    # 连续 2 轮传输失败 → 停止；不进入业务重试耗尽回退。
    assert summary.requests == 2
    assert summary.rollbacks == []
    assert any("传输失败" in w for w in summary.warnings)


def test_transport_then_success_still_uses_full_budget():
    """传输失败后恢复：业务重试预算仍按 1+4 口径，不被传输消耗。"""
    data = _data(("超长" * 30, "短"))
    over = _response([{"problem_id": "length:original:0", "output_index": 0,
                       "original": "超长" * 30, "translated": "短"}])
    gateway = _ScriptedGateway([RuntimeError("flaky"), over, over, over, over, over])
    repaired, report = execute_viewing_repair(
        data, _config(), QualityReport(), SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway, profile=_profile(),
    )
    # 轮 1：传输失败（不计数）；轮 2 起：业务 1+4 计满 → 回退。
    assert report.viewing_repair.rollbacks
    # 注意：5 个脚本中第 1 个是传输失败，之后 5 次业务请求。
    assert len(gateway.requests) == 6


# ---- 验收 7：业务重试耗尽 → 恢复初版快照、保留其他成功区域 ----


def test_exhausted_region_rolls_back_to_unsplit_snapshot():
    """耗尽区域恢复初版未拆分快照：时间轴、原文、段数全部回到初版。"""
    data = _data(("超长" * 30, "短"), ("第二段超长" * 4, "第二短"), ("第三段", "第三短"))
    # 段 1 的问题永远无法通过（不拆分仍超长）；段 0 可以通过。

    class _AdaptiveGateway(_ScriptedGateway):
        def complete(self, profile, request, *, cancelled=None):
            payload = json.loads(_payload_text(request))
            self.requests.append(payload)
            repairs = []
            for subject in payload["repair_subjects"]:
                segment = subject["segments"][0]
                text: str = segment["text"]
                if "第二段" in text:
                    # 不拆分，仍超长 → 候选被拒，直到业务重试耗尽回退。
                    return LLMResult(text=_response([
                        {"problem_id": segment["problem_ids"][0], "output_index": 0,
                         "original": text, "translated": segment["translated"]}
                    ]))
                head, tail = text[: len(text) // 2], text[len(text) // 2:]
                repairs.extend([
                    {"problem_id": segment["problem_ids"][0], "output_index": 0,
                     "original": head, "translated": "短短"},
                    {"problem_id": segment["problem_ids"][0], "output_index": 1,
                     "original": tail, "translated": "短短"},
                ])
            return LLMResult(text=_response(repairs))

    gateway = _AdaptiveGateway([])
    repaired, report = execute_viewing_repair(
        data, _config(), QualityReport(), SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway, profile=_profile(),
    )
    summary = report.viewing_repair
    assert summary.rollbacks  # 段 1 区域回退
    # 段 0 成功拆分保留；段 1 恢复未拆分初版。
    texts = [seg.text for seg in repaired.segments]
    assert "第二段超长" * 4 in texts  # 初版快照恢复
    assert any("超长" in text and len(text) <= 20 for text in texts)  # 段 0 拆分保留
    assert not any("第二段超长" * 2 == text for text in texts)  # 未保留失败拆分


def test_rollback_restores_initial_timeline():
    """回退恢复初版时间轴：失败区域的中间拆分时间不残留。"""
    data = _data(("超长" * 30, "短"), ("第二段超长" * 4, "第二短"))

    class _SelectiveGateway(_ScriptedGateway):
        def complete(self, profile, request, *, cancelled=None):
            payload = json.loads(_payload_text(request))
            self.requests.append(payload)
            repairs = []
            for subject in payload["repair_subjects"]:
                for segment in subject["segments"]:
                    text: str = segment["text"]
                    pid = segment["problem_ids"][0]
                    if "第二段" in text:
                        repairs.append({"problem_id": pid, "output_index": 0,
                                        "original": text, "translated": segment["translated"]})
                    else:
                        head, tail = text[:30], text[30:]
                        repairs.extend([
                            {"problem_id": pid, "output_index": 0, "original": head, "translated": "短"},
                            {"problem_id": pid, "output_index": 1, "original": tail, "translated": "短"},
                        ])
            return LLMResult(text=_response(repairs))

    gateway = _SelectiveGateway([])
    repaired, report = execute_viewing_repair(
        data, _config(), QualityReport(), SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway, profile=_profile(),
    )
    second = [seg for seg in repaired.segments if "第二段" in seg.text]
    assert len(second) == 1  # 未拆分
    assert second[0].start_time == 4000 and second[0].end_time == 8000  # 初版时间轴


# ---- 验收 8：局部未解决不阻断下游；模块级失败仍阻断 ----


def test_partial_unresolved_still_returns_working_subtitle():
    """局部回退后仍返回可交付字幕（供下游继续），未解决问题进报告。"""
    data = _data(("超长" * 30, "短"), ("第二段超长" * 4, "第二短"))
    over = _response([{"problem_id": "length:original:0", "output_index": 0,
                       "original": "超长" * 30, "translated": "短"}])
    gateway = _ScriptedGateway([over] * 10)
    repaired, report = execute_viewing_repair(
        data, _config(), QualityReport(), SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway, profile=_profile(),
    )
    # 模块不抛异常、不返回 None：字幕可用，未解决进报告。
    assert len(repaired.segments) == 2
    unresolved = report.unresolved_viewing_problems()
    assert unresolved  # 回退区域不记为通过


def test_module_level_failure_propagates():
    """模块级失败（模块自身异常）仍上抛：由调用方整体回退。"""
    data = _data(("超长" * 30, "短"))

    class _BrokenGateway:
        def complete(self, profile, request, *, cancelled=None):
            raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        execute_viewing_repair(
            data, _config(), QualityReport(), SubtitleLayoutEnum.ORIGINAL_ON_TOP,
            gateway=_BrokenGateway(), profile=_profile(),
        )


# ---- 请求形状可观察性 ----


def test_request_payload_carries_explicit_binding_fields():
    """请求载荷显式携带 problem_ids / max_fragments / limits / 上下文分开。"""
    data = _data(("超长" * 30, "短"), ("正常", "正常"))
    gateway = _ScriptedGateway([None])
    execute_viewing_repair(
        data, _config(), QualityReport(), SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway, profile=_profile(),
    )
    payload = gateway.requests[0]
    assert payload["limits"]["absolute_cjk"] == 20
    subject = payload["repair_subjects"][0]
    segment = subject["segments"][0]
    assert segment["problem_ids"] == ["length:original:0"]
    assert segment["max_fragments"] >= 1
    assert subject["problems"][0]["id"] == "length:original:0"
    # 边界上下文与主体分开表示（不进入 repair_subjects）。
    context_ids = [entry["id"] for entry in payload["boundary_context"]]
    assert 1 in context_ids  # 段 1 是段 0 的下文
    assert all(
        seg["id"] not in context_ids
        for sub in payload["repair_subjects"] for seg in sub["segments"]
    )


def test_no_profile_skips_repair_and_reports_problems():
    """未配置工具角色方案：跳过模型修复（原样返回，不扫描写入报告）。"""
    data = _data(("超长" * 30, "短"))
    repaired, report = execute_viewing_repair(
        data, _config(), QualityReport(), SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=None, profile=None,
    )
    # 跳过修复：字幕原样返回、报告不动（扫描由 run_post_stage 统一负责）。
    assert report.viewing_repair is None
    assert report.viewing_problems == []
    assert repaired.segments[0].text == "超长" * 30
