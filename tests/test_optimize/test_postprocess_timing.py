"""F3 时轴间隙闭合 + 尾部补偿测试。"""

import pytest

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.entities import SubtitleLayoutEnum
from videocaptioner.core.postprocess import PostprocessConfig
from videocaptioner.core.postprocess.report import QualityReport
from videocaptioner.core.postprocess.timing import (
    apply_tail_compensation,
    close_gaps,
    compensation_for_gap,
)


def _sentence_data(spans):
    # 句子级文本（避免被判定为词级而跳过）
    return ASRData([
        ASRDataSeg("这是一句完整的中文字幕内容", s, e) for s, e in spans
    ])


def test_close_gaps_extend_closes_small_gap():
    """0.5s 间隙应被闭合（前段延长到后段开始）。"""
    data = _sentence_data([(0, 1000), (1500, 2500)])
    cfg = PostprocessConfig(fix_gaps=True)
    data, report = close_gaps(data, cfg, QualityReport())
    assert data.segments[0].end_time == 1500
    assert data.segments[1].start_time == 1500
    assert report.stage("close_gaps").changed == 1


def test_close_gaps_ignores_large_gap():
    """超过 max_gap_ms 的间隙不动。"""
    data = _sentence_data([(0, 1000), (2000, 3000)])
    cfg = PostprocessConfig(fix_gaps=True, max_gap_ms=800)
    data, _ = close_gaps(data, cfg, QualityReport())
    assert data.segments[0].end_time == 1000


def test_close_gaps_midpoint_mode():
    """midpoint 模式吸附到间隙 3/4 点。"""
    data = _sentence_data([(0, 1000), (1500, 2500)])
    cfg = PostprocessConfig(fix_gaps=True, gap_mode="midpoint")
    data, _ = close_gaps(data, cfg, QualityReport())
    # mid = (1000+1500)//2 + 500//4 = 1250 + 125 = 1375
    assert data.segments[0].end_time == 1375
    assert data.segments[1].start_time == 1375


def test_close_gaps_skips_word_level():
    """词级时间戳数据整体跳过。"""
    data = ASRData([ASRDataSeg("hello", 0, 1000), ASRDataSeg("world", 1500, 2500)])
    cfg = PostprocessConfig(fix_gaps=True)
    data, report = close_gaps(data, cfg, QualityReport())
    assert data.segments[0].end_time == 1000
    assert report.stage("close_gaps").changed == 0


# ---- 尾部补偿曲线 compensation_for_gap ----
# 默认曲线：max_gap=800, min_comp=200, max_comp_gap=2000, max_comp=800
# 斜坡 comp(g) = 200 + round((g-800)/2)（span=1200, rise=600）


def test_compensation_curve_key_values():
    cfg = PostprocessConfig()
    assert compensation_for_gap(0, cfg) == 0
    assert compensation_for_gap(800, cfg) == 0  # 恰在最大闭合间隙：仍不补偿
    assert compensation_for_gap(801, cfg) == 200  # 跨过即跳到最小补偿
    assert compensation_for_gap(1000, cfg) == 300
    assert compensation_for_gap(1400, cfg) == 500
    assert compensation_for_gap(2000, cfg) == 800  # 饱和到最大补偿
    assert compensation_for_gap(5000, cfg) == 800  # 更大间隙不再增加


def test_compensation_curve_monotonic_hold_and_blank():
    """补偿区内：补偿量单调不降，留白也单调不降（斜率≤1 的直接后果）。"""
    cfg = PostprocessConfig()
    prev_comp = -1
    prev_blank = -1
    for gap in range(cfg.max_gap_ms + 1, 6001, 5):
        comp = compensation_for_gap(gap, cfg)
        blank = gap - comp
        assert comp >= prev_comp
        assert blank >= prev_blank
        assert comp <= gap  # 永不重叠
        prev_comp, prev_blank = comp, blank


def test_tail_compensation_below_close_gap_is_noop():
    """gap <= 最大闭合间隙：交给闪轴闭合，不补偿。"""
    data = _sentence_data([(0, 1000), (1600, 2600)])  # gap 600
    cfg = PostprocessConfig(tail_compensation=True)
    data, report = apply_tail_compensation(data, cfg, QualityReport())
    assert data.segments[0].end_time == 1000
    assert report.stage("tail_compensation").changed == 0


def test_tail_compensation_ramp_extends_previous_end():
    """max_gap < gap < 最大补偿间隙：按曲线追加补偿，保留留白。"""
    data = _sentence_data([(0, 1000), (2400, 3400)])  # gap 1400
    cfg = PostprocessConfig(tail_compensation=True)
    data, report = apply_tail_compensation(data, cfg, QualityReport())
    assert data.segments[0].end_time == 1500  # +compensation_for_gap(1400)=500
    assert data.segments[1].start_time == 2400  # 下一段不动
    assert report.stage("tail_compensation").changed == 1


def test_tail_compensation_saturates_at_max():
    """超过最大补偿间隙：补偿封顶在最大补偿。"""
    data = _sentence_data([(0, 1000), (4000, 5000)])  # gap 3000 > 2000
    cfg = PostprocessConfig(tail_compensation=True)
    data, _ = apply_tail_compensation(data, cfg, QualityReport())
    assert data.segments[0].end_time == 1800  # +max_compensation_ms(800)


def test_tail_compensation_respects_max_duration():
    """延长后不得超过 max_duration_ms 显示时长。"""
    data = _sentence_data([(0, 6900), (8300, 9300)])  # 已显示 6900ms，gap 1400
    cfg = PostprocessConfig(tail_compensation=True, max_duration_ms=7000)
    data, _ = apply_tail_compensation(data, cfg, QualityReport())
    assert data.segments[0].end_time == 7000  # room = 100，截断


def test_tail_compensation_skips_protected_music_marker():
    """音乐 / 歌词占位符段是受保护段，不补偿。"""
    data = ASRData([
        ASRDataSeg("[Music]", 0, 1000),
        ASRDataSeg("下一句正常字幕内容在这里", 2400, 3400),  # gap 1400
    ])
    cfg = PostprocessConfig(tail_compensation=True)
    data, report = apply_tail_compensation(
        data, cfg, QualityReport(), SubtitleLayoutEnum.ONLY_ORIGINAL
    )
    assert data.segments[0].end_time == 1000
    assert report.stage("tail_compensation").changed == 0


def test_tail_compensation_skips_word_level():
    """词级时间戳数据整体跳过。"""
    data = ASRData([ASRDataSeg("hello", 0, 1000), ASRDataSeg("world", 2400, 3400)])
    cfg = PostprocessConfig(tail_compensation=True)
    data, report = apply_tail_compensation(data, cfg, QualityReport())
    assert data.segments[0].end_time == 1000
    assert report.stage("tail_compensation").changed == 0


def test_tail_compensation_keeps_timeline_ordered_and_non_overlapping():
    """连续多段：每处补偿后仍保持有序、不重叠。"""
    data = _sentence_data([(0, 1000), (2400, 3400), (5400, 6400)])  # gaps 1400, 2000
    cfg = PostprocessConfig(tail_compensation=True)
    data, report = apply_tail_compensation(data, cfg, QualityReport())
    assert report.stage("tail_compensation").changed == 2
    for i in range(len(data.segments) - 1):
        assert data.segments[i].end_time < data.segments[i + 1].start_time


def test_tail_compensation_disabled_by_default():
    """默认关闭（管线以 cfg.tail_compensation 门控）。"""
    assert PostprocessConfig().tail_compensation is False


# ---- 配置校验：补偿曲线约束 ----


def test_config_rejects_non_boolean_tail_compensation():
    with pytest.raises(ValueError, match="tail_compensation must be a boolean"):
        PostprocessConfig(tail_compensation=1)


def test_config_rejects_min_compensation_above_max_gap():
    with pytest.raises(ValueError, match="min_compensation_ms cannot exceed max_gap_ms"):
        PostprocessConfig(min_compensation_ms=900, max_gap_ms=800)


def test_config_rejects_max_compensation_gap_not_above_max_gap():
    with pytest.raises(ValueError, match="max_compensation_gap_ms must exceed max_gap_ms"):
        PostprocessConfig(max_gap_ms=800, max_compensation_gap_ms=800)


def test_config_rejects_max_below_min_compensation():
    with pytest.raises(
        ValueError, match="max_compensation_ms cannot be less than min_compensation_ms"
    ):
        PostprocessConfig(min_compensation_ms=600, max_compensation_ms=400)


def test_config_rejects_ramp_steeper_than_one():
    # 斜率 = (1500-200)/(1000-800) = 6.5 > 1
    with pytest.raises(ValueError, match="slope <= 1"):
        PostprocessConfig(
            max_gap_ms=800,
            min_compensation_ms=200,
            max_compensation_gap_ms=1000,
            max_compensation_ms=1500,
        )


def test_any_enabled_includes_tail_compensation():
    # trim_trailing_punct 默认开，故以关闭它为基线隔离 tail_compensation 的作用
    assert PostprocessConfig(trim_trailing_punct=False).any_enabled() is False
    assert (
        PostprocessConfig(trim_trailing_punct=False, tail_compensation=True).any_enabled()
        is True
    )
