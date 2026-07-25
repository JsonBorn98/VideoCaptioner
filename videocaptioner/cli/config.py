"""CLI configuration management.

Config priority (highest to lowest):
  1. Command-line arguments
  2. Environment variables (VIDEOCAPTIONER_*)
  3. User config file (~/.config/videocaptioner/config.toml)
  4. Built-in defaults
"""

import json
import os
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional

from platformdirs import user_config_dir

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]

APP_NAME = "videocaptioner"


class _BuiltConfig(dict):
    """Runtime config with source-presence metadata kept out of TOML keys."""

    _legacy_llm_api_key_explicit: bool

# Default config directory
CONFIG_DIR = Path(user_config_dir(APP_NAME))
CONFIG_FILE = CONFIG_DIR / "config.toml"

# Environment variable mappings: env var name → config dotted key
# Supports both OpenAI standard names and VIDEOCAPTIONER_ prefixed names
ENV_MAP: Dict[str, str] = {
    # OpenAI standard (most tools recognize these)
    "OPENAI_API_KEY": "llm.api_key",
    "OPENAI_BASE_URL": "llm.api_base",
    "OPENAI_MODEL": "llm.model",
    # VIDEOCAPTIONER_ prefixed (take precedence over standard)
    "VIDEOCAPTIONER_LLM_API_KEY": "llm.api_key",
    "VIDEOCAPTIONER_LLM_API_BASE": "llm.api_base",
    "VIDEOCAPTIONER_LLM_MODEL": "llm.model",
    "VIDEOCAPTIONER_WHISPER_API_KEY": "whisper_api.api_key",
    "VIDEOCAPTIONER_WHISPER_API_BASE": "whisper_api.api_base",
    "VIDEOCAPTIONER_DEEPLX_ENDPOINT": "translate.deeplx_endpoint",
    "VIDEOCAPTIONER_SOURCE_LANG": "translate.source_language",
    "VIDEOCAPTIONER_TARGET_LANG": "translate.target_language",
    "VIDEOCAPTIONER_DUBBING_PROVIDER": "dubbing.provider",
    "VIDEOCAPTIONER_DUB_PRESET": "dubbing.preset",
    "VIDEOCAPTIONER_TTS_API_KEY": "dubbing.api_key",
    "VIDEOCAPTIONER_TTS_API_BASE": "dubbing.api_base",
    "VIDEOCAPTIONER_TTS_MODEL": "dubbing.model",
    "VIDEOCAPTIONER_TTS_VOICE": "dubbing.voice",
    "VIDEOCAPTIONER_TTS_STYLE_PROMPT": "dubbing.style_prompt",
    "VIDEOCAPTIONER_TTS_WORKERS": "dubbing.tts_workers",
    "VIDEOCAPTIONER_TTS_USE_CACHE": "dubbing.use_cache",
    "VIDEOCAPTIONER_TTS_FIT_MODE": "dubbing.fit_mode",
    "VIDEOCAPTIONER_DUB_TIMING": "dubbing.timing",
    "VIDEOCAPTIONER_DUB_AUDIO_MODE": "dubbing.audio_mode",
    "VIDEOCAPTIONER_TTS_MAX_SPEED": "dubbing.max_speed",
    "VIDEOCAPTIONER_TTS_REWRITE_TOO_LONG": "dubbing.rewrite_too_long",
    "VIDEOCAPTIONER_TTS_MIX_ORIGINAL_AUDIO": "dubbing.mix_original_audio",
    "VIDEOCAPTIONER_AUDIO_LOUDNORM": "transcribe.audio_loudnorm",
    "VIDEOCAPTIONER_MIMO_ASR_API_KEY": "transcribe.mimo_asr.api_key",
    "VIDEOCAPTIONER_MIMO_ASR_API_BASE": "transcribe.mimo_asr.api_base",
    "VIDEOCAPTIONER_MIMO_ASR_MODEL": "transcribe.mimo_asr.model",
    "VIDEOCAPTIONER_MIMO_ASR_TIMEOUT": "transcribe.mimo_asr.timeout",
    "VIDEOCAPTIONER_MIMO_ASR_CONCURRENCY": "transcribe.mimo_asr.concurrency",
    "VIDEOCAPTIONER_QWEN_ASR_MODEL": "transcribe.qwen.asr_model",
    "VIDEOCAPTIONER_QWEN_ALIGNER_MODEL": "transcribe.qwen.aligner_model",
    "VIDEOCAPTIONER_QWEN_MODEL_DIR": "transcribe.qwen.model_dir",
    "VIDEOCAPTIONER_QWEN_DEVICE": "transcribe.qwen.device",
    "VIDEOCAPTIONER_QWEN_DTYPE": "transcribe.qwen.dtype",
    "VIDEOCAPTIONER_QWEN_MAX_NEW_TOKENS": "transcribe.qwen.max_new_tokens",
    "VIDEOCAPTIONER_QWEN_CHUNK_OVERLAP_SECONDS": "transcribe.qwen.chunk_overlap_seconds",
    "VIDEOCAPTIONER_QWEN_COMPILE_ALIGNER": "transcribe.qwen.compile_aligner",
}

# Translation profiles deliberately live outside ``[llm]``.  The global
# variables above keep their established meaning for subtitle optimization,
# splitting and other legacy LLM features; these role-specific variables only
# affect LLM translation.  The shorter ``VIDEOCAPTIONER_LLM_<ROLE>_*`` names
# are retained as aliases, while the explicit TRANSLATE_LLM form wins when
# both are present because it is inserted last.
_TRANSLATION_LLM_ENV_FIELDS = {
    "API_KEY": "api_key",
    "API_BASE": "api_base",
    "BASE_URL": "api_base",
    "MODEL": "model",
    "TRANSPORT": "transport",
    "DIALECT": "dialect",
    "WORK_CONTEXT_TOKENS": "work_context_tokens",
    "MAX_CONCURRENCY": "max_concurrency",
    "OPENAI_ENDPOINT": "openai_endpoint",
    "ENDPOINT": "openai_endpoint",
    "MAX_OUTPUT_TOKENS": "max_output_tokens",
    "REQUEST_OPTIONS_JSON": "request_options_json",
}
for _role in ("main", "review"):
    for _env_suffix, _field in _TRANSLATION_LLM_ENV_FIELDS.items():
        ENV_MAP[f"VIDEOCAPTIONER_LLM_{_role.upper()}_{_env_suffix}"] = (
            f"translate.llm.{_role}.{_field}"
        )
        ENV_MAP[f"VIDEOCAPTIONER_TRANSLATE_LLM_{_role.upper()}_{_env_suffix}"] = (
            f"translate.llm.{_role}.{_field}"
        )

DEFAULTS: Dict[str, Any] = {
    "llm": {
        "api_key": "",
        "api_base": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "work_context_tokens": 65536,
        "max_concurrency": 4,
    },
    "whisper_api": {
        "api_key": "",
        "api_base": "https://api.openai.com/v1",
        "model": "whisper-1",
        "prompt": "",
    },
    "transcribe": {
        "asr": "bijian",
        "language": "auto",
        "audio_loudnorm": False,
        "faster_whisper": {
            "model": "large-v3",
            "device": "auto",
            "vad_filter": True,
            "vad_method": "silero-v4-fw",
            "vad_threshold": 0.5,
            "voice_extraction": False,
            "prompt": "",
        },
        "whisper_cpp": {
            "model": "large-v2",
        },
        "mimo_asr": {
            "api_key": "",
            "api_base": "https://api.xiaomimimo.com/v1",
            "model": "mimo-v2.5-asr",
            "timeout": 600,
            "concurrency": 2,
        },
        "qwen": {
            "asr_model": "Qwen/Qwen3-ASR-1.7B",
            "aligner_model": "Qwen/Qwen3-ForcedAligner-0.6B",
            "model_dir": "",
            "device": "auto",
            "dtype": "auto",
            "max_new_tokens": 2048,
            "chunk_overlap_seconds": 10,
            "compile_aligner": False,
        },
    },
    "subtitle": {
        "optimize": True,
        "translate": False,
        "split": True,
        "max_word_count_cjk": 18,
        "max_word_count_english": 12,
        "thread_num": 4,
        "batch_size": 20,
        "optimization_prompt": "",
    },
    "postprocess": {
        # Complete workflows run the dedicated stage by default. The selected
        # profile store remains the source of truth for capability defaults.
        "enabled": True,
        "profile": "balanced",
        "media": "",
    },
    "translate": {
        "mode": "enhanced_llm",
        "service": "bing",
        "source_language": "auto",
        "target_language": "zh-Hans",
        "reflect": False,
        "deeplx_endpoint": "",
        "main_prompt": "",
        "review_prompt": "",
        "enhanced_batch_size": 10,
        "term_context_radius": 10,
        "boundary_context_radius": 3,
        "glossary_path": "",
    },
    "synthesize": {
        "subtitle_mode": "soft",
        "quality": "medium",
        "layout": "target-above",
        "render_mode": "ass",
        "style": "default",
    },
    "dubbing": {
        "provider": "edge",
        "preset": "edge-cn-female",
        "api_key": "",
        "api_base": "",
        "model": "edge-tts",
        "voice": "zh-CN-XiaoxiaoNeural",
        "response_format": "mp3",
        "sample_rate": 32000,
        "speed": 1.0,
        "gain": 0,
        "tts_workers": 5,
        "use_cache": True,
        "style_prompt": "",
        "timing": "balanced",
        "audio_mode": "replace",
        "fit_mode": "tempo",
        "max_speed": 2.0,
        "target_padding_ms": 80,
        "rewrite_too_long": False,
        "rewrite_threshold": 1.15,
        "mix_original_audio": False,
        "original_audio_volume": 0.25,
        "dubbed_audio_volume": 1.0,
    },
    "output": {
        "format": "srt",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge override into base recursively. Override values take precedence."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _set_nested(d: dict, dotted_key: str, value: Any) -> None:
    """Set a value in a nested dict using dotted key notation (e.g. 'llm.api_key')."""
    keys = dotted_key.split(".")
    for key in keys[:-1]:
        d = d.setdefault(key, {})
    d[keys[-1]] = value


def _get_nested(d: dict, dotted_key: str, default: Any = None) -> Any:
    """Get a value from a nested dict using dotted key notation."""
    keys = dotted_key.split(".")
    for key in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(key, default)  # type: ignore[assignment]
        if d is default:
            return default
    return d


def load_config_file(path: Optional[Path] = None) -> dict:
    """Load and parse a TOML config file. Returns empty dict if file doesn't exist."""
    path = path or CONFIG_FILE
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        import sys

        print(f"! Warning: Failed to parse config file {path}: {e}", file=sys.stderr)
        print("  Run 'videocaptioner config init' to recreate it.", file=sys.stderr)
        return {}


def load_env_overrides() -> dict:
    """Read environment variables and map them to config keys.

    Supports both OpenAI standard names (OPENAI_API_KEY) and
    VIDEOCAPTIONER_ prefixed names. Prefixed names take precedence.
    """
    overrides: Dict[str, Any] = {}
    for env_var, dotted_key in ENV_MAP.items():
        value = os.environ.get(env_var)
        if value is not None:
            try:
                parsed_value = _parse_value(value, dotted_key)
            except ValueError as exc:
                print(f"! Warning: Invalid environment value {env_var}: {exc}", file=sys.stderr)
                continue
            _set_nested(overrides, dotted_key, parsed_value)
    return overrides


def _normalize_translation_llm_aliases(config: dict, *, source: str) -> dict:
    """Normalize role aliases inside one priority layer before layers are merged."""

    normalized = deepcopy(config)
    translate = normalized.get("translate")
    llm = translate.get("llm") if isinstance(translate, dict) else None
    if not isinstance(llm, dict):
        return normalized
    for role in ("main", "review"):
        role_config = llm.get(role)
        if not isinstance(role_config, dict):
            continue
        for alias, canonical in (
            ("base_url", "api_base"),
            ("endpoint", "openai_endpoint"),
        ):
            if alias not in role_config:
                continue
            if canonical in role_config and role_config[canonical] != role_config[alias]:
                raise ValueError(
                    f"{source} translate.llm.{role}.{alias} conflicts with "
                    f"translate.llm.{role}.{canonical}"
                )
            role_config[canonical] = role_config.pop(alias)
    return normalized


def build_config(
    cli_overrides: Optional[dict] = None,
    config_path: Optional[Path] = None,
) -> dict:
    """Build final config by merging all sources (priority: cli > env > file > defaults)."""
    config = deepcopy(DEFAULTS)
    # Layer 1: config file
    file_config = _normalize_translation_llm_aliases(
        load_config_file(config_path), source="config file"
    )
    # Configurations written before the three-mode CLI used ``service`` as the
    # workflow selector.  Classify those files before merging defaults so an
    # existing Bing/Google/DeepLX user is not silently moved to an LLM mode.
    translate_config = file_config.get("translate")
    if (
        isinstance(translate_config, dict)
        and "mode" not in translate_config
        and "service" in translate_config
    ):
        translate_config["mode"] = (
            "enhanced_llm" if translate_config.get("service") == "llm" else "non_llm"
        )
    config = _deep_merge(config, file_config)
    # Layer 2: environment variables
    env_config = _normalize_translation_llm_aliases(
        load_env_overrides(), source="environment"
    )
    config = _deep_merge(config, env_config)
    # Layer 3: CLI argument overrides
    if cli_overrides:
        cli_config = _normalize_translation_llm_aliases(
            cli_overrides, source="command line"
        )
        config = _deep_merge(config, cli_config)
    result = _BuiltConfig(config)
    missing = object()
    result._legacy_llm_api_key_explicit = any(
        _get_nested(layer, "llm.api_key", missing) is not missing
        for layer in (file_config, env_config, cli_overrides or {})
    )
    return result


def get(config: dict, key: str, default: Any = None) -> Any:
    """Convenience accessor for dotted keys."""
    return _get_nested(config, key, default)


def build_legacy_llm_profile(config: dict):
    """Build an immutable model profile from the CLI's legacy ``[llm]`` table.

    The CLI intentionally keeps accepting the established flat configuration.
    Both enhanced roles receive snapshots of this profile; they still use
    separate role prompts and independent calls.
    """
    from videocaptioner.core.llm import (
        LLMModelProfile,
        LLMTransport,
        ProviderDialect,
    )

    return LLMModelProfile(
        profile_id="cli-legacy",
        name="CLI legacy model",
        transport=LLMTransport.OPENAI_COMPATIBLE,
        dialect=ProviderDialect.GENERIC,
        base_url=str(get(config, "llm.api_base", "https://api.openai.com/v1")),
        api_key=str(get(config, "llm.api_key", "")),
        model=str(get(config, "llm.model", "")),
        work_context_tokens=int(get(config, "llm.work_context_tokens", 65536)),
        max_concurrency=int(get(config, "llm.max_concurrency", 4)),
    )


_TRANSLATION_LLM_PROFILE_FIELDS = frozenset(
    {
        "api_key",
        "api_base",
        "base_url",
        "model",
        "transport",
        "dialect",
        "work_context_tokens",
        "max_concurrency",
        "openai_endpoint",
        "endpoint",
        "max_output_tokens",
        "request_options_json",
    }
)


def _translation_llm_role_config(config: dict, role: str) -> dict[str, Any]:
    """Return a role's explicit config fields without applying inheritance."""
    if role not in {"main", "review"}:
        raise ValueError("translation LLM role must be 'main' or 'review'")
    raw = get(config, f"translate.llm.{role}", {})
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"translate.llm.{role} must be a TOML table")
    unknown = set(raw) - _TRANSLATION_LLM_PROFILE_FIELDS
    if unknown:
        fields = ", ".join(sorted(unknown))
        raise ValueError(f"unknown translate.llm.{role} field(s): {fields}")

    normalized = dict(raw)
    for alias, canonical in (("base_url", "api_base"), ("endpoint", "openai_endpoint")):
        if alias not in normalized:
            continue
        if canonical in normalized and normalized[canonical] != normalized[alias]:
            raise ValueError(
                f"translate.llm.{role}.{alias} conflicts with "
                f"translate.llm.{role}.{canonical}"
            )
        normalized[canonical] = normalized.pop(alias)
    return normalized


def _parse_translation_request_options(raw: Any, *, role: str) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise ValueError(f"translate.llm.{role}.request_options_json must be a JSON string")
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"translate.llm.{role}.request_options_json is invalid JSON: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise ValueError(
            f"translate.llm.{role}.request_options_json must decode to a JSON object"
        )
    return value


def _parse_translation_max_output_tokens(raw: Any, *, role: str) -> int | None:
    if raw is None or (isinstance(raw, str) and raw.strip().lower() == "auto"):
        return None
    if isinstance(raw, (bool, float)):
        raise ValueError(
            f"translate.llm.{role}.max_output_tokens must be 'auto' or a positive integer"
        )
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"translate.llm.{role}.max_output_tokens must be 'auto' or a positive integer"
        ) from exc
    if isinstance(raw, str) and raw.strip() != str(value):
        raise ValueError(
            f"translate.llm.{role}.max_output_tokens must be 'auto' or a positive integer"
        )
    if value < 1:
        raise ValueError(f"translate.llm.{role}.max_output_tokens must be at least 1")
    return value


def _parse_translation_integer(raw: Any, *, role: str, field: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise ValueError(f"translate.llm.{role}.{field} must be an integer")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"translate.llm.{role}.{field} must be an integer") from exc
    if isinstance(raw, str) and raw.strip() != str(value):
        raise ValueError(f"translate.llm.{role}.{field} must be an integer")
    return value


def _enum_config_value(raw: Any) -> str:
    value = getattr(raw, "value", raw)
    return value if isinstance(value, str) else str(value)


def _build_translation_llm_profile(
    values: dict[str, Any],
    *,
    role: str,
):
    from videocaptioner.core.llm import (
        LLMModelProfile,
        LLMTransport,
        OpenAIEndpoint,
        ProviderDialect,
    )

    request_options = _parse_translation_request_options(
        values.get("request_options_json", "{}"), role=role
    )
    max_output_tokens = _parse_translation_max_output_tokens(
        values.get("max_output_tokens", "auto"), role=role
    )
    try:
        profile = LLMModelProfile(
            profile_id=f"cli-{role}",
            name=f"CLI {role} translation model",
            transport=LLMTransport(_enum_config_value(values["transport"])),
            dialect=ProviderDialect(_enum_config_value(values["dialect"])),
            base_url=values["api_base"],
            api_key=values["api_key"],
            model=values["model"],
            work_context_tokens=_parse_translation_integer(
                values["work_context_tokens"], role=role, field="work_context_tokens"
            ),
            max_concurrency=_parse_translation_integer(
                values["max_concurrency"], role=role, field="max_concurrency"
            ),
            openai_endpoint=OpenAIEndpoint(
                _enum_config_value(values["openai_endpoint"])
            ),
            request_options=request_options,
            max_output_tokens=max_output_tokens,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid translate.llm.{role} profile: {exc}") from exc

    # Request-body protection depends on the final transport and endpoint, so
    # it is deliberately checked after all role inheritance has been applied.
    try:
        from videocaptioner.core.llm.request_options import validate_profile_request_options

        validate_profile_request_options(profile)
    except ValueError as exc:
        raise ValueError(f"invalid translate.llm.{role} request options: {exc}") from exc
    return profile


def _resolve_translation_llm_main(config: dict):
    legacy_profile = build_legacy_llm_profile(config)
    base_values: dict[str, Any] = {
        "api_key": legacy_profile.api_key,
        "api_base": legacy_profile.base_url,
        "model": legacy_profile.model,
        "transport": legacy_profile.transport.value,
        "dialect": legacy_profile.dialect.value,
        "work_context_tokens": legacy_profile.work_context_tokens,
        "max_concurrency": legacy_profile.max_concurrency,
        "openai_endpoint": "chat_completions",
        "request_options_json": "{}",
        "max_output_tokens": "auto",
    }

    main_overrides = _translation_llm_role_config(config, "main")
    main_values = {**base_values, **main_overrides}
    main_profile = (
        _build_translation_llm_profile(main_values, role="main")
        if main_overrides
        else legacy_profile
    )
    return main_profile, main_values


def build_translation_llm_profiles(config: dict):
    """Build immutable main/review profiles with field-presence inheritance.

    Resolution is ``[llm] -> main -> review``.  Missing role tables return the
    exact inherited profile object, which preserves the historical
    ``cli-legacy`` profile when no new translation configuration is present.
    """
    main_profile, main_values = _resolve_translation_llm_main(config)

    review_overrides = _translation_llm_role_config(config, "review")
    review_values = {**main_values, **review_overrides}
    review_profile = (
        _build_translation_llm_profile(review_values, role="review")
        if review_overrides
        else main_profile
    )
    return main_profile, review_profile


def build_translation_llm_profile(config: dict, role: str):
    """Build one resolved translation role profile, ignoring unused later roles."""
    if role not in {"main", "review"}:
        raise ValueError("translation LLM role must be 'main' or 'review'")
    main_profile, main_values = _resolve_translation_llm_main(config)
    if role == "main":
        return main_profile
    review_overrides = _translation_llm_role_config(config, "review")
    if not review_overrides:
        return main_profile
    return _build_translation_llm_profile(
        {**main_values, **review_overrides}, role="review"
    )


def translation_llm_role_allows_empty_api_key(config: dict, role: str) -> bool:
    """Whether an empty key was explicitly selected in a role inheritance chain."""
    main = _translation_llm_role_config(config, "main")
    legacy_explicit = getattr(config, "_legacy_llm_api_key_explicit", None)
    if legacy_explicit is None:
        legacy = config.get("llm")
        legacy_explicit = isinstance(legacy, dict) and "api_key" in legacy
    if role == "main":
        return "api_key" in main or bool(legacy_explicit)
    if role == "review":
        review = _translation_llm_role_config(config, "review")
        return "api_key" in review or "api_key" in main or bool(legacy_explicit)
    raise ValueError("translation LLM role must be 'main' or 'review'")


def ensure_config_dir() -> Path:
    """Ensure the config directory exists and return its path."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return CONFIG_DIR


def _parse_value(raw: str, key: str) -> Any:
    """Parse a string value into the correct Python type based on DEFAULTS."""
    # Infer type from defaults
    default_val = _get_nested(DEFAULTS, key)
    if isinstance(default_val, bool):
        if raw.lower() in ("true", "1", "yes"):
            return True
        if raw.lower() in ("false", "0", "no"):
            return False
        raise ValueError(f"Expected boolean for '{key}', got '{raw}' (use true/false)")
    if isinstance(default_val, int):
        try:
            return int(raw)
        except ValueError:
            raise ValueError(f"Expected integer for '{key}', got '{raw}'")
    if isinstance(default_val, float):
        try:
            return float(raw)
        except ValueError:
            raise ValueError(f"Expected number for '{key}', got '{raw}'")
    return raw


def save_config_value(key: str, value: str, config_path: Optional[Path] = None) -> None:
    """Set a single value in the config file. Creates the file if it doesn't exist."""
    path = config_path or CONFIG_FILE
    ensure_config_dir()

    existing = load_config_file(path)
    _set_nested(existing, key, _parse_value(value, key))

    with open(path, "w", encoding="utf-8") as f:
        _write_toml(f, existing)
    # Restrict permissions — config may contain API keys
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _write_toml(f, data: dict, parent_key: str = "") -> None:
    """Write a dict as valid TOML, handling arbitrary nesting depth."""
    # Write scalar values at this level first
    for key, value in data.items():
        if not isinstance(value, dict):
            f.write(f"{key} = {_toml_value(value)}\n")

    # Write sub-tables recursively
    for key, value in data.items():
        if isinstance(value, dict):
            full_key = f"{parent_key}.{key}" if parent_key else key
            f.write(f"\n[{full_key}]\n")
            _write_toml(f, value, full_key)


def _toml_value(value: Any) -> str:
    """Convert a Python value to TOML representation."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        return f'"{escaped}"'
    return f'"{value!s}"'


def format_config(config: dict, indent: int = 0) -> str:
    """Format config dict for display."""
    lines = []
    prefix = "  " * indent
    for key, value in config.items():
        if isinstance(value, dict):
            lines.append(f"{prefix}{key}:")
            lines.append(format_config(value, indent + 1))
        elif isinstance(value, str) and ("key" in key or "token" in key) and value:
            # Mask sensitive values
            masked = f"{value[:4]}...{value[-4:]}" if len(value) > 8 else "****"
            lines.append(f"{prefix}{key} = {masked}")
        else:
            lines.append(f"{prefix}{key} = {value}")
    return "\n".join(lines)
