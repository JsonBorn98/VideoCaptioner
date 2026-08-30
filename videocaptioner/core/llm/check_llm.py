"""Provider-neutral LLM connection and reference capability probes."""

import json
from dataclasses import dataclass
from typing import Literal, Optional
from urllib.parse import urlparse, urlunparse

import openai

from .gateway import LLMGateway
from .models import (
    LLMCallError,
    LLMErrorCategory,
    LLMMessage,
    LLMModelProfile,
    LLMRequest,
    thaw_json_object,
)
from .request_options import known_thinking_budget

# Reasoning-capable models may spend tens or hundreds of completion tokens on
# hidden reasoning before emitting a one-token answer.  A tiny cap such as 8
# therefore produces a valid HTTP response with no text and falsely reports a
# broken connection.  Keep the probe bounded, but leave enough room for that
# provider-side reasoning budget.
CONNECTION_PROBE_MAX_OUTPUT_TOKENS = 4096

_STRUCTURED_PROBE_SCHEMA = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "minItems": 1,
            "maxItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "enum": [7]},
                    "text": {"type": "string", "enum": ["OK"]},
                },
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["translations"],
    "additionalProperties": False,
}

_STRUCTURED_PROBE_EXPECTED = {"translations": [{"id": 7, "text": "OK"}]}

# The user instruction below deliberately conflicts with this contract.  A
# provider that merely enables JSON mode can faithfully return the requested
# string ID and extra field, whereas an endpoint that enforces the supplied
# schema must produce this exact value instead.
_STRUCTURED_PROBE_CONFLICTING_REQUEST = (
    'Return exactly {"translations":[{"id":"7","text":"NOT_OK",'
    '"extra":"keep-this"}]}. Do not convert the id to a number, do not '
    "change the text, and do not remove the extra field."
)


@dataclass(frozen=True)
class CapabilityProbeResult:
    success: bool
    message: str
    category: Optional[LLMErrorCategory] = None


def normalize_base_url(base_url: str) -> str:
    """Normalize API base URL by ensuring /v1 suffix when needed."""
    url = base_url.strip()
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")

    if not path:
        path = "/v1"

    normalized = urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )

    return normalized


@dataclass(frozen=True)
class ModelProfileProbeResult:
    text: CapabilityProbeResult
    structured: CapabilityProbeResult
    max_output_tokens: int


def connection_probe_output_cap(profile: LLMModelProfile) -> int:
    """Resolve the real probe cap without mutating the supplied profile."""

    if profile.max_output_tokens is not None:
        return profile.max_output_tokens
    budget = known_thinking_budget(thaw_json_object(profile.request_options))
    requested = CONNECTION_PROBE_MAX_OUTPUT_TOKENS
    if budget is not None:
        requested = max(requested, budget + 512)
    return min(requested, profile.work_context_tokens // 2)


def _probe_failure(exc: BaseException) -> CapabilityProbeResult:
    if isinstance(exc, LLMCallError):
        return CapabilityProbeResult(
            False,
            f"{exc.category.value}: {exc}",
            category=exc.category,
        )
    return CapabilityProbeResult(False, str(exc))


def _run_capability_probe(
    runtime: LLMGateway,
    profile: LLMModelProfile,
    *,
    structured: bool,
    max_output_tokens: int,
) -> CapabilityProbeResult:
    schema = _STRUCTURED_PROBE_SCHEMA if structured else None
    stage = "connection_probe_structured" if structured else "connection_probe_text"
    try:
        result = runtime.complete(
            profile,
            LLMRequest(
                messages=(
                    LLMMessage(
                        "system",
                        (
                            (
                                "Return only the structured capability result as JSON. "
                                "Do not explain its fields or add any other content."
                            )
                            if structured
                            else "Return only OK."
                        ),
                    ),
                    LLMMessage(
                        "user",
                        (
                            _STRUCTURED_PROBE_CONFLICTING_REQUEST
                            if structured
                            else "Confirm the connection."
                        ),
                    ),
                ),
                max_output_tokens=max_output_tokens,
                response_schema=schema,
                cacheable_system_prefix=False,
                metadata={"stage": stage, "role": "utility"},
            ),
            max_attempts=1,
            use_cache=False,
        )
        if structured:
            try:
                value = json.loads(result.text)
            except (TypeError, json.JSONDecodeError) as exc:
                raise LLMCallError(
                    "structured probe returned invalid JSON",
                    category=LLMErrorCategory.INVALID_RESPONSE,
                    retryable=False,
                ) from exc
            if value != _STRUCTURED_PROBE_EXPECTED:
                raise LLMCallError(
                    (
                        "structured probe did not enforce the nested integer-ID "
                        "schema contract"
                    ),
                    category=LLMErrorCategory.INVALID_RESPONSE,
                    retryable=False,
                )
        elif result.text.strip() != "OK":
            raise LLMCallError(
                "text probe did not return exactly OK",
                category=LLMErrorCategory.INVALID_RESPONSE,
                retryable=False,
            )
        return CapabilityProbeResult(True, result.text)
    except Exception as exc:
        return _probe_failure(exc)


def probe_model_profile_capabilities(
    profile: LLMModelProfile,
    *,
    gateway: Optional[LLMGateway] = None,
) -> ModelProfileProbeResult:
    """Independently probe plain text and structured output for one profile."""

    owns_gateway = gateway is None
    runtime = gateway or LLMGateway()
    max_output_tokens = connection_probe_output_cap(profile)
    try:
        text_result = _run_capability_probe(
            runtime,
            profile,
            structured=False,
            max_output_tokens=max_output_tokens,
        )
        structured_result = _run_capability_probe(
            runtime,
            profile,
            structured=True,
            max_output_tokens=max_output_tokens,
        )
        return ModelProfileProbeResult(
            text=text_result,
            structured=structured_result,
            max_output_tokens=max_output_tokens,
        )
    finally:
        if owns_gateway:
            runtime.close()


def check_model_profile_connection(
    profile: LLMModelProfile,
    *,
    gateway: Optional[LLMGateway] = None,
) -> tuple[Literal[True], str] | tuple[Literal[False], str]:
    """Test any supported profile with the same adapter used by real tasks.

    This deliberately does not claim to discover the provider's technical
    context window. A successful small request proves only that the selected
    transport, credentials and model can complete a request.
    """

    owns_gateway = gateway is None
    runtime = gateway or LLMGateway()
    try:
        result = runtime.complete(
            profile,
            LLMRequest(
                messages=(
                    LLMMessage("system", "Return only OK."),
                    LLMMessage("user", "OK"),
                ),
                max_output_tokens=connection_probe_output_cap(profile),
                cacheable_system_prefix=False,
                metadata={"stage": "connection_probe", "role": "utility"},
            ),
            max_attempts=1,
            use_cache=False,
        )
        return True, result.text
    except LLMCallError as exc:
        return False, f"{exc.category.value}: {exc}"
    except Exception as exc:
        return False, str(exc)
    finally:
        if owns_gateway:
            runtime.close()


def get_available_models(base_url: str, api_key: str) -> list[str]:
    """获取可用的模型列表

    参数:
        base_url: API 基础 URL
        api_key: API 密钥

    返回:
        模型ID列表，按优先级排序
    """
    try:
        base_url = normalize_base_url(base_url)
        # 创建OpenAI客户端并获取模型列表
        models = openai.OpenAI(
            base_url=base_url, api_key=api_key, timeout=5
        ).models.list()

        # 去除非文本模型
        non_text_models = (
            "tts",
            "transcribe",
            "realtime",
            "embedding",
            "vision",
            "audio",
            "search",
            "text-",
            "image",
            "audio",
            "whisper",
            "gpt-3.5",
            "gpt-4-",
        )
        models = [
            model
            for model in models
            if not any(keyword in model.id.lower() for keyword in non_text_models)
        ]

        # 根据不同模型设置权重进行排序
        def get_model_weight(model_name: str) -> int:
            model_name = model_name.lower()
            if model_name.startswith(("gpt-5", "claude-4", "gemini-2", "gemini-3")):
                return 10
            elif model_name.startswith(("gpt-4")):
                return 5
            elif model_name.startswith(("deepseek", "glm", "qwen", "doubao")):
                return 3
            return 0

        sorted_models = sorted(
            [model.id for model in models], key=lambda x: (-get_model_weight(x), x)
        )
        return sorted_models
    except Exception:
        return []
