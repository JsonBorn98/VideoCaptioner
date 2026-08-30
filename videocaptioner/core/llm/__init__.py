"""LLM unified client module."""

from .check_llm import (
    check_llm_connection,
    check_model_profile_connection,
    get_available_models,
)
from .check_whisper import check_whisper_connection
from .client import call_llm, get_llm_client
from .gateway import LLMGateway
from .models import (
    JSONValue,
    LLMCallError,
    LLMMessage,
    LLMModelProfile,
    LLMRequest,
    LLMResult,
    LLMTransport,
    LLMUsage,
    OpenAIEndpoint,
    ProviderDialect,
    llm_messages_from_dicts,
    thaw_json_object,
)
from .profiles import LLMModelProfileStore
from .utility import (
    UtilityProfileError,
    resolve_utility_profile,
    validate_utility_profile,
)

__all__ = [
    "call_llm",
    "get_llm_client",
    "check_llm_connection",
    "check_model_profile_connection",
    "get_available_models",
    "check_whisper_connection",
    "JSONValue",
    "LLMCallError",
    "LLMGateway",
    "LLMMessage",
    "LLMModelProfile",
    "LLMModelProfileStore",
    "LLMRequest",
    "LLMResult",
    "LLMTransport",
    "LLMUsage",
    "OpenAIEndpoint",
    "ProviderDialect",
    "UtilityProfileError",
    "llm_messages_from_dicts",
    "resolve_utility_profile",
    "thaw_json_object",
    "validate_utility_profile",
]
