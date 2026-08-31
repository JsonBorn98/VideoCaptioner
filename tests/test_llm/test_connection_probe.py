from videocaptioner.core.llm.check_llm import (
    CONNECTION_PROBE_MAX_OUTPUT_TOKENS,
    OutputLimitProbeStatus,
    check_model_profile_connection,
    connection_probe_output_cap,
    probe_model_output_limit,
    probe_model_profile_capabilities,
)
from videocaptioner.core.llm.models import (
    LLMCallError,
    LLMErrorCategory,
    LLMModelProfile,
    LLMResult,
    LLMTransport,
    ProviderDialect,
)

_STRUCTURED_PROBE_SCHEMA = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "minItems": 1,
            "maxItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "enum": [7]},
                    "text": {"type": "string", "enum": ["OK"]},
                },
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["translations"],
    "additionalProperties": False,
}


def _profile(
    transport: LLMTransport,
    dialect: ProviderDialect,
    **overrides,
) -> LLMModelProfile:
    return LLMModelProfile(
        profile_id=transport.value,
        name=transport.value,
        transport=transport,
        dialect=dialect,
        base_url="https://example.test/v1",
        api_key="secret",
        model="model",
        **overrides,
    )


class _Gateway:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def complete(self, profile, request, **kwargs):
        self.calls.append((profile, request, kwargs))
        result = self.result.pop(0) if isinstance(self.result, list) else self.result
        if isinstance(result, BaseException):
            raise result
        return result


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
        assert (
            gateway.calls[0][1].max_output_tokens
            == CONNECTION_PROBE_MAX_OUTPUT_TOKENS
        )
        assert gateway.calls[0][2]["max_attempts"] == 1
        assert gateway.calls[0][2]["use_cache"] is False


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


def test_probe_output_cap_uses_explicit_value_or_known_thinking_budget():
    explicit = _profile(
        LLMTransport.OPENAI_COMPATIBLE,
        ProviderDialect.OPENAI,
        max_output_tokens=777,
        request_options={"thinking": {"budget_tokens": 10_000}},
    )
    auto = _profile(
        LLMTransport.GEMINI,
        ProviderDialect.GEMINI,
        work_context_tokens=16_384,
        request_options={
            "thinking": {"budget_tokens": 2000},
            "generationConfig": {"thinkingConfig": {"thinkingBudget": 5000}},
            "extra_body": {"thinking": {"budget_tokens": 6000}},
        },
    )

    assert connection_probe_output_cap(explicit) == 777
    assert connection_probe_output_cap(auto) == 6512


def test_dual_probe_reports_text_and_structured_results_independently():
    failure = LLMCallError(
        "text unavailable",
        category=LLMErrorCategory.CONFIGURATION,
        retryable=False,
    )
    gateway = _Gateway(
        [failure, LLMResult(text='{"translations":[{"id":7,"text":"OK"}]}')]
    )
    profile = _profile(LLMTransport.GEMINI, ProviderDialect.GEMINI)

    result = probe_model_profile_capabilities(profile, gateway=gateway)

    assert result.text.success is False
    assert result.text.category is LLMErrorCategory.CONFIGURATION
    assert result.structured.success is True
    assert result.max_output_tokens == CONNECTION_PROBE_MAX_OUTPUT_TOKENS
    assert len(gateway.calls) == 2
    assert gateway.calls[0][1].metadata["stage"] == "connection_probe_text"
    assert gateway.calls[0][1].response_schema is None
    assert gateway.calls[1][1].metadata["stage"] == "connection_probe_structured"
    structured_request = gateway.calls[1][1]
    assert structured_request.response_schema == _STRUCTURED_PROBE_SCHEMA
    assert structured_request.messages[1].content == (
        'Return exactly {"translations":[{"id":"7","text":"NOT_OK",'
        '"extra":"keep-this"}]}. Do not convert the id to a number, do not '
        "change the text, and do not remove the extra field."
    )
    assert all(call[1].max_output_tokens == 4096 for call in gateway.calls)
    assert all(call[2]["max_attempts"] == 1 for call in gateway.calls)
    assert all(call[2]["use_cache"] is False for call in gateway.calls)


def test_dual_probe_locally_rejects_inexact_results_without_blocking_other_probe():
    gateway = _Gateway(
        [
            LLMResult(text="not OK"),
            LLMResult(text='{"translations":[{"id":"7","text":"OK"}]}'),
        ]
    )

    result = probe_model_profile_capabilities(
        _profile(LLMTransport.OPENAI_COMPATIBLE, ProviderDialect.OPENAI),
        gateway=gateway,
    )

    assert result.text.success is False
    assert result.text.category is LLMErrorCategory.INVALID_RESPONSE
    assert result.structured.success is False
    assert result.structured.category is LLMErrorCategory.INVALID_RESPONSE


def test_output_limit_probe_uses_work_context_minus_one_and_keeps_config_when_accepted():
    profile = _profile(
        LLMTransport.OPENAI_COMPATIBLE,
        ProviderDialect.OPENAI,
        work_context_tokens=16_384,
        max_output_tokens=8_192,
    )
    gateway = _Gateway(LLMResult(text="OK"))

    result = probe_model_output_limit(profile, gateway=gateway)

    assert result.status is OutputLimitProbeStatus.AT_LEAST_PROBE_VALUE
    assert result.suggested_value is None
    assert result.model_output_limit is None
    assert result.apply_suggested is False
    assert result.probe_max_output_tokens == 16_383
    assert profile.max_output_tokens == 8_192
    assert len(gateway.calls) == 1
    sent_profile, request, kwargs = gateway.calls[0]
    assert sent_profile.max_output_tokens == 16_383
    assert request.max_output_tokens == 16_383
    assert request.metadata == {"stage": "output_limit_probe", "role": "utility"}
    assert kwargs["max_attempts"] == 1
    assert kwargs["use_cache"] is False


def test_output_limit_probe_retries_once_with_suggested_value_before_allowing_fill():
    profile = _profile(
        LLMTransport.OPENAI_COMPATIBLE,
        ProviderDialect.OPENAI,
        work_context_tokens=16_384,
        max_output_tokens=10_000,
    )
    overflow = LLMCallError(
        "max tokens too large",
        category=LLMErrorCategory.OUTPUT_LIMIT,
        retryable=False,
        status_code=400,
        model_output_limit=8192,
    )
    gateway = _Gateway([overflow, LLMResult(text="OK")])

    result = probe_model_output_limit(profile, gateway=gateway)

    assert result.status is OutputLimitProbeStatus.SUGGESTED
    assert result.model_output_limit == 8192
    assert result.suggested_value == 8192
    assert result.apply_suggested is True
    assert result.probe_max_output_tokens == 16_383
    assert profile.max_output_tokens == 10_000
    assert len(gateway.calls) == 2
    assert gateway.calls[0][0].max_output_tokens == 16_383
    assert gateway.calls[0][1].max_output_tokens == 16_383
    assert gateway.calls[1][0].max_output_tokens == 8192
    assert gateway.calls[1][1].max_output_tokens == 8192
    assert gateway.calls[1][1].metadata == {
        "stage": "output_limit_probe_retry",
        "role": "utility",
    }
    assert gateway.calls[1][2]["max_attempts"] == 1
    assert gateway.calls[1][2]["use_cache"] is False


def test_output_limit_probe_caps_suggested_value_at_work_context_minus_one():
    profile = _profile(
        LLMTransport.GEMINI,
        ProviderDialect.GEMINI,
        work_context_tokens=16_384,
        max_output_tokens=7_000,
    )
    overflow = LLMCallError(
        "maxOutputTokens too large",
        category=LLMErrorCategory.OUTPUT_LIMIT,
        retryable=False,
        status_code=400,
        model_output_limit=65_536,
    )
    gateway = _Gateway([overflow, LLMResult(text="OK")])

    result = probe_model_output_limit(profile, gateway=gateway)

    assert result.status is OutputLimitProbeStatus.SUGGESTED
    assert result.model_output_limit == 65_536
    assert result.suggested_value == 16_383
    assert result.apply_suggested is False
    assert profile.max_output_tokens == 7_000
    assert gateway.calls[0][0].max_output_tokens == 16_383
    assert gateway.calls[1][0].max_output_tokens == 16_383
    assert gateway.calls[1][1].max_output_tokens == 16_383


def test_output_limit_probe_does_not_fill_auto_output_mode():
    profile = _profile(
        LLMTransport.OPENAI_COMPATIBLE,
        ProviderDialect.OPENAI,
        work_context_tokens=16_384,
    )
    overflow = LLMCallError(
        "max tokens too large",
        category=LLMErrorCategory.OUTPUT_LIMIT,
        retryable=False,
        status_code=400,
        model_output_limit=8192,
    )
    gateway = _Gateway([overflow, LLMResult(text="OK")])

    result = probe_model_output_limit(profile, gateway=gateway)

    assert result.status is OutputLimitProbeStatus.SUGGESTED
    assert result.suggested_value == 8192
    assert result.apply_suggested is False
    assert profile.max_output_tokens is None
    assert gateway.calls[0][0].max_output_tokens == 16_383
    assert gateway.calls[1][0].max_output_tokens == 8192


def test_output_limit_probe_passes_through_unparseable_error_without_suggestion():
    profile = _profile(
        LLMTransport.OPENAI_COMPATIBLE,
        ProviderDialect.OPENAI,
        work_context_tokens=16_384,
        max_output_tokens=10_000,
    )
    failure = LLMCallError(
        "bad request",
        category=LLMErrorCategory.CONFIGURATION,
        retryable=False,
        status_code=400,
    )
    gateway = _Gateway(failure)

    result = probe_model_output_limit(profile, gateway=gateway)

    assert result.status is OutputLimitProbeStatus.UNPARSEABLE
    assert result.suggested_value is None
    assert result.model_output_limit is None
    assert result.apply_suggested is False
    assert result.message == "configuration: bad request"
    assert len(gateway.calls) == 1
    assert gateway.calls[0][0].max_output_tokens == 16_383
    assert profile.max_output_tokens == 10_000


def test_output_limit_probe_does_not_allow_fill_when_verification_retry_fails():
    profile = _profile(
        LLMTransport.OPENAI_COMPATIBLE,
        ProviderDialect.OPENAI,
        work_context_tokens=16_384,
        max_output_tokens=10_000,
    )
    overflow = LLMCallError(
        "max tokens too large",
        category=LLMErrorCategory.OUTPUT_LIMIT,
        retryable=False,
        status_code=400,
        model_output_limit=8192,
    )
    retry_failure = LLMCallError(
        "still too large",
        category=LLMErrorCategory.OUTPUT_LIMIT,
        retryable=False,
        status_code=400,
        model_output_limit=4096,
    )
    gateway = _Gateway([overflow, retry_failure])

    result = probe_model_output_limit(profile, gateway=gateway)

    assert result.status is OutputLimitProbeStatus.RETRY_FAILED
    assert result.suggested_value == 8192
    assert result.model_output_limit == 8192
    assert result.apply_suggested is False
    assert result.message == "output-limit: still too large"
    assert len(gateway.calls) == 2
    assert profile.max_output_tokens == 10_000
    assert gateway.calls[0][0].max_output_tokens == 16_383
    assert gateway.calls[1][0].max_output_tokens == 8192
    assert gateway.calls[1][1].max_output_tokens == 8192
