"""One-time, idempotent migration from legacy global LLM translation settings."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping

from videocaptioner.core.llm.models import (
    LLMModelProfile,
    LLMTransport,
    ProviderDialect,
)
from videocaptioner.core.llm.profiles import LLMModelProfileStore
from videocaptioner.core.translate.enhanced.defaults import (
    DEFAULT_REVIEW_TRANSLATION_PROMPT,
)

MIGRATION_VERSION = 1

_LEGACY_LLM_SERVICE = "LLM 大模型翻译"
_LEGACY_PROVIDER_FIELDS = {
    "OpenAI 兼容": ("OpenAI", "OpenAI_Model", "OpenAI_API_Base", "OpenAI_API_Key"),
    "SiliconCloud": (
        "SiliconCloud",
        "SiliconCloud_Model",
        "SiliconCloud_API_Base",
        "SiliconCloud_API_Key",
    ),
    "DeepSeek": ("DeepSeek", "DeepSeek_Model", "DeepSeek_API_Base", "DeepSeek_API_Key"),
    "Ollama": ("Ollama", "Ollama_Model", "Ollama_API_Base", "Ollama_API_Key"),
    "LM Studio": ("LM Studio", "LmStudio_Model", "LmStudio_API_Base", "LmStudio_API_Key"),
    "Gemini": ("Gemini", "Gemini_Model", "Gemini_API_Base", "Gemini_API_Key"),
    "ChatGLM": ("ChatGLM", "ChatGLM_Model", "ChatGLM_API_Base", "ChatGLM_API_Key"),
}


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=4) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _profile_id(provider: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", provider.casefold()).strip("-") or "llm"
    return f"legacy-{slug}"


def migrate_legacy_translation_settings(
    settings_path: str | Path,
    *,
    profile_store: LLMModelProfileStore | None = None,
) -> bool:
    """Migrate legacy settings before qconfig validates unknown new enum fields."""

    path = Path(settings_path)
    if not path.exists():
        return False
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(document, dict):
        return False
    translate = document.setdefault("Translate", {})
    subtitle = document.setdefault("Subtitle", {})
    llm = document.setdefault("LLM", {})
    if not isinstance(translate, dict) or not isinstance(subtitle, dict) or not isinstance(llm, dict):
        return False
    try:
        if int(translate.get("TranslationMigrationVersion", 0)) >= MIGRATION_VERSION:
            return False
    except (TypeError, ValueError):
        pass

    legacy_service = str(translate.get("TranslatorServiceEnum", ""))
    old_prompt = str(subtitle.get("CustomPromptText", ""))
    translate["MainTranslationPrompt"] = old_prompt
    translate.setdefault("ReviewTranslationPrompt", DEFAULT_REVIEW_TRANSLATION_PROMPT)
    translate["EnhancedBatchSize"] = int(translate.get("BatchSize", 10) or 10)
    subtitle["CustomPromptText"] = ""

    if legacy_service == _LEGACY_LLM_SERVICE:
        translate["TranslationMode"] = "enhanced_llm"
        selected_provider = str(llm.get("LLMService", "OpenAI 兼容"))
        provider_name, model_key, base_key, api_key = _LEGACY_PROVIDER_FIELDS.get(
            selected_provider, _LEGACY_PROVIDER_FIELDS["OpenAI 兼容"]
        )
        profile = LLMModelProfile(
            profile_id=_profile_id(provider_name),
            name=f"旧配置 · {provider_name}",
            transport=LLMTransport.OPENAI_COMPATIBLE,
            dialect=ProviderDialect.GENERIC,
            base_url=str(llm.get(base_key, "") or "http://localhost/v1"),
            api_key=str(llm.get(api_key, "")),
            model=str(llm.get(model_key, "") or "unconfigured"),
            work_context_tokens=65_536,
            max_concurrency=max(1, min(50, int(translate.get("ThreadNum", 10) or 10))),
        )
        store = profile_store or LLMModelProfileStore()
        try:
            existing = store.get(profile.profile_id)
        except KeyError:
            existing = store.save(profile)
        translate["MainLLMProfileId"] = existing.profile_id
        translate["ReviewLLMProfileId"] = existing.profile_id
    else:
        translate["TranslationMode"] = "non_llm"

    translate["TranslationMigrationVersion"] = MIGRATION_VERSION
    _atomic_write(path, document)
    return True


__all__ = ["MIGRATION_VERSION", "migrate_legacy_translation_settings"]
