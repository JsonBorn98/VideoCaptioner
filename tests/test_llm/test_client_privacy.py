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
