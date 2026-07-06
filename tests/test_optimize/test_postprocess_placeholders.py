"""F1 占位符清理测试。"""

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.postprocess import PostprocessConfig, run_pre_stage
from videocaptioner.core.postprocess.placeholders import is_placeholder


def test_is_placeholder_recognizes_common_forms():
    """常见占位符形式应被识别。"""
    assert is_placeholder("[Music]")
    assert is_placeholder("[音乐]")
    assert is_placeholder("（掌声）")
    assert is_placeholder("♪")
    assert is_placeholder("♪ Music")
    assert is_placeholder("[Speaking Japanese]")


def test_is_placeholder_preserves_meaningful_bracket_content():
    """含实义内容的括号不应被误判为占位符。"""
    assert not is_placeholder("[第3章] 内容")
    assert not is_placeholder("这是一句正常字幕")
    assert not is_placeholder("[John] Hello there")


def test_remove_placeholders_deletes_both_side_placeholder_segments():
    """双语两侧均为占位符（或一侧为空）的段应被删除并计数。"""
    data = ASRData([
        ASRDataSeg("[Music]", 0, 1000),
        ASRDataSeg("Hello", 1000, 2000, "你好"),
        ASRDataSeg("[音乐]", 2000, 3000, "[音乐]"),
        ASRDataSeg("♪", 3000, 4000),
    ])
    cfg = PostprocessConfig(remove_placeholders=True)
    data, report = run_pre_stage(data, cfg)
    texts = [s.text for s in data.segments]
    assert texts == ["Hello"]
    assert report.stage("placeholders").changed == 3


def test_remove_placeholders_clears_translation_only_placeholder():
    """仅译文侧是占位符时，只清空译文，保留原文段。"""
    data = ASRData([ASRDataSeg("Real speech", 0, 1000, "[Applause]")])
    cfg = PostprocessConfig(remove_placeholders=True)
    data, report = run_pre_stage(data, cfg)
    assert len(data.segments) == 1
    assert data.segments[0].text == "Real speech"
    assert data.segments[0].translated_text == ""


def test_remove_placeholders_default_off_is_noop():
    """默认关闭时不改变任何段。"""
    data = ASRData([ASRDataSeg("[Music]", 0, 1000)])
    data, _ = run_pre_stage(data, PostprocessConfig())
    assert [s.text for s in data.segments] == ["[Music]"]
