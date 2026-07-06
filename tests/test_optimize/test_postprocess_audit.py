"""F4/F6 阅读速度与时长异常审计测试。"""

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.postprocess import PostprocessConfig
from videocaptioner.core.postprocess.audit import audit
from videocaptioner.core.postprocess.report import QualityReport


def test_hard_warning_for_fast_cjk():
    """超过中文硬 CPS 限的段应记入硬警告。"""
    # 20 字 / 0.8s = 25 cps > 11
    data = ASRData([ASRDataSeg("这是一句非常长的中文字幕内容需要很快阅读完毕", 0, 800)])
    cfg = PostprocessConfig(audit_reading_speed=True)
    _, report = audit(data, cfg, QualityReport())
    assert len(report.audit.hard) == 1
    assert report.audit.hard[0].is_cjk


def test_comfort_warning_for_short_duration():
    """时长低于感知下限记入舒适警告。"""
    data = ASRData([ASRDataSeg("短句", 0, 500)])
    cfg = PostprocessConfig(audit_reading_speed=True)
    _, report = audit(data, cfg, QualityReport())
    assert len(report.audit.comfort) >= 1


def test_long_duration_anomaly():
    """时长 > max_duration_ms 记入长时长异常。"""
    data = ASRData([ASRDataSeg("一句被长时间显示的字幕", 0, 8000)])
    cfg = PostprocessConfig(audit_reading_speed=True)
    _, report = audit(data, cfg, QualityReport())
    assert len(report.audit.long_duration) == 1
    assert "时长>7" in report.audit.long_duration[0].reason


def test_short_text_long_display_anomaly():
    """短文本（≤12 字）长显示（>4s）记入长时长异常。"""
    data = ASRData([ASRDataSeg("你好", 0, 5000)])
    cfg = PostprocessConfig(audit_reading_speed=True)
    _, report = audit(data, cfg, QualityReport())
    assert any("短文本" in a.reason for a in report.audit.long_duration)


def test_overlap_detection():
    """负间隙（重叠）记入结构警告。"""
    data = ASRData([
        ASRDataSeg("第一句完整的中文字幕", 0, 2000),
        ASRDataSeg("第二句完整的中文字幕", 1500, 3000),
    ])
    cfg = PostprocessConfig(audit_reading_speed=True)
    _, report = audit(data, cfg, QualityReport())
    assert len(report.audit.overlaps) == 1
    assert report.audit.overlaps[0].overlap_ms == 500


def test_audit_does_not_mutate_segments():
    """审计不修改任何段。"""
    data = ASRData([ASRDataSeg("测试字幕内容", 0, 800)])
    before = [(s.text, s.start_time, s.end_time) for s in data.segments]
    audit(data, PostprocessConfig(audit_reading_speed=True), QualityReport())
    after = [(s.text, s.start_time, s.end_time) for s in data.segments]
    assert before == after


def test_hard_warning_carries_context():
    """硬警告应附带 ±1 相邻段上下文。"""
    data = ASRData([
        ASRDataSeg("前一句完整的中文字幕内容", 0, 3000),
        ASRDataSeg("这是一句非常长的中文字幕内容需要很快阅读完毕", 3000, 3800),
        ASRDataSeg("后一句完整的中文字幕内容", 3800, 6000),
    ])
    cfg = PostprocessConfig(audit_reading_speed=True)
    _, report = audit(data, cfg, QualityReport())
    assert report.audit.hard
    ctx = report.audit.hard[0].context
    assert len(ctx) == 3
    assert any(c["current"] for c in ctx)
