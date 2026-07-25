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
    build_legacy_llm_profile,
    build_translation_llm_profiles,
    load_config_file,
    load_env_overrides,
    save_config_value,
)


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
        assert _parse_value("gpt-4o", "llm.model") == "gpt-4o"

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

        save_config_value("llm.model", "gpt-4o", config_path=config_file)
        save_config_value("subtitle.thread_num", "8", config_path=config_file)
        save_config_value("subtitle.optimize", "false", config_path=config_file)

        loaded = load_config_file(config_file)
        assert loaded["llm"]["model"] == "gpt-4o"
        assert loaded["subtitle"]["thread_num"] == 8
        assert loaded["subtitle"]["optimize"] is False


class TestBuildConfig:
    def test_defaults_only(self):
        config = build_config(config_path=None)
        assert config["llm"]["model"] == DEFAULTS["llm"]["model"]

    def test_cli_overrides(self):
        config = build_config(cli_overrides={"llm": {"model": "custom"}})
        assert config["llm"]["model"] == "custom"

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("VIDEOCAPTIONER_LLM_MODEL", "env-model")
        config = build_config()
        assert config["llm"]["model"] == "env-model"

    def test_priority_cli_over_env(self, monkeypatch):
        monkeypatch.setenv("VIDEOCAPTIONER_LLM_MODEL", "env-model")
        config = build_config(cli_overrides={"llm": {"model": "cli-model"}})
        assert config["llm"]["model"] == "cli-model"

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
                "llm": {"api_key": ""},
                "subtitle": {
                    "optimize": False,
                    "translate": False,
                    "compress_fast_subtitles": True,
                },
            }
        )

        assert validate_subtitle(config) is False
        assert "LLM API key" in capsys.readouterr().err


class TestTranslationLLMProfiles:
    def test_new_role_table_does_not_trigger_legacy_non_llm_migration(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[translate.llm.main]\nmodel = "translation-model"\n',
            encoding="utf-8",
        )

        config = build_config(config_path=config_file)

        assert config["translate"]["mode"] == "enhanced_llm"

    def test_missing_new_sections_is_exact_legacy_fallback(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[llm]\napi_key = "legacy-key"\nmodel = "legacy-model"\n',
            encoding="utf-8",
        )
        config = build_config(config_path=config_file)

        legacy = build_legacy_llm_profile(config)
        main, review = build_translation_llm_profiles(config)

        assert main == legacy
        assert review == legacy
        assert main.profile_id == "cli-legacy"
        assert main is review

    def test_role_fields_inherit_by_presence_and_support_explicit_clears(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            """
[llm]
api_key = "legacy-key"
api_base = "https://legacy.example/v1"
model = "legacy-model"
work_context_tokens = 65536

[translate.llm.main]
api_key = ""
model = "main-model"
openai_endpoint = "responses"
max_output_tokens = 2048
request_options_json = '{"reasoning":{"effort":"high"}}'

[translate.llm.review]
model = "review-model"
max_output_tokens = "auto"
request_options_json = "{}"
""".strip(),
            encoding="utf-8",
        )

        main, review = build_translation_llm_profiles(
            build_config(config_path=config_file)
        )

        assert main.profile_id == "cli-main"
        assert main.api_key == ""
        assert main.model == "main-model"
        assert main.openai_endpoint.value == "responses"
        assert main.max_output_tokens == 2048
        assert main.request_options["reasoning"]["effort"] == "high"
        assert review.profile_id == "cli-review"
        assert review.api_key == ""
        assert review.base_url == "https://legacy.example/v1"
        assert review.model == "review-model"
        assert review.openai_endpoint.value == "responses"
        assert review.max_output_tokens is None
        assert dict(review.request_options) == {}

    def test_source_priority_keeps_role_layers_above_global_cli(
        self, tmp_path, monkeypatch
    ):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            """
[llm]
api_key = "file-global-key"
model = "file-global"

[translate.llm.main]
model = "file-main"
openai_endpoint = "chat_completions"

[translate.llm.review]
model = "file-review"
""".strip(),
            encoding="utf-8",
        )
        monkeypatch.setenv("VIDEOCAPTIONER_LLM_MODEL", "env-global")
        monkeypatch.setenv("VIDEOCAPTIONER_TRANSLATE_LLM_MAIN_MODEL", "env-main")
        monkeypatch.setenv("VIDEOCAPTIONER_TRANSLATE_LLM_REVIEW_MODEL", "env-review")
        config = build_config(
            cli_overrides={
                "llm": {"model": "cli-global"},
                "translate": {
                    "llm": {
                        "main": {
                            "openai_endpoint": "responses",
                            "max_output_tokens": "2048",
                        },
                        "review": {"max_output_tokens": "auto"},
                    }
                },
            },
            config_path=config_file,
        )

        main, review = build_translation_llm_profiles(config)

        assert config["llm"]["model"] == "cli-global"
        assert main.model == "env-main"
        assert main.openai_endpoint.value == "responses"
        assert main.max_output_tokens == 2048
        assert review.model == "env-review"
        assert review.openai_endpoint.value == "responses"
        assert review.max_output_tokens is None

    def test_lower_priority_role_aliases_are_normalized_before_env_and_cli_merge(
        self, tmp_path, monkeypatch
    ):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            """
[llm]
api_key = "key"

[translate.llm.main]
base_url = "https://file.example/v1"
endpoint = "chat_completions"

[translate.llm.review]
base_url = "https://review-file.example/v1"
endpoint = "chat_completions"
""".strip(),
            encoding="utf-8",
        )
        monkeypatch.setenv(
            "VIDEOCAPTIONER_TRANSLATE_LLM_MAIN_API_BASE",
            "https://env.example/v1",
        )
        monkeypatch.setenv(
            "VIDEOCAPTIONER_TRANSLATE_LLM_MAIN_OPENAI_ENDPOINT", "responses"
        )

        config = build_config(
            config_path=config_file,
            cli_overrides={
                "translate": {
                    "llm": {
                        "review": {
                            "api_base": "https://cli-review.example/v1",
                            "openai_endpoint": "responses",
                        }
                    }
                }
            },
        )
        main, review = build_translation_llm_profiles(config)

        assert main.base_url == "https://env.example/v1"
        assert main.openai_endpoint.value == "responses"
        assert review.base_url == "https://cli-review.example/v1"
        assert review.openai_endpoint.value == "responses"

    def test_conflicting_aliases_in_the_same_source_are_rejected(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            """
[translate.llm.main]
base_url = "https://alias.example/v1"
api_base = "https://canonical.example/v1"
""".strip(),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="config file.*conflicts"):
            build_config(config_path=config_file)

    def test_role_environment_supports_complete_connection_fields(
        self, tmp_path, monkeypatch
    ):
        config_file = tmp_path / "empty.toml"
        config_file.write_text("", encoding="utf-8")
        prefix = "VIDEOCAPTIONER_TRANSLATE_LLM_MAIN_"
        values = {
            "API_KEY": "",
            "API_BASE": "https://anthropic.example/v1",
            "MODEL": "claude-test",
            "TRANSPORT": "anthropic-messages",
            "DIALECT": "anthropic",
            "WORK_CONTEXT_TOKENS": "32768",
            "MAX_CONCURRENCY": "2",
            "OPENAI_ENDPOINT": "chat_completions",
            "MAX_OUTPUT_TOKENS": "4096",
            "REQUEST_OPTIONS_JSON": '{"thinking":{"type":"enabled","budget_tokens":1024}}',
        }
        for suffix, value in values.items():
            monkeypatch.setenv(prefix + suffix, value)

        main, _ = build_translation_llm_profiles(
            build_config(config_path=config_file)
        )

        assert main.api_key == ""
        assert main.base_url == "https://anthropic.example/v1"
        assert main.model == "claude-test"
        assert main.transport.value == "anthropic-messages"
        assert main.dialect.value == "anthropic"
        assert main.work_context_tokens == 32768
        assert main.max_concurrency == 2
        assert main.max_output_tokens == 4096
        assert main.request_options["thinking"]["budget_tokens"] == 1024

    @pytest.mark.parametrize("raw", ["", "[]", "null", "{not-json}"])
    def test_request_options_json_must_be_a_valid_object(self, raw):
        config = build_config(
            cli_overrides={
                "llm": {"api_key": "key"},
                "translate": {
                    "llm": {"main": {"request_options_json": raw}}
                },
            }
        )

        with pytest.raises(ValueError, match="request_options_json"):
            build_translation_llm_profiles(config)

    @pytest.mark.parametrize("raw", ["0", "-1", "one", True, 1.0, 1.5])
    def test_max_output_tokens_rejects_invalid_values(self, raw):
        config = build_config(
            cli_overrides={
                "llm": {"api_key": "key"},
                "translate": {"llm": {"main": {"max_output_tokens": raw}}},
            }
        )

        with pytest.raises(ValueError, match="max_output_tokens"):
            build_translation_llm_profiles(config)

    def test_protected_request_option_is_rejected_during_cli_preflight(self):
        config = build_config(
            cli_overrides={
                "llm": {"api_key": "key"},
                "translate": {
                    "llm": {
                        "main": {"request_options_json": '{"model":"other"}'}
                    }
                },
            }
        )

        with pytest.raises(ValueError, match="application-controlled"):
            build_translation_llm_profiles(config)

    def test_native_transport_rejects_responses_endpoint(self):
        config = build_config(
            cli_overrides={
                "translate": {
                    "llm": {
                        "main": {
                            "api_key": "",
                            "transport": "gemini",
                            "dialect": "gemini",
                            "openai_endpoint": "responses",
                        }
                    }
                }
            }
        )

        with pytest.raises(ValueError, match="chat_completions"):
            build_translation_llm_profiles(config)

    def test_explicit_empty_key_is_valid_for_keyless_translation_profile(self):
        from videocaptioner.cli.validators import validate_translation_llm

        config = build_config(
            cli_overrides={
                "translate": {"llm": {"main": {"api_key": ""}}},
            }
        )

        assert validate_translation_llm(config, "single_llm") is True

    @pytest.mark.parametrize("mode", ["single_llm", "enhanced_llm"])
    def test_explicit_empty_legacy_key_is_valid_without_role_sections(
        self, tmp_path, mode
    ):
        from videocaptioner.cli.validators import validate_translation_llm

        config_file = tmp_path / "config.toml"
        config_file.write_text(
            """
[llm]
api_key = ""
api_base = "http://localhost:11434/v1"
model = "local-model"
""".strip(),
            encoding="utf-8",
        )
        config = build_config(config_path=config_file)

        assert validate_translation_llm(config, mode) is True

    def test_single_mode_does_not_validate_unused_review_profile(self):
        from videocaptioner.cli.validators import validate_translation_llm

        config = build_config(
            cli_overrides={
                "llm": {"api_key": "key"},
                "translate": {
                    "llm": {
                        "review": {"request_options_json": '{"model":"forbidden"}'}
                    }
                },
            }
        )

        assert validate_translation_llm(config, "single_llm") is True
        assert validate_translation_llm(config, "enhanced_llm") is False
