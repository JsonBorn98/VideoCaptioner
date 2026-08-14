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
    OpenAIEndpoint,
    ProviderDialect,
)
from .request_options import (
    RequestOptionsError,
    merge_profile_request_options,
    validate_structured_output_compatibility,
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


def _diagnostic_text(value: Any) -> Optional[str]:
    """Normalize provider enums into a short, content-free diagnostic label."""

    value = getattr(value, "value", value)
    if not isinstance(value, str):
        return None
    normalized = "".join(
        character
        for character in value.strip()[:64]
        if character.isalnum() or character in {"_", "-", "."}
    )
    return normalized or None


def _openai_chat_usage(response: Any) -> LLMUsage:
    usage = _read_attr(response, "usage")
    prompt_details = _read_attr(usage, "prompt_tokens_details") if usage else None
    completion_details = (
        _read_attr(usage, "completion_tokens_details") if usage else None
    )
    return LLMUsage(
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


def _openai_responses_usage(response: Any) -> LLMUsage:
    usage = _read_attr(response, "usage")
    input_details = _read_attr(usage, "input_tokens_details") if usage else None
    output_details = _read_attr(usage, "output_tokens_details") if usage else None
    return LLMUsage(
        input_tokens=_optional_int(_read_attr(usage, "input_tokens")) if usage else None,
        output_tokens=_optional_int(_read_attr(usage, "output_tokens")) if usage else None,
        cache_read_tokens=(
            _optional_int(_read_attr(input_details, "cached_tokens"))
            if input_details
            else None
        ),
        reasoning_tokens=(
            _optional_int(_read_attr(output_details, "reasoning_tokens"))
            if output_details
            else None
        ),
    )


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
    diagnostic = response.text.strip()
    message = f"LLM provider returned HTTP {status}"
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
    if _is_context_limit_error(status, diagnostic):
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

    def _merge_request_options(self, application_body: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return merge_profile_request_options(self.profile, application_body)
        except RequestOptionsError as exc:
            raise LLMCallError(
                str(exc),
                category=LLMErrorCategory.CONFIGURATION,
                retryable=False,
            ) from exc

    def _validate_structured_output_compatibility(self) -> None:
        try:
            validate_structured_output_compatibility(self.profile)
        except RequestOptionsError as exc:
            raise LLMCallError(
                str(exc),
                category=LLMErrorCategory.CONFIGURATION,
                retryable=False,
            ) from exc



class OpenAICompatibleAdapter(LLMAdapter):
    def __init__(self, profile: LLMModelProfile, client: Any = None) -> None:
        super().__init__(profile)
        self.client = client or openai.OpenAI(
            base_url=profile.base_url,
            api_key=profile.api_key or "not-required",
        )

    def complete(self, request: LLMRequest) -> LLMResult:
        try:
            if self.profile.openai_endpoint is OpenAIEndpoint.RESPONSES:
                return self._complete_responses(request)
            return self._complete_chat(request)
        except (openai.RateLimitError, openai.APITimeoutError, openai.APIConnectionError) as exc:
            retry_after: Optional[float] = None
            response = getattr(exc, "response", None)
            try:
                header_value = response.headers.get("Retry-After") if response else None
                retry_after = float(header_value) if header_value else None
            except (TypeError, ValueError):
                retry_after = None
            raise LLMCallError(
                (
                    "LLM provider rate limit exceeded"
                    if isinstance(exc, openai.RateLimitError)
                    else "LLM provider request timed out"
                    if isinstance(exc, openai.APITimeoutError)
                    else "Could not connect to LLM provider"
                ),
                category=LLMErrorCategory.TRANSIENT,
                retryable=True,
                retry_after_seconds=retry_after,
            ) from None
        except openai.InternalServerError as exc:
            raise LLMCallError(
                f"LLM provider returned HTTP {getattr(exc, 'status_code', 500)}",
                category=LLMErrorCategory.TRANSIENT,
                retryable=True,
                status_code=getattr(exc, "status_code", None),
            ) from None
        except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
            raise LLMCallError(
                "LLM provider authentication or permission check failed",
                category=LLMErrorCategory.AUTHENTICATION,
                retryable=False,
                status_code=getattr(exc, "status_code", None),
            ) from None
        except openai.APIStatusError as exc:
            status = getattr(exc, "status_code", None)
            retryable = status == 429 or (status is not None and status >= 500)
            message = _exception_text(exc)
            context_limit = _is_context_limit_error(status, message)
            raise LLMCallError(
                (
                    f"LLM provider returned HTTP {status}"
                    if status is not None
                    else "LLM provider returned an API error"
                ),
                category=(
                    LLMErrorCategory.CONTEXT_LIMIT
                    if context_limit
                    else LLMErrorCategory.TRANSIENT
                    if retryable
                    else LLMErrorCategory.CONFIGURATION
                ),
                retryable=retryable and not context_limit,
                status_code=status,
            ) from None

    def _effective_output_cap(self, request: LLMRequest) -> Optional[int]:
        return (
            self.profile.max_output_tokens
            if self.profile.max_output_tokens is not None
            else request.max_output_tokens
        )

    def _complete_chat(self, request: LLMRequest) -> LLMResult:
        application_body: dict[str, Any] = {
            "model": self.profile.model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
            "stream": False,
            "n": 1,
            "store": False,
        }
        output_cap = self._effective_output_cap(request)
        if output_cap is not None:
            application_body["max_completion_tokens"] = output_cap
        if request.response_schema is not None:
            if self.profile.dialect in {ProviderDialect.OPENAI, ProviderDialect.QWEN}:
                application_body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "structured_response",
                        "strict": True,
                        "schema": dict(request.response_schema),
                    },
                }
            else:
                application_body["response_format"] = {"type": "json_object"}
        final_body = self._merge_request_options(application_body)
        kwargs = {
            name: final_body.pop(name)
            for name in (
                "model",
                "messages",
                "stream",
                "n",
                "max_completion_tokens",
                "response_format",
            )
            if name in final_body
        }
        kwargs["extra_body"] = final_body
        response = self.client.chat.completions.create(**kwargs)

        choices = _read_attr(response, "choices", []) or []
        choice = choices[0] if choices else None
        message = _read_attr(choice, "message") if choice is not None else None
        text = _read_attr(message, "content", "") if message is not None else ""
        finish_reason = _diagnostic_text(_read_attr(choice, "finish_reason"))
        response_status = _diagnostic_text(_read_attr(response, "status"))
        usage = _openai_chat_usage(response)
        if not isinstance(text, str) or not text.strip():
            refused = bool(_read_attr(message, "refusal")) if message is not None else False
            if refused:
                error_message = "LLM provider refused the request"
                response_status = response_status or "refusal"
            elif finish_reason in {"length", "max_tokens", "max_output_tokens"}:
                error_message = "LLM provider reached the output limit without final content"
            else:
                error_message = "LLM provider returned empty content"
            raise LLMCallError(
                error_message,
                category=LLMErrorCategory.INVALID_RESPONSE,
                retryable=(
                    not refused and finish_reason not in {"content_filter", "safety"}
                ),
                finish_reason=finish_reason,
                response_status=response_status,
                choice_count=len(choices),
                usage=usage,
            )
        return LLMResult(text=text.strip(), usage=usage, raw=response)

    def _complete_responses(self, request: LLMRequest) -> LLMResult:
        application_body: dict[str, Any] = {
            "model": self.profile.model,
            "input": [
                {
                    "role": message.role,
                    "content": [{"type": "input_text", "text": message.content}],
                }
                for message in request.messages
            ],
            "stream": False,
            "background": False,
            "store": False,
        }
        output_cap = self._effective_output_cap(request)
        if output_cap is not None:
            application_body["max_output_tokens"] = output_cap
        if request.response_schema is not None:
            application_body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "structured_response",
                    "strict": True,
                    "schema": dict(request.response_schema),
                }
            }
        final_body = self._merge_request_options(application_body)
        kwargs = {
            name: final_body.pop(name)
            for name in ("model", "input", "stream", "background", "max_output_tokens")
            if name in final_body
        }
        kwargs["extra_body"] = final_body
        response = self.client.responses.create(**kwargs)

        status_value = _diagnostic_text(_read_attr(response, "status"))
        status_label = status_value or "missing"
        incomplete_details = _read_attr(response, "incomplete_details")
        finish_reason = _diagnostic_text(_read_attr(incomplete_details, "reason"))
        usage = _openai_responses_usage(response)
        output_items = _read_attr(response, "output", []) or []
        if status_label != "completed":
            raise LLMCallError(
                f"Responses API returned non-completed status: {status_label}",
                category=LLMErrorCategory.INVALID_RESPONSE,
                retryable=status_label in {"incomplete", "missing"},
                finish_reason=finish_reason,
                response_status=status_label,
                choice_count=len(output_items),
                usage=usage,
            )

        text_parts: list[str] = []
        refused = False
        for output_item in output_items:
            if _read_attr(output_item, "type") != "message":
                continue
            for content_item in _read_attr(output_item, "content", []) or []:
                content_type = _read_attr(content_item, "type")
                if content_type == "refusal":
                    refused = True
                elif content_type == "output_text":
                    part = _read_attr(content_item, "text")
                    if isinstance(part, str):
                        text_parts.append(part)
        if refused:
            raise LLMCallError(
                "Responses API refused the request",
                category=LLMErrorCategory.INVALID_RESPONSE,
                retryable=False,
                response_status="refusal",
                choice_count=len(output_items),
                usage=usage,
            )
        text = "".join(text_parts).strip()
        if not text:
            raise LLMCallError(
                "Responses API returned no final output_text",
                category=LLMErrorCategory.INVALID_RESPONSE,
                retryable=True,
                finish_reason=finish_reason,
                response_status=status_label,
                choice_count=len(output_items),
                usage=usage,
            )
        return LLMResult(text=text, usage=usage, raw=response)


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
        if request.response_schema is not None:
            self._validate_structured_output_compatibility()
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
        output_cap = (
            self.profile.max_output_tokens
            if self.profile.max_output_tokens is not None
            else request.max_output_tokens
        )
        application_body: dict[str, Any] = {
            "model": self.profile.model,
            "system": system,
            "messages": messages,
            "stream": False,
            "max_tokens": output_cap or 4096,
        }
        if request.response_schema is not None:
            application_body["tools"] = [
                {
                    "name": "structured_response",
                    "description": "Return the requested structured response.",
                    "input_schema": dict(request.response_schema),
                }
            ]
            application_body["tool_choice"] = {
                "type": "tool",
                "name": "structured_response",
            }
        payload = self._merge_request_options(application_body)
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
                (
                    "LLM provider request timed out"
                    if isinstance(exc, requests.Timeout)
                    else "Could not connect to LLM provider"
                ),
                category=LLMErrorCategory.TRANSIENT,
                retryable=True,
            ) from None
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
        raw_usage = value.get("usage", {})
        usage = LLMUsage(
            input_tokens=_optional_int(raw_usage.get("input_tokens")),
            output_tokens=_optional_int(raw_usage.get("output_tokens")),
            cache_read_tokens=_optional_int(raw_usage.get("cache_read_input_tokens")),
            cache_write_tokens=_optional_int(
                raw_usage.get("cache_creation_input_tokens")
            ),
        )
        finish_reason = _diagnostic_text(value.get("stop_reason"))
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
                retryable=finish_reason not in {"safety", "refusal"},
                finish_reason=finish_reason,
                response_status="empty",
                choice_count=len(content),
                usage=usage,
            )
        return LLMResult(
            text=text,
            usage=usage,
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
        generation_config: dict[str, Any] = {
            "candidateCount": 1,
        }
        output_cap = (
            self.profile.max_output_tokens
            if self.profile.max_output_tokens is not None
            else request.max_output_tokens
        )
        if output_cap is not None:
            generation_config["maxOutputTokens"] = output_cap
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
        payload = self._merge_request_options(payload)
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
                (
                    "LLM provider request timed out"
                    if isinstance(exc, requests.Timeout)
                    else "Could not connect to LLM provider"
                ),
                category=LLMErrorCategory.TRANSIENT,
                retryable=True,
            ) from None
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
                    (
                        "LLM provider request timed out"
                        if isinstance(exc, requests.Timeout)
                        else "Could not connect to LLM provider"
                    ),
                    category=LLMErrorCategory.TRANSIENT,
                    retryable=True,
                ) from None
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
        candidate = candidates[0] if candidates and isinstance(candidates[0], Mapping) else {}
        parts = (
            candidate.get("content", {}).get("parts", [])
            if isinstance(candidate.get("content"), Mapping)
            else []
        )
        raw_usage = value.get("usageMetadata", {})
        prompt_tokens = _optional_int(raw_usage.get("promptTokenCount"))
        cached_tokens = _optional_int(raw_usage.get("cachedContentTokenCount"))
        usage = LLMUsage(
            input_tokens=prompt_tokens,
            output_tokens=_optional_int(raw_usage.get("candidatesTokenCount")),
            cache_read_tokens=cached_tokens,
        )
        prompt_feedback = value.get("promptFeedback", {})
        block_reason = (
            _diagnostic_text(prompt_feedback.get("blockReason"))
            if isinstance(prompt_feedback, Mapping)
            else None
        )
        finish_reason = _diagnostic_text(candidate.get("finishReason")) or block_reason
        text = "".join(
            str(item.get("text", "")) for item in parts if isinstance(item, Mapping)
        ).strip()
        if not text:
            raise LLMCallError(
                "Gemini returned empty content",
                category=LLMErrorCategory.INVALID_RESPONSE,
                retryable=finish_reason not in {
                    "SAFETY",
                    "PROHIBITED_CONTENT",
                    "BLOCKLIST",
                },
                finish_reason=finish_reason,
                response_status="blocked" if block_reason else "empty",
                choice_count=len(candidates),
                usage=usage,
            )
        return LLMResult(
            text=text,
            usage=usage,
            raw=value,
        )
