"""Native provider transports behind a provider-neutral interface."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from abc import ABC, abstractmethod
from dataclasses import replace
from typing import Any, Literal, Mapping, Optional
from urllib.parse import quote, urlparse, urlunparse

import openai
import requests

from videocaptioner.core.utils.logger import setup_logger

from .models import (
    LLMCallError,
    LLMErrorCategory,
    LLMModelProfile,
    LLMRequest,
    LLMResult,
    LLMUsage,
    OpenAIEndpoint,
    ProviderDialect,
    is_output_limit_finish_reason,
)
from .request_options import (
    RequestOptionsError,
    merge_profile_request_options,
    validate_structured_output_compatibility,
)

logger = setup_logger("llm_adapters")

StructuredChatStrategy = Literal["json_schema", "tool", "json_object"]

# Request deadline in seconds.  Every transport constructor defaults to this;
# the OpenAI SDK's own default is Timeout(connect=5.0, read=600, ...).
# LLMRequest.timeout overrides the adapter default for a single request.
DEFAULT_TIMEOUT_SECONDS = 120.0
TIMEOUT_SECONDS_PER_OUTPUT_TOKEN = 0.015


def request_timeout_seconds(
    max_output_tokens: Optional[int],
    *,
    baseline: float = DEFAULT_TIMEOUT_SECONDS,
) -> float:
    """Scale the request deadline with the output cap; small batches keep the baseline."""

    if max_output_tokens is None:
        return baseline
    return baseline + max_output_tokens * TIMEOUT_SECONDS_PER_OUTPUT_TOKEN

# Every transport labels the structured contract it sends, as a schema name or as
# a tool name.  Anthropic also matches the label when reading the reply back, so
# keep one identifier for all of them.
_STRUCTURED_RESPONSE_NAME = "structured_response"

# ``response_format={"type": "json_schema"}`` is only honoured by a subset of
# OpenAI-compatible services.  Several gateways accept the field and then ignore
# it, answering with unconstrained output and no HTTP error that would reveal the
# downgrade.  A forced function call carries the same schema through a path those
# providers do enforce, so route every dialect to the strongest contract it
# actually implements.
_NATIVE_SCHEMA_DIALECTS = frozenset(
    {
        ProviderDialect.OPENAI,
        ProviderDialect.QWEN,
        # Google's OpenAI compatibility layer implements response_format.
        ProviderDialect.GEMINI,
    }
)
_FORCED_TOOL_SCHEMA_DIALECTS = frozenset(
    {
        ProviderDialect.DEEPSEEK,
        ProviderDialect.KIMI,
        ProviderDialect.GLM,
        # A proxy fronting Anthropic can only express a schema as a tool, which
        # is exactly what AnthropicMessagesAdapter sends on the native transport.
        ProviderDialect.ANTHROPIC,
    }
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
    model_output_limit = _parse_model_output_limit(status, diagnostic)
    if model_output_limit is not None:
        return LLMCallError(
            message,
            category=LLMErrorCategory.OUTPUT_LIMIT,
            retryable=False,
            status_code=status,
            model_output_limit=model_output_limit,
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


_GEMINI_OUTPUT_RANGE = re.compile(
    r"maxoutputtokens.{0,240}supported range is from \d+ \(inclusive\) to (\d+) \(exclusive\)",
    re.DOTALL,
)
_ANTHROPIC_OUTPUT_LIMIT = re.compile(
    r"max_tokens:\s*\d+\s*>\s*(\d+).{0,120}maximum allowed number of output tokens",
    re.DOTALL,
)
_OPENAI_COMPLETION_CAP = re.compile(r"supports at most (\d+) completion tokens")


def _positive_token_limit(value: int) -> Optional[int]:
    return value if value >= 1 else None


def _parse_model_output_limit(status_code: Optional[int], diagnostic: str) -> Optional[int]:
    """Return the provider-stated model output cap, or None when the body has none.

    Only the documented 400/422 shapes are accepted. Missing or unreadable
    numbers stay None so callers pass the error through instead of guessing.
    """

    if status_code not in {400, 422}:
        return None
    normalized = diagnostic.casefold()
    gemini = _GEMINI_OUTPUT_RANGE.search(normalized)
    if gemini is not None:
        return _positive_token_limit(int(gemini.group(1)) - 1)
    anthropic = _ANTHROPIC_OUTPUT_LIMIT.search(normalized)
    if anthropic is not None:
        return _positive_token_limit(int(anthropic.group(1)))
    openai_cap = _OPENAI_COMPLETION_CAP.search(normalized)
    if openai_cap is not None:
        return _positive_token_limit(int(openai_cap.group(1)))
    return None


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


def _structured_tool_arguments(message: Any) -> Optional[str]:
    """Return the forced structured tool call's arguments as a JSON document.

    Compliant services send ``arguments`` as a JSON string.  Proxies frequently
    send the already-decoded object instead, so accept both and normalize to the
    string shape the rest of the pipeline parses.
    """

    for call in _read_attr(message, "tool_calls", []) or []:
        function = _read_attr(call, "function")
        if _read_attr(function, "name") != _STRUCTURED_RESPONSE_NAME:
            continue
        arguments = _read_attr(function, "arguments")
        if isinstance(arguments, str) and arguments.strip():
            return arguments.strip()
        if isinstance(arguments, Mapping):
            try:
                return json.dumps(arguments, ensure_ascii=False)
            except (TypeError, ValueError):
                return None
    return None


def _rejects_forced_tool_request(exc: openai.APIStatusError) -> bool:
    """Return whether a provider refused the request shape rather than its content.

    Endpoints that do not implement function calling reject the whole request,
    and the wording varies too much between gateways to match on reliably.  Treat
    any client-side rejection as a possible tool-capability gap so the schema can
    still be attempted through JSON mode, but never swallow input overflow, which
    carries its own category and would recur identically.
    """

    status = getattr(exc, "status_code", None)
    if status not in {400, 404, 422}:
        return False
    diagnostic = _exception_text(exc)
    if _parse_model_output_limit(status, diagnostic) is not None:
        return False
    return not _is_context_limit_error(status, diagnostic)


class LLMAdapter(ABC):
    # Every transport constructor sets this; see DEFAULT_TIMEOUT_SECONDS.
    timeout: float

    def __init__(self, profile: LLMModelProfile) -> None:
        self.profile = profile

    @abstractmethod
    def complete(self, request: LLMRequest) -> LLMResult:
        raise NotImplementedError

    def close(self) -> None:
        """Release provider resources owned by this adapter, if any."""

    def _effective_timeout(self, request: LLMRequest) -> float:
        """Return this request's deadline, falling back to the adapter default."""

        if request.timeout is not None:
            return request.timeout
        return request_timeout_seconds(request.max_output_tokens, baseline=self.timeout)

    def _effective_profile(self, request: LLMRequest) -> LLMModelProfile:
        if request.request_options_override is None:
            return self.profile
        return replace(
            self.profile,
            request_options=request.request_options_override,
        )

    def _merge_request_options(
        self, application_body: Mapping[str, Any], request: LLMRequest
    ) -> dict[str, Any]:
        try:
            return merge_profile_request_options(
                self._effective_profile(request), application_body
            )
        except RequestOptionsError as exc:
            raise LLMCallError(
                str(exc),
                category=LLMErrorCategory.CONFIGURATION,
                retryable=False,
            ) from exc

    def _validate_structured_output_compatibility(self, request: LLMRequest) -> None:
        try:
            validate_structured_output_compatibility(self._effective_profile(request))
        except RequestOptionsError as exc:
            raise LLMCallError(
                str(exc),
                category=LLMErrorCategory.CONFIGURATION,
                retryable=False,
            ) from exc



class OpenAICompatibleAdapter(LLMAdapter):
    def __init__(
        self,
        profile: LLMModelProfile,
        client: Any = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(profile)
        self.timeout = timeout
        self.client = client or openai.OpenAI(
            base_url=profile.base_url,
            api_key=profile.api_key or "not-required",
            timeout=timeout,
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
            diagnostic = _exception_text(exc)
            model_output_limit = _parse_model_output_limit(status, diagnostic)
            context_limit = (
                model_output_limit is None and _is_context_limit_error(status, diagnostic)
            )
            if model_output_limit is not None:
                category = LLMErrorCategory.OUTPUT_LIMIT
            elif context_limit:
                category = LLMErrorCategory.CONTEXT_LIMIT
            elif retryable:
                category = LLMErrorCategory.TRANSIENT
            else:
                category = LLMErrorCategory.CONFIGURATION
            raise LLMCallError(
                (
                    f"LLM provider returned HTTP {status}"
                    if status is not None
                    else "LLM provider returned an API error"
                ),
                category=category,
                retryable=retryable and not context_limit and model_output_limit is None,
                status_code=status,
                model_output_limit=model_output_limit,
            ) from None

    def _effective_output_cap(self, request: LLMRequest) -> Optional[int]:
        return (
            self.profile.max_output_tokens
            if self.profile.max_output_tokens is not None
            else request.max_output_tokens
        )

    def _transport_options(self, request: LLMRequest) -> dict[str, Any]:
        """Per-request transport options that never enter the HTTP body."""

        return {"timeout": self._effective_timeout(request)}

    def _structured_chat_strategy(self) -> StructuredChatStrategy:
        """Pick how to transmit ``response_schema`` on the chat completions API.

        An unidentified endpoint keeps bare JSON mode: it is the one structured
        control every OpenAI-compatible service accepts, and the pipeline already
        restates the schema in the prompt for providers that enforce nothing.
        """

        if self.profile.dialect in _NATIVE_SCHEMA_DIALECTS:
            return "json_schema"
        if self.profile.dialect in _FORCED_TOOL_SCHEMA_DIALECTS:
            return "tool"
        return "json_object"

    def _complete_chat(self, request: LLMRequest) -> LLMResult:
        strategy = (
            self._structured_chat_strategy() if request.response_schema is not None else None
        )
        try:
            return self._complete_chat_once(request, strategy)
        except openai.APIStatusError as exc:
            if strategy != "tool" or not _rejects_forced_tool_request(exc):
                raise
            logger.warning(
                "Model %s rejected the forced structured tool call (HTTP %s); "
                "retrying once in JSON mode",
                self.profile.model,
                getattr(exc, "status_code", None),
            )
        return self._complete_chat_once(request, "json_object")

    def _complete_chat_once(
        self, request: LLMRequest, strategy: Optional[StructuredChatStrategy]
    ) -> LLMResult:
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
        if strategy == "json_schema":
            application_body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": _STRUCTURED_RESPONSE_NAME,
                    "strict": True,
                    "schema": dict(request.response_schema or {}),
                },
            }
        elif strategy == "tool":
            application_body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": _STRUCTURED_RESPONSE_NAME,
                        "description": "Return the requested structured response.",
                        "parameters": dict(request.response_schema or {}),
                    },
                }
            ]
            application_body["tool_choice"] = {
                "type": "function",
                "function": {"name": _STRUCTURED_RESPONSE_NAME},
            }
        elif strategy == "json_object":
            application_body["response_format"] = {"type": "json_object"}
        final_body = self._merge_request_options(application_body, request)
        kwargs = {
            name: final_body.pop(name)
            for name in (
                "model",
                "messages",
                "stream",
                "n",
                "max_completion_tokens",
                "response_format",
                "tools",
                "tool_choice",
            )
            if name in final_body
        }
        kwargs["extra_body"] = final_body
        kwargs.update(self._transport_options(request))
        response = self.client.chat.completions.create(**kwargs)

        choices = _read_attr(response, "choices", []) or []
        choice = choices[0] if choices else None
        message = _read_attr(choice, "message") if choice is not None else None
        text = _read_attr(message, "content", "") if message is not None else ""
        if strategy == "tool" and message is not None:
            # A provider that ignores the forced tool choice still answers in
            # content, which is no worse than what JSON mode would have returned.
            arguments = _structured_tool_arguments(message)
            if arguments is not None:
                text = arguments
        finish_reason = _diagnostic_text(_read_attr(choice, "finish_reason"))
        response_status = _diagnostic_text(_read_attr(response, "status"))
        usage = _openai_chat_usage(response)
        if is_output_limit_finish_reason(finish_reason):
            raise LLMCallError(
                "LLM provider reached the output limit before final content",
                category=LLMErrorCategory.INVALID_RESPONSE,
                retryable=True,
                finish_reason=finish_reason,
                response_status=response_status,
                choice_count=len(choices),
                usage=usage,
            )
        if not isinstance(text, str) or not text.strip():
            refused = bool(_read_attr(message, "refusal")) if message is not None else False
            if refused:
                error_message = "LLM provider refused the request"
                response_status = response_status or "refusal"
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
                    "name": _STRUCTURED_RESPONSE_NAME,
                    "strict": True,
                    "schema": dict(request.response_schema),
                }
            }
        final_body = self._merge_request_options(application_body, request)
        kwargs = {
            name: final_body.pop(name)
            for name in ("model", "input", "stream", "background", "max_output_tokens")
            if name in final_body
        }
        kwargs["extra_body"] = final_body
        kwargs.update(self._transport_options(request))
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
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(profile)
        self.session = session or requests.Session()
        self.timeout = timeout

    def complete(self, request: LLMRequest) -> LLMResult:
        if request.response_schema is not None:
            self._validate_structured_output_compatibility(request)
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
                    "name": _STRUCTURED_RESPONSE_NAME,
                    "description": "Return the requested structured response.",
                    "input_schema": dict(request.response_schema),
                }
            ]
            application_body["tool_choice"] = {
                "type": "tool",
                "name": _STRUCTURED_RESPONSE_NAME,
            }
        payload = self._merge_request_options(application_body, request)
        try:
            response = self.session.post(
                _endpoint(self.profile.base_url, "/v1/messages"),
                headers={
                    "x-api-key": self.profile.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=payload,
                timeout=self._effective_timeout(request),
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
                and item.get("name") == _STRUCTURED_RESPONSE_NAME
            ]
            if tool_inputs:
                text = json.dumps(tool_inputs[0], ensure_ascii=False)
        if is_output_limit_finish_reason(finish_reason):
            raise LLMCallError(
                "Anthropic reached the output limit before final content",
                category=LLMErrorCategory.INVALID_RESPONSE,
                retryable=True,
                finish_reason=finish_reason,
                response_status="truncated",
                choice_count=len(content),
                usage=usage,
            )
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
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
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
        payload = self._merge_request_options(payload, request)
        base = self.profile.base_url.rstrip("/")
        url = f"{base}/models/{quote(self.profile.model, safe='')}:generateContent"
        try:
            response = self.session.post(
                url,
                params={"key": self.profile.api_key},
                json=payload,
                timeout=self._effective_timeout(request),
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
                    timeout=self._effective_timeout(request),
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
        if is_output_limit_finish_reason(finish_reason):
            raise LLMCallError(
                "Gemini reached the output limit before final content",
                category=LLMErrorCategory.INVALID_RESPONSE,
                retryable=True,
                finish_reason=finish_reason,
                response_status="truncated",
                choice_count=len(candidates),
                usage=usage,
            )
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
