"""Provider-neutral LLM connection and reference capability probes."""

from typing import Literal, Optional

import openai

from videocaptioner.core.llm.client import normalize_base_url

from .gateway import LLMGateway
from .models import (
    LLMCallError,
    LLMMessage,
    LLMModelProfile,
    LLMRequest,
    LLMTransport,
    ProviderDialect,
)


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
                temperature=0,
                max_output_tokens=8,
                cacheable_system_prefix=False,
                metadata={"stage": "connection_probe", "role": "utility"},
            ),
            max_attempts=1,
        )
        return True, result.text
    except LLMCallError as exc:
        return False, f"{exc.category.value}: {exc}"
    except Exception as exc:
        return False, str(exc)
    finally:
        if owns_gateway:
            runtime.close()


def check_llm_connection(
    base_url: str, api_key: str, model: str
) -> tuple[Literal[True], Optional[str]] | tuple[Literal[False], Optional[str]]:
    """测试 LLM API 连接

    使用指定的API设置与LLM进行对话测试。

    参数:
        base_url: API 基础 URL
        api_key: API 密钥
        model: 模型名称

    返回:
        (是否成功, Error output或AI助手的回复)
    """
    profile = LLMModelProfile(
        profile_id="legacy-connection-check",
        name="Legacy connection check",
        transport=LLMTransport.OPENAI_COMPATIBLE,
        dialect=ProviderDialect.GENERIC,
        base_url=normalize_base_url(base_url),
        api_key=api_key.strip(),
        model=model,
    )
    return check_model_profile_connection(profile)


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
