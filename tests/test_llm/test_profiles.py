import json

import pytest

from videocaptioner.core.llm.models import (
    LLMModelProfile,
    LLMTransport,
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
) -> LLMModelProfile:
    return LLMModelProfile(
        profile_id=profile_id,
        name=name,
        transport=LLMTransport.OPENAI_COMPATIBLE,
        dialect=ProviderDialect.GENERIC,
        base_url="https://example.test/v1",
        api_key="secret",
        model="example-model",
        work_context_tokens=65_536,
        max_concurrency=3,
    )


def test_profile_and_store_round_trip(tmp_path):
    profile = _profile()
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


def test_store_rejects_duplicate_name_case_insensitively(tmp_path):
    path = tmp_path / "profiles.json"
    store = LLMModelProfileStore(path)
    original = store.save(_profile(name="Review Model"))

    with pytest.raises(LLMProfileConflictError, match="already exists"):
        store.save(_profile(profile_id="review", name="review model"))

    reloaded = LLMModelProfileStore(path)
    assert reloaded.list() == (original,)


@pytest.mark.parametrize(
    "document",
    [
        {"schema": "wrong", "version": PROFILE_SCHEMA_VERSION, "profiles": []},
        {"schema": PROFILE_SCHEMA, "version": 999, "profiles": []},
        {"schema": PROFILE_SCHEMA, "version": PROFILE_SCHEMA_VERSION},
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
