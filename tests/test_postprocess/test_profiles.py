import json

import pytest

from videocaptioner.core.postprocess.profiles import (
    FACTORY_BASELINES,
    FactoryTemplateError,
    PostprocessProfileStore,
)


def test_templates_have_editable_working_values_and_immutable_factory_baseline(tmp_path):
    path = tmp_path / "postprocess-profiles.json"
    store = PostprocessProfileStore(path)

    assert store.get("balanced").config.speed_optimize is True
    changed = store.set_field("balanced", "normalize_quotes", True)
    assert changed.config.normalize_quotes is True
    assert PostprocessProfileStore(path).get("balanced").config.normalize_quotes is True
    assert FACTORY_BASELINES["balanced"]["normalize_quotes"] is False

    with pytest.raises(TypeError):
        FACTORY_BASELINES["balanced"]["speed_overrides"]["hard_cps_cjk"] = 13
    changed.config.normalize_quotes = False
    assert store.get("balanced").config.normalize_quotes is True


def test_custom_copies_template_working_values_but_resets_to_factory_baseline(tmp_path):
    store = PostprocessProfileStore(tmp_path / "profiles.json")
    store.set_field("smooth", "normalize_quotes", True)
    custom = store.copy_template("smooth", "Cinema", profile_id="cinema")

    assert custom.base_template_id == "smooth"
    assert custom.config.normalize_quotes is True
    store.set_field("smooth", "normalize_quotes", False)
    store.set_field("cinema", "fix_gaps", True)
    store.set_field("cinema", "save_timing_sidecar", True)

    reset = store.reset_profile("cinema")
    assert reset.config.normalize_quotes is False
    assert reset.config.fix_gaps is False
    assert reset.config.save_timing_sidecar is False
    assert reset.config.speed_profile == "smooth"


def test_custom_profiles_cannot_be_created_without_one_of_three_templates(tmp_path):
    store = PostprocessProfileStore(tmp_path / "profiles.json")

    with pytest.raises(FactoryTemplateError, match="originate"):
        store.copy_template("unknown", "Invalid")
    with pytest.raises(FactoryTemplateError, match="cannot be deleted"):
        store.delete("balanced")


def test_reload_picks_up_edits_from_another_store_instance(tmp_path):
    """长期持有的 store 需 reload 才能看到另一实例（如设置页）保存的改动。"""
    path = tmp_path / "profiles.json"
    page_store = PostprocessProfileStore(path)
    settings_store = PostprocessProfileStore(path)

    settings_store.set_field("balanced", "tail_compensation", True)
    settings_store.set_field("balanced", "max_gap_ms", 501)

    # 未 reload 前，page_store 仍是构造时的内存快照
    assert page_store.get("balanced").config.tail_compensation is False
    page_store.reload()
    assert page_store.get("balanced").config.tail_compensation is True
    assert page_store.resolve_config("balanced").max_gap_ms == 501


def test_archived_llm_model_key_is_dropped_not_rejected(tmp_path):
    """票 12 退役 llm_model 后，老存档带着该键仍可加载（向前兼容契约）。

    旧档的 llm_model 无对应新字段：直接丢弃、方案取默认 None——模型与
    连接由 utility_llm_profile 运行期注入，不来自持久化存档。
    """
    path = tmp_path / "profiles.json"
    store = PostprocessProfileStore(path)
    store.copy_template("balanced", "Legacy", profile_id="legacy")

    document = json.loads(path.read_text(encoding="utf-8"))
    for profile in document["profiles"]:
        if profile["id"] == "legacy":
            profile["config"]["llm_model"] = "gpt-4o-mini"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    loaded = PostprocessProfileStore(path).get("legacy")

    assert loaded.config.utility_llm_profile is None
    # 其余持久化字段不受丢弃影响
    assert loaded.config.speed_profile == "balanced"
