"""Consumer-seam tests for the dubbing duration rewrite.

The rewriter follows the shared consumer seam: callers inject a resolved
model profile and (here) a fake gateway, so these tests observe the request
shape — profile pass-through, response schema mount, stage/role metadata,
timeout fallback — without any network access.
"""

import json
from types import SimpleNamespace

import pytest

import videocaptioner.core.dubbing.rewriter as rewriter_module
from videocaptioner.core.dubbing.models import DubbingConfig, DubbingSegment
from videocaptioner.core.dubbing.rewriter import (
    REWRITE_RESPONSE_SCHEMA,
    rewrite_segments_if_needed,
    should_rewrite,
)
from videocaptioner.core.llm.adapters import OpenAICompatibleAdapter
from videocaptioner.core.llm.models import (
    LLMMessage,
    LLMModelProfile,
    LLMRequest,
    LLMResult,
    LLMTransport,
    ProviderDialect,
)
from videocaptioner.core.llm.utility import UTILITY_PROFILE_CARD, UtilityProfileError


def _profile(**overrides) -> LLMModelProfile:
    values = {
        "profile_id": "dub-utility",
        "name": "Dub Utility",
        "transport": LLMTransport.OPENAI_COMPATIBLE,
        "dialect": ProviderDialect.GENERIC,
        "base_url": "https://dub.test/v1",
        "api_key": "secret",
        "model": "dub-model",
    }
    values.update(overrides)
    return LLMModelProfile(**values)


def _segment(index: int = 1, text: str = "这是一条很长很长的字幕内容需要压缩改写") -> DubbingSegment:
    # 19 CJK chars in 1s needs 19 chars/s; comfortable is 5.5 * 1.15 = 6.3.
    return DubbingSegment(index=index, start_ms=0, end_ms=1000, text=text, speaker="Alice")


def _config(rewrite: bool = True) -> DubbingConfig:
    return DubbingConfig(
        provider="edge",
        api_key="",
        base_url="",
        model="edge-tts",
        rewrite_too_long=rewrite,
    )


class _CapturingGateway:
    def __init__(self, text: str = '{"items":[]}') -> None:
        self.text = text
        self.calls = []

    def complete(self, profile, request, *, cancelled=None):
        self.calls.append((profile, request, cancelled))
        return LLMResult(text=self.text)


def test_should_rewrite_flags_cjk_line_over_comfortable_rate():
    assert should_rewrite(_segment(), 1.15) is True
    assert should_rewrite(_segment(text="短"), 1.15) is False


def test_disabled_rewrite_skips_the_llm_entirely():
    gateway = _CapturingGateway()
    segment = _segment()

    rewrite_segments_if_needed([segment], _config(rewrite=False), None, gateway=gateway)

    assert gateway.calls == []
    assert segment.rewritten_text is None


def test_enabled_rewrite_without_profile_raises_with_guidance():
    with pytest.raises(UtilityProfileError, match=UTILITY_PROFILE_CARD):
        rewrite_segments_if_needed([_segment()], _config(), None)


def test_no_overlong_segments_skips_the_llm():
    gateway = _CapturingGateway()

    rewrite_segments_if_needed([_segment(text="短")], _config(), _profile(), gateway=gateway)

    assert gateway.calls == []


def test_rewrite_request_carries_profile_schema_metadata_and_default_timeout():
    profile = _profile()
    gateway = _CapturingGateway()
    segment = _segment()

    rewrite_segments_if_needed([segment], _config(), profile, gateway=gateway)

    assert len(gateway.calls) == 1
    used_profile, request, cancelled = gateway.calls[0]
    assert used_profile is profile
    assert cancelled is None
    assert request.metadata == {"stage": "llm_dub_rewrite", "role": "utility"}
    assert dict(request.response_schema) == REWRITE_RESPONSE_SCHEMA
    # No request-level timeout: the adapter's constructor default (120s) applies.
    assert request.timeout is None
    assert request.max_output_tokens == profile.max_output_tokens

    assert request.messages[0].role == "system"
    payload = json.loads(request.messages[1].content.rsplit("\n\n", 1)[-1])
    assert payload["items"] == [
        {
            "index": 1,
            "duration_seconds": 1.0,
            "speaker": "Alice",
            "text": segment.text,
        }
    ]


def test_rewrite_applies_returned_items_and_keeps_missing_lines():
    gateway = _CapturingGateway(
        text='{"items":[{"index":1,"text":" 更短的台词 "},{"index":9,"text":"未知行"}]}'
    )
    first = _segment(index=1)
    second = _segment(index=2)

    rewrite_segments_if_needed([first, second], _config(), _profile(), gateway=gateway)

    assert first.rewritten_text == "更短的台词"
    assert second.rewritten_text is None
    assert second.text_for_tts == second.text


def test_non_object_payload_is_treated_as_no_items():
    gateway = _CapturingGateway(text="null")
    segment = _segment()

    rewrite_segments_if_needed([segment], _config(), _profile(), gateway=gateway)

    assert segment.rewritten_text is None


def test_missing_gateway_is_constructed_lazily_and_closed(monkeypatch):
    owned = []

    class _OwnedGateway:
        def __init__(self) -> None:
            owned.append(self)
            self.closed = False

        def complete(self, profile, request, *, cancelled=None):
            return LLMResult(text='{"items":[{"index":1,"text":"短"}]}')

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(rewriter_module, "LLMGateway", _OwnedGateway)
    segment = _segment()

    rewrite_segments_if_needed([segment], _config(), _profile())

    assert len(owned) == 1
    assert owned[0].closed is True
    assert segment.rewritten_text == "短"


def test_injected_gateway_is_never_closed():
    class _TrackingGateway(_CapturingGateway):
        def __init__(self) -> None:
            super().__init__('{"items":[]}')
            self.closed = False

        def close(self) -> None:
            self.closed = True

    gateway = _TrackingGateway()

    rewrite_segments_if_needed([_segment()], _config(), _profile(), gateway=gateway)

    assert gateway.closed is False


def test_generic_dialect_request_body_matches_legacy_json_mode():
    """Zero-regression guard for the generic tier.

    With the items schema mounted, an unidentified (generic) dialect must
    receive exactly the bare ``response_format`` json-object body the old
    direct OpenAI client sent — byte for byte, no tools, no schema payload.
    """
    completions = SimpleNamespace(kwargs=None)

    class _Completions:
        def create(self, **kwargs):
            completions.kwargs = kwargs
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(message=SimpleNamespace(content='{"items":[]}'))
                ],
            )

    adapter = OpenAICompatibleAdapter(
        _profile(),
        client=SimpleNamespace(chat=SimpleNamespace(completions=_Completions())),
    )

    result = adapter.complete(
        LLMRequest(
            messages=(
                LLMMessage("system", "Shorten dubbing lines. Return only JSON."),
                LLMMessage("user", "Rewrite the overlong lines."),
            ),
            response_schema=REWRITE_RESPONSE_SCHEMA,
            metadata={"stage": "llm_dub_rewrite", "role": "utility"},
        )
    )

    assert completions.kwargs["response_format"] == {"type": "json_object"}
    assert "tools" not in completions.kwargs
    assert "tool_choice" not in completions.kwargs
    assert result.text == '{"items":[]}'
