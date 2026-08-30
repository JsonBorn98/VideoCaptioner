"""profile command — inspect the LLM model profile store.

The store is the single source of LLM connections and credentials (ADR-0015).
``show`` masks the api_key so terminal output never leaks a credential; the
store file itself stays directly readable for agents that need the raw key.
"""

from argparse import Namespace

from videocaptioner.cli import exit_codes as EXIT
from videocaptioner.cli import output
from videocaptioner.cli.config import _profile_store, mask_credential, save_config_value


def run(args: Namespace, config: dict) -> int:
    del config  # The profile store is read directly; kept for the run(args, config) shape.
    action = getattr(args, "profile_action", None)

    if action == "list":
        return _list()
    if action == "show":
        return _show(args.id)
    if action == "set-default":
        return _set_default(args.id)

    output.error("No action specified. Use: videocaptioner profile <list|show|set-default>")
    return EXIT.USAGE_ERROR


def _list() -> int:
    profiles = _profile_store().list()
    if not profiles:
        from videocaptioner.core.llm.profiles import DEFAULT_LLM_PROFILES_PATH

        output.error("No model profiles found.")
        output.hint(f"Profile store: {DEFAULT_LLM_PROFILES_PATH}")
        output.hint("Create a profile in the GUI (翻译设置 → 方案 → 新建).")
        return EXIT.GENERAL_ERROR

    print("Available model profiles:\n")
    print(f"  {'ID':<24} {'NAME':<20} {'MODEL'}")
    separator = "─"
    print(f"  {separator * 24} {separator * 20} {separator * 32}")
    for profile in profiles:
        print(f"  {profile.profile_id:<24} {profile.name:<20} {profile.model}")
    print("\nUsage:")
    print("  videocaptioner profile show <id>")
    print("  videocaptioner profile set-default <id>")
    print("  videocaptioner config set llm.profile_id <id>")
    return EXIT.SUCCESS


def _show(profile_id: str) -> int:
    from videocaptioner.core.llm.profiles import (
        DEFAULT_LLM_PROFILES_PATH,
        LLMProfileNotFoundError,
    )

    try:
        profile = _profile_store().get(profile_id)
    except LLMProfileNotFoundError:
        output.error(f"Model profile '{profile_id}' does not exist.")
        _hint_available_ids()
        return EXIT.GENERAL_ERROR

    values = profile.to_dict()
    print(f"Profile '{values['id']}':\n")
    for key in sorted(values):
        value = values[key]
        if key == "api_key":
            value = mask_credential(str(value)) if str(value) else "(empty)"
        print(f"  {key}: {value}")
    print(f"\nRaw profile file (api_key in clear text): {DEFAULT_LLM_PROFILES_PATH}")
    return EXIT.SUCCESS


def _set_default(profile_id: str) -> int:
    from videocaptioner.core.llm.profiles import LLMProfileNotFoundError

    try:
        _profile_store().get(profile_id)
    except LLMProfileNotFoundError:
        output.error(f"Model profile '{profile_id}' does not exist.")
        _hint_available_ids()
        return EXIT.GENERAL_ERROR

    try:
        save_config_value("llm.profile_id", profile_id)
    except ValueError as exc:
        output.error(str(exc))
        return EXIT.GENERAL_ERROR
    output.success(f"llm.profile_id = {profile_id}")
    output.hint("This selects the main translation profile (and the utility derivation source).")
    return EXIT.SUCCESS


def _hint_available_ids() -> None:
    profiles = _profile_store().list()
    if profiles:
        output.hint(
            "Available profile ids: " + ", ".join(item.profile_id for item in profiles)
        )
        return
    from videocaptioner.core.llm.profiles import DEFAULT_LLM_PROFILES_PATH

    output.hint("The profile store is empty.")
    output.hint(f"Profile store: {DEFAULT_LLM_PROFILES_PATH}")
    output.hint("Create a profile in the GUI (翻译设置 → 方案 → 新建).")


__all__ = ["run"]
