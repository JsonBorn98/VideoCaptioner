import json

import pytest

from videocaptioner.core.speed.profiles import (
    PROFILE_SCHEMA,
    PROFILE_SCHEMA_VERSION,
    BuiltinProfileError,
    ProfileConflictError,
    ProfileValidationError,
    SpeedProfileStore,
)


def test_copy_builtin_edit_reset_rename_delete_and_reload(tmp_path):
    path = tmp_path / "profiles.json"
    store = SpeedProfileStore(path)
    profile = store.copy_builtin("balanced", "My profile", profile_id="my-profile")

    assert profile.policy.hard_cps_cjk == 11
    changed = store.set_field("my-profile", "hard_cps_cjk", 13)
    assert changed.policy.hard_cps_cjk == 13
    assert changed.overrides == {"hard_cps_cjk": 13.0}

    reset = store.reset_field("my-profile", "hard_cps_cjk")
    assert reset.policy.hard_cps_cjk == 11
    assert reset.overrides == {}
    assert store.rename("my-profile", "Renamed").name == "Renamed"
    assert SpeedProfileStore(path).get_custom("my-profile").name == "Renamed"

    store.delete("my-profile")
    assert SpeedProfileStore(path).list_custom() == ()


def test_builtin_profiles_are_resolved_but_cannot_be_mutated(tmp_path):
    store = SpeedProfileStore(tmp_path / "profiles.json")

    assert store.resolve_policy("smooth").hard_cps_cjk == 10
    with pytest.raises(BuiltinProfileError, match="read-only"):
        store.set_field("balanced", "hard_cps_cjk", 12)
    with pytest.raises(BuiltinProfileError, match="read-only"):
        store.rename("balanced", "Changed")
    with pytest.raises(BuiltinProfileError, match="read-only"):
        store.delete("balanced")


def test_names_and_ids_are_unique(tmp_path):
    store = SpeedProfileStore(tmp_path / "profiles.json")
    store.copy_builtin("loose", "Existing", profile_id="existing")

    with pytest.raises(ProfileConflictError, match="id"):
        store.copy_builtin("balanced", "Different", profile_id="existing")
    with pytest.raises(ProfileConflictError, match="name"):
        store.copy_builtin("balanced", "EXISTING", profile_id="another")


def test_export_import_round_trip_is_versioned(tmp_path):
    source = SpeedProfileStore(tmp_path / "source.json")
    source.copy_builtin("smooth", "Cinema", profile_id="cinema")
    source.set_field("cinema", "comfort_cps_cjk", 7.5)
    exported = tmp_path / "cinema.json"
    source.export_profile("cinema", exported)

    document = json.loads(exported.read_text(encoding="utf-8"))
    assert document["schema"] == PROFILE_SCHEMA
    assert document["version"] == PROFILE_SCHEMA_VERSION

    target = SpeedProfileStore(tmp_path / "target.json")
    imported = target.import_profile(exported)
    assert imported.base_preset.value == "smooth"
    assert imported.policy.comfort_cps_cjk == 7.5


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda data: data.update(extra=True), "Unknown document"),
        (lambda data: data.update(version=99), "Unsupported profile version"),
        (lambda data: data["profile"].update(extra=True), "Unknown profile"),
        (
            lambda data: data["profile"]["overrides"].update(unknown_setting=3),
            "Unknown policy field",
        ),
        (
            lambda data: data["profile"]["overrides"].update(hard_cps_cjk=1000),
            "at most",
        ),
    ],
)
def test_import_rejects_unknown_fields_versions_and_out_of_range_values(
    tmp_path, mutation, message
):
    source = SpeedProfileStore(tmp_path / "source.json")
    source.copy_builtin("balanced", "Valid", profile_id="valid")
    exported = tmp_path / "profile.json"
    source.export_profile("valid", exported)
    data = json.loads(exported.read_text(encoding="utf-8"))
    mutation(data)
    exported.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ProfileValidationError, match=message):
        SpeedProfileStore(tmp_path / "target.json").import_profile(exported)


def test_invalid_types_and_cross_field_constraints_are_rejected(tmp_path):
    store = SpeedProfileStore(tmp_path / "profiles.json")
    store.copy_builtin("balanced", "Custom", profile_id="custom")

    with pytest.raises(ProfileValidationError, match="boolean"):
        store.set_field("custom", "bidirectional_smoothing", 1)
    with pytest.raises(ProfileValidationError, match="integer"):
        store.set_field("custom", "local_window_radius", 4.0)
    with pytest.raises(ProfileValidationError, match="comfort CPS"):
        store.set_field("custom", "hard_cps_cjk", 8)
    with pytest.raises(ProfileValidationError, match="emergency"):
        store.set_field("custom", "adjacent_p90_target", 4)


def test_failed_atomic_replace_preserves_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "profiles.json"
    store = SpeedProfileStore(path)
    store.copy_builtin("balanced", "Stable", profile_id="stable")
    previous = path.read_bytes()

    def fail_replace(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr("videocaptioner.core.speed.profiles.os.replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        store.rename("stable", "Uncommitted")

    assert path.read_bytes() == previous
    assert store.get_custom("stable").name == "Stable"
    assert not list(tmp_path.glob("*.tmp"))
