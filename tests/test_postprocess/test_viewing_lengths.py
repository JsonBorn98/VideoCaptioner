"""每侧显示策略与长度验收（票 02）：折算字符数、混合阈值与单行扫描。

只验证外部行为：核心任务入口与 ``scan_viewing_lengths`` 的可见结果，
不锁定内部调用顺序（见 spec「Testing Decisions」）。
"""

from __future__ import annotations

import pytest

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.entities import SubtitleLayoutEnum
from videocaptioner.core.postprocess import (
    PostprocessConfig,
    PostprocessTask,
    run_postprocess_task,
    scan_viewing_lengths,
    weighted_length,
)
from videocaptioner.core.postprocess.viewing import effective_length_limit


def _data(*pairs: tuple[str, str]) -> ASRData:
    return ASRData([ASRDataSeg(text, 0, 2000, translated) for text, translated in pairs])


# ---- 折算字符数（D04/D25，验收标准 2）----


def test_weighted_length_weights():
    # CJK/全角计 1；拉丁字母、数字、半角标点计 0.5；空白及零宽格式字符计 0；其他符号计 1。
    assert weighted_length("你好世界") == 4.0
    assert weighted_length("ｈｅｌｌｏ") == 5.0  # 全角字母按全角形式计 1
    assert weighted_length("hello") == 2.5
    assert weighted_length("abc 123") == 3.0  # 6 个半角字符 × 0.5，空格计 0
    assert weighted_length("你好 hello") == 2.0 + 2.5
    assert weighted_length("　") == 0.0  # 全角空格是空白
    assert weighted_length("a​b") == 1.0  # 零宽空格（Cf）不增加长度
    assert weighted_length("→") == 1.0  # 其他符号计 1
    assert weighted_length("，。！") == 3.0  # 全角标点计 1
    assert weighted_length("don't") == 2.5  # 半角撇号 0.5


# ---- 混合语言有效阈值（D25，验收标准 4）----


def test_effective_limit_pure_languages():
    cfg_cjk, cfg_latin = 20.0, 25.0
    assert effective_length_limit("纯中文文本", cjk_limit=cfg_cjk, latin_limit=cfg_latin) == cfg_cjk
    assert (
        effective_length_limit("plain english", cjk_limit=cfg_cjk, latin_limit=cfg_latin)
        == cfg_latin
    )


def test_effective_limit_mixed_interpolates_by_share():
    limit = effective_length_limit(
        "你好abc", cjk_limit=20.0, latin_limit=30.0
    )
    # CJK 权重 2，总权重 3.5 → 中文占比 2/3.5；阈值在 30 与 20 之间线性插值。
    cjk_share = 2.0 / 3.5
    expected = 30.0 + cjk_share * (20.0 - 30.0)
    assert limit == pytest.approx(expected)


def test_effective_limit_blank_text_uses_latin():
    assert effective_length_limit("  ", cjk_limit=20.0, latin_limit=25.0) == 25.0


# ---- 上限排序校验（D03，验收标准 3）----


def test_target_above_absolute_is_rejected():
    with pytest.raises(ValueError, match="target_cjk cannot exceed"):
        PostprocessConfig(single_line_target_cjk=25, single_line_absolute_cjk=20)
    with pytest.raises(ValueError, match="target_latin cannot exceed"):
        PostprocessConfig(single_line_target_latin=30, single_line_absolute_latin=25)


def test_non_positive_limits_are_rejected():
    with pytest.raises(ValueError, match="must be positive"):
        PostprocessConfig(single_line_absolute_cjk=0)
    with pytest.raises(ValueError, match="must be positive"):
        PostprocessConfig(single_line_target_latin=-1)


def test_invalid_display_mode_is_rejected():
    with pytest.raises(ValueError, match="original_display_mode"):
        PostprocessConfig(original_display_mode="wrap")
    with pytest.raises(ValueError, match="translated_display_mode"):
        PostprocessConfig(translated_display_mode="single")


def test_display_mode_for_resolves_sides():
    cfg = PostprocessConfig(
        original_display_mode="auto_wrap", translated_display_mode="single_line"
    )
    assert cfg.display_mode_for("original") == "auto_wrap"
    assert cfg.display_mode_for("translated") == "single_line"
    with pytest.raises(ValueError, match="unknown display side"):
        cfg.display_mode_for("third")


# ---- 每侧独立扫描（D02/D19，验收标准 1、5）----


def test_auto_wrap_side_skips_length_and_line_constraints():
    long_text = "超长" * 30  # 折算 60 > 绝对上限 20
    # 译文侧同时超长且多行；自动换行侧的长度与行数约束都被跳过。
    data = _data((long_text, long_text + "\n第二行"))
    cfg = PostprocessConfig(translated_display_mode="auto_wrap")
    problems = scan_viewing_lengths(data, cfg, SubtitleLayoutEnum.ORIGINAL_ON_TOP)
    # 原文侧单行限长照常扫描；译文侧自动换行完全跳过。
    assert problems
    assert all(problem.side == "original" for problem in problems)
    assert {p.problem_id for p in problems} == {"length:original:0"}


def test_both_auto_wrap_produces_no_problems():
    data = _data(("任意长度" * 40, "any length at all " * 40))
    cfg = PostprocessConfig(
        original_display_mode="auto_wrap", translated_display_mode="auto_wrap"
    )
    assert scan_viewing_lengths(data, cfg, SubtitleLayoutEnum.ORIGINAL_ON_TOP) == []


def test_independent_modes_default_to_single_line():
    cfg = PostprocessConfig()
    assert cfg.original_display_mode == "single_line"
    assert cfg.translated_display_mode == "single_line"
    assert cfg.any_viewing_single_line() is True


def test_layout_only_sides_are_respected():
    data = _data(("超长" * 30, "超长" * 30))
    cfg = PostprocessConfig()
    problems = scan_viewing_lengths(data, cfg, SubtitleLayoutEnum.ONLY_ORIGINAL)
    assert problems and all(problem.side == "original" for problem in problems)
    problems = scan_viewing_lengths(data, cfg, SubtitleLayoutEnum.ONLY_TRANSLATE)
    assert problems and all(problem.side == "translated" for problem in problems)


# ---- 问题结构与长度验收（验收标准 6）----


def test_overlength_produces_problem_with_limits_and_reason():
    data = _data(("超长" * 30, "ok"))
    cfg = PostprocessConfig()
    problems = scan_viewing_lengths(data, cfg, SubtitleLayoutEnum.ORIGINAL_ON_TOP)
    length_problems = [p for p in problems if p.problem_id.startswith("length:")]
    assert len(length_problems) == 1
    problem = length_problems[0]
    assert problem.side == "original"
    assert problem.segment_index == 0
    assert problem.weighted_length == 60.0
    assert problem.absolute_limit == 20.0  # 纯中文 → 中文绝对上限
    assert problem.target_limit == 16.0
    assert problem.resolved is False
    assert "绝对上限" in problem.reason


def test_multiline_single_line_side_is_a_problem():
    data = _data(("第一行\n第二行", "ok"))
    cfg = PostprocessConfig()
    problems = scan_viewing_lengths(data, cfg, SubtitleLayoutEnum.ORIGINAL_ON_TOP)
    line_problems = [p for p in problems if p.problem_id.startswith("lines:")]
    assert len(line_problems) == 1
    assert line_problems[0].side == "original"


def test_within_absolute_limit_is_not_a_problem():
    data = _data(("刚好", "fine"))
    cfg = PostprocessConfig()
    assert scan_viewing_lengths(data, cfg, SubtitleLayoutEnum.ORIGINAL_ON_TOP) == []


def test_blank_side_text_is_skipped():
    data = _data(("好", ""))
    cfg = PostprocessConfig()
    assert scan_viewing_lengths(data, cfg, SubtitleLayoutEnum.ORIGINAL_ON_TOP) == []


# ---- 核心任务入口行为（票 01 切面延续）----


def test_task_reports_unresolved_viewing_problems(tmp_path):
    source = tmp_path / "input.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n" + "超长" * 30 + "\n",
        encoding="utf-8",
    )
    result = run_postprocess_task(
        PostprocessTask(
            str(source),
            postprocessed_subtitle_path=str(tmp_path / "result.srt"),
            config_snapshot=PostprocessConfig(
                trim_trailing_punct=True, audit_reading_speed=True
            ),
        )
    )
    assert result.succeeded
    unresolved = result.report.unresolved_viewing_problems()
    assert unresolved
    assert all(not problem.resolved for problem in unresolved)
    # 回退区域不会被记为通过：默认 resolved=False，只有显式验收才置 True。
    assert result.report.viewing_problems


def test_task_auto_wrap_side_contributes_no_problems(tmp_path):
    source = tmp_path / "input.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n" + "超长" * 30 + "\n",
        encoding="utf-8",
    )
    result = run_postprocess_task(
        PostprocessTask(
            str(source),
            postprocessed_subtitle_path=str(tmp_path / "result.srt"),
            config_snapshot=PostprocessConfig(
                trim_trailing_punct=True,
                audit_reading_speed=True,
                original_display_mode="auto_wrap",
                translated_display_mode="auto_wrap",
            ),
        )
    )
    assert result.succeeded
    assert result.report.unresolved_viewing_problems() == []
