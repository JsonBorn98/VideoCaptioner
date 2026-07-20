"""Native provider transports behind a provider-neutral interface."""

from __future__ import annotations

import hashlib
import json
import threading
from abc import ABC, abstractmethod
from typing import Any, Mapping, Optional
from urllib.parse import quote, urlparse, urlunparse

import openai
import requests

from .models import (
    LLMCallError,
    LLMErrorCategory,
    LLMModelProfile,
    LLMRequest,
    LLMResult,
    LLMUsage,
    ProviderDialect,
)


def _read_attr(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _endpoint(base_url: str, suffix: str) -> str:
    parsed = urlparse(base_url.rstrip("/"))
    path = parsed.path.rstrip("/")
    normalized_suffix = "/" + suffix.lstrip("/")
    if path.endswith(normalized_suffix):
        return base_url.rstrip("/")
    if path.endswith("/v1") and normalized_suffix.startswith("/v1/"):
        normalized_suffix = normalized_suffix[len("/v1") :]
    return urlunparse(parsed._replace(path=path + normalized_suffix))


def _http_error(response: requests.Response) -> LLMCallError:
    status = response.status_code
    message = response.text.strip() or f"LLM provider returned HTTP {status}"
    if status in {401, 403}:
        return LLMCallError(
            message,
            category=LLMErrorCategory.AUTHENTICATION,
            retryable=False,
            status_code=status,
        )
    if status == 429 or status >= 500:
        retry_after: Optional[float] = None
        try:
            if response.headers.get("Retry-After"):
                retry_after = float(response.headers["Retry-After"])
        except (TypeError, ValueError):
            retry_after = None
        return LLMCallError(
            message,
            category=LLMErrorCategory.TRANSIENT,
            retryable=True,
            status_code=status,
            retry_after_seconds=retry_after,
        )
    if _is_context_limit_error(status, message):
        return LLMCallError(
            message,
            category=LLMErrorCategory.CONTEXT_LIMIT,
            retryable=False,
            status_code=status,
        )
    return LLMCallError(
        message,
        category=LLMErrorCategory.CONFIGURATION,
        retryable=False,
        status_code=status,
    )


_CONTEXT_LIMIT_MARKERS = (
    "context length",
    "context window",
    "context_limit",
    "context_length_exceeded",
    "maximum context",
    "max context",
    "prompt is too long",
    "prompt too long",
    "input is too long",
    "input too long",
    "too many input tokens",
    "exceeds the maximum number of tokens",
    "exceed the maximum token",
    "token limit exceeded",
    "request too large",
    "上下文长度",
    "上下文窗口",
    "超过最大上下文",
    "输入过长",
)


def _is_context_limit_error(status_code: Optional[int], message: str) -> bool:
    """Conservatively recognize provider-declared input/context overflow."""

    if status_code == 413:
        return True
    if status_code not in {400, 422}:
        return False
    normalized = message.casefold()
    return any(marker in normalized for marker in _CONTEXT_LIMIT_MARKERS)


def _exception_text(exc: BaseException) -> str:
    parts = [str(exc)]
    body = getattr(exc, "body", None)
    if body is not None:
        try:
            parts.append(json.dumps(body, ensure_ascii=False))
        except (TypeError, ValueError):
            parts.append(str(body))
    response = getattr(exc, "response", None)
    response_text = getattr(response, "text", None)
    if response_text:
        parts.append(str(response_text))
    return "\n".join(part for part in parts if part)


class LLMAdapter(ABC):
    def __init__(self, profile: LLMModelProfile) -> None:
        self.profile = profile

    @abstractmethod
    def complete(self, request: LLMRequest) -> LLMResult:
        raise NotImplementedError

    def close(self) -> None:
        """Release provider resources owned by this adapter, if any."""



class OpenAICompatibleAdapter(LLMAdapter):
    def __init__(self, profile: LLMModelProfile, client: Any = None) -> None:
        super().__init__(profile)
        self.client = client or openai.OpenAI(
            base_url=profile.base_url,
            api_key=profile.api_key or "not-required",
        )

    def complete(self, request: LLMRequest) -> LLMResult:
        kwargs: dict[str, Any] = {
            "model": self.profile.model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
            "temperature": request.temperature,
        }
        if request.max_output_tokens is not None:
            kwargs["max_tokens"] = request.max_output_tokens
        if request.response_schema is not None:
            if self.profile.dialect in {ProviderDialect.OPENAI, ProviderDialect.QWEN}:
                kwargs["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "structured_response",
                        "strict": True,
                        "schema": dict(request.response_schema),
                    },
                }
            else:
                kwargs["response_format"] = {"type": "json_object"}
        try:
            response = self.client.chat.completions.create(**kwargs)
        except (openai.RateLimitError, openai.APITimeoutError, openai.APIConnectionError) as exc:
            retry_after: Optional[float] = None
            response = getattr(exc, "response", None)
            try:
                header_value = response.headers.get("Retry-After") if response else None
                retry_after = float(header_value) if header_value else None
            except (TypeError, ValueError):
                retry_after = None
            raise LLMCallError(
                str(exc),
                category=LLMErrorCategory.TRANSIENT,
                retryable=True,
                retry_after_seconds=retry_after,
            ) from exc
        except openai.InternalServerError as exc:
            raise LLMCallError(
                str(exc),
                category=LLMErrorCategory.TRANSIENT,
                retryable=True,
                status_code=getattr(exc, "status_code", None),
            ) from exc
        except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
            raise LLMCallError(
                str(exc),
                category=LLMErrorCategory.AUTHENTICATION,
                retryable=False,
                status_code=getattr(exc, "status_code", None),
            ) from exc
        except openai.APIStatusError as exc:
            status = getattr(exc, "status_code", None)
            retryable = status == 429 or (status is not None and status >= 500)
            message = _exception_text(exc)
            context_limit = _is_context_limit_error(status, message)
            raise LLMCallError(
                message,
                category=(
                    LLMErrorCategory.CONTEXT_LIMIT
                    if context_limit
                    else LLMErrorCategory.TRANSIENT
                    if retryable
                    else LLMErrorCategory.CONFIGURATION
                ),
                retryable=retryable and not context_limit,
                status_code=status,
            ) from exc

        choices = _read_attr(response, "choices", []) or []
        message = _read_attr(choices[0], "message") if choices else None
        text = _read_attr(message, "content", "") if message is not None else ""
        if not isinstance(text, str) or not text.strip():
            raise LLMCallError(
                "LLM provider returned empty content",
                category=LLMErrorCategory.INVALID_RESPONSE,
                retryable=False,
            )
        usage = _read_attr(response, "usage")
        prompt_details = _read_attr(usage, "prompt_tokens_details") if usage else None
        completion_details = (
            _read_attr(usage, "completion_tokens_details") if usage else None
        )
        normalized = LLMUsage(
            input_tokens=_optional_int(_read_attr(usage, "prompt_tokens")) if usage else None,
            output_tokens=(
                _optional_int(_read_attr(usage, "completion_tokens")) if usage else None
            ),
            cache_read_tokens=(
                next(
                    (
                        value
                        for value in (
                            _optional_int(_read_attr(prompt_details, "cached_tokens")),
                            _optional_int(_read_attr(usage, "cache_read_input_tokens")),
                            _optional_int(_read_attr(usage, "prompt_cache_hit_tokens")),
                            _optional_int(_read_attr(usage, "cached_tokens")),
                        )
                        if value is not None
                    ),
                    None,
                )
                if usage
                else None
            ),
            cache_write_tokens=(
                _optional_int(_read_attr(usage, "cache_creation_input_tokens"))
                if usage
                else None
            ),
            reasoning_tokens=(
                _optional_int(_read_attr(completion_details, "reasoning_tokens"))
                if completion_details
                else None
            ),
        )
        return LLMResult(text=text.strip(), usage=normalized, raw=response)


class AnthropicMessagesAdapter(LLMAdapter):
    def __init__(
        self,
        profile: LLMModelProfile,
        session: Optional[requests.Session] = None,
        timeout: float = 120.0,
    ) -> None:
        super().__init__(profile)
        self.session = session or requests.Session()
        self.timeout = timeout

    def complete(self, request: LLMRequest) -> LLMResult:
        system_text = "\n\n".join(
            item.content for item in request.messages if item.role == "system"
        )
        messages = [
            {"role": item.role, "content": item.content}
            for item in request.messages
            if item.role != "system"
        ]
        system: Any = system_text
        if system_text and request.cacheable_system_prefix:
            system = [
                {
                    "type": "text",
                    "text": system_text,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        payload: dict[str, Any] = {
            "model": self.profile.model,
            "system": system,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens or 4096,
        }
        if request.response_schema is not None:
            payload["tools"] = [
                {
                    "name": "structured_response",
                    "description": "Return the requested structured response.",
                    "input_schema": dict(request.response_schema),
                }
            ]
            payload["tool_choice"] = {
                "type": "tool",
                "name": "structured_response",
            }
        try:
            response = self.session.post(
                _endpoint(self.profile.base_url, "/v1/messages"),
                headers={
                    "x-api-key": self.profile.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=payload,
                timeout=self.timeout,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise LLMCallError(
                str(exc), category=LLMErrorCategory.TRANSIENT, retryable=True
            ) from exc
        if not response.ok:
            raise _http_error(response)
        try:
            value = response.json()
        except requests.JSONDecodeError as exc:
            raise LLMCallError(
                "Anthropic returned invalid JSON",
                category=LLMErrorCategory.INVALID_RESPONSE,
                retryable=False,
            ) from exc
        content = value.get("content", [])
        text = "".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, Mapping) and item.get("type") == "text"
        ).strip()
        if request.response_schema is not None:
            tool_inputs = [
                item.get("input")
                for item in content
                if isinstance(item, Mapping)
                and item.get("type") == "tool_use"
                and item.get("name") == "structured_response"
            ]
            if tool_inputs:
                text = json.dumps(tool_inputs[0], ensure_ascii=False)
        if not text:
            raise LLMCallError(
                "Anthropic returned empty content",
                category=LLMErrorCategory.INVALID_RESPONSE,
                retryable=False,
            )
        usage = value.get("usage", {})
        return LLMResult(
            text=text,
            usage=LLMUsage(
                input_tokens=_optional_int(usage.get("input_tokens")),
                output_tokens=_optional_int(usage.get("output_tokens")),
                cache_read_tokens=_optional_int(usage.get("cache_read_input_tokens")),
                cache_write_tokens=_optional_int(
                    usage.get("cache_creation_input_tokens")
                ),
            ),
            raw=value,
        )


class GeminiAdapter(LLMAdapter):
    _EXPLICIT_CACHE_MIN_PREFIX_CHARS = 8192

    def __init__(
        self,
        profile: LLMModelProfile,
        session: Optional[requests.Session] = None,
        timeout: float = 120.0,
    ) -> None:
        super().__init__(profile)
        self.session = session or requests.Session()
        self.timeout = timeout
        self._cache_lock = threading.Lock()
        self._cached_prefixes: dict[str, str] = {}
        self._uncacheable_prefixes: set[str] = set()

    def _cache_endpoint(self) -> str:
        return f"{self.profile.base_url.rstrip('/')}/cachedContents"

    def _delete_cached_content(self, name: str) -> None:
        try:
            self.session.delete(
                f"{self.profile.base_url.rstrip('/')}/{name}",
                params={"key": self.profile.api_key},
                timeout=self.timeout,
            )
        except (requests.Timeout, requests.ConnectionError):
            return

    def _drop_cached_prefix(self, fingerprint: str) -> None:
        with self._cache_lock:
            name = self._cached_prefixes.pop(fingerprint, "")
            self._uncacheable_prefixes.add(fingerprint)
        if name:
            self._delete_cached_content(name)

    def _prepare_cached_prefix(self, system_text: str) -> Optional[str]:
        """Best-effort explicit cache creation; failure always degrades safely."""

        if len(system_text) < self._EXPLICIT_CACHE_MIN_PREFIX_CHARS:
            return None
        fingerprint = hashlib.sha256(system_text.encode("utf-8")).hexdigest()
        with self._cache_lock:
            existing = self._cached_prefixes.get(fingerprint)
            if existing:
                return existing
            if fingerprint in self._uncacheable_prefixes:
                return None
            try:
                response = self.session.post(
                    self._cache_endpoint(),
                    params={"key": self.profile.api_key},
                    json={
                        "model": f"models/{self.profile.model}",
                        "systemInstruction": {"parts": [{"text": system_text}]},
                        "ttl": "3600s",
                        "displayName": f"videocaptioner-{fingerprint[:16]}",
                    },
                    timeout=self.timeout,
                )
                if response.ok:
                    value = response.json()
                    name = value.get("name") if isinstance(value, Mapping) else None
                    if isinstance(name, str) and name.startswith("cachedContents/"):
                        self._cached_prefixes[fingerprint] = name
                        return name
            except (requests.Timeout, requests.ConnectionError, requests.JSONDecodeError):
                pass
            self._uncacheable_prefixes.add(fingerprint)
            return None

    def close(self) -> None:
        with self._cache_lock:
            names = tuple(self._cached_prefixes.values())
            self._cached_prefixes.clear()
        for name in names:
            self._delete_cached_content(name)

    def complete(self, request: LLMRequest) -> LLMResult:
        system_text = "\n\n".join(
            item.content for item in request.messages if item.role == "system"
        )
        contents = [
            {
                "role": "model" if item.role == "assistant" else "user",
                "parts": [{"text": item.content}],
            }
            for item in request.messages
            if item.role != "system"
        ]
        generation_config: dict[str, Any] = {"temperature": request.temperature}
        if request.max_output_tokens is not None:
            generation_config["maxOutputTokens"] = request.max_output_tokens
        if request.response_schema is not None:
            generation_config["responseMimeType"] = "application/json"
            generation_config["responseSchema"] = dict(request.response_schema)
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": generation_config,
        }
        cached_content = (
            self._prepare_cached_prefix(system_text)
            if system_text and request.cacheable_system_prefix
            else None
        )
        if cached_content:
            payload["cachedContent"] = cached_content
        elif system_text:
            payload["systemInstruction"] = {"parts": [{"text": system_text}]}
        base = self.profile.base_url.rstrip("/")
        url = f"{base}/models/{quote(self.profile.model, safe='')}:generateContent"
        try:
            response = self.session.post(
                url,
                params={"key": self.profile.api_key},
                json=payload,
                timeout=self.timeout,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise LLMCallError(
                str(exc), category=LLMErrorCategory.TRANSIENT, retryable=True
            ) from exc
        if not response.ok and cached_content:
            fingerprint = hashlib.sha256(system_text.encode("utf-8")).hexdigest()
            self._drop_cached_prefix(fingerprint)
            payload.pop("cachedContent", None)
            payload["systemInstruction"] = {"parts": [{"text": system_text}]}
            try:
                response = self.session.post(
                    url,
                    params={"key": self.profile.api_key},
                    json=payload,
                    timeout=self.timeout,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                raise LLMCallError(
                    str(exc), category=LLMErrorCategory.TRANSIENT, retryable=True
                ) from exc
        if not response.ok:
            raise _http_error(response)
        try:
            value = response.json()
        except requests.JSONDecodeError as exc:
            raise LLMCallError(
                "Gemini returned invalid JSON",
                category=LLMErrorCategory.INVALID_RESPONSE,
                retryable=False,
            ) from exc
        candidates = value.get("candidates", [])
        parts = (
            candidates[0].get("content", {}).get("parts", [])
            if candidates and isinstance(candidates[0], Mapping)
            else []
        )
        text = "".join(
            str(item.get("text", "")) for item in parts if isinstance(item, Mapping)
        ).strip()
        if not text:
            raise LLMCallError(
                "Gemini returned empty content",
                category=LLMErrorCategory.INVALID_RESPONSE,
                retryable=False,
            )
        usage = value.get("usageMetadata", {})
        prompt_tokens = _optional_int(usage.get("promptTokenCount"))
        cached_tokens = _optional_int(usage.get("cachedContentTokenCount"))
        return LLMResult(
            text=text,
            usage=LLMUsage(
                input_tokens=prompt_tokens,
                output_tokens=_optional_int(usage.get("candidatesTokenCount")),
                cache_read_tokens=cached_tokens,
            ),
            raw=value,
        )
