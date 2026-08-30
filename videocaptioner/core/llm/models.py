"""Provider-neutral LLM request, response, usage and profile models."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence, TypeAlias, Union, cast


class LLMTransport(str, Enum):
    OPENAI_COMPATIBLE = "openai-compatible"
    ANTHROPIC_MESSAGES = "anthropic-messages"
    GEMINI = "gemini"


class OpenAIEndpoint(str, Enum):
    CHAT_COMPLETIONS = "chat_completions"
    RESPONSES = "responses"


class ProviderDialect(str, Enum):
    GENERIC = "generic"
    OPENAI = "openai"
    DEEPSEEK = "deepseek"
    KIMI = "kimi"
    GLM = "glm"
    QWEN = "qwen"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"


_PROFILE_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")

REQUEST_OPTIONS_MAX_BYTES = 64 * 1024
REQUEST_OPTIONS_MAX_DEPTH = 16

JSONScalar: TypeAlias = Union[None, bool, int, float, str]
JSONValue: TypeAlias = Union[
    JSONScalar,
    list["JSONValue"],
    tuple["JSONValue", ...],
    Mapping[str, "JSONValue"],
]


def _freeze_json_value(value: Any, *, container_depth: int) -> JSONValue:
    if value is None or type(value) in {bool, int, str}:
        return cast(JSONScalar, value)
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("request_options numbers must be finite")
        return value
    if isinstance(value, Mapping):
        if container_depth > REQUEST_OPTIONS_MAX_DEPTH:
            raise ValueError(
                f"request_options nesting depth must not exceed {REQUEST_OPTIONS_MAX_DEPTH}"
            )
        frozen: dict[str, JSONValue] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("request_options object keys must be strings")
            frozen[key] = _freeze_json_value(item, container_depth=container_depth + 1)
        return MappingProxyType(frozen)
    if type(value) in {list, tuple}:
        if container_depth > REQUEST_OPTIONS_MAX_DEPTH:
            raise ValueError(
                f"request_options nesting depth must not exceed {REQUEST_OPTIONS_MAX_DEPTH}"
            )
        return tuple(
            _freeze_json_value(item, container_depth=container_depth + 1) for item in value
        )
    raise ValueError(
        "request_options values must be JSON null, booleans, numbers, strings, arrays, or objects"
    )


def thaw_json_value(value: JSONValue) -> Any:
    """Return a mutable, JSON-serializable copy of a frozen JSON value."""

    if isinstance(value, Mapping):
        return {key: thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw_json_value(item) for item in value]
    return value


def thaw_json_object(value: Mapping[str, JSONValue]) -> dict[str, Any]:
    """Return a mutable, JSON-serializable copy of a frozen JSON object."""

    return {key: thaw_json_value(item) for key, item in value.items()}


def freeze_json_object(value: Mapping[str, Any]) -> Mapping[str, JSONValue]:
    """Validate, copy and recursively freeze a request-options JSON object."""

    if not isinstance(value, Mapping):
        raise ValueError("request_options must be a JSON object")
    frozen = _freeze_json_value(value, container_depth=1)
    frozen_object = cast(Mapping[str, JSONValue], frozen)
    mutable = thaw_json_object(frozen_object)
    try:
        payload = json.dumps(
            mutable,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("request_options must be valid UTF-8 JSON") from exc
    if len(payload) > REQUEST_OPTIONS_MAX_BYTES:
        raise ValueError(
            f"request_options must not exceed {REQUEST_OPTIONS_MAX_BYTES} UTF-8 bytes"
        )
    return frozen_object


@dataclass(frozen=True)
class LLMModelProfile:
    """A named, reusable connection and model configuration."""

    profile_id: str
    name: str
    transport: LLMTransport
    dialect: ProviderDialect
    base_url: str
    api_key: str
    model: str
    work_context_tokens: int = 65_536
    max_concurrency: int = 4
    openai_endpoint: OpenAIEndpoint = OpenAIEndpoint.CHAT_COMPLETIONS
    request_options: Mapping[str, JSONValue] = field(default_factory=dict)
    max_output_tokens: Optional[int] = None

    def __post_init__(self) -> None:
        if type(self.profile_id) is not str:
            raise ValueError("profile_id must be a string")
        if not _PROFILE_ID_RE.fullmatch(self.profile_id):
            raise ValueError("profile_id must contain 1-64 lowercase ASCII id characters")
        if type(self.name) is not str:
            raise ValueError("name must be a string")
        name = self.name.strip()
        if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
            raise ValueError("name must contain 1-80 printable characters")
        if not isinstance(self.transport, LLMTransport):
            raise ValueError("transport must be an LLMTransport")
        if not isinstance(self.dialect, ProviderDialect):
            raise ValueError("dialect must be a ProviderDialect")
        if not isinstance(self.openai_endpoint, OpenAIEndpoint):
            raise ValueError("openai_endpoint must be an OpenAIEndpoint")
        if (
            self.transport is not LLMTransport.OPENAI_COMPATIBLE
            and self.openai_endpoint is not OpenAIEndpoint.CHAT_COMPLETIONS
        ):
            raise ValueError(
                "openai_endpoint must be chat_completions for native LLM transports"
            )
        if type(self.base_url) is not str:
            raise ValueError("base_url must be a string")
        if not self.base_url.strip():
            raise ValueError("base_url is required")
        if type(self.api_key) is not str:
            raise ValueError("api_key must be a string")
        if type(self.model) is not str:
            raise ValueError("model must be a string")
        if not self.model.strip():
            raise ValueError("model is required")
        if type(self.work_context_tokens) is not int:
            raise ValueError("work_context_tokens must be an integer")
        if self.work_context_tokens < 16_384:
            raise ValueError("work_context_tokens must be at least 16384")
        if type(self.max_concurrency) is not int:
            raise ValueError("max_concurrency must be an integer")
        if not 1 <= self.max_concurrency <= 50:
            raise ValueError("max_concurrency must be between 1 and 50")
        if self.max_output_tokens is not None:
            if type(self.max_output_tokens) is not int:
                raise ValueError("max_output_tokens must be an integer or None")
            if not 1 <= self.max_output_tokens < self.work_context_tokens:
                raise ValueError(
                    "max_output_tokens must be at least 1 and less than work_context_tokens"
                )
        request_options = freeze_json_object(self.request_options)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "base_url", self.base_url.strip())
        object.__setattr__(self, "model", self.model.strip())
        object.__setattr__(self, "request_options", request_options)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.profile_id,
            "name": self.name,
            "transport": self.transport.value,
            "dialect": self.dialect.value,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "model": self.model,
            "work_context_tokens": self.work_context_tokens,
            "max_concurrency": self.max_concurrency,
            "openai_endpoint": self.openai_endpoint.value,
            "request_options": thaw_json_object(self.request_options),
            "max_output_tokens": self.max_output_tokens,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LLMModelProfile":
        expected = {
            "id",
            "name",
            "transport",
            "dialect",
            "base_url",
            "api_key",
            "model",
            "work_context_tokens",
            "max_concurrency",
            "openai_endpoint",
            "request_options",
            "max_output_tokens",
        }
        if not isinstance(value, Mapping):
            raise ValueError("model profile must be an object")
        if set(value) != expected:
            raise ValueError("model profile fields do not match schema")
        string_fields = {
            "id",
            "name",
            "transport",
            "dialect",
            "base_url",
            "api_key",
            "model",
            "openai_endpoint",
        }
        if any(type(value[key]) is not str for key in string_fields):
            raise ValueError("model profile string fields must be strings")
        if type(value["work_context_tokens"]) is not int:
            raise ValueError("work_context_tokens must be an integer")
        if type(value["max_concurrency"]) is not int:
            raise ValueError("max_concurrency must be an integer")
        if not isinstance(value["request_options"], Mapping):
            raise ValueError("request_options must be a JSON object")
        max_output_tokens = value["max_output_tokens"]
        if max_output_tokens is not None and type(max_output_tokens) is not int:
            raise ValueError("max_output_tokens must be an integer or null")
        return cls(
            profile_id=value["id"],
            name=value["name"],
            transport=LLMTransport(value["transport"]),
            dialect=ProviderDialect(value["dialect"]),
            base_url=value["base_url"],
            api_key=value["api_key"],
            model=value["model"],
            work_context_tokens=value["work_context_tokens"],
            max_concurrency=value["max_concurrency"],
            openai_endpoint=OpenAIEndpoint(value["openai_endpoint"]),
            request_options=value["request_options"],
            max_output_tokens=max_output_tokens,
        )


@dataclass(frozen=True)
class LLMMessage:
    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant"}:
            raise ValueError(f"unsupported message role: {self.role}")


@dataclass(frozen=True)
class LLMRequest:
    messages: Sequence[LLMMessage]
    # Deprecated compatibility input. Adapters intentionally never serialize it.
    temperature: float = 0.2
    max_output_tokens: Optional[int] = None
    response_schema: Optional[Mapping[str, Any]] = None
    cacheable_system_prefix: bool = True
    metadata: Mapping[str, str] = field(default_factory=dict)
    request_options_override: Optional[Mapping[str, JSONValue]] = None
    # None means "use the adapter's constructor default"; a value overrides the
    # adapter's request deadline for this request alone.
    timeout: Optional[float] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        if self.timeout is not None:
            if type(self.timeout) not in {int, float}:
                raise ValueError("timeout must be a positive number or None")
            if not math.isfinite(self.timeout) or self.timeout <= 0:
                raise ValueError("timeout must be a positive number or None")
        if self.request_options_override is not None:
            object.__setattr__(
                self,
                "request_options_override",
                freeze_json_object(self.request_options_override),
            )
        if self.response_schema is not None:
            object.__setattr__(
                self,
                "response_schema",
                MappingProxyType(dict(self.response_schema)),
            )


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None

    def __add__(self, other: "LLMUsage") -> "LLMUsage":
        def add_optional(left: Optional[int], right: Optional[int]) -> Optional[int]:
            if left is None and right is None:
                return None
            return (left or 0) + (right or 0)

        return LLMUsage(
            input_tokens=add_optional(self.input_tokens, other.input_tokens),
            output_tokens=add_optional(self.output_tokens, other.output_tokens),
            cache_read_tokens=add_optional(self.cache_read_tokens, other.cache_read_tokens),
            cache_write_tokens=add_optional(self.cache_write_tokens, other.cache_write_tokens),
            reasoning_tokens=add_optional(self.reasoning_tokens, other.reasoning_tokens),
        )


@dataclass(frozen=True)
class LLMResult:
    text: str
    usage: LLMUsage = field(default_factory=LLMUsage)
    raw: Any = field(default=None, repr=False, compare=False)


class LLMErrorCategory(str, Enum):
    TRANSIENT = "transient"
    AUTHENTICATION = "authentication"
    CONFIGURATION = "configuration"
    CONTEXT_LIMIT = "context-limit"
    INVALID_RESPONSE = "invalid-response"
    CANCELLED = "cancelled"


def is_output_limit_finish_reason(reason: Optional[str]) -> bool:
    """Return whether a provider finish label denotes generated-token exhaustion."""

    return bool(
        reason
        and reason.casefold().replace("-", "_")
        in {"length", "max_tokens", "max_output_tokens"}
    )


class LLMCallError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        category: LLMErrorCategory,
        retryable: bool,
        status_code: Optional[int] = None,
        retry_after_seconds: Optional[float] = None,
        attempts: int = 1,
        finish_reason: Optional[str] = None,
        response_status: Optional[str] = None,
        choice_count: Optional[int] = None,
        usage: Optional[LLMUsage] = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.attempts = attempts
        self.finish_reason = finish_reason
        self.response_status = response_status
        self.choice_count = choice_count
        self.usage = usage
