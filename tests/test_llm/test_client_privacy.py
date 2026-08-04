import json

import httpx
import openai
import pytest

from videocaptioner.core.llm import client
from videocaptioner.core.llm.models import LLMCallError, LLMErrorCategory


def test_legacy_client_does_not_propagate_provider_error_body(monkeypatch):
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    response = httpx.Response(400, request=request)

    def fail(*_args, **_kwargs):
        raise openai.BadRequestError(
            "SECRET_PROVIDER_BODY",
            response=response,
            body={"error": {"prompt": "SECRET_PROVIDER_BODY"}},
        )

    monkeypatch.setattr(client, "_call_llm_api", fail)

    with pytest.raises(LLMCallError) as caught:
        client.call_llm.__wrapped__(
            messages=[{"role": "user", "content": "private subtitle"}],
            model="model",
        )

    assert caught.value.category is LLMErrorCategory.CONFIGURATION
    assert caught.value.status_code == 400
    assert "SECRET_PROVIDER_BODY" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


def test_legacy_client_ignores_temperature_before_the_sdk_call(monkeypatch):
    captured = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return object()

    class FakeClient:
        class Chat:
            completions = FakeCompletions()

        chat = Chat()

    monkeypatch.setattr(client, "get_llm_client", FakeClient)
    monkeypatch.setattr(client, "log_llm_response", lambda _response: None)

    # Retain the positional legacy argument without letting it escape to a provider.
    client._call_llm_api(
        [{"role": "user", "content": "subtitle"}], "legacy-model", 0.2, timeout=5
    )

    assert captured == {
        "model": "legacy-model",
        "messages": [{"role": "user", "content": "subtitle"}],
        "timeout": 5,
    }


def test_legacy_extra_body_cannot_restore_temperature_in_final_http_json(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "legacy-model",
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
    extra_body = {
        "temperature": 0.2,
        "reasoning_effort": "high",
        "chat_template_kwargs": {
            "temperature": 0.3,
            "enable_thinking": True,
        },
    }
    monkeypatch.setattr(client, "get_llm_client", lambda: sdk_client)
    monkeypatch.setattr(client, "log_llm_response", lambda _response: None)

    try:
        client._call_llm_api(
            [{"role": "user", "content": "subtitle"}],
            "legacy-model",
            extra_body=extra_body,
        )
    finally:
        sdk_client.close()

    assert "temperature" not in captured
    assert "temperature" not in captured["chat_template_kwargs"]
    assert captured["reasoning_effort"] == "high"
    assert captured["chat_template_kwargs"]["enable_thinking"] is True
    assert extra_body["temperature"] == 0.2
