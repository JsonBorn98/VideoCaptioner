"""F5 快速字幕 LLM 压缩重译测试。"""

import json
from types import SimpleNamespace

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.postprocess import PostprocessConfig, run_post_stage


def _resp(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


def _fast_cjk_data():
    # 20 字 / 0.8s = 25 cps，远超硬限 11
    return ASRData([ASRDataSeg("这是一句非常长的中文字幕内容需要很快阅读完毕", 0, 800)])


def test_compress_writes_back_valid_result(monkeypatch):
    """合法压缩结果应写回中文侧，时间戳不变。"""

    def fake(messages, model, temperature=1, **kwargs):
        return _resp(json.dumps({"1": "这是一句非常长的"}, ensure_ascii=False))

    monkeypatch.setattr("videocaptioner.core.postprocess.compress.call_llm", fake)
    data = _fast_cjk_data()
    cfg = PostprocessConfig(
        compress_fast_subtitles=True, llm_model="gpt-4o-mini", trim_trailing_punct=False
    )
    data, report = run_post_stage(data, cfg)
    assert data.segments[0].text == "这是一句非常长的"
    assert data.segments[0].start_time == 0 and data.segments[0].end_time == 800
    assert report.stage("compress").changed == 1


def test_compress_keeps_original_when_result_too_long(monkeypatch):
    """超长/不合格结果应保留原文并记入失败队列。"""

    def fake(messages, model, temperature=1, **kwargs):
        # 返回比 target 还长的文本
        return _resp(json.dumps({"1": "这个压缩结果依然非常长根本没有压缩到位一点用都没有"}, ensure_ascii=False))

    monkeypatch.setattr("videocaptioner.core.postprocess.compress.call_llm", fake)
    original = "这是一句非常长的中文字幕内容需要很快阅读完毕"
    data = _fast_cjk_data()
    cfg = PostprocessConfig(
        compress_fast_subtitles=True, llm_model="gpt-4o-mini", trim_trailing_punct=False
    )
    data, report = run_post_stage(data, cfg)
    assert data.segments[0].text == original
    assert report.compress_failures


def test_compress_missing_model_is_skipped():
    """未配置模型时跳过压缩，段数不变。"""
    data = _fast_cjk_data()
    cfg = PostprocessConfig(compress_fast_subtitles=True, trim_trailing_punct=False)
    data, report = run_post_stage(data, cfg)
    assert len(data.segments) == 1
    assert report.stage("compress").changed == 0


def test_compress_segment_count_never_changes(monkeypatch):
    """压缩绝不改变段数。"""

    def fake(messages, model, temperature=1, **kwargs):
        return _resp(json.dumps({"1": "短"}, ensure_ascii=False))

    monkeypatch.setattr("videocaptioner.core.postprocess.compress.call_llm", fake)
    data = _fast_cjk_data()
    cfg = PostprocessConfig(
        compress_fast_subtitles=True, llm_model="gpt-4o-mini", trim_trailing_punct=False
    )
    data, _ = run_post_stage(data, cfg)
    assert len(data.segments) == 1
