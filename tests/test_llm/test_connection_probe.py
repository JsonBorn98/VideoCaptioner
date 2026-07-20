from videocaptioner.core.llm.check_llm import check_model_profile_connection
from videocaptioner.core.llm.models import (
    LLMCallError,
    LLMErrorCategory,
    LLMModelProfile,
    LLMResult,
    LLMTransport,
    ProviderDialect,
)


def _profile(transport: LLMTransport, dialect: ProviderDialect) -> LLMModelProfile:
    return LLMModelProfile(
        profile_id=transport.value,
        name=transport.value,
        transport=transport,
        dialect=dialect,
        base_url="https://example.test/v1",
        api_key="secret",
        model="model",
    )


class _Gateway:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def complete(self, profile, request, **kwargs):
        self.calls.append((profile, request, kwargs))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def test_connection_probe_uses_gateway_for_every_native_transport():
    profiles = (
        _profile(LLMTransport.OPENAI_COMPATIBLE, ProviderDialect.GENERIC),
        _profile(LLMTransport.ANTHROPIC_MESSAGES, ProviderDialect.ANTHROPIC),
        _profile(LLMTransport.GEMINI, ProviderDialect.GEMINI),
    )
    for profile in profiles:
        gateway = _Gateway(LLMResult(text="OK"))
        success, message = check_model_profile_connection(profile, gateway=gateway)
        assert success is True
        assert message == "OK"
        assert gateway.calls[0][0] is profile
        assert gateway.calls[0][1].metadata == {
            "stage": "connection_probe",
            "role": "utility",
        }
        assert gateway.calls[0][2]["max_attempts"] == 1


def test_connection_probe_preserves_structured_error_category():
    failure = LLMCallError(
        "bad key",
        category=LLMErrorCategory.AUTHENTICATION,
        retryable=False,
    )
    success, message = check_model_profile_connection(
        _profile(LLMTransport.GEMINI, ProviderDialect.GEMINI),
        gateway=_Gateway(failure),
    )
    assert success is False
    assert message == "authentication: bad key"
