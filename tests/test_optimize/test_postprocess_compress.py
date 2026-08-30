"""F5 快速字幕 LLM 压缩重译测试。"""

import json

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.llm.models import (
    LLMModelProfile,
    LLMResult,
    LLMTransport,
    ProviderDialect,
)
from videocaptioner.core.postprocess import PostprocessConfig, run_post_stage
from videocaptioner.core.postprocess.report import QualityReport


class _CapturingGateway:
    """Fake gateway：记录请求形状并回放固定 JSON 文本。"""

    def __init__(self, text: str):
        self.text = text
        self.requests = []

    def complete(self, profile, request, *, cancelled=None):
        self.requests.append((profile, request, cancelled))
        return LLMResult(text=self.text)


def _profile() -> LLMModelProfile:
    return LLMModelProfile(
        profile_id="compress-profile",
        name="Compress Profile",
        transport=LLMTransport.OPENAI_COMPATIBLE,
        dialect=ProviderDialect.GENERIC,
        base_url="https://compress.test/v1",
        api_key="secret",
        model="compress-model",
        work_context_tokens=16_384,
    )


def _fast_cjk_data():
    # 20 字 / 0.8s = 25 cps，远超硬限 11
    return ASRData([ASRDataSeg("这是一句非常长的中文字幕内容需要很快阅读完毕", 0, 800)])


def _config(**overrides) -> PostprocessConfig:
    return PostprocessConfig(
        compress_fast_subtitles=True,
        utility_llm_profile=_profile(),
        trim_trailing_punct=False,
        **overrides,
    )


def test_compress_writes_back_valid_result_and_request_shape():
    """合法压缩结果应写回中文侧；请求经 gateway 且无 timeout（落 120 秒默认）。"""
    gateway = _CapturingGateway(json.dumps({"1": "这是一句非常长的"}, ensure_ascii=False))
    from videocaptioner.core.postprocess.compress import compress_fast_subtitles

    data, report = compress_fast_subtitles(_fast_cjk_data(), _config(), QualityReport(), gateway)
    assert data.segments[0].text == "这是一句非常长的"
    assert data.segments[0].start_time == 0 and data.segments[0].end_time == 800
    assert report.stage("compress").changed == 1

    used_profile, request, cancelled = gateway.requests[0]
    assert used_profile.profile_id == "compress-profile"
    assert cancelled is None
    assert request.timeout is None  # 不传 timeout → adapter 构造默认 120 秒
    assert request.metadata == {"stage": "llm_compress", "role": "utility"}
    assert request.response_schema is None  # 压缩保持纯文本 JSON 解析，无 schema
    assert request.messages[0].role == "system"


def test_compress_keeps_original_when_result_too_long():
    """超长/不合格结果应保留原文并记入失败队列。"""
    # 返回比 target 还长的文本
    gateway = _CapturingGateway(
        json.dumps({"1": "这个压缩结果依然非常长根本没有压缩到位一点用都没有"}, ensure_ascii=False)
    )
    from videocaptioner.core.postprocess.compress import compress_fast_subtitles

    original = "这是一句非常长的中文字幕内容需要很快阅读完毕"
    data = _fast_cjk_data()
    data, report = compress_fast_subtitles(data, _config(), QualityReport(), gateway)
    assert data.segments[0].text == original
    assert report.compress_failures


def test_compress_missing_profile_is_skipped():
    """未配置工具角色方案时跳过压缩，段数不变。"""
    data = _fast_cjk_data()
    cfg = PostprocessConfig(compress_fast_subtitles=True, trim_trailing_punct=False)
    data, report = run_post_stage(data, cfg)
    assert len(data.segments) == 1
    assert report.stage("compress").changed == 0


def test_compress_segment_count_never_changes():
    """压缩绝不改变段数。"""
    gateway = _CapturingGateway(json.dumps({"1": "短"}, ensure_ascii=False))
    from videocaptioner.core.postprocess.compress import compress_fast_subtitles

    data = _fast_cjk_data()
    data, _ = compress_fast_subtitles(data, _config(), QualityReport(), gateway)
    assert len(data.segments) == 1


def test_compress_retries_with_feedback_then_succeeds():
    """校验失败后反馈重试（agent loop），最终合格才写回。"""

    class RetryGateway:
        def __init__(self):
            self.calls = 0

        def complete(self, profile, request, *, cancelled=None):
            self.calls += 1
            # 第一次超长（触发校验失败反馈），第二次压到 target 内且保持相似度。
            text = (
                json.dumps({"1": "这个压缩结果依然非常长根本没有压缩到位一点用都没有"})
                if self.calls == 1
                else json.dumps({"1": "这是一句长字幕"}, ensure_ascii=False)
            )
            return LLMResult(text=text)

    from videocaptioner.core.postprocess.compress import compress_fast_subtitles

    gateway = RetryGateway()
    data = _fast_cjk_data()
    data, report = compress_fast_subtitles(data, _config(), QualityReport(), gateway)
    assert gateway.calls == 2
    assert data.segments[0].text == "这是一句长字幕"
    assert report.stage("compress").changed == 1
