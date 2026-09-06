"""旧观看限长与原文改写配置链的删除验证（票 07；D05/D11/D20、ADR-0020）。

后处理独占观看约束后，上游只保留内容语义分段与请求容量分批：
- 旧 CJK/英文观看长度参数与原文改写开关从数据类、函数签名、CLI 与配置默认值整体删除。
- 旧持久化残留值直接丢弃：不迁移、不兼容读取，不改变新任务行为。
- 上游分段仍可工作且逐字保留完整内容，不读取任何观看显示上限。
- 新后处理配置使用稳定默认值即可创建有效任务。
"""

import dataclasses
import inspect
import json
from argparse import Namespace

import pytest

from videocaptioner.core.entities import SubtitleConfig
from videocaptioner.core.postprocess.config import PostprocessConfig
from videocaptioner.core.postprocess.profiles import (
    PROFILE_SCHEMA,
    PROFILE_SCHEMA_VERSION,
    PostprocessProfileStore,
)
from videocaptioner.core.speed.pipeline import optimize_speed
from videocaptioner.core.split.split import SubtitleSplitter
from videocaptioner.core.split.split_by_llm import (
    SEGMENT_TARGET_CJK,
    SEGMENT_TARGET_ENGLISH,
    split_by_llm,
)

REMOVED_VIEWING_FIELDS = ("max_word_count_cjk", "max_word_count_english")
REMOVED_REWRITE_FIELD = "optimize_both_sides"


# ---------------------------------------------------------------------------
# 数据类与函数签名：旧参数整体删除
# ---------------------------------------------------------------------------


def test_subtitle_config_drops_viewing_length_fields():
    names = {field.name for field in dataclasses.fields(SubtitleConfig)}
    assert not set(REMOVED_VIEWING_FIELDS) & names
    # 旧打印输出不再呈现观看限长。
    printed = SubtitleConfig(need_split=True).print_config()
    assert "Max Words" not in printed


def test_postprocess_config_drops_original_rewrite_switch():
    names = {field.name for field in dataclasses.fields(PostprocessConfig)}
    assert REMOVED_REWRITE_FIELD not in names
    config = PostprocessConfig()
    assert not hasattr(config, REMOVED_REWRITE_FIELD)


def test_splitter_and_llm_split_drop_viewing_limit_parameters():
    for signature in (
        inspect.signature(SubtitleSplitter.__init__),
        inspect.signature(split_by_llm),
    ):
        assert not set(REMOVED_VIEWING_FIELDS) & set(signature.parameters)


def test_optimize_speed_drops_both_sides_parameter():
    assert REMOVED_REWRITE_FIELD not in inspect.signature(optimize_speed).parameters


# ---------------------------------------------------------------------------
# CLI：旧参数入口与配置默认值不再被读取
# ---------------------------------------------------------------------------


def test_cli_parser_rejects_removed_viewing_flags():
    from videocaptioner.cli.main import build_parser

    parser = build_parser()
    for flag in ("--max-cjk", "--max-english"):
        with pytest.raises(SystemExit):
            parser.parse_args(["subtitle", "input.srt", flag, "5"])


def test_cli_defaults_and_overrides_drop_viewing_keys():
    from videocaptioner.cli.config import DEFAULTS
    from videocaptioner.cli.main import _build_cli_overrides

    assert not set(REMOVED_VIEWING_FIELDS) & set(DEFAULTS["subtitle"])
    overrides = _build_cli_overrides(Namespace())
    assert not set(REMOVED_VIEWING_FIELDS) & set(overrides.get("subtitle", {}))


# ---------------------------------------------------------------------------
# 旧持久化残留值：丢弃而非迁移，不改变新任务行为
# ---------------------------------------------------------------------------


def test_legacy_profile_archive_drops_original_rewrite_value(tmp_path):
    """旧档残留的原文改写开关直接丢弃：不迁移、不读入新行为（D05/D20）。"""
    path = tmp_path / "profiles.json"
    document = {
        "schema": PROFILE_SCHEMA,
        "version": PROFILE_SCHEMA_VERSION,
        "profiles": [
            {
                "id": "cinema",
                "name": "Cinema",
                "base_template_id": "balanced",
                "is_template": False,
                "config": {REMOVED_REWRITE_FIELD: True, "normalize_quotes": True},
            }
        ],
    }
    path.write_text(json.dumps(document), encoding="utf-8")

    config = PostprocessProfileStore(path).get("cinema").config
    assert config.normalize_quotes is True  # 其余持久化值原样保留
    assert not hasattr(config, REMOVED_REWRITE_FIELD)  # 开关值未被读取


# ---------------------------------------------------------------------------
# 上游语义分段仍可工作，不读取观看显示上限
# ---------------------------------------------------------------------------


def _word_segments(text: str):
    from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg

    return ASRData(
        [
            ASRDataSeg(char, index * 100, (index + 1) * 100)
            for index, char in enumerate(text)
        ]
    )


def test_rule_segmentation_keeps_working_without_viewing_limits():
    text = "今天我们一起来看一个关于字幕后处理的例子"
    splitter = SubtitleSplitter(thread_num=1, model="", use_llm=False)
    try:
        result = splitter.split_subtitle(_word_segments(text))
    finally:
        splitter.stop()
    assert not hasattr(splitter, "max_word_count_cjk")
    # 上游只做内容语义分段：逐字保留完整内容，绝无观看限长截断。
    assert "".join(seg.text for seg in result.segments).replace(" ", "") == text
    assert result.segments  # 仍产出有效分段


def test_llm_split_prompt_uses_fixed_segmentation_targets():
    from videocaptioner.core.llm.models import (
        LLMModelProfile,
        LLMResult,
        LLMTransport,
        ProviderDialect,
    )

    class CapturingGateway:
        def __init__(self):
            self.requests = []

        def complete(self, profile, request, *, cancelled=None):
            self.requests.append(request)
            return LLMResult(text="第一段<br>第二段")

    profile = LLMModelProfile(
        profile_id="removal-test",
        name="Removal test",
        transport=LLMTransport.OPENAI_COMPATIBLE,
        dialect=ProviderDialect.GENERIC,
        base_url="https://mock.local/v1",
        api_key="test-api-key",
        model="gpt-4o-mini",
    )
    gateway = CapturingGateway()
    split_by_llm("第一段第二段", profile=profile, gateway=gateway)

    system_prompt = gateway.requests[0].messages[0].content
    # 模板变量全部替换：提示词不再携带未替换的旧参数占位。
    assert "$" not in system_prompt
    assert "max_word_count" not in system_prompt
    # 内容语义分段目标为固定值（D11）：不来自任何观看限长配置。
    assert str(SEGMENT_TARGET_CJK) in system_prompt
    assert str(SEGMENT_TARGET_ENGLISH) in system_prompt


# ---------------------------------------------------------------------------
# 新配置稳定默认值即可创建有效任务
# ---------------------------------------------------------------------------


def test_default_postprocess_config_creates_valid_profile_config(tmp_path):
    """新后处理配置缺省值可直接解析为有效配置，不执行迁移。"""
    PostprocessConfig()  # 权威默认值自身有效
    config = PostprocessProfileStore(tmp_path / "profiles.json").resolve_config("balanced")
    assert config.speed_optimize is True
    assert config.speed_profile == "balanced"
    assert config.any_enabled()
