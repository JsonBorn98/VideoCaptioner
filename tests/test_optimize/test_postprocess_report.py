"""F7 QA 报告渲染测试。"""

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.postprocess import PostprocessConfig, build_qa_report, run_post_stage
from videocaptioner.core.postprocess.report import QualityReport, StageReport


def test_build_qa_report_contains_sections():
    """QA 报告应包含四个主要章节。"""
    data = ASRData([
        ASRDataSeg("这是一句非常长的中文字幕内容需要很快阅读完毕", 0, 800),
        ASRDataSeg("一句被长时间显示的字幕", 1000, 9000),
    ])
    cfg = PostprocessConfig(qa_report=True, trim_trailing_punct=False)
    data, report = run_post_stage(data, cfg)
    report.source_path = "in.srt"
    report.output_path = "out.srt"
    md = build_qa_report(report)
    assert "## 文件信息" in md
    assert "## 处理摘要" in md
    assert "## 校验摘要" in md
    assert "## 译者复查队列" in md
    assert "## 人工 QA 注意事项" in md


def test_qa_report_stage_counts_rendered():
    """处理摘要应展示各步骤变更计数。"""
    report = QualityReport(source_path="a", output_path="b", segment_count=3)
    sr = StageReport("placeholders")
    sr.add(2)
    report.stages["placeholders"] = sr
    md = build_qa_report(report)
    assert "占位符清理: 2 处" in md


def test_qa_report_table_row_cap():
    """复查表格行数受 40 行上限限制并注明省略数。"""
    from videocaptioner.core.postprocess.report import AuditResult, DurationAnomaly

    result = AuditResult(segment_count=100)
    for i in range(50):
        result.long_duration.append(
            DurationAnomaly(i, "0", "1", 8.0, 5, "时长>7s", "文本", "")
        )
    report = QualityReport(audit=result)
    md = build_qa_report(report)
    assert "省略 10 条长时长样本" in md


def test_speed_report_shows_triggering_language_field():
    """英文侧超速时，快速外文表应展示英文字段而不是中文译文。"""
    data = ASRData([ASRDataSeg("this sentence is much too fast", 0, 500, "这句很快")])
    _, report = run_post_stage(
        data,
        PostprocessConfig(qa_report=True, trim_trailing_punct=False),
    )

    md = build_qa_report(report)

    assert "this sentence is much too fast" in md
    assert "| 这句很快 |" not in md
