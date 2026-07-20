from types import SimpleNamespace

import httpx
import openai

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
    ProviderDialect,
)


def _profile(
    transport: LLMTransport,
    dialect: ProviderDialect,
    *,
    base_url: str,
    model: str = "test-model",
) -> LLMModelProfile:
    return LLMModelProfile(
        profile_id=f"test-{transport.value}",
        name=f"Test {transport.value}",
        transport=transport,
        dialect=dialect,
        base_url=base_url,
        api_key="test-key",
        model=model,
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
    assert completions.kwargs["temperature"] == 0.25
    assert completions.kwargs["max_tokens"] == 321
    assert completions.kwargs["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "structured_response",
            "strict": True,
            "schema": dict(_request().response_schema),
        },
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
        "temperature": 0.25,
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
            "temperature": 0.25,
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
    response.text = "prompt is too long for the maximum context length"
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
    else:
        raise AssertionError("context overflow should fail")


def test_openai_compatible_context_overflow_has_structured_category():
    class OverflowCompletions:
        def create(self, **_kwargs):
            request = httpx.Request("POST", "https://api.openai.test/v1/chat/completions")
            response = httpx.Response(400, request=request)
            raise openai.BadRequestError(
                "maximum context length exceeded",
                response=response,
                body={"error": {"code": "context_length_exceeded"}},
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
    else:
        raise AssertionError("context overflow should fail")
