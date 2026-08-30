"""CLI configuration management.

Config priority (highest to lowest):
  1. Command-line arguments
  2. Environment variables (VIDEOCAPTIONER_*)
  3. User config file (~/.config/videocaptioner/config.toml)
  4. Built-in defaults
"""

import os
import sys
from collections.abc import Mapping
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


# Default config directory
CONFIG_DIR = Path(user_config_dir(APP_NAME))
CONFIG_FILE = CONFIG_DIR / "config.toml"

# Credential-only environment override (ADR-0015). Unlike the keys in ENV_MAP
# it never writes into the TOML tree: it swaps the api_key of an already
# resolved profile so CI can inject a key without persisting it. It is also
# deliberately NOT the OpenAI standard name — a key exported for another tool
# in the host shell must not be silently adopted here.
LLM_API_KEY_ENV_OVERRIDE = "VIDEOCAPTIONER_LLM_API_KEY"

# Sentinel distinguishing "key absent" from a falsy stored value.
_MISSING = object()

# Environment variable mappings: env var name → config dotted key.
#
# LLM model selection is profile-based (ADR-0015): the three profile-id keys
# below select entries from the model profile store. Credentials live in the
# store itself; VIDEOCAPTIONER_LLM_API_KEY is deliberately NOT mapped here —
# it is a narrow credential-only override applied after a profile resolves
# (see apply_env_api_key_override). OPENAI_* standard names are not recognized
# so keys set for other tools in the host shell are never silently adopted.
ENV_MAP: Dict[str, str] = {
    "VIDEOCAPTIONER_LLM_PROFILE_ID": "llm.profile_id",
    "VIDEOCAPTIONER_LLM_REVIEW_PROFILE_ID": "llm.review_profile_id",
    "VIDEOCAPTIONER_LLM_UTILITY_PROFILE_ID": "llm.utility_profile_id",
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

DEFAULTS: Dict[str, Any] = {
    # LLM model selection is profile-based: every LLM consumer resolves its
    # connection from the model profile store (the same file the GUI edits).
    #   profile_id        — main translation; also the derivation source for
    #                       utility roles (split/optimize/postprocess/dub rewrite)
    #   review_profile_id — enhanced-LLM review; must be set for enhanced mode
    #   utility_profile_id — independent utility binding; empty = derive from main
    "llm": {
        "profile_id": "",
        "review_profile_id": "",
        "utility_profile_id": "",
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
    """Set a value in a nested dict using dotted key notation (e.g. 'llm.profile_id')."""
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

    Only VIDEOCAPTIONER_ prefixed names are recognized; OPENAI_* standard
    names are deliberately not (see ENV_MAP).
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


def build_config(
    cli_overrides: Optional[dict] = None,
    config_path: Optional[Path] = None,
) -> dict:
    """Build final config by merging all sources (priority: cli > env > file > defaults)."""
    config = deepcopy(DEFAULTS)
    # Layer 1: config file
    file_config = load_config_file(config_path)
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
    env_config = load_env_overrides()
    config = _deep_merge(config, env_config)
    # Layer 3: CLI argument overrides
    if cli_overrides:
        config = _deep_merge(config, cli_overrides)
    # The CLI is agent-facing: silently ignored LLM keys are a debugging hell,
    # so leftover pre-profile keys get a one-time stderr warning plus migration
    # guidance. The data itself is tolerated, not migrated (dead data, no fail).
    _warn_legacy_llm_keys(file_config, cli_overrides or {}, os.environ)
    return config


_LEGACY_LLM_FILE_KEYS = (
    "llm.api_key",
    "llm.api_base",
    "llm.model",
    "llm.work_context_tokens",
    "llm.max_concurrency",
    "translate.llm.main",
    "translate.llm.review",
)

# Environment variables whose mappings were removed with the [llm] table and
# the translate.llm.* inline tables (ADR-0015). Checked by exact prefix/name.
_LEGACY_LLM_ENV_VARS = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "VIDEOCAPTIONER_LLM_API_BASE",
    "VIDEOCAPTIONER_LLM_MODEL",
)

_LEGACY_LLM_ENV_PREFIXES = (
    "VIDEOCAPTIONER_LLM_MAIN_",
    "VIDEOCAPTIONER_LLM_REVIEW_",
    "VIDEOCAPTIONER_TRANSLATE_LLM_",
)


def _legacy_llm_key_hits(env: Mapping[str, str]) -> list[str]:
    """Return the legacy LLM environment variables actually set right now."""

    hits = [name for name in _LEGACY_LLM_ENV_VARS if env.get(name)]
    hits.extend(
        name
        for name in env
        if any(name.startswith(prefix) for prefix in _LEGACY_LLM_ENV_PREFIXES)
    )
    return sorted(set(hits))


def _warn_legacy_llm_keys(
    file_config: dict, cli_overrides: dict, env: Mapping[str, str]
) -> None:
    """Warn once when pre-profile LLM keys are still present in any layer."""

    # File/CLI layers report the known legacy TOML keys they still carry; the
    # env layer reports the legacy variable names actually set (not what
    # ENV_MAP mapped, which no longer covers them).
    keys: list[tuple[str, list[str]]] = []
    for source, layer in (("config file", file_config), ("command line", cli_overrides)):
        hits = [
            key
            for key in _LEGACY_LLM_FILE_KEYS
            if _get_nested(layer, key, _MISSING) is not _MISSING
        ]
        if hits:
            keys.append((source, hits))
    env_hits = _legacy_llm_key_hits(env)
    if env_hits:
        keys.append(("environment", env_hits))
    if not keys:
        return

    print("! Warning: obsolete LLM config keys were ignored:", file=sys.stderr)
    for source, hits in keys:
        print(f"    {source}: {', '.join(hits)}", file=sys.stderr)
    _print_llm_migration_hint()


def _print_llm_migration_hint() -> None:
    """Print where the agent should put LLM config now (store path + profile ids)."""

    from videocaptioner.core.llm.profiles import DEFAULT_LLM_PROFILES_PATH

    print("  LLM model config now comes from the model profile store:", file=sys.stderr)
    print(f"    {DEFAULT_LLM_PROFILES_PATH}", file=sys.stderr)
    print(
        "  Reference a profile in config.toml with: llm.profile_id, "
        "llm.review_profile_id, llm.utility_profile_id",
        file=sys.stderr,
    )
    print(
        "  Select one per run with --llm-profile / --review-profile / --utility-profile, "
        "or VIDEOCAPTIONER_LLM_PROFILE_ID and its _REVIEW/_UTILITY variants.",
        file=sys.stderr,
    )
    available = list_llm_profile_ids()
    if available:
        print(f"  Available profile ids: {', '.join(available)}", file=sys.stderr)
    else:
        print("  No profiles exist yet; create one in the GUI profile editor.", file=sys.stderr)


def list_llm_profile_ids() -> list[str]:
    """Best-effort list of profile ids for guidance text; never raises."""

    try:
        return [profile.profile_id for profile in profile_store().list()]
    except Exception:
        return []


def get(config: dict, key: str, default: Any = None) -> Any:
    """Convenience accessor for dotted keys."""
    return _get_nested(config, key, default)


def llm_profile_ids(config: dict) -> dict[str, str]:
    """Return the three LLM profile-id selections from the merged config."""

    return {
        "main": str(get(config, "llm.profile_id", "") or "").strip(),
        "review": str(get(config, "llm.review_profile_id", "") or "").strip(),
        "utility": str(get(config, "llm.utility_profile_id", "") or "").strip(),
    }


def mask_credential(value: str) -> str:
    """Mask a credential for terminal display (shared by config/profile output)."""

    return f"{value[:4]}...{value[-4:]}" if len(value) > 8 else "****"


def apply_env_api_key_override(profile):
    """Swap only the credential of a resolved profile from the environment.

    VIDEOCAPTIONER_LLM_API_KEY is a narrow CI-oriented override: it replaces
    the api_key of an already resolved profile and leaves base_url and model
    untouched, so a key can be injected without writing it to disk. The
    resulting profile's api_key also feeds the gateway cache key, so an
    overridden key never reuses another key's cached responses. Returns the
    profile unchanged when the variable is unset or blank.

    The override also flips the gateway request-log marker, so every entry of
    this process records key_source="env_override" (the marker lives with the
    other request-log display state in request_logger).
    """
    from dataclasses import replace

    from videocaptioner.core.llm.request_logger import set_env_api_key_override

    key = os.environ.get(LLM_API_KEY_ENV_OVERRIDE, "").strip()
    set_env_api_key_override(bool(key))
    return replace(profile, api_key=key) if key else profile


def profile_store():
    """Construct the model profile store lazily (keeps config import cheap).

    Public so command modules share one construction point instead of
    reaching into a private helper.
    """

    from videocaptioner.core.llm.profiles import LLMModelProfileStore

    return LLMModelProfileStore()


def _lookup_profile(store, profile_id: str, *, description: str):
    """Fetch one profile by id; an empty id or a missing id both fail fast."""

    from videocaptioner.core.llm.profiles import LLMProfileNotFoundError

    try:
        return store.get(profile_id)
    except LLMProfileNotFoundError:
        available = ", ".join(item.profile_id for item in store.list()) or "(none)"
        raise ValueError(
            f"{description} model profile '{profile_id}' does not exist in the "
            f"profile store. Available profile ids: {available}"
        ) from None


def _resolve_bound_profile(config: dict, role: str, store, empty_reason: str, description: str):
    """Shared skeleton for resolving an explicitly bound role profile.

    A blank id and an unknown id both fail fast with guidance (never a silent
    fallback or a read of ambient environment variables), and the resolved
    profile passes through the env credential override. The utility role wraps
    the core resolver instead (a blank binding derives from the main profile),
    so it does not share this skeleton.
    """

    profile_id = llm_profile_ids(config)[role]
    if not profile_id:
        raise ValueError(_llm_profile_guidance(empty_reason))
    store = profile_store() if store is None else store
    profile = _lookup_profile(store, profile_id, description=description)
    return apply_env_api_key_override(profile)


def resolve_main_llm_profile(config: dict, store=None):
    """Resolve the main translation profile, failing fast with guidance.

    The profile store is the only source of LLM connections; a blank or missing
    profile_id is an error pointing at the store file and the three TOML keys
    (never a silent read of ambient environment variables).
    """

    return _resolve_bound_profile(
        config,
        "main",
        store,
        empty_reason=(
            "llm.profile_id is not set; LLM translation needs a main model "
            "profile (the GUI wording for the same condition: 未配置主翻译"
            "模型配置方案，无法使用 LLM 翻译)."
        ),
        description="main translation",
    )


def resolve_review_llm_profile(config: dict, store=None):
    """Resolve the enhanced-mode review profile; a blank id fails fast.

    Enhanced translation requires a dedicated review profile and never falls
    back to the main profile (matching the GUI's missing_translation_roles
    precedent). Single-LLM mode does not call this at all.
    """

    return _resolve_bound_profile(
        config,
        "review",
        store,
        empty_reason=(
            "llm.review_profile_id is not set; enhanced_llm translation "
            "requires a dedicated review profile and never falls back to the "
            "main profile."
        ),
        description="review translation",
    )


def resolve_cli_utility_profile(config: dict, store=None):
    """Resolve the utility-role profile for CLI consumers.

    Wraps the shared resolver (core/llm/utility.py) so the CLI never surfaces
    the GUI card wording to an agent: the failure is restated from the config
    ids (which the resolver already validated) with the store path, the TOML
    keys, and the available profile ids. Resolution semantics are unchanged —
    an independent utility binding wins, then the main profile derives, and a
    lost binding is an error rather than a silent fallback. Like the main and
    review roles, the resolved profile passes through the env credential
    override (VIDEOCAPTIONER_LLM_API_KEY) so all three roles behave alike.
    """

    from videocaptioner.core.llm.utility import UtilityProfileError, resolve_utility_profile

    ids = llm_profile_ids(config)
    store = profile_store() if store is None else store
    try:
        profile = resolve_utility_profile(store, ids["main"], ids["utility"])
    except UtilityProfileError:
        if ids["utility"]:
            reason = (
                f"llm.utility_profile_id = '{ids['utility']}' does not exist in "
                "the profile store; utility roles never silently fall back to "
                "the main profile when a binding is set."
            )
        elif ids["main"]:
            reason = (
                f"llm.profile_id = '{ids['main']}' does not exist in the profile "
                "store, so the utility role cannot be derived from it."
            )
        else:
            reason = (
                "llm.profile_id and llm.utility_profile_id are both unset; utility "
                "roles (split/optimize/postprocess/dub rewrite) have no model "
                "profile to resolve."
            )
        raise ValueError(_llm_profile_guidance(reason)) from None
    return apply_env_api_key_override(profile)


def _llm_profile_guidance(reason: str) -> str:
    """Point an agent at the profile store file, keys, and field shape."""

    from videocaptioner.core.llm.profiles import DEFAULT_LLM_PROFILES_PATH

    available = list_llm_profile_ids()
    available_text = (
        f"Available profile ids: {', '.join(available)}"
        if available
        else "No profiles exist yet; create one in the GUI profile editor "
        "(翻译设置 → 方案 → 新建), or write a profile object directly."
    )
    return (
        f"{reason}\n"
        f"  Model profile store: {DEFAULT_LLM_PROFILES_PATH}\n"
        "  Reference one with llm.profile_id / llm.review_profile_id / "
        "llm.utility_profile_id in config.toml, or --llm-profile / --review-profile "
        "/ --utility-profile on the command line.\n"
        "  A profile object looks like: "
        '{"id": "...", "name": "...", "transport": "openai-compatible", '
        '"dialect": "generic", "base_url": "...", "api_key": "...", '
        '"model": "...", "work_context_tokens": 65536, "max_concurrency": 4, '
        '"openai_endpoint": "chat_completions", "request_options": {}, '
        '"max_output_tokens": null}\n'
        f"  {available_text}"
    )


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
            lines.append(f"{prefix}{key} = {mask_credential(value)}")
        else:
            lines.append(f"{prefix}{key} = {value}")
    return "\n".join(lines)
