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
