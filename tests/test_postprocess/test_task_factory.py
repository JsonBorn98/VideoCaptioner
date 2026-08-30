import pytest

from videocaptioner.core.entities import SubtitleExportPolicy, SubtitleLayoutEnum
from videocaptioner.core.llm.models import LLMModelProfile, LLMTransport, ProviderDialect
from videocaptioner.core.llm.profiles import LLMModelProfileStore
from videocaptioner.core.postprocess import PostprocessConfig, PostprocessLayoutMode
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.task_factory import TaskFactory


@pytest.fixture(autouse=True)
def _seed_utility_profiles(tmp_path, monkeypatch):
    """给测试环境种子一个主翻译方案，供默认 balanced 方案的语义修复派生。"""

    store_path = tmp_path / "profiles.json"
    LLMModelProfileStore(store_path).save(
        LLMModelProfile(
            profile_id="main-profile",
            name="Main Profile",
            transport=LLMTransport.OPENAI_COMPATIBLE,
            dialect=ProviderDialect.GENERIC,
            base_url="https://main.test/v1",
            api_key="secret",
            model="main-model",
            work_context_tokens=16_384,
        )
    )
    monkeypatch.setattr(
        "videocaptioner.core.llm.profiles.DEFAULT_LLM_PROFILES_PATH", store_path
    )
    original = cfg.get(cfg.main_llm_profile_id)
    cfg.set(cfg.main_llm_profile_id, "main-profile")
    yield
    cfg.set(cfg.main_llm_profile_id, original)


def test_workflow_postprocess_task_trusts_upstream_layout(tmp_path):
    subtitle = tmp_path / "【初版字幕】sample.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n译文\nsource\n", encoding="utf-8")
    original = cfg.get(cfg.subtitle_layout)
    try:
        cfg.set(cfg.subtitle_layout, SubtitleLayoutEnum.TRANSLATE_ON_TOP)
        task = TaskFactory.create_postprocess_task(str(subtitle), need_next_task=True)
        assert task.layout_mode is PostprocessLayoutMode.TRANSLATE_ON_TOP
    finally:
        cfg.set(cfg.subtitle_layout, original)


def test_independent_postprocess_task_uses_auto_structure_detection(tmp_path):
    subtitle = tmp_path / "external.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")

    task = TaskFactory.create_postprocess_task(str(subtitle), need_next_task=False)

    assert task.layout_mode is PostprocessLayoutMode.AUTO


def test_workflow_postprocess_task_carries_frozen_export_contract(tmp_path):
    subtitle = tmp_path / "【初版字幕】sample.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n译文\nsource\n", encoding="utf-8"
    )
    policy = SubtitleExportPolicy(enabled=True, format="ass")

    task = TaskFactory.create_postprocess_task(
        str(subtitle),
        need_next_task=True,
        workflow_base_name="episode-01",
        export_policy=policy,
    )

    assert task.workflow_base_name == "episode-01"
    assert task.export_policy is policy


def test_workflow_postprocess_task_derives_utility_profile_from_main_binding(tmp_path):
    """默认 balanced 方案会发起语义修复：工具方案从主翻译方案派生并塞进配置。"""
    task = TaskFactory.create_postprocess_task(
        str(tmp_path / "initial.srt"), need_next_task=True
    )

    profile = task.config_snapshot.utility_llm_profile
    assert profile is not None
    # 派生方案剥离翻译专属调优字段（工具角色请求形态由解析器统一保证）。
    assert profile.profile_id == "main-profile"
    assert profile.max_output_tokens is None
    assert profile.request_options == {}


def test_workflow_postprocess_task_prefers_independent_utility_binding(tmp_path):
    """工具模型卡绑定独立方案时，独立绑定优先于主翻译派生。"""
    store = LLMModelProfileStore(tmp_path / "profiles.json")
    store.save(
        LLMModelProfile(
            profile_id="utility-profile",
            name="Utility Profile",
            transport=LLMTransport.OPENAI_COMPATIBLE,
            dialect=ProviderDialect.GENERIC,
            base_url="https://utility.test/v1",
            api_key="secret",
            model="utility-model",
            work_context_tokens=16_384,
        )
    )
    original = cfg.get(cfg.utility_llm_profile_id)
    cfg.set(cfg.utility_llm_profile_id, "utility-profile")
    try:
        task = TaskFactory.create_postprocess_task(
            str(tmp_path / "initial.srt"), need_next_task=True
        )
    finally:
        cfg.set(cfg.utility_llm_profile_id, original)

    assert task.config_snapshot.utility_llm_profile.profile_id == "utility-profile"


def test_disabled_postprocess_task_needs_no_utility_profile(tmp_path):
    """未启用的后处理任务不发起工具 LLM 请求，无需解析方案。"""
    task = TaskFactory.create_postprocess_task(
        str(tmp_path / "initial.srt"), enabled=False
    )

    assert task.config_snapshot.utility_llm_profile is None


def test_snapshot_profile_is_preserved_not_re_resolved(tmp_path):
    """调用方已注入的工具方案直通，不被重新解析覆盖。"""
    profile = LLMModelProfile(
        profile_id="injected-profile",
        name="Injected Profile",
        transport=LLMTransport.OPENAI_COMPATIBLE,
        dialect=ProviderDialect.GENERIC,
        base_url="https://injected.test/v1",
        api_key="secret",
        model="injected-model",
        work_context_tokens=16_384,
    )

    task = TaskFactory.create_postprocess_task(
        str(tmp_path / "initial.srt"),
        config_snapshot=PostprocessConfig(utility_llm_profile=profile),
    )

    assert task.config_snapshot.utility_llm_profile is profile
