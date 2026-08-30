"""Direct seam tests for the utility-role model profile resolver."""

import pytest

from videocaptioner.core.llm.models import (
    LLMModelProfile,
    LLMTransport,
    OpenAIEndpoint,
    ProviderDialect,
)
from videocaptioner.core.llm.profiles import LLMModelProfileStore
from videocaptioner.core.llm.request_options import (
    RequestOptionsError,
    validate_structured_output_compatibility,
)
from videocaptioner.core.llm.utility import (
    UTILITY_PROFILE_CARD,
    UtilityProfileError,
    resolve_utility_profile,
    validate_utility_profile,
)


def _profile(
    profile_id: str = "primary",
    name: str = "Primary model",
    **overrides,
) -> LLMModelProfile:
    values = {
        "profile_id": profile_id,
        "name": name,
        "transport": LLMTransport.OPENAI_COMPATIBLE,
        "dialect": ProviderDialect.GENERIC,
        "base_url": "https://example.test/v1",
        "api_key": "secret",
        "model": "example-model",
        "work_context_tokens": 65_536,
        "max_concurrency": 3,
    }
    values.update(overrides)
    return LLMModelProfile(**values)


def _store(tmp_path, *profiles: LLMModelProfile) -> LLMModelProfileStore:
    store = LLMModelProfileStore(tmp_path / "profiles.json")
    for profile in profiles:
        store.save(profile)
    return store


def test_bound_profile_wins_over_derivation(tmp_path):
    main = _profile("main", name="Main model")
    utility = _profile("utility", name="Utility model", model="utility-model")
    store = _store(tmp_path, main, utility)

    resolved = resolve_utility_profile(store, "main", "utility")
    assert resolved.model == "utility-model"

    # A binding resolves even when the main translation id is empty.
    assert resolve_utility_profile(store, "", "utility").model == "utility-model"


def test_lost_binding_raises_instead_of_falling_back(tmp_path):
    main = _profile("main", name="Main model")
    store = _store(tmp_path, main)

    with pytest.raises(UtilityProfileError, match="已不存在"):
        resolve_utility_profile(store, "main", "deleted")


def test_derived_profile_strips_tuning_and_keeps_infra_fields(tmp_path):
    main = _profile(
        "main",
        name="Main model",
        openai_endpoint=OpenAIEndpoint.RESPONSES,
        request_options={"reasoning": {"effort": "high"}},
        max_output_tokens=4096,
    )
    store = _store(tmp_path, main)

    resolved = resolve_utility_profile(store, "main")

    assert resolved.openai_endpoint is OpenAIEndpoint.CHAT_COMPLETIONS
    assert not resolved.request_options
    assert resolved.max_output_tokens is None
    for field_name in (
        "profile_id",
        "name",
        "transport",
        "dialect",
        "base_url",
        "api_key",
        "model",
        "work_context_tokens",
        "max_concurrency",
    ):
        assert getattr(resolved, field_name) == getattr(main, field_name)

    # The stored translation profile keeps its tuning fields.
    stored = store.get("main")
    assert stored.openai_endpoint is OpenAIEndpoint.RESPONSES
    assert stored.max_output_tokens == 4096


def test_bound_profile_is_stripped_the_same_way(tmp_path):
    utility = _profile(
        "utility",
        name="Utility model",
        openai_endpoint=OpenAIEndpoint.RESPONSES,
        request_options={"reasoning": {"effort": "high"}},
        max_output_tokens=2048,
    )
    store = _store(tmp_path, utility)

    resolved = resolve_utility_profile(store, "", "utility")

    assert resolved.openai_endpoint is OpenAIEndpoint.CHAT_COMPLETIONS
    assert not resolved.request_options
    assert resolved.max_output_tokens is None


def test_no_main_and_no_binding_raises_with_card_guidance(tmp_path):
    store = _store(tmp_path)

    with pytest.raises(UtilityProfileError, match=UTILITY_PROFILE_CARD):
        resolve_utility_profile(store, "")
    with pytest.raises(UtilityProfileError):
        resolve_utility_profile(store, None, None)
    with pytest.raises(UtilityProfileError):
        resolve_utility_profile(store, "   ")


def test_lost_main_binding_raises_with_card_guidance(tmp_path):
    store = _store(tmp_path)

    with pytest.raises(UtilityProfileError, match=UTILITY_PROFILE_CARD):
        resolve_utility_profile(store, "deleted-main")


def test_ids_are_stripped_before_resolution(tmp_path):
    main = _profile("main", name="Main model")
    utility = _profile("utility", name="Utility model")
    store = _store(tmp_path, main, utility)

    assert resolve_utility_profile(store, " main ").profile_id == "main"
    assert resolve_utility_profile(store, "main", " utility ").profile_id == "utility"


@pytest.mark.parametrize(
    ("transport", "dialect", "request_options"),
    [
        (
            LLMTransport.OPENAI_COMPATIBLE,
            ProviderDialect.GENERIC,
            {"reasoning": {"effort": "high"}},
        ),
        (
            LLMTransport.ANTHROPIC_MESSAGES,
            ProviderDialect.ANTHROPIC,
            {"thinking": {"type": "enabled", "budget_tokens": 2048}},
        ),
        (
            LLMTransport.GEMINI,
            ProviderDialect.GEMINI,
            {"generationConfig": {"topP": 0.9}},
        ),
    ],
)
def test_derived_profile_passes_preflight_for_every_transport(
    tmp_path, transport, dialect, request_options
):
    main = _profile(
        "main",
        name="Main model",
        transport=transport,
        dialect=dialect,
        request_options=request_options,
        max_output_tokens=4096,
    )
    store = _store(tmp_path, main)

    resolved = resolve_utility_profile(store, "main")

    validate_utility_profile(resolved)


def test_stripping_unblocks_anthropic_structured_output_fallback(tmp_path):
    main = _profile(
        "main",
        name="Main model",
        transport=LLMTransport.ANTHROPIC_MESSAGES,
        dialect=ProviderDialect.ANTHROPIC,
        request_options={"thinking": {"type": "enabled"}},
    )
    # The raw translation profile is rejected for schema requests...
    with pytest.raises(RequestOptionsError):
        validate_structured_output_compatibility(main)

    # ...while the stripped utility profile always passes the bottom-line check.
    store = _store(tmp_path, main)
    validate_utility_profile(resolve_utility_profile(store, "main"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"openai_endpoint": OpenAIEndpoint.RESPONSES},
        {"request_options": {"reasoning": {"effort": "high"}}},
        {"max_output_tokens": 4096},
    ],
)
def test_preflight_rejects_non_default_tuning(overrides):
    profile = _profile(**overrides)

    with pytest.raises(UtilityProfileError):
        validate_utility_profile(profile)


def test_preflight_reuses_request_option_validation():
    profile = _profile(request_options={"model": "other-model"})

    with pytest.raises(RequestOptionsError):
        validate_utility_profile(profile)
