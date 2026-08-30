"""Tests for CLI config system — TOML read/write, merging, type safety."""

import pytest

from videocaptioner.cli.config import (
    DEFAULTS,
    _deep_merge,
    _get_nested,
    _parse_value,
    _set_nested,
    _toml_value,
    build_config,
    load_config_file,
    load_env_overrides,
    save_config_value,
)
from videocaptioner.core.llm.models import LLMModelProfile, LLMTransport, ProviderDialect
from videocaptioner.core.llm.profiles import LLMModelProfileStore


@pytest.fixture
def profile_store(tmp_path, monkeypatch):
    """Seed a temporary model profile store and point the default path at it."""

    store_path = tmp_path / "llm_model_profiles.json"
    store = LLMModelProfileStore(store_path)
    store.save(
        LLMModelProfile(
            profile_id="main-profile",
            name="Main Profile",
            transport=LLMTransport.OPENAI_COMPATIBLE,
            dialect=ProviderDialect.GENERIC,
            base_url="https://main.test/v1",
            api_key="main-secret",
            model="main-model",
            work_context_tokens=16_384,
        )
    )
    store.save(
        LLMModelProfile(
            profile_id="review-profile",
            name="Review Profile",
            transport=LLMTransport.OPENAI_COMPATIBLE,
            dialect=ProviderDialect.GENERIC,
            base_url="https://review.test/v1",
            api_key="review-secret",
            model="review-model",
            work_context_tokens=16_384,
        )
    )
    store.save(
        LLMModelProfile(
            profile_id="utility-profile",
            name="Utility Profile",
            transport=LLMTransport.OPENAI_COMPATIBLE,
            dialect=ProviderDialect.GENERIC,
            base_url="https://utility.test/v1",
            api_key="utility-secret",
            model="utility-model",
            work_context_tokens=16_384,
        )
    )
    monkeypatch.setattr(
        "videocaptioner.core.llm.profiles.DEFAULT_LLM_PROFILES_PATH", store_path
    )
    return store


def test_default_dubbing_uses_keyless_edge_tts():
    assert DEFAULTS["dubbing"]["provider"] == "edge"
    assert DEFAULTS["dubbing"]["preset"] == "edge-cn-female"
    assert DEFAULTS["dubbing"]["api_key"] == ""
    assert DEFAULTS["dubbing"]["voice"] == "zh-CN-XiaoxiaoNeural"


class TestDeepMerge:
    def test_flat_override(self):
        assert _deep_merge({"a": 1}, {"a": 2}) == {"a": 2}

    def test_nested_merge(self):
        base = {"x": {"a": 1, "b": 2}}
        override = {"x": {"b": 3, "c": 4}}
        result = _deep_merge(base, override)
        assert result == {"x": {"a": 1, "b": 3, "c": 4}}

    def test_does_not_mutate_base(self):
        base = {"a": 1}
        _deep_merge(base, {"a": 2})
        assert base == {"a": 1}

    def test_empty_override(self):
        base = {"a": 1}
        assert _deep_merge(base, {}) == {"a": 1}


class TestNestedAccess:
    def test_get_nested(self):
        d = {"a": {"b": {"c": 42}}}
        assert _get_nested(d, "a.b.c") == 42

    def test_get_nested_missing(self):
        assert _get_nested({"a": 1}, "b", "default") == "default"

    def test_get_nested_deep_missing(self):
        assert _get_nested({"a": {"b": 1}}, "a.c.d", None) is None

    def test_set_nested(self):
        d: dict = {}
        _set_nested(d, "a.b.c", 42)
        assert d == {"a": {"b": {"c": 42}}}

    def test_set_nested_overwrite(self):
        d = {"a": {"b": 1}}
        _set_nested(d, "a.b", 2)
        assert d == {"a": {"b": 2}}


class TestParseValue:
    def test_bool_true(self):
        assert _parse_value("true", "subtitle.optimize") is True
        assert _parse_value("yes", "subtitle.optimize") is True
        assert _parse_value("1", "subtitle.optimize") is True

    def test_bool_false(self):
        assert _parse_value("false", "subtitle.optimize") is False
        assert _parse_value("no", "subtitle.optimize") is False
        assert _parse_value("0", "subtitle.optimize") is False

    def test_bool_invalid(self):
        with pytest.raises(ValueError, match="Expected boolean"):
            _parse_value("maybe", "subtitle.optimize")

    def test_int(self):
        assert _parse_value("8", "subtitle.thread_num") == 8
        assert isinstance(_parse_value("8", "subtitle.thread_num"), int)

    def test_int_invalid(self):
        with pytest.raises(ValueError, match="Expected integer"):
            _parse_value("abc", "subtitle.thread_num")

    def test_string(self):
        assert _parse_value("main-profile", "llm.profile_id") == "main-profile"

    def test_unknown_key_stays_string(self):
        # Key not in DEFAULTS → stays string
        assert _parse_value("anything", "unknown.key") == "anything"


class TestTomlValue:
    def test_bool(self):
        assert _toml_value(True) == "true"
        assert _toml_value(False) == "false"

    def test_int(self):
        assert _toml_value(42) == "42"

    def test_float(self):
        assert _toml_value(0.5) == "0.5"

    def test_string(self):
        assert _toml_value("hello") == '"hello"'

    def test_string_with_quotes(self):
        assert _toml_value('say "hi"') == '"say \\"hi\\""'

    def test_string_with_newline(self):
        assert _toml_value("line1\nline2") == '"line1\\nline2"'


class TestConfigRoundtrip:
    def test_save_and_load(self, tmp_path):
        config_file = tmp_path / "config.toml"

        save_config_value("llm.profile_id", "main-profile", config_path=config_file)
        save_config_value("subtitle.thread_num", "8", config_path=config_file)
        save_config_value("subtitle.optimize", "false", config_path=config_file)

        loaded = load_config_file(config_file)
        assert loaded["llm"]["profile_id"] == "main-profile"
        assert loaded["subtitle"]["thread_num"] == 8
        assert loaded["subtitle"]["optimize"] is False


class TestBuildConfig:
    def test_defaults_only(self):
        config = build_config(config_path=None)
        assert config["llm"]["profile_id"] == DEFAULTS["llm"]["profile_id"]
        assert config["llm"]["review_profile_id"] == ""
        assert config["llm"]["utility_profile_id"] == ""

    def test_llm_section_is_profile_ids_only(self):
        assert set(DEFAULTS["llm"]) == {"profile_id", "review_profile_id", "utility_profile_id"}

    def test_cli_overrides(self):
        config = build_config(cli_overrides={"llm": {"profile_id": "custom"}})
        assert config["llm"]["profile_id"] == "custom"

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("VIDEOCAPTIONER_LLM_PROFILE_ID", "env-profile")
        config = build_config()
        assert config["llm"]["profile_id"] == "env-profile"

    def test_priority_cli_over_env(self, monkeypatch):
        monkeypatch.setenv("VIDEOCAPTIONER_LLM_PROFILE_ID", "env-profile")
        config = build_config(cli_overrides={"llm": {"profile_id": "cli-profile"}})
        assert config["llm"]["profile_id"] == "cli-profile"

    def test_openai_standard_env_names_are_not_recognized(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "host-shell-key")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://host.example/v1")
        monkeypatch.setenv("OPENAI_MODEL", "host-model")

        config = build_config()

        assert config["llm"] == {
            "profile_id": "",
            "review_profile_id": "",
            "utility_profile_id": "",
        }
        assert "llm" not in load_env_overrides()

    def test_env_api_key_is_not_a_config_key(self, monkeypatch):
        # VIDEOCAPTIONER_LLM_API_KEY only swaps a resolved profile's credential;
        # it must never appear in the merged config tree.
        monkeypatch.setenv("VIDEOCAPTIONER_LLM_API_KEY", "ci-key")

        config = build_config()

        assert "api_key" not in config["llm"]

    def test_env_values_are_typed(self, monkeypatch):
        monkeypatch.setenv("VIDEOCAPTIONER_TTS_MAX_SPEED", "2.0")
        monkeypatch.setenv("VIDEOCAPTIONER_TTS_WORKERS", "3")
        monkeypatch.setenv("VIDEOCAPTIONER_TTS_REWRITE_TOO_LONG", "true")
        monkeypatch.setenv("VIDEOCAPTIONER_TTS_MIX_ORIGINAL_AUDIO", "false")
        monkeypatch.setenv("VIDEOCAPTIONER_MIMO_ASR_TIMEOUT", "120")
        monkeypatch.setenv("VIDEOCAPTIONER_MIMO_ASR_CONCURRENCY", "4")
        monkeypatch.setenv("VIDEOCAPTIONER_QWEN_MAX_NEW_TOKENS", "4096")
        monkeypatch.setenv("VIDEOCAPTIONER_QWEN_COMPILE_ALIGNER", "true")

        overrides = load_env_overrides()

        assert overrides["dubbing"]["max_speed"] == 2.0
        assert overrides["dubbing"]["tts_workers"] == 3
        assert overrides["dubbing"]["rewrite_too_long"] is True
        assert overrides["dubbing"]["mix_original_audio"] is False
        assert overrides["transcribe"]["mimo_asr"]["timeout"] == 120
        assert overrides["transcribe"]["mimo_asr"]["concurrency"] == 4
        assert overrides["transcribe"]["qwen"]["max_new_tokens"] == 4096
        assert overrides["transcribe"]["qwen"]["compile_aligner"] is True

    def test_compress_fast_requires_llm_validation(self, capsys):
        from videocaptioner.cli.validators import validate_subtitle

        config = build_config(
            cli_overrides={
                "subtitle": {
                    "optimize": False,
                    "translate": False,
                    "compress_fast_subtitles": True,
                },
            }
        )

        assert validate_subtitle(config) is False
        assert "llm.profile_id" in capsys.readouterr().err


class TestLLMProfileResolution:
    """Profile-id resolution for the three [llm] keys against the profile store."""

    def test_main_profile_resolves_from_store(self, profile_store):
        from videocaptioner.cli.config import resolve_main_llm_profile

        config = build_config(cli_overrides={"llm": {"profile_id": "main-profile"}})

        profile = resolve_main_llm_profile(config)

        assert profile.profile_id == "main-profile"
        assert profile.model == "main-model"
        assert profile.base_url == "https://main.test/v1"

    def test_review_profile_resolves_from_store(self, profile_store):
        from videocaptioner.cli.config import resolve_review_llm_profile

        config = build_config(
            cli_overrides={"llm": {"review_profile_id": "review-profile"}}
        )

        profile = resolve_review_llm_profile(config)

        assert profile.profile_id == "review-profile"
        assert profile.model == "review-model"

    def test_utility_profile_prefers_independent_binding(self, profile_store):
        from videocaptioner.cli.config import resolve_cli_utility_profile

        config = build_config(
            cli_overrides={
                "llm": {
                    "profile_id": "main-profile",
                    "utility_profile_id": "utility-profile",
                }
            }
        )

        profile = resolve_cli_utility_profile(config)

        assert profile.profile_id == "utility-profile"

    def test_utility_profile_derives_from_main_when_unbound(self, profile_store):
        from videocaptioner.cli.config import resolve_cli_utility_profile

        config = build_config(cli_overrides={"llm": {"profile_id": "main-profile"}})

        profile = resolve_cli_utility_profile(config)

        assert profile.profile_id == "main-profile"

    def test_utility_derivation_strips_translation_tuning_fields(self, profile_store):
        from videocaptioner.cli.config import resolve_cli_utility_profile
        from videocaptioner.core.llm.models import OpenAIEndpoint

        profile_store.save(
            LLMModelProfile(
                profile_id="tuned-main",
                name="Tuned Main",
                transport=LLMTransport.OPENAI_COMPATIBLE,
                dialect=ProviderDialect.GENERIC,
                base_url="https://tuned.test/v1",
                api_key="tuned-secret",
                model="tuned-model",
                work_context_tokens=16_384,
                openai_endpoint=OpenAIEndpoint.RESPONSES,
                request_options={"reasoning": {"effort": "high"}},
                max_output_tokens=2048,
            )
        )
        config = build_config(cli_overrides={"llm": {"profile_id": "tuned-main"}})

        profile = resolve_cli_utility_profile(config)

        assert profile.openai_endpoint is OpenAIEndpoint.CHAT_COMPLETIONS
        assert dict(profile.request_options) == {}
        assert profile.max_output_tokens is None

    def test_missing_main_profile_fails_fast_with_store_guidance(self, profile_store):
        from videocaptioner.cli.config import resolve_main_llm_profile

        with pytest.raises(ValueError) as excinfo:
            resolve_main_llm_profile(build_config())

        message = str(excinfo.value)
        assert "llm.profile_id is not set" in message
        assert "llm_model_profiles.json" in message
        assert "main-profile" in message  # available ids are listed
        assert "翻译设置页" not in message  # no GUI card wording on the CLI surface

    def test_missing_review_profile_fails_fast_in_enhanced_mode(self, profile_store):
        from videocaptioner.cli.config import resolve_review_llm_profile

        config = build_config(cli_overrides={"llm": {"profile_id": "main-profile"}})

        with pytest.raises(ValueError, match="llm.review_profile_id is not set"):
            resolve_review_llm_profile(config)

    def test_unknown_profile_id_lists_available_ids(self, profile_store):
        from videocaptioner.cli.config import resolve_main_llm_profile

        config = build_config(cli_overrides={"llm": {"profile_id": "ghost"}})

        with pytest.raises(ValueError) as excinfo:
            resolve_main_llm_profile(config)

        message = str(excinfo.value)
        assert "'ghost' does not exist" in message
        assert "main-profile" in message
        assert "review-profile" in message

    def test_empty_utility_binding_is_valid_derivation(self, profile_store):
        # An explicit empty utility id means "derive from main", not an error.
        from videocaptioner.cli.config import resolve_cli_utility_profile

        config = build_config(
            cli_overrides={
                "llm": {"profile_id": "main-profile", "utility_profile_id": ""}
            }
        )

        assert resolve_cli_utility_profile(config).profile_id == "main-profile"

    def test_env_api_key_overrides_only_the_credential(self, profile_store, monkeypatch):
        from videocaptioner.cli.config import resolve_main_llm_profile

        monkeypatch.setenv("VIDEOCAPTIONER_LLM_API_KEY", "ci-injected-key")
        config = build_config(cli_overrides={"llm": {"profile_id": "main-profile"}})

        profile = resolve_main_llm_profile(config)

        assert profile.api_key == "ci-injected-key"
        assert profile.base_url == "https://main.test/v1"
        assert profile.model == "main-model"
        assert profile.profile_id == "main-profile"

    def test_blank_env_api_key_keeps_the_stored_credential(self, profile_store, monkeypatch):
        from videocaptioner.cli.config import resolve_main_llm_profile

        monkeypatch.setenv("VIDEOCAPTIONER_LLM_API_KEY", "   ")
        config = build_config(cli_overrides={"llm": {"profile_id": "main-profile"}})

        assert resolve_main_llm_profile(config).api_key == "main-secret"

    def test_env_api_key_overrides_the_utility_role_credential(self, profile_store, monkeypatch):
        # The utility role passes through the same env credential override as
        # the main and review roles (spec: only the credential is swapped).
        from videocaptioner.cli.config import resolve_cli_utility_profile

        monkeypatch.setenv("VIDEOCAPTIONER_LLM_API_KEY", "ci-injected-key")
        config = build_config(
            cli_overrides={
                "llm": {"profile_id": "main-profile", "utility_profile_id": "utility-profile"}
            }
        )

        profile = resolve_cli_utility_profile(config)

        assert profile.api_key == "ci-injected-key"
        assert profile.base_url == "https://utility.test/v1"
        assert profile.model == "utility-model"
        assert profile.profile_id == "utility-profile"

    def test_blank_env_api_key_keeps_the_utility_stored_credential(
        self, profile_store, monkeypatch
    ):
        from videocaptioner.cli.config import resolve_cli_utility_profile

        monkeypatch.setenv("VIDEOCAPTIONER_LLM_API_KEY", "  ")
        config = build_config(cli_overrides={"llm": {"profile_id": "main-profile"}})

        assert resolve_cli_utility_profile(config).api_key == "main-secret"

    def test_env_override_marks_the_gateway_request_log(self, profile_store, monkeypatch):
        # Story 18: with the key injected, request logs record where it came
        # from (key_source=env_override); without it the field stays absent.
        from videocaptioner.cli.config import resolve_main_llm_profile
        from videocaptioner.core.llm import request_logger

        monkeypatch.setenv("VIDEOCAPTIONER_LLM_API_KEY", "ci-injected-key")
        config = build_config(cli_overrides={"llm": {"profile_id": "main-profile"}})

        try:
            assert resolve_main_llm_profile(config).api_key == "ci-injected-key"
            assert request_logger.is_env_api_key_override_active() is True
        finally:
            request_logger.set_env_api_key_override(False)

        monkeypatch.delenv("VIDEOCAPTIONER_LLM_API_KEY")
        assert resolve_main_llm_profile(config).api_key == "main-secret"
        assert request_logger.is_env_api_key_override_active() is False

    def test_empty_api_key_in_store_is_not_a_validation_error(self, profile_store):
        # Keyless local services store an empty api_key; existence is what matters.
        from videocaptioner.cli.config import resolve_main_llm_profile
        from videocaptioner.cli.validators import validate_translation_llm

        profile_store.save(
            LLMModelProfile(
                profile_id="local",
                name="Local",
                transport=LLMTransport.OPENAI_COMPATIBLE,
                dialect=ProviderDialect.GENERIC,
                base_url="http://localhost:11434/v1",
                api_key="",
                model="local-model",
                work_context_tokens=16_384,
            )
        )
        config = build_config(cli_overrides={"llm": {"profile_id": "local"}})

        assert resolve_main_llm_profile(config).api_key == ""
        assert validate_translation_llm(config, "single_llm") is True


class TestValidateLLM:
    def test_validates_through_the_profile_store(self, profile_store, capsys):
        from videocaptioner.cli.validators import validate_llm

        config = build_config(cli_overrides={"llm": {"profile_id": "main-profile"}})

        assert validate_llm(config) is True
        assert capsys.readouterr().err == ""

    def test_missing_profile_fails_with_guidance(self, profile_store, capsys):
        from videocaptioner.cli.validators import validate_llm

        assert validate_llm(build_config()) is False
        err = capsys.readouterr().err
        assert "llm.profile_id" in err
        assert "llm_model_profiles.json" in err

    def test_unknown_profile_fails_listing_available_ids(self, profile_store, capsys):
        from videocaptioner.cli.validators import validate_llm

        config = build_config(cli_overrides={"llm": {"profile_id": "ghost"}})

        assert validate_llm(config) is False
        assert "Available profile ids: main-profile" in capsys.readouterr().err

    def test_utility_binding_is_not_required(self, profile_store):
        # An empty utility id means "derive from main", which is valid.
        from videocaptioner.cli.validators import validate_llm

        config = build_config(cli_overrides={"llm": {"profile_id": "main-profile"}})

        assert validate_llm(config) is True


class TestValidateTranslationLLM:
    def test_single_mode_requires_only_main(self, profile_store):
        from videocaptioner.cli.validators import validate_translation_llm

        config = build_config(cli_overrides={"llm": {"profile_id": "main-profile"}})

        assert validate_translation_llm(config, "single_llm") is True

    def test_enhanced_mode_requires_a_review_profile(self, profile_store, capsys):
        from videocaptioner.cli.validators import validate_translation_llm

        config = build_config(cli_overrides={"llm": {"profile_id": "main-profile"}})

        assert validate_translation_llm(config, "enhanced_llm") is False
        err = capsys.readouterr().err
        assert "llm.review_profile_id is not set" in err
        assert "single_llm" in err  # points at the lighter alternative

    def test_enhanced_mode_accepts_a_set_review_profile(self, profile_store):
        from videocaptioner.cli.validators import validate_translation_llm

        config = build_config(
            cli_overrides={
                "llm": {
                    "profile_id": "main-profile",
                    "review_profile_id": "review-profile",
                }
            }
        )

        assert validate_translation_llm(config, "enhanced_llm") is True

    def test_enhanced_mode_rejects_an_unknown_review_profile(self, profile_store):
        from videocaptioner.cli.validators import validate_translation_llm

        config = build_config(
            cli_overrides={
                "llm": {
                    "profile_id": "main-profile",
                    "review_profile_id": "ghost",
                }
            }
        )

        assert validate_translation_llm(config, "enhanced_llm") is False

    def test_missing_main_profile_fails_for_both_modes(self, profile_store):
        from videocaptioner.cli.validators import validate_translation_llm

        for mode in ("single_llm", "enhanced_llm"):
            assert validate_translation_llm(build_config(), mode) is False


class TestLegacyLLMKeyWarning:
    """Leftover pre-profile keys warn once on stderr and are not migrated."""

    def test_file_layer_legacy_keys_warn(self, tmp_path, capsys):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[llm]\napi_key = "legacy-key"\nmodel = "gpt-4o"\n',
            encoding="utf-8",
        )

        config = build_config(config_path=config_file)

        # Dead data is tolerated: the value survives, nothing is migrated.
        assert config["llm"]["api_key"] == "legacy-key"
        err = capsys.readouterr().err
        assert "obsolete LLM config keys" in err
        assert "llm.api_key" in err
        assert "config file" in err

    def test_inline_role_tables_warn(self, tmp_path, capsys):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[translate.llm.main]\nmodel = "translation-model"\n',
            encoding="utf-8",
        )

        config = build_config(config_path=config_file)

        assert config["translate"]["llm"]["main"]["model"] == "translation-model"
        err = capsys.readouterr().err
        assert "translate.llm.main" in err

    def test_legacy_env_names_warn(self, monkeypatch, capsys):
        monkeypatch.setenv("OPENAI_API_KEY", "host-shell-key")
        monkeypatch.setenv("VIDEOCAPTIONER_LLM_MAIN_MODEL", "env-main")
        monkeypatch.setenv("VIDEOCAPTIONER_TRANSLATE_LLM_REVIEW_MODEL", "env-review")

        build_config()

        err = capsys.readouterr().err
        assert "environment" in err
        assert "OPENAI_API_KEY" in err
        assert "VIDEOCAPTIONER_LLM_MAIN_MODEL" in err
        assert "VIDEOCAPTIONER_TRANSLATE_LLM_REVIEW_MODEL" in err

    def test_warning_lists_available_profile_ids(self, tmp_path, monkeypatch, capsys):
        store_path = tmp_path / "llm_model_profiles.json"
        LLMModelProfileStore(store_path).save(
            LLMModelProfile(
                profile_id="main-profile",
                name="Main Profile",
                transport=LLMTransport.OPENAI_COMPATIBLE,
                dialect=ProviderDialect.GENERIC,
                base_url="https://main.test/v1",
                api_key="main-secret",
                model="main-model",
                work_context_tokens=16_384,
            )
        )
        monkeypatch.setattr(
            "videocaptioner.core.llm.profiles.DEFAULT_LLM_PROFILES_PATH", store_path
        )
        config_file = tmp_path / "config.toml"
        config_file.write_text('[llm]\nmodel = "gpt-4o"\n', encoding="utf-8")

        build_config(config_path=config_file)

        assert "Available profile ids: main-profile" in capsys.readouterr().err

    def test_clean_config_does_not_warn(self, tmp_path, capsys, monkeypatch):
        for name in (
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_MODEL",
            "VIDEOCAPTIONER_LLM_API_KEY",
        ):
            monkeypatch.delenv(name, raising=False)
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[llm]\nprofile_id = "main-profile"\n', encoding="utf-8"
        )

        build_config(config_path=config_file)

        assert capsys.readouterr().err == ""

    def test_warning_is_emitted_once_per_build(self, tmp_path, capsys):
        config_file = tmp_path / "config.toml"
        config_file.write_text('[llm]\napi_key = "legacy-key"\n', encoding="utf-8")

        build_config(config_path=config_file)

        assert capsys.readouterr().err.count("obsolete LLM config keys") == 1
