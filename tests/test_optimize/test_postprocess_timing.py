"""F3 时轴间隙闭合测试。"""

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.postprocess import PostprocessConfig
from videocaptioner.core.postprocess.report import QualityReport
from videocaptioner.core.postprocess.timing import close_gaps


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
