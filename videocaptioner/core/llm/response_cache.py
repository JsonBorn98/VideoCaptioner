"""Content-addressed disk cache for gateway completion responses.

The cache is an optimization, never a dependency: every internal failure
(disk full, corrupted entry, deserialization mismatch) degrades to a miss
or an abandoned write instead of interrupting the request path.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from diskcache import Cache

from videocaptioner.core.utils.cache import get_gateway_cache, is_cache_enabled
from videocaptioner.core.utils.logger import setup_logger

from .models import (
    LLMModelProfile,
    LLMRequest,
    LLMResult,
    thaw_json_object,
)

logger = setup_logger("llm_response_cache")

# Bump when request-construction logic changes (adapter behavior, key
# material fields); old entries become unreachable and expire naturally.
KEY_VERSION = "gateway-cache-v1"

# Value-level marker doubling the key-side version guard against stale
# reads if the key version was forgotten.
VALUE_SCHEMA = "gateway-cache-v1"

EXPIRE_SECONDS = 3600


def _message_entries(request: LLMRequest) -> list[dict[str, str]]:
    return [
        {"role": message.role, "content": message.content}
        for message in request.messages
    ]


def _schema_entry(request: LLMRequest) -> Optional[Any]:
    if request.response_schema is None:
        return None
    return dict(request.response_schema)


def _override_entry(request: LLMRequest) -> Optional[dict[str, Any]]:
    if request.request_options_override is None:
        return None
    return thaw_json_object(request.request_options_override)


def _cache_key(profile: LLMModelProfile, request: LLMRequest) -> str:
    """Build the content-addressed key from an explicit field allowlist.

    Only request-shaping fields enter the key. Pure identity and
    observability fields (profile_id, name, work_context_tokens,
    max_concurrency, metadata, deprecated temperature) are excluded.
    """

    material = {
        "key_version": KEY_VERSION,
        "profile": {
            "transport": profile.transport.value,
            "dialect": profile.dialect.value,
            "base_url": profile.base_url,
            "api_key": profile.api_key,
            "model": profile.model,
            "openai_endpoint": profile.openai_endpoint.value,
            "request_options": thaw_json_object(profile.request_options),
        },
        "request": {
            "messages": _message_entries(request),
            "max_output_tokens": request.max_output_tokens,
            "response_schema": _schema_entry(request),
            "request_options_override": _override_entry(request),
            "cacheable_system_prefix": request.cacheable_system_prefix,
        },
    }
    serialized = json.dumps(material, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(serialized.encode()).hexdigest()


class GatewayResponseCache:
    """Disk cache for successful gateway completions, keyed by request content."""

    def __init__(self, cache: Optional[Cache] = None) -> None:
        # An empty diskcache.Cache is falsy (it implements __len__), so an
        # injected cache must be tested against None, never with `or`.
        self._cache = get_gateway_cache() if cache is None else cache

    def lookup(
        self, profile: LLMModelProfile, request: LLMRequest
    ) -> Optional[LLMResult]:
        """Return the cached result, or None on miss/disabled/failure."""

        if not is_cache_enabled():
            return None
        try:
            payload = self._cache.get(_cache_key(profile, request))
            if not isinstance(payload, dict):
                return None
            if payload.get("schema") != VALUE_SCHEMA:
                return None
            text = payload.get("text")
            if not isinstance(text, str):
                return None
            return LLMResult(text=text)
        except Exception:
            logger.debug("gateway cache lookup failed; treating as miss", exc_info=True)
            return None

    def store(
        self, profile: LLMModelProfile, request: LLMRequest, result: LLMResult
    ) -> None:
        """Persist one successful completion; failures abandon the write."""

        if not is_cache_enabled():
            return
        try:
            payload = {"schema": VALUE_SCHEMA, "text": result.text}
            self._cache.set(
                _cache_key(profile, request), payload, expire=EXPIRE_SECONDS
            )
        except Exception:
            logger.debug("gateway cache store failed; skipping write", exc_info=True)


__all__ = ["GatewayResponseCache"]
