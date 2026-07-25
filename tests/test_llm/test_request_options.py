from __future__ import annotations

import pytest

from videocaptioner.core.llm.models import (
    LLMModelProfile,
    LLMTransport,
    OpenAIEndpoint,
    ProviderDialect,
)
from videocaptioner.core.llm.request_options import (
    RequestOptionsError,
    merge_profile_request_options,
    validate_profile_request_options,
)


def _nested(path: tuple[str, ...], value=True):
    result = value
    for name in reversed(path):
        result = {name: result}
    return result


def _profile(
    transport: LLMTransport,
    *,
    endpoint: OpenAIEndpoint = OpenAIEndpoint.CHAT_COMPLETIONS,
    options=None,
) -> LLMModelProfile:
    dialect = {
        LLMTransport.OPENAI_COMPATIBLE: ProviderDialect.OPENAI,
        LLMTransport.ANTHROPIC_MESSAGES: ProviderDialect.ANTHROPIC,
        LLMTransport.GEMINI: ProviderDialect.GEMINI,
    }[transport]
    return LLMModelProfile(
        profile_id="request-options-test",
        name="Request options test",
        transport=transport,
        dialect=dialect,
        base_url="https://example.test/v1",
        api_key="secret",
        model="model",
        openai_endpoint=endpoint,
        request_options=options or {},
    )


@pytest.mark.parametrize(
    ("transport", "endpoint", "path"),
    [
        *(
            (
                LLMTransport.OPENAI_COMPATIBLE,
                OpenAIEndpoint.CHAT_COMPLETIONS,
                (name,),
            )
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
        ),
        *(
            (LLMTransport.OPENAI_COMPATIBLE, OpenAIEndpoint.RESPONSES, path)
            for path in (
                ("model",),
                ("input",),
                ("instructions",),
                ("stream",),
                ("background",),
                ("tools",),
                ("tool_choice",),
                ("parallel_tool_calls",),
                ("max_tool_calls",),
                ("previous_response_id",),
                ("conversation",),
                ("prompt",),
                ("max_output_tokens",),
                ("text", "format"),
            )
        ),
        *(
            (LLMTransport.ANTHROPIC_MESSAGES, OpenAIEndpoint.CHAT_COMPLETIONS, (name,))
            for name in (
                "model",
                "messages",
                "system",
                "stream",
                "max_tokens",
                "tools",
                "tool_choice",
            )
        ),
        *(
            (LLMTransport.GEMINI, OpenAIEndpoint.CHAT_COMPLETIONS, path)
            for path in (
                ("model",),
                ("contents",),
                ("systemInstruction",),
                ("cachedContent",),
                ("tools",),
                ("toolConfig",),
                ("generationConfig", "candidateCount"),
                ("generationConfig", "maxOutputTokens"),
                ("generationConfig", "responseMimeType"),
                ("generationConfig", "responseSchema"),
            )
        ),
    ],
)
def test_every_documented_protected_path_is_rejected(transport, endpoint, path):
    profile = _profile(transport, endpoint=endpoint, options=_nested(path))

    with pytest.raises(RequestOptionsError, match=r"application-controlled") as caught:
        validate_profile_request_options(profile)

    assert ".".join(path) in str(caught.value)


@pytest.mark.parametrize(
    ("transport", "endpoint", "options"),
    [
        (
            LLMTransport.OPENAI_COMPATIBLE,
            OpenAIEndpoint.CHAT_COMPLETIONS,
            {"metadata": {"model": "nested"}, "text": {"format": "ordinary"}},
        ),
        (
            LLMTransport.OPENAI_COMPATIBLE,
            OpenAIEndpoint.RESPONSES,
            {"metadata": {"input": "nested"}, "text": {"verbosity": "low"}},
        ),
        (
            LLMTransport.ANTHROPIC_MESSAGES,
            OpenAIEndpoint.CHAT_COMPLETIONS,
            {"thinking": {"system": "nested"}},
        ),
        (
            LLMTransport.GEMINI,
            OpenAIEndpoint.CHAT_COMPLETIONS,
            {
                "labels": {"contents": "nested"},
                "generationConfig": {
                    "temperature": 0.4,
                    "thinkingConfig": {"thinkingBudget": 1024},
                },
            },
        ),
    ],
)
def test_protection_is_exact_path_not_recursive_name_matching(transport, endpoint, options):
    validate_profile_request_options(_profile(transport, endpoint=endpoint, options=options))


def test_responses_shallow_merge_restores_only_application_text_format():
    profile = _profile(
        LLMTransport.OPENAI_COMPATIBLE,
        endpoint=OpenAIEndpoint.RESPONSES,
        options={"text": {"verbosity": "low"}, "unknown": {"nested": [1, 2]}},
    )
    application = {
        "model": "model",
        "input": [],
        "text": {"format": {"type": "json_object"}, "old": "discarded"},
    }

    merged = merge_profile_request_options(profile, application)

    assert merged["text"] == {
        "verbosity": "low",
        "format": {"type": "json_object"},
    }
    assert merged["unknown"] == {"nested": [1, 2]}


@pytest.mark.parametrize(
    "options",
    [
        {"$omit": "temperature"},
        {"$omit": ["top_p"]},
        {"$omit": ["temperature"], "temperature": 0.1},
    ],
)
def test_omit_rejects_invalid_and_self_contradictory_configuration(options):
    profile = _profile(LLMTransport.OPENAI_COMPATIBLE, options=options)

    with pytest.raises(RequestOptionsError):
        validate_profile_request_options(profile)


def test_gemini_omit_temperature_uses_native_nested_path():
    profile = _profile(
        LLMTransport.GEMINI,
        options={"$omit": ["temperature"], "generationConfig": {"topP": 0.7}},
    )

    merged = merge_profile_request_options(
        profile,
        {"generationConfig": {"candidateCount": 1, "temperature": 0.2}},
    )

    assert merged == {"generationConfig": {"topP": 0.7, "candidateCount": 1}}


def test_literal_extra_body_is_not_treated_as_an_sdk_control_layer():
    profile = _profile(
        LLMTransport.OPENAI_COMPATIBLE,
        options={"extra_body": {"enable_thinking": True}},
    )

    merged = merge_profile_request_options(profile, {"model": "model"})

    assert merged["extra_body"] == {"enable_thinking": True}
