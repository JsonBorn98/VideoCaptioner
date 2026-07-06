"""F2 文本规范化测试（引号 + 弱尾标点）。"""

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.postprocess import PostprocessConfig, run_normalize_stage, run_post_stage
from videocaptioner.core.postprocess.normalize import (
    QuoteState,
    normalize_quotes,
    trim_weak_trailing,
)


def test_normalize_quotes_converts_and_tracks_state_across_calls():
    """引号跨段开闭状态应正确保持。"""
    state = QuoteState()
    first, c1 = normalize_quotes('他说"你好', state)
    assert first == "他说「你好"
    assert c1 == 1
    second, c2 = normalize_quotes('世界"结束', state)
    assert second == "世界」结束"


def test_normalize_quotes_keeps_english_apostrophe():
    """英文词内撇号 don't 不应被改动。"""
    out, changed = normalize_quotes("don't stop", QuoteState())
    assert out == "don't stop"
    assert changed == 0


def test_trim_weak_trailing_strips_before_closer():
    """闭合符前的弱标点应被删除（。」-> 」）。"""
    out, changed = trim_weak_trailing("他说「你好。」")
    assert out == "他说「你好」"
    assert changed == 1


def test_trim_weak_trailing_preserves_strong_punctuation():
    """强标点（？！…）应保留。"""
    out, changed = trim_weak_trailing("真的吗？")
    assert out == "真的吗？"
    assert changed == 0


def test_normalize_stage_default_matches_legacy_remove_punctuation():
    """默认路径（引号关、尾标点开）应与旧 remove_punctuation 逐字节一致。"""
    base = [
        ASRDataSeg("你好，", 0, 1000, "hi，"),
        ASRDataSeg("世界。", 1000, 2000, "world。"),
        ASRDataSeg("keep!", 2000, 3000, "保留？"),
    ]
    legacy = ASRData([ASRDataSeg(s.text, s.start_time, s.end_time, s.translated_text) for s in base])
    legacy.remove_punctuation()

    new = ASRData([ASRDataSeg(s.text, s.start_time, s.end_time, s.translated_text) for s in base])
    new, _ = run_normalize_stage(new, PostprocessConfig())

    assert [(s.text, s.translated_text) for s in legacy.segments] == [
        (s.text, s.translated_text) for s in new.segments
    ]


def test_normalize_stage_enhanced_only_touches_cjk():
    """开启引号规范化时，英文侧仍走旧标点集（. 不删），中文侧走扩展逻辑。"""
    data = ASRData([ASRDataSeg("你好，", 0, 1000, "hello.")])
    cfg = PostprocessConfig(normalize_quotes=True, trim_trailing_punct=True)
    data, _ = run_normalize_stage(data, cfg)
    seg = data.segments[0]
    assert seg.text == "你好"
    assert seg.translated_text == "hello."


def test_keep_trailing_punct_disables_trimming():
    """trim_trailing_punct=False 时不删任何尾标点。"""
    data = ASRData([ASRDataSeg("你好，", 0, 1000)])
    cfg = PostprocessConfig(trim_trailing_punct=False)
    data, _ = run_normalize_stage(data, cfg)
    assert data.segments[0].text == "你好，"


def test_post_stage_runs_explicit_normalize_without_llm_steps():
    """只开规则规范化、不开优化/翻译时，保存前仍应执行 normalize。"""
    data = ASRData([ASRDataSeg('他说"你好。"', 0, 1000)])
    cfg = PostprocessConfig(normalize_quotes=True)

    data, _ = run_post_stage(data, cfg)

    assert data.segments[0].text == "他说「你好」"
