import json
from dataclasses import replace
from types import MappingProxyType

import pytest

from videocaptioner.core.llm.models import (
    LLMModelProfile,
    LLMTransport,
    OpenAIEndpoint,
    ProviderDialect,
)
from videocaptioner.core.llm.profiles import (
    PROFILE_SCHEMA,
    PROFILE_SCHEMA_VERSION,
    LLMModelProfileStore,
    LLMProfileConflictError,
    LLMProfileError,
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


def test_profile_and_store_round_trip(tmp_path):
    profile = _profile(
        openai_endpoint=OpenAIEndpoint.RESPONSES,
        request_options={
            "reasoning": {"effort": "high"},
            "metadata": {"tags": ["translation", 2, True, None]},
        },
        max_output_tokens=4096,
    )
    assert LLMModelProfile.from_dict(profile.to_dict()) == profile

    path = tmp_path / "profiles.json"
    stored = LLMModelProfileStore(path).save(profile)

    assert stored == profile
    assert LLMModelProfileStore(path).get(profile.profile_id) == profile
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document == {
        "schema": PROFILE_SCHEMA,
        "version": PROFILE_SCHEMA_VERSION,
        "profiles": [profile.to_dict()],
    }


def test_request_options_are_recursively_copied_frozen_and_restored():
    source = {
        "reasoning": {"effort": "high"},
        "metadata": {"tags": ["translation", {"priority": 2}]},
    }
    profile = _profile(request_options=source)

    source["reasoning"]["effort"] = "low"
    source["metadata"]["tags"].append("later")

    assert isinstance(profile.request_options, MappingProxyType)
    assert profile.request_options["reasoning"]["effort"] == "high"
    assert profile.request_options["metadata"]["tags"] == (
        "translation",
        {"priority": 2},
    )
    with pytest.raises(TypeError):
        profile.request_options["new"] = True
    with pytest.raises(TypeError):
        profile.request_options["reasoning"]["effort"] = "low"

    restored = profile.to_dict()["request_options"]
    assert restored == {
        "reasoning": {"effort": "high"},
        "metadata": {"tags": ["translation", {"priority": 2}]},
    }
    restored["reasoning"]["effort"] = "low"
    assert profile.request_options["reasoning"]["effort"] == "high"


@pytest.mark.parametrize(
    "request_options",
    [
        [],
        {"bad": object()},
        {1: "non-string key"},
        {"bad": {1, 2}},
        {"bad": float("nan")},
        {"bad": float("inf")},
        {"bad": float("-inf")},
    ],
)
def test_profile_rejects_non_json_request_options(request_options):
    with pytest.raises(ValueError, match="request_options"):
        _profile(request_options=request_options)


def test_profile_enforces_request_options_size_and_depth_limits():
    with pytest.raises(ValueError, match="65536 UTF-8 bytes"):
        _profile(request_options={"value": "界" * 22_000})

    allowed = {}
    cursor = allowed
    for index in range(15):
        child = {}
        cursor[f"level_{index}"] = child
        cursor = child
    _profile(request_options=allowed)

    too_deep = {}
    cursor = too_deep
    for index in range(16):
        child = {}
        cursor[f"level_{index}"] = child
        cursor = child
    with pytest.raises(ValueError, match="nesting depth"):
        _profile(request_options=too_deep)


@pytest.mark.parametrize("max_output_tokens", [0, -1, 65_536, True, 1.5, "4096"])
def test_profile_rejects_invalid_max_output_tokens(max_output_tokens):
    with pytest.raises(ValueError, match="max_output_tokens"):
        _profile(max_output_tokens=max_output_tokens)


@pytest.mark.parametrize(
    "transport",
    [LLMTransport.ANTHROPIC_MESSAGES, LLMTransport.GEMINI],
)
def test_native_transport_rejects_responses_endpoint(transport):
    with pytest.raises(ValueError, match="native LLM transports"):
        _profile(transport=transport, openai_endpoint=OpenAIEndpoint.RESPONSES)


def test_profile_from_dict_is_strict_v2():
    profile = _profile()
    document = profile.to_dict()

    for field, invalid in (
        ("id", 1),
        ("transport", LLMTransport.OPENAI_COMPATIBLE),
        ("work_context_tokens", "65536"),
        ("max_concurrency", True),
        ("request_options", []),
        ("max_output_tokens", 4096.0),
    ):
        candidate = {**document, field: invalid}
        with pytest.raises(ValueError):
            LLMModelProfile.from_dict(candidate)


def test_profile_allows_null_concurrency_clamp():
    profile = _profile(max_concurrency=None)
    restored = LLMModelProfile.from_dict(profile.to_dict())
    assert restored.max_concurrency is None
    assert profile.to_dict()["max_concurrency"] is None


def test_v1_collection_migrates_in_memory_and_only_writes_v2_on_mutation(tmp_path):
    path = tmp_path / "profiles.json"
    v2_profile = _profile(
        openai_endpoint=OpenAIEndpoint.RESPONSES,
        request_options={"reasoning": {"effort": "high"}},
        max_output_tokens=4096,
    ).to_dict()
    v1_profile = {
        key: value
        for key, value in v2_profile.items()
        if key not in {"openai_endpoint", "request_options", "max_output_tokens"}
    }
    original = json.dumps(
        {"schema": PROFILE_SCHEMA, "version": 1, "profiles": [v1_profile]},
        ensure_ascii=False,
    )
    path.write_text(original, encoding="utf-8")

    store = LLMModelProfileStore(path)
    migrated = store.get("primary")

    assert migrated.openai_endpoint is OpenAIEndpoint.CHAT_COMPLETIONS
    assert migrated.request_options == {}
    assert migrated.max_output_tokens is None
    assert path.read_text(encoding="utf-8") == original

    store.save(replace(migrated, request_options={"reasoning_effort": "medium"}))
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["version"] == PROFILE_SCHEMA_VERSION == 3
    assert written["profiles"][0]["openai_endpoint"] == "chat_completions"
    assert written["profiles"][0]["request_options"] == {"reasoning_effort": "medium"}
    assert written["profiles"][0]["max_output_tokens"] is None


def test_v2_legacy_default_concurrency_clears_to_no_clamp(tmp_path):
    path = tmp_path / "profiles.json"
    v2_profile = _profile(max_concurrency=4).to_dict()
    path.write_text(
        json.dumps(
            {"schema": PROFILE_SCHEMA, "version": 2, "profiles": [v2_profile]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    migrated = LLMModelProfileStore(path).get("primary")
    assert migrated.max_concurrency is None


def test_v3_explicit_concurrency_clamp_is_kept(tmp_path):
    path = tmp_path / "profiles.json"
    store = LLMModelProfileStore(path)
    stored = store.save(_profile(max_concurrency=4))
    assert stored.max_concurrency == 4
    assert LLMModelProfileStore(path).get("primary").max_concurrency == 4


def test_store_rejects_duplicate_name_case_insensitively(tmp_path):
    path = tmp_path / "profiles.json"
    store = LLMModelProfileStore(path)
    original = store.save(_profile(name="Review Model"))

    with pytest.raises(LLMProfileConflictError, match="already exists"):
        store.save(_profile(profile_id="review", name="review model"))

    reloaded = LLMModelProfileStore(path)
    assert reloaded.list() == (original,)


@pytest.mark.parametrize(
    "request_options",
    [
        {"messages": []},
        {"$omit": ["unsupported"]},
        {"$omit": ["temperature"], "temperature": 0.2},
    ],
)
def test_store_rejects_invalid_or_protected_request_options_before_writing(
    tmp_path, request_options
):
    path = tmp_path / "profiles.json"
    store = LLMModelProfileStore(path)

    with pytest.raises(LLMProfileError, match="request options"):
        store.save(_profile(request_options=request_options))

    assert not path.exists()


@pytest.mark.parametrize(
    "document",
    [
        {"schema": "wrong", "version": PROFILE_SCHEMA_VERSION, "profiles": []},
        {"schema": PROFILE_SCHEMA, "version": True, "profiles": []},
        {"schema": PROFILE_SCHEMA, "version": 999, "profiles": []},
        {"schema": PROFILE_SCHEMA, "version": PROFILE_SCHEMA_VERSION},
        {
            "schema": PROFILE_SCHEMA,
            "version": 1,
            "profiles": [_profile().to_dict()],
        },
        {
            "schema": PROFILE_SCHEMA,
            "version": PROFILE_SCHEMA_VERSION,
            "profiles": [{**_profile().to_dict(), "unexpected": True}],
        },
    ],
)
def test_store_rejects_invalid_collection_or_profile_schema(tmp_path, document):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(LLMProfileError):
        LLMModelProfileStore(path)
