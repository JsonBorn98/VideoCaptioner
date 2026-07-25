"""Provider-native request option validation and deterministic body merging."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .models import (
    LLMModelProfile,
    LLMTransport,
    OpenAIEndpoint,
    thaw_json_object,
)

JSONPath = tuple[str, ...]


class RequestOptionsError(ValueError):
    """Raised when profile request options conflict with application-owned fields."""


_OPENAI_CHAT_PROTECTED: tuple[JSONPath, ...] = tuple(
    (name,)
    for name in (
        "model",
        "messages",
        "stream",
        "n",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "functions",
        "function_call",
        "max_tokens",
        "max_completion_tokens",
        "response_format",
    )
)

_OPENAI_RESPONSES_PROTECTED: tuple[JSONPath, ...] = (
    *((name,) for name in (
        "model",
        "input",
        "instructions",
        "stream",
        "background",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "max_tool_calls",
        "previous_response_id",
        "conversation",
        "prompt",
        "max_output_tokens",
    )),
    ("text", "format"),
)

_ANTHROPIC_PROTECTED: tuple[JSONPath, ...] = tuple(
    (name,)
    for name in (
        "model",
        "messages",
        "system",
        "stream",
        "max_tokens",
        "tools",
        "tool_choice",
    )
)

_GEMINI_PROTECTED: tuple[JSONPath, ...] = (
    *((name,) for name in (
        "model",
        "contents",
        "systemInstruction",
        "cachedContent",
        "tools",
        "toolConfig",
    )),
    ("generationConfig", "candidateCount"),
    ("generationConfig", "maxOutputTokens"),
    ("generationConfig", "responseMimeType"),
    ("generationConfig", "responseSchema"),
)

_THINKING_BUDGET_PATHS: tuple[JSONPath, ...] = (
    ("thinking", "budget_tokens"),
    ("generationConfig", "thinkingConfig", "thinkingBudget"),
    ("extra_body", "thinking_budget"),
    ("extra_body", "thinking", "budget_tokens"),
    ("chat_template_kwargs", "thinking_budget"),
)


@dataclass(frozen=True)
class PreparedRequestOptions:
    body: dict[str, Any]
    omit_temperature: bool


def _protected_paths(profile: LLMModelProfile) -> tuple[JSONPath, ...]:
    if profile.transport is LLMTransport.OPENAI_COMPATIBLE:
        if profile.openai_endpoint is OpenAIEndpoint.RESPONSES:
            return _OPENAI_RESPONSES_PROTECTED
        return _OPENAI_CHAT_PROTECTED
    if profile.transport is LLMTransport.ANTHROPIC_MESSAGES:
        return _ANTHROPIC_PROTECTED
    if profile.transport is LLMTransport.GEMINI:
        return _GEMINI_PROTECTED
    raise RequestOptionsError(f"unsupported LLM transport: {profile.transport}")


def _temperature_path(profile: LLMModelProfile) -> JSONPath:
    if profile.transport is LLMTransport.GEMINI:
        return ("generationConfig", "temperature")
    return ("temperature",)


def _path_value(value: Mapping[str, Any], path: Sequence[str]) -> tuple[bool, Any]:
    current: Any = value
    for name in path:
        if not isinstance(current, Mapping) or name not in current:
            return False, None
        current = current[name]
    return True, current


def _set_path(value: dict[str, Any], path: Sequence[str], item: Any) -> None:
    current = value
    for name in path[:-1]:
        child = current.get(name)
        if not isinstance(child, dict):
            child = {}
            current[name] = child
        current = child
    current[path[-1]] = deepcopy(item)


def _delete_path(value: dict[str, Any], path: Sequence[str]) -> None:
    current: Any = value
    for name in path[:-1]:
        if not isinstance(current, dict):
            return
        current = current.get(name)
    if isinstance(current, dict):
        current.pop(path[-1], None)


def prepare_profile_request_options(profile: LLMModelProfile) -> PreparedRequestOptions:
    """Validate and thaw a profile's options for one adapter call."""

    options = thaw_json_object(profile.request_options)
    omit_value = options.pop("$omit", [])
    if not isinstance(omit_value, list) or any(
        not isinstance(item, str) for item in omit_value
    ):
        raise RequestOptionsError("request_options.$omit must be an array of strings")
    unsupported = sorted(set(omit_value) - {"temperature"})
    if unsupported:
        raise RequestOptionsError(
            "request_options.$omit only supports 'temperature'; unsupported: "
            + ", ".join(unsupported)
        )

    if (
        profile.transport is LLMTransport.OPENAI_COMPATIBLE
        and profile.openai_endpoint is OpenAIEndpoint.RESPONSES
        and "text" in options
        and not isinstance(options["text"], Mapping)
    ):
        raise RequestOptionsError("request_options.text must be an object")
    if (
        profile.transport is LLMTransport.GEMINI
        and "generationConfig" in options
        and not isinstance(options["generationConfig"], Mapping)
    ):
        raise RequestOptionsError("request_options.generationConfig must be an object")

    for path in _protected_paths(profile):
        exists, _ = _path_value(options, path)
        if exists:
            raise RequestOptionsError(
                "request_options."
                + ".".join(path)
                + " is application-controlled and cannot be overridden"
            )

    omit_temperature = "temperature" in omit_value
    if omit_temperature:
        temperature_path = _temperature_path(profile)
        exists, _ = _path_value(options, temperature_path)
        if exists:
            raise RequestOptionsError(
                "request_options.$omit removes temperature, but request_options."
                + ".".join(temperature_path)
                + " also provides it"
            )
    return PreparedRequestOptions(options, omit_temperature)


def validate_profile_request_options(profile: LLMModelProfile) -> None:
    """Raise a clear error for protected paths or invalid local control keys."""

    prepare_profile_request_options(profile)


def merge_profile_request_options(
    profile: LLMModelProfile,
    application_body: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply ``$omit`` and a top-level shallow patch, then restore protected paths."""

    prepared = prepare_profile_request_options(profile)
    result = deepcopy(dict(application_body))
    protected_values: list[tuple[JSONPath, Any]] = []
    for path in _protected_paths(profile):
        exists, item = _path_value(result, path)
        if exists:
            protected_values.append((path, deepcopy(item)))

    if prepared.omit_temperature:
        _delete_path(result, _temperature_path(profile))
    result.update(deepcopy(prepared.body))

    for path, item in protected_values:
        _set_path(result, path, item)
    for path, expected in protected_values:
        exists, actual = _path_value(result, path)
        if not exists or actual != expected:  # pragma: no cover - defensive invariant
            raise AssertionError(f"failed to restore protected request path: {'.'.join(path)}")
    return result


def known_thinking_budget(options: Mapping[str, Any]) -> int | None:
    """Return the largest positive integer from the documented budget paths."""

    budgets: list[int] = []
    for path in _THINKING_BUDGET_PATHS:
        exists, value = _path_value(options, path)
        if exists and type(value) in {int, float} and value > 0 and float(value).is_integer():
            budgets.append(int(value))
    return max(budgets, default=None)


__all__ = [
    "RequestOptionsError",
    "known_thinking_budget",
    "merge_profile_request_options",
    "prepare_profile_request_options",
    "validate_profile_request_options",
]
