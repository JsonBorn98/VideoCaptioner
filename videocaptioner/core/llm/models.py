"""Provider-neutral LLM request, response, usage and profile models."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence


class LLMTransport(str, Enum):
    OPENAI_COMPATIBLE = "openai-compatible"
    ANTHROPIC_MESSAGES = "anthropic-messages"
    GEMINI = "gemini"


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

    def __post_init__(self) -> None:
        if not _PROFILE_ID_RE.fullmatch(self.profile_id):
            raise ValueError("profile_id must contain 1-64 lowercase ASCII id characters")
        name = self.name.strip()
        if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
            raise ValueError("name must contain 1-80 printable characters")
        if not self.base_url.strip():
            raise ValueError("base_url is required")
        if not self.model.strip():
            raise ValueError("model is required")
        if self.work_context_tokens < 16_384:
            raise ValueError("work_context_tokens must be at least 16384")
        if not 1 <= self.max_concurrency <= 50:
            raise ValueError("max_concurrency must be between 1 and 50")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "base_url", self.base_url.strip())
        object.__setattr__(self, "model", self.model.strip())

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
        }
        if set(value) != expected:
            raise ValueError("model profile fields do not match schema")
        return cls(
            profile_id=str(value["id"]),
            name=str(value["name"]),
            transport=LLMTransport(str(value["transport"])),
            dialect=ProviderDialect(str(value["dialect"])),
            base_url=str(value["base_url"]),
            api_key=str(value["api_key"]),
            model=str(value["model"]),
            work_context_tokens=int(value["work_context_tokens"]),
            max_concurrency=int(value["max_concurrency"]),
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
    temperature: float = 0.2
    max_output_tokens: Optional[int] = None
    response_schema: Optional[Mapping[str, Any]] = None
    cacheable_system_prefix: bool = True
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
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
    ) -> None:
        super().__init__(message)
        self.category = category
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.attempts = attempts
