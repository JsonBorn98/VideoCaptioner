import json

from videocaptioner.core.llm.models import LLMTransport, ProviderDialect
from videocaptioner.core.llm.profiles import LLMModelProfileStore
from videocaptioner.core.translate.enhanced.defaults import (
    DEFAULT_REVIEW_TRANSLATION_PROMPT,
)
from videocaptioner.ui.common.translation_migration import (
    MIGRATION_VERSION,
    migrate_legacy_translation_settings,
)


def _legacy_settings(*, service: str, prompt: str = "legacy prompt") -> dict:
    return {
        "Translate": {
            "TranslatorServiceEnum": service,
            "BatchSize": 17,
            "ThreadNum": 6,
        },
        "Subtitle": {
            "CustomPromptText": prompt,
            "NeedTranslate": True,
        },
        "LLM": {
            "LLMService": "DeepSeek",
            "DeepSeek_Model": "deepseek-chat",
            "DeepSeek_API_Base": "https://api.deepseek.example/v1",
            "DeepSeek_API_Key": "legacy-secret",
        },
    }


def _write_settings(path, document: dict) -> None:
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


def _read_settings(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_legacy_llm_migrates_to_enhanced_with_one_profile_bound_to_both_roles(
    tmp_path,
) -> None:
    settings_path = tmp_path / "settings.json"
    profiles_path = tmp_path / "profiles.json"
    _write_settings(settings_path, _legacy_settings(service="LLM 大模型翻译"))
    store = LLMModelProfileStore(profiles_path)

    assert migrate_legacy_translation_settings(settings_path, profile_store=store)

    settings = _read_settings(settings_path)
    translate = settings["Translate"]
    assert translate["TranslationMode"] == "enhanced_llm"
    assert translate["MainLLMProfileId"] == translate["ReviewLLMProfileId"]
    profiles = store.list()
    assert len(profiles) == 1
    profile = profiles[0]
    assert profile.profile_id == translate["MainLLMProfileId"] == "legacy-deepseek"
    assert profile.name == "旧配置 · DeepSeek"
    assert profile.transport is LLMTransport.OPENAI_COMPATIBLE
    assert profile.dialect is ProviderDialect.GENERIC
    assert profile.base_url == "https://api.deepseek.example/v1"
    assert profile.api_key == "legacy-secret"
    assert profile.model == "deepseek-chat"
    assert profile.max_concurrency == 6


def test_legacy_non_llm_keeps_service_and_selects_non_llm_mode(tmp_path) -> None:
    settings_path = tmp_path / "settings.json"
    profiles_path = tmp_path / "profiles.json"
    service = "微软翻译"
    _write_settings(settings_path, _legacy_settings(service=service))
    store = LLMModelProfileStore(profiles_path)

    assert migrate_legacy_translation_settings(settings_path, profile_store=store)

    translate = _read_settings(settings_path)["Translate"]
    assert translate["TranslatorServiceEnum"] == service
    assert translate["TranslationMode"] == "non_llm"
    assert store.list() == ()
    assert not profiles_path.exists()


def test_legacy_prompt_moves_only_to_main_prompt_and_clears_old_field(tmp_path) -> None:
    settings_path = tmp_path / "settings.json"
    profiles_path = tmp_path / "profiles.json"
    old_prompt = "Use the user's established terminology."
    _write_settings(
        settings_path,
        _legacy_settings(service="微软翻译", prompt=old_prompt),
    )

    migrate_legacy_translation_settings(
        settings_path,
        profile_store=LLMModelProfileStore(profiles_path),
    )

    settings = _read_settings(settings_path)
    assert settings["Translate"]["MainTranslationPrompt"] == old_prompt
    assert (
        settings["Translate"]["ReviewTranslationPrompt"]
        == DEFAULT_REVIEW_TRANSLATION_PROMPT
    )
    assert settings["Translate"]["ReviewTranslationPrompt"] != old_prompt
    assert settings["Subtitle"]["CustomPromptText"] == ""


def test_migration_is_idempotent_and_does_not_duplicate_profile(tmp_path) -> None:
    settings_path = tmp_path / "settings.json"
    profiles_path = tmp_path / "profiles.json"
    _write_settings(settings_path, _legacy_settings(service="LLM 大模型翻译"))
    store = LLMModelProfileStore(profiles_path)

    assert migrate_legacy_translation_settings(settings_path, profile_store=store)
    settings_after_first = settings_path.read_bytes()
    profiles_after_first = profiles_path.read_bytes()

    assert not migrate_legacy_translation_settings(settings_path, profile_store=store)
    assert settings_path.read_bytes() == settings_after_first
    assert profiles_path.read_bytes() == profiles_after_first
    assert len(LLMModelProfileStore(profiles_path).list()) == 1
    assert (
        _read_settings(settings_path)["Translate"]["TranslationMigrationVersion"]
        == MIGRATION_VERSION
    )


def test_corrupt_settings_json_is_left_untouched(tmp_path) -> None:
    settings_path = tmp_path / "settings.json"
    profiles_path = tmp_path / "profiles.json"
    corrupt = b'{"Translate": invalid json'
    settings_path.write_bytes(corrupt)

    assert not migrate_legacy_translation_settings(
        settings_path,
        profile_store=LLMModelProfileStore(profiles_path),
    )
    assert settings_path.read_bytes() == corrupt
    assert not profiles_path.exists()
