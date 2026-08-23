import json
from types import SimpleNamespace

import httpx
import openai
import pytest

from videocaptioner.core.llm.adapters import (
    AnthropicMessagesAdapter,
    GeminiAdapter,
    OpenAICompatibleAdapter,
)
from videocaptioner.core.llm.models import (
    LLMCallError,
    LLMErrorCategory,
    LLMMessage,
    LLMModelProfile,
    LLMRequest,
    LLMTransport,
    OpenAIEndpoint,
    ProviderDialect,
)


def _profile(
    transport: LLMTransport,
    dialect: ProviderDialect,
    *,
    base_url: str,
    model: str = "test-model",
    openai_endpoint: OpenAIEndpoint = OpenAIEndpoint.CHAT_COMPLETIONS,
    request_options=None,
    max_output_tokens=None,
) -> LLMModelProfile:
    return LLMModelProfile(
        profile_id=f"test-{transport.value}",
        name=f"Test {transport.value}",
        transport=transport,
        dialect=dialect,
        base_url=base_url,
        api_key="test-key",
        model=model,
        openai_endpoint=openai_endpoint,
        request_options=request_options or {},
        max_output_tokens=max_output_tokens,
    )


def _request() -> LLMRequest:
    return LLMRequest(
        messages=(
            LLMMessage("system", "System rules"),
            LLMMessage("user", "Translate this"),
            LLMMessage("assistant", "Prior answer"),
        ),
        temperature=0.25,
        max_output_tokens=321,
        response_schema={
            "type": "object",
            "properties": {"translation": {"type": "string"}},
            "required": ["translation"],
        },
    )


class _OpenAICompletions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=' {"translation":"ok"} '))],
            usage=SimpleNamespace(
                prompt_tokens=100,
                completion_tokens=20,
                prompt_tokens_details=SimpleNamespace(cached_tokens=60),
                completion_tokens_details=SimpleNamespace(reasoning_tokens=7),
                cache_creation_input_tokens=11,
            ),
        )


def test_openai_compatible_maps_request_and_usage():
    completions = _OpenAICompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    adapter = OpenAICompatibleAdapter(
        _profile(
            LLMTransport.OPENAI_COMPATIBLE,
            ProviderDialect.OPENAI,
            base_url="https://api.openai.test/v1",
        ),
        client=client,
    )

    result = adapter.complete(_request())

    assert completions.kwargs["model"] == "test-model"
    assert completions.kwargs["messages"] == [
        {"role": "system", "content": "System rules"},
        {"role": "user", "content": "Translate this"},
        {"role": "assistant", "content": "Prior answer"},
    ]
    assert completions.kwargs["stream"] is False
    assert completions.kwargs["n"] == 1
    assert completions.kwargs["max_completion_tokens"] == 321
    assert completions.kwargs["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "structured_response",
            "strict": True,
            "schema": dict(_request().response_schema),
        },
    }
    assert completions.kwargs["extra_body"] == {
        "store": False,
    }
    assert result.text == '{"translation":"ok"}'
    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 20
    assert result.usage.cache_read_tokens == 60
    assert result.usage.cache_write_tokens == 11
    assert result.usage.reasoning_tokens == 7


def test_generic_openai_compatible_preserves_deepseek_cache_hit_usage():
    class DeepSeekCompletions:
        def create(self, **_kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
                usage=SimpleNamespace(
                    prompt_tokens=100,
                    completion_tokens=5,
                    prompt_tokens_details=None,
                    prompt_cache_hit_tokens=72,
                ),
            )

    adapter = OpenAICompatibleAdapter(
        _profile(
            LLMTransport.OPENAI_COMPATIBLE,
            ProviderDialect.GENERIC,
            base_url="https://api.deepseek.test/v1",
        ),
        client=SimpleNamespace(chat=SimpleNamespace(completions=DeepSeekCompletions())),
    )

    result = adapter.complete(_request())

    assert result.usage.cache_read_tokens == 72
    assert result.usage.cache_write_tokens is None


class _ToolCallCompletions:
    """A provider that honours a forced function call, as GLM and Kimi do."""

    def __init__(self, arguments):
        self.arguments = arguments
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                function=SimpleNamespace(
                                    name="structured_response",
                                    arguments=self.arguments,
                                )
                            )
                        ],
                    ),
                    finish_reason="tool_calls",
                )
            ],
            usage=None,
        )


def _chat_adapter(dialect: ProviderDialect, completions) -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(
        _profile(
            LLMTransport.OPENAI_COMPATIBLE,
            dialect,
            base_url="https://gateway.test/v1",
        ),
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
    )


@pytest.mark.parametrize(
    "dialect",
    [
        ProviderDialect.GLM,
        ProviderDialect.KIMI,
        ProviderDialect.DEEPSEEK,
        ProviderDialect.ANTHROPIC,
    ],
)
def test_chat_dialects_without_native_schema_force_a_structured_tool_call(dialect):
    completions = _ToolCallCompletions('{"translation":"ok"}')

    result = _chat_adapter(dialect, completions).complete(_request())

    assert "response_format" not in completions.kwargs
    assert completions.kwargs["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "structured_response",
                "description": "Return the requested structured response.",
                "parameters": dict(_request().response_schema),
            },
        }
    ]
    assert completions.kwargs["tool_choice"] == {
        "type": "function",
        "function": {"name": "structured_response"},
    }
    assert result.text == '{"translation":"ok"}'


def test_structured_tool_call_accepts_proxy_decoded_arguments():
    completions = _ToolCallCompletions({"translation": "ok"})

    result = _chat_adapter(ProviderDialect.GLM, completions).complete(_request())

    assert json.loads(result.text) == {"translation": "ok"}


def test_ignored_tool_choice_degrades_to_message_content():
    class IgnoresToolChoice:
        def create(self, **_kwargs):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=' {"translation":"ok"} ',
                            tool_calls=None,
                        ),
                        finish_reason="stop",
                    )
                ],
                usage=None,
            )

    result = _chat_adapter(ProviderDialect.GLM, IgnoresToolChoice()).complete(_request())

    assert result.text == '{"translation":"ok"}'


def test_chat_unidentified_dialect_keeps_portable_json_mode():
    completions = _OpenAICompletions()

    result = _chat_adapter(ProviderDialect.GENERIC, completions).complete(_request())

    assert completions.kwargs["response_format"] == {"type": "json_object"}
    assert "tools" not in completions.kwargs
    assert "tool_choice" not in completions.kwargs
    assert result.text == '{"translation":"ok"}'


def test_chat_without_response_schema_sends_no_structured_controls():
    completions = _OpenAICompletions()

    _chat_adapter(ProviderDialect.GLM, completions).complete(
        LLMRequest(messages=(LLMMessage("user", "Hi"),), max_output_tokens=32)
    )

    assert "response_format" not in completions.kwargs
    assert "tools" not in completions.kwargs
    assert "tool_choice" not in completions.kwargs


class _RejectsForcedTool:
    def __init__(self, message: str):
        self.message = message
        self.bodies = []

    def create(self, **kwargs):
        self.bodies.append(kwargs)
        if "tools" in kwargs:
            request = httpx.Request("POST", "https://gateway.test/v1/chat/completions")
            raise openai.BadRequestError(
                self.message,
                response=httpx.Response(400, request=request),
                body=None,
            )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"translation":"ok"}'),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )


def test_forced_structured_tool_falls_back_to_json_mode_when_rejected():
    completions = _RejectsForcedTool("tool_choice is not supported by this model")

    result = _chat_adapter(ProviderDialect.DEEPSEEK, completions).complete(_request())

    assert len(completions.bodies) == 2
    assert "tools" in completions.bodies[0]
    assert "tools" not in completions.bodies[1]
    assert completions.bodies[1]["response_format"] == {"type": "json_object"}
    assert result.text == '{"translation":"ok"}'


def test_tool_capability_fallback_never_masks_context_overflow():
    completions = _RejectsForcedTool("maximum context length exceeded")

    with pytest.raises(LLMCallError) as excinfo:
        _chat_adapter(ProviderDialect.GLM, completions).complete(_request())

    assert excinfo.value.category is LLMErrorCategory.CONTEXT_LIMIT
    assert len(completions.bodies) == 1


class _OpenAIResponses:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def test_openai_responses_maps_standard_body_text_and_usage():
    responses = _OpenAIResponses(
        SimpleNamespace(
            status="completed",
            output=[
                SimpleNamespace(type="reasoning", content=[]),
                SimpleNamespace(
                    type="message",
                    content=[
                        SimpleNamespace(type="output_text", text=' {"translation":'),
                        SimpleNamespace(type="output_text", text='"ok"} '),
                    ],
                ),
                SimpleNamespace(
                    type="message",
                    content=[SimpleNamespace(type="output_text", text="")],
                ),
            ],
            usage=SimpleNamespace(
                input_tokens=90,
                output_tokens=30,
                input_tokens_details=SimpleNamespace(cached_tokens=55),
                output_tokens_details=SimpleNamespace(reasoning_tokens=12),
            ),
        )
    )
    profile = _profile(
        LLMTransport.OPENAI_COMPATIBLE,
        ProviderDialect.OPENAI,
        base_url="https://api.openai.test/v1",
        openai_endpoint=OpenAIEndpoint.RESPONSES,
        request_options={
            "reasoning": {"effort": "high"},
            "text": {"verbosity": "low"},
            "store": True,
            "extra_body": {"enable_thinking": True},
        },
    )
    client = SimpleNamespace(responses=responses)

    result = OpenAICompatibleAdapter(profile, client=client).complete(_request())

    assert responses.kwargs["model"] == "test-model"
    assert responses.kwargs["input"] == [
        {
            "role": "system",
            "content": [{"type": "input_text", "text": "System rules"}],
        },
        {
            "role": "user",
            "content": [{"type": "input_text", "text": "Translate this"}],
        },
        {
            "role": "assistant",
            "content": [{"type": "input_text", "text": "Prior answer"}],
        },
    ]
    assert responses.kwargs["stream"] is False
    assert responses.kwargs["background"] is False
    assert responses.kwargs["max_output_tokens"] == 321
    assert responses.kwargs["extra_body"] == {
        "store": True,
        "reasoning": {"effort": "high"},
        "text": {
            "verbosity": "low",
            "format": {
                "type": "json_schema",
                "name": "structured_response",
                "strict": True,
                "schema": dict(_request().response_schema),
            },
        },
        "extra_body": {"enable_thinking": True},
    }
    assert result.text == '{"translation":"ok"}'
    assert result.usage.input_tokens == 90
    assert result.usage.output_tokens == 30
    assert result.usage.cache_read_tokens == 55
    assert result.usage.reasoning_tokens == 12


@pytest.mark.parametrize(
    ("response", "message", "retryable"),
    [
        (
            SimpleNamespace(status="incomplete", output=[]),
            "non-completed status: incomplete",
            True,
        ),
        (
            SimpleNamespace(
                status="completed",
                output=[
                    SimpleNamespace(
                        type="message",
                        content=[SimpleNamespace(type="refusal", refusal="no")],
                    )
                ],
            ),
            "refused the request",
            False,
        ),
        (
            SimpleNamespace(
                status="completed",
                output=[SimpleNamespace(type="reasoning", content=[])],
            ),
            "no final output_text",
            True,
        ),
    ],
)
def test_openai_responses_rejects_non_final_results(response, message, retryable):
    profile = _profile(
        LLMTransport.OPENAI_COMPATIBLE,
        ProviderDialect.OPENAI,
        base_url="https://api.openai.test/v1",
        openai_endpoint=OpenAIEndpoint.RESPONSES,
    )
    adapter = OpenAICompatibleAdapter(
        profile,
        client=SimpleNamespace(responses=_OpenAIResponses(response)),
    )

    with pytest.raises(LLMCallError, match=message) as caught:
        adapter.complete(_request())

    assert caught.value.category is LLMErrorCategory.INVALID_RESPONSE
    assert caught.value.retryable is retryable


def test_openai_chat_empty_completion_preserves_safe_diagnostics():
    class EmptyCompletions:
        def create(self, **_kwargs):
            return SimpleNamespace(
                status="completed",
                choices=[
                    SimpleNamespace(
                        finish_reason="length",
                        message=SimpleNamespace(content=None, refusal=None),
                    )
                ],
                usage=SimpleNamespace(
                    prompt_tokens=3000,
                    completion_tokens=8192,
                    completion_tokens_details=SimpleNamespace(reasoning_tokens=8192),
                ),
            )

    adapter = OpenAICompatibleAdapter(
        _profile(
            LLMTransport.OPENAI_COMPATIBLE,
            ProviderDialect.OPENAI,
            base_url="https://api.openai.test/v1",
        ),
        client=SimpleNamespace(
            chat=SimpleNamespace(completions=EmptyCompletions())
        ),
    )

    with pytest.raises(LLMCallError, match="output limit") as caught:
        adapter.complete(_request())

    error = caught.value
    assert error.category is LLMErrorCategory.INVALID_RESPONSE
    assert error.retryable is True
    assert error.finish_reason == "length"
    assert error.response_status == "completed"
    assert error.choice_count == 1
    assert error.usage.input_tokens == 3000
    assert error.usage.output_tokens == 8192
    assert error.usage.reasoning_tokens == 8192


def test_openai_chat_rejects_nonempty_truncated_content():
    class TruncatedCompletions:
        def create(self, **_kwargs):
            return SimpleNamespace(
                status="completed",
                choices=[
                    SimpleNamespace(
                        finish_reason="length",
                        message=SimpleNamespace(content='{"translation":', refusal=None),
                    )
                ],
            )

    adapter = OpenAICompatibleAdapter(
        _profile(
            LLMTransport.OPENAI_COMPATIBLE,
            ProviderDialect.OPENAI,
            base_url="https://api.openai.test/v1",
        ),
        client=SimpleNamespace(
            chat=SimpleNamespace(completions=TruncatedCompletions())
        ),
    )

    with pytest.raises(LLMCallError, match="output limit") as caught:
        adapter.complete(_request())

    assert caught.value.finish_reason == "length"
    assert caught.value.response_status == "completed"


def test_openai_chat_accepts_legacy_temperature_omit_and_passes_unknown_options():
    completions = _OpenAICompletions()
    profile = _profile(
        LLMTransport.OPENAI_COMPATIBLE,
        ProviderDialect.OPENAI,
        base_url="https://api.openai.test/v1",
        request_options={
            "$omit": ["temperature"],
            "reasoning_effort": "high",
            "extra_body": {"enable_thinking": True},
            "metadata": {"model": "ordinary nested value"},
        },
        max_output_tokens=777,
    )
    adapter = OpenAICompatibleAdapter(
        profile,
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
    )

    adapter.complete(_request())

    assert completions.kwargs["max_completion_tokens"] == 777
    assert completions.kwargs["extra_body"] == {
        "store": False,
        "reasoning_effort": "high",
        "extra_body": {"enable_thinking": True},
        "metadata": {"model": "ordinary nested value"},
    }


def test_request_options_override_is_request_scoped_and_preserves_profile():
    completions = _OpenAICompletions()
    profile = _profile(
        LLMTransport.OPENAI_COMPATIBLE,
        ProviderDialect.OPENAI,
        base_url="https://api.openai.test/v1",
        request_options={
            "reasoning_effort": "high",
            "metadata": {"mode": "profile"},
        },
    )
    adapter = OpenAICompatibleAdapter(
        profile,
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
    )
    base_request = _request()
    request = LLMRequest(
        messages=base_request.messages,
        max_output_tokens=base_request.max_output_tokens,
        request_options_override={
            "reasoning_effort": "low",
            "metadata": {"mode": "adaptive-retry"},
        },
        response_schema=base_request.response_schema,
    )

    adapter.complete(request)

    assert completions.kwargs["extra_body"]["reasoning_effort"] == "low"
    assert completions.kwargs["extra_body"]["metadata"] == {
        "mode": "adaptive-retry"
    }
    assert profile.request_options["reasoning_effort"] == "high"
    assert profile.request_options["metadata"]["mode"] == "profile"


def test_adapter_reports_protected_request_option_as_non_retryable_configuration():
    profile = _profile(
        LLMTransport.OPENAI_COMPATIBLE,
        ProviderDialect.OPENAI,
        base_url="https://api.openai.test/v1",
        request_options={"messages": []},
    )
    adapter = OpenAICompatibleAdapter(
        profile,
        client=SimpleNamespace(chat=SimpleNamespace(completions=_OpenAICompletions())),
    )

    with pytest.raises(LLMCallError, match=r"request_options\.messages") as caught:
        adapter.complete(_request())

    assert caught.value.category is LLMErrorCategory.CONFIGURATION
    assert caught.value.retryable is False


def test_openai_sdk_extra_body_produces_the_expected_final_http_json():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    sdk_client = openai.OpenAI(
        api_key="test-key",
        base_url="https://api.openai.test/v1",
        http_client=http_client,
    )
    profile = _profile(
        LLMTransport.OPENAI_COMPATIBLE,
        ProviderDialect.OPENAI,
        base_url="https://api.openai.test/v1",
        request_options={
            "reasoning_effort": "high",
            "extra_body": {"enable_thinking": True},
        },
    )

    try:
        OpenAICompatibleAdapter(profile, client=sdk_client).complete(
            LLMRequest(messages=(LLMMessage("user", "hello"),))
        )
    finally:
        sdk_client.close()

    assert captured["reasoning_effort"] == "high"
    assert captured["extra_body"] == {"enable_thinking": True}
    assert captured["store"] is False
    assert captured["stream"] is False
    assert "temperature" not in captured


class _Response:
    def __init__(self, value):
        self._value = value
        self.ok = True
        self.status_code = 200
        self.text = ""

    def json(self):
        return self._value


class _Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_anthropic_maps_request_cache_hint_and_usage():
    session = _Session(
        _Response(
            {
                "content": [{"type": "text", "text": " translated "}],
                "usage": {
                    "input_tokens": 80,
                    "output_tokens": 12,
                    "cache_read_input_tokens": 50,
                    "cache_creation_input_tokens": 9,
                },
            }
        )
    )
    adapter = AnthropicMessagesAdapter(
        _profile(
            LLMTransport.ANTHROPIC_MESSAGES,
            ProviderDialect.ANTHROPIC,
            base_url="https://api.anthropic.test/v1",
        ),
        session=session,
        timeout=17,
    )

    result = adapter.complete(_request())

    url, kwargs = session.calls[0]
    assert url == "https://api.anthropic.test/v1/messages"
    assert kwargs["headers"]["x-api-key"] == "test-key"
    assert kwargs["timeout"] == 17
    assert kwargs["json"] == {
        "model": "test-model",
        "system": [
            {
                "type": "text",
                "text": "System rules",
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [
            {"role": "user", "content": "Translate this"},
            {"role": "assistant", "content": "Prior answer"},
        ],
        "stream": False,
        "max_tokens": 321,
        "tools": [
            {
                "name": "structured_response",
                "description": "Return the requested structured response.",
                "input_schema": {
                    "type": "object",
                    "properties": {"translation": {"type": "string"}},
                    "required": ["translation"],
                },
            }
        ],
        "tool_choice": {"type": "tool", "name": "structured_response"},
    }
    assert result.text == "translated"
    assert result.usage.input_tokens == 80
    assert result.usage.output_tokens == 12
    assert result.usage.cache_read_tokens == 50
    assert result.usage.cache_write_tokens == 9


def test_anthropic_empty_completion_preserves_stop_reason_and_usage():
    adapter = AnthropicMessagesAdapter(
        _profile(
            LLMTransport.ANTHROPIC_MESSAGES,
            ProviderDialect.ANTHROPIC,
            base_url="https://api.anthropic.test/v1",
        ),
        session=_Session(
            _Response(
                {
                    "content": [],
                    "stop_reason": "max_tokens",
                    "usage": {"input_tokens": 3000, "output_tokens": 8192},
                }
            )
        ),
    )

    with pytest.raises(LLMCallError, match="output limit") as caught:
        adapter.complete(_request())

    error = caught.value
    assert error.retryable is True
    assert error.finish_reason == "max_tokens"
    assert error.response_status == "truncated"
    assert error.choice_count == 0
    assert error.usage.input_tokens == 3000
    assert error.usage.output_tokens == 8192


def test_anthropic_rejects_nonempty_truncated_content():
    session = _Session(
        _Response(
            {
                "content": [{"type": "text", "text": '{"translation":'}],
                "stop_reason": "max_tokens",
                "usage": {"input_tokens": 100, "output_tokens": 200},
            }
        )
    )
    adapter = AnthropicMessagesAdapter(
        _profile(
            LLMTransport.ANTHROPIC_MESSAGES,
            ProviderDialect.ANTHROPIC,
            base_url="https://api.anthropic.test/v1",
        ),
        session=session,
    )

    with pytest.raises(LLMCallError, match="output limit") as caught:
        adapter.complete(
            LLMRequest(messages=_request().messages, max_output_tokens=321)
        )

    assert caught.value.finish_reason == "max_tokens"
    assert caught.value.response_status == "truncated"


def test_anthropic_applies_native_thinking_options_with_legacy_temperature_omit():
    session = _Session(_Response({"content": [{"type": "text", "text": "ok"}]}))
    adapter = AnthropicMessagesAdapter(
        _profile(
            LLMTransport.ANTHROPIC_MESSAGES,
            ProviderDialect.ANTHROPIC,
            base_url="https://api.anthropic.test/v1",
            request_options={
                "$omit": ["temperature"],
                "thinking": {"type": "enabled", "budget_tokens": 2048},
                "output_config": {"effort": "high"},
            },
            max_output_tokens=900,
        ),
        session=session,
    )

    adapter.complete(
        LLMRequest(
            messages=_request().messages,
            max_output_tokens=_request().max_output_tokens,
        )
    )

    payload = session.calls[0][1]["json"]
    assert "temperature" not in payload
    assert payload["max_tokens"] == 900
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 2048}
    assert payload["output_config"] == {"effort": "high"}


def test_anthropic_rejects_manual_thinking_with_forced_structured_tool_before_http():
    session = _Session(_Response({"content": [{"type": "text", "text": "unused"}]}))
    adapter = AnthropicMessagesAdapter(
        _profile(
            LLMTransport.ANTHROPIC_MESSAGES,
            ProviderDialect.ANTHROPIC,
            base_url="https://api.anthropic.test/v1",
            request_options={
                "thinking": {"type": "enabled", "budget_tokens": 2048},
            },
        ),
        session=session,
    )

    with pytest.raises(LLMCallError) as caught:
        adapter.complete(_request())

    assert caught.value.category is LLMErrorCategory.CONFIGURATION
    assert caught.value.retryable is False
    assert "force tool_choice" in str(caught.value)
    assert session.calls == []


def test_anthropic_request_override_cannot_bypass_structured_thinking_validation():
    session = _Session(_Response({"content": [{"type": "text", "text": "unused"}]}))
    adapter = AnthropicMessagesAdapter(
        _profile(
            LLMTransport.ANTHROPIC_MESSAGES,
            ProviderDialect.ANTHROPIC,
            base_url="https://api.anthropic.test/v1",
        ),
        session=session,
    )
    base_request = _request()
    request = LLMRequest(
        messages=base_request.messages,
        max_output_tokens=base_request.max_output_tokens,
        response_schema=base_request.response_schema,
        request_options_override={
            "thinking": {"type": "enabled", "budget_tokens": 2048}
        },
    )

    with pytest.raises(LLMCallError) as caught:
        adapter.complete(request)

    assert caught.value.category is LLMErrorCategory.CONFIGURATION
    assert "force tool_choice" in str(caught.value)
    assert session.calls == []


def test_gemini_maps_request_schema_and_usage():
    session = _Session(
        _Response(
            {
                "candidates": [
                    {"content": {"parts": [{"text": " translated "}]}}
                ],
                "usageMetadata": {
                    "promptTokenCount": 70,
                    "candidatesTokenCount": 10,
                    "cachedContentTokenCount": 40,
                },
            }
        )
    )
    adapter = GeminiAdapter(
        _profile(
            LLMTransport.GEMINI,
            ProviderDialect.GEMINI,
            base_url="https://generativelanguage.test/v1beta",
            model="gemini/test model",
        ),
        session=session,
        timeout=19,
    )

    result = adapter.complete(_request())

    url, kwargs = session.calls[0]
    assert url == (
        "https://generativelanguage.test/v1beta/models/"
        "gemini%2Ftest%20model:generateContent"
    )
    assert kwargs["params"] == {"key": "test-key"}
    assert kwargs["timeout"] == 19
    assert kwargs["json"] == {
        "contents": [
            {"role": "user", "parts": [{"text": "Translate this"}]},
            {"role": "model", "parts": [{"text": "Prior answer"}]},
        ],
        "generationConfig": {
            "candidateCount": 1,
            "maxOutputTokens": 321,
            "responseMimeType": "application/json",
            "responseSchema": dict(_request().response_schema),
        },
        "systemInstruction": {"parts": [{"text": "System rules"}]},
    }
    assert result.text == "translated"
    assert result.usage.input_tokens == 70
    assert result.usage.output_tokens == 10
    assert result.usage.cache_read_tokens == 40
    assert result.usage.cache_write_tokens is None


@pytest.mark.parametrize(
    ("body", "retryable", "finish_reason", "response_status", "message"),
    [
        (
            {
                "candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": []}}],
                "usageMetadata": {
                    "promptTokenCount": 3000,
                    "candidatesTokenCount": 8192,
                },
            },
            True,
            "MAX_TOKENS",
            "truncated",
            "output limit",
        ),
        (
            {
                "candidates": [],
                "promptFeedback": {"blockReason": "SAFETY"},
                "usageMetadata": {"promptTokenCount": 25},
            },
            False,
            "SAFETY",
            "blocked",
            "empty content",
        ),
    ],
)
def test_gemini_empty_completion_preserves_safe_diagnostics(
    body, retryable, finish_reason, response_status, message
):
    adapter = GeminiAdapter(
        _profile(
            LLMTransport.GEMINI,
            ProviderDialect.GEMINI,
            base_url="https://generativelanguage.test/v1beta",
        ),
        session=_Session(_Response(body)),
    )

    with pytest.raises(LLMCallError, match=message) as caught:
        adapter.complete(_request())

    error = caught.value
    assert error.retryable is retryable
    assert error.finish_reason == finish_reason
    assert error.response_status == response_status
    assert error.choice_count == len(body["candidates"])
    assert error.usage.input_tokens == body["usageMetadata"]["promptTokenCount"]


def test_gemini_rejects_nonempty_truncated_content():
    body = {
        "candidates": [
            {
                "finishReason": "MAX_TOKENS",
                "content": {"parts": [{"text": '{"translation":'}]},
            }
        ],
        "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 200},
    }
    adapter = GeminiAdapter(
        _profile(
            LLMTransport.GEMINI,
            ProviderDialect.GEMINI,
            base_url="https://generativelanguage.test/v1beta",
        ),
        session=_Session(_Response(body)),
    )

    with pytest.raises(LLMCallError, match="output limit") as caught:
        adapter.complete(_request())

    assert caught.value.finish_reason == "MAX_TOKENS"
    assert caught.value.response_status == "truncated"


def test_gemini_shallow_options_restore_protected_generation_fields():
    session = _Session(
        _Response({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})
    )
    adapter = GeminiAdapter(
        _profile(
            LLMTransport.GEMINI,
            ProviderDialect.GEMINI,
            base_url="https://generativelanguage.test/v1beta",
            request_options={
                "generationConfig": {
                    "thinkingConfig": {"thinkingBudget": 4096},
                    "topP": 0.9,
                },
                "labels": {"purpose": "translation"},
            },
            max_output_tokens=654,
        ),
        session=session,
    )

    adapter.complete(_request())

    payload = session.calls[0][1]["json"]
    assert payload["generationConfig"] == {
        "thinkingConfig": {"thinkingBudget": 4096},
        "topP": 0.9,
        "candidateCount": 1,
        "maxOutputTokens": 654,
        "responseMimeType": "application/json",
        "responseSchema": dict(_request().response_schema),
    }
    assert payload["labels"] == {"purpose": "translation"}


class _QueueSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.deletes = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def delete(self, url, **kwargs):
        self.deletes.append((url, kwargs))
        return _Response({})


def test_gemini_explicit_cache_lifecycle_and_usage_never_fakes_writes():
    cache_created = _Response({"name": "cachedContents/task-prefix"})
    generated = _Response(
        {
            "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
            "usageMetadata": {
                "promptTokenCount": 3000,
                "candidatesTokenCount": 1,
                "cachedContentTokenCount": 2500,
            },
        }
    )
    session = _QueueSession([cache_created, generated, generated])
    adapter = GeminiAdapter(
        _profile(
            LLMTransport.GEMINI,
            ProviderDialect.GEMINI,
            base_url="https://generativelanguage.test/v1beta",
        ),
        session=session,
    )
    request = LLMRequest(
        messages=(
            LLMMessage("system", "stable-prefix\n" * 700),
            LLMMessage("user", "dynamic"),
        )
    )

    first = adapter.complete(request)
    second = adapter.complete(request)

    assert session.calls[0][0].endswith("/cachedContents")
    assert session.calls[1][1]["json"]["cachedContent"] == "cachedContents/task-prefix"
    assert "systemInstruction" not in session.calls[1][1]["json"]
    assert len(session.calls) == 3
    assert first.usage.cache_read_tokens == 2500
    assert first.usage.cache_write_tokens is None
    assert second.usage.cache_write_tokens is None

    adapter.close()
    assert session.deletes[0][0].endswith("/cachedContents/task-prefix")


def test_gemini_cache_creation_failure_degrades_to_stable_prefix_request():
    failed_cache = _Response({})
    failed_cache.ok = False
    failed_cache.status_code = 400
    failed_cache.text = "cached content is below the minimum token count"
    generated = _Response(
        {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
    )
    session = _QueueSession([failed_cache, generated, generated])
    adapter = GeminiAdapter(
        _profile(
            LLMTransport.GEMINI,
            ProviderDialect.GEMINI,
            base_url="https://generativelanguage.test/v1beta",
        ),
        session=session,
    )
    request = LLMRequest(
        messages=(
            LLMMessage("system", "stable-prefix\n" * 700),
            LLMMessage("user", "dynamic"),
        )
    )

    first = adapter.complete(request)
    adapter.complete(request)

    assert first.text == "ok"
    assert len(session.calls) == 3
    assert "systemInstruction" in session.calls[1][1]["json"]
    assert "cachedContent" not in session.calls[1][1]["json"]
    assert "systemInstruction" in session.calls[2][1]["json"]
    assert first.usage.cache_write_tokens is None


def test_native_http_context_overflow_has_structured_category():
    response = _Response({})
    response.ok = False
    response.status_code = 400
    response.text = (
        "prompt is too long for the maximum context length: SECRET_PROVIDER_BODY"
    )
    adapter = AnthropicMessagesAdapter(
        _profile(
            LLMTransport.ANTHROPIC_MESSAGES,
            ProviderDialect.ANTHROPIC,
            base_url="https://api.anthropic.test/v1",
        ),
        session=_Session(response),
    )

    try:
        adapter.complete(_request())
    except LLMCallError as exc:
        assert exc.category is LLMErrorCategory.CONTEXT_LIMIT
        assert exc.retryable is False
        assert "SECRET_PROVIDER_BODY" not in str(exc)
        assert exc.__cause__ is None
    else:
        raise AssertionError("context overflow should fail")


def test_openai_compatible_context_overflow_has_structured_category():
    class OverflowCompletions:
        def create(self, **_kwargs):
            request = httpx.Request("POST", "https://api.openai.test/v1/chat/completions")
            response = httpx.Response(400, request=request)
            raise openai.BadRequestError(
                "maximum context length exceeded: SECRET_PROVIDER_BODY",
                response=response,
                body={
                    "error": {
                        "code": "context_length_exceeded",
                        "prompt": "SECRET_PROVIDER_BODY",
                    }
                },
            )

    adapter = OpenAICompatibleAdapter(
        _profile(
            LLMTransport.OPENAI_COMPATIBLE,
            ProviderDialect.OPENAI,
            base_url="https://api.openai.test/v1",
        ),
        client=SimpleNamespace(chat=SimpleNamespace(completions=OverflowCompletions())),
    )

    try:
        adapter.complete(_request())
    except LLMCallError as exc:
        assert exc.category is LLMErrorCategory.CONTEXT_LIMIT
        assert exc.retryable is False
        assert "SECRET_PROVIDER_BODY" not in str(exc)
        assert exc.__cause__ is None
    else:
        raise AssertionError("context overflow should fail")
