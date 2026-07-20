"""LLM 翻译器（使用 OpenAI）"""

import hashlib
import json
from typing import Any, Callable, Dict, List, Optional, Tuple

import json_repair
import openai

from videocaptioner.core.llm import (
    LLMGateway,
    LLMMessage,
    LLMModelProfile,
    LLMRequest,
    call_llm,
)
from videocaptioner.core.prompts import get_prompt
from videocaptioner.core.translate.base import BaseTranslator, SubtitleProcessData, logger
from videocaptioner.core.translate.types import TargetLanguage
from videocaptioner.core.utils.cache import generate_cache_key


class LLMTranslator(BaseTranslator):
    """LLM 翻译器（OpenAI兼容API）"""

    MAX_STEPS = 3

    def __init__(
        self,
        thread_num: int,
        batch_num: int,
        target_language: TargetLanguage,
        model: str,
        custom_prompt: str,
        is_reflect: bool,
        update_callback: Optional[Callable],
        profile: Optional[LLMModelProfile] = None,
        gateway: Optional[LLMGateway] = None,
    ):
        super().__init__(
            thread_num=thread_num,
            batch_num=batch_num,
            target_language=target_language,
            update_callback=update_callback,
        )

        self.model = model
        self.custom_prompt = custom_prompt
        self.is_reflect = is_reflect
        self.profile = profile
        self.gateway = gateway or (LLMGateway() if profile is not None else None)

    def _translate_chunk(
        self, subtitle_chunk: List[SubtitleProcessData]
    ) -> List[SubtitleProcessData]:
        """翻译字幕块"""
        logger.debug(
            f"[+]正在翻译字幕: {subtitle_chunk[0].index} - {subtitle_chunk[-1].index}"
        )

        # 转换为字典格式用于API调用
        subtitle_dict = {str(data.index): data.original_text for data in subtitle_chunk}

        # 获取提示词
        if self.is_reflect:
            prompt = get_prompt(
                "translate/reflect",
                target_language=self.target_language,
                custom_prompt=self.custom_prompt,
            )
        else:
            prompt = get_prompt(
                "translate/standard",
                target_language=self.target_language,
                custom_prompt=self.custom_prompt,
            )

        try:
            # 使用agent loop进行翻译，自动验证和修正
            result_dict = self._agent_loop(prompt, subtitle_dict)

            # 处理反思翻译模式的结果
            if self.is_reflect and isinstance(result_dict, dict):
                processed_result = {
                    k: f"{v.get('native_translation', v) if isinstance(v, dict) else v}"
                    for k, v in result_dict.items()
                }
            else:
                processed_result = {k: f"{v}" for k, v in result_dict.items()}

            # 将结果填充回SubtitleProcessData
            for data in subtitle_chunk:
                data.translated_text = processed_result.get(
                    str(data.index), data.original_text
                )
            return subtitle_chunk
        except openai.RateLimitError as e:
            logger.error(f"OpenAI Rate Limit Error: {str(e)}")
            raise
        except openai.AuthenticationError as e:
            logger.error(f"OpenAI Authentication Error: {str(e)}")
            raise
        except openai.NotFoundError as e:
            logger.error(f"OpenAI NotFound Error: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"LLM translation error: {e}")
            raise
            return self._translate_chunk_single(subtitle_chunk)

    def _agent_loop(
        self, system_prompt: str, subtitle_dict: Dict[str, str]
    ) -> Dict[str, Any]:
        """Agent loop翻译字幕块"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(subtitle_dict, ensure_ascii=False)},
        ]
        # llm 反馈循环
        for attempt in range(self.MAX_STEPS):
            response_text = self._call_text(messages)
            response_dict = json_repair.loads(response_text)
            if not isinstance(response_dict, dict):
                response_dict = {}
            is_valid, error_message = self._validate_llm_response(
                response_dict, subtitle_dict
            )
            if is_valid:
                # Keep value structure intact: reflect mode stores nested
                # dicts ({"native_translation": ...}) that the caller extracts
                # via v.get("native_translation"). Only normalize keys to str.
                return {str(k): v for k, v in response_dict.items()}
            else:
                # Routine self-healing retry (mirrors optimize's agent loop);
                # a real, content-losing failure surfaces as the ValueError below.
                logger.debug(
                    f"翻译验证失败，开始反馈循环 (第{attempt + 1}次尝试): {error_message}"
                )
                messages.append(
                    {
                        "role": "assistant",
                        "content": response_text,
                    }
                )
                messages.append(
                    {
                        "role": "user",
                        "content": f"Error: {error_message}\n\nFix the errors above and output ONLY a valid JSON dictionary with ALL {len(subtitle_dict)} keys",
                    }
                )

        raise ValueError(
            "LLM response did not produce a valid translation dictionary "
            f"after {self.MAX_STEPS} attempts"
        )

    def _validate_llm_response(
        self, response_dict: Any, subtitle_dict: Dict[str, str]
    ) -> Tuple[bool, str]:
        """验证LLM翻译结果（支持普通和反思模式）

        Returns: (is_valid, error_feedback)
        """
        if not isinstance(response_dict, dict):
            return (
                False,
                f"Output must be a dict, got {type(response_dict).__name__}. Use format: {{'0': 'text', '1': 'text'}}",
            )

        expected_keys = set(subtitle_dict.keys())
        actual_keys = set(response_dict.keys())

        def sort_keys(keys):
            return sorted(keys, key=lambda x: int(x) if x.isdigit() else x)

        # 检查键是否匹配
        if expected_keys != actual_keys:
            missing = expected_keys - actual_keys
            extra = actual_keys - expected_keys
            error_parts = []

            if missing:
                error_parts.append(
                    f"Missing keys {sort_keys(missing)} - you must translate these items"
                )
            if extra:
                error_parts.append(
                    f"Extra keys {sort_keys(extra)} - these keys are not in input, remove them"
                )

            return (False, "; ".join(error_parts))

        # 如果是反思模式，检查嵌套结构
        if self.is_reflect:
            for key, value in response_dict.items():
                if not isinstance(value, dict):
                    return (
                        False,
                        f"Key '{key}': value must be a dict with 'native_translation' field. Got {type(value).__name__}.",
                    )

                if "native_translation" not in value:
                    available_keys = list(value.keys())
                    return (
                        False,
                        f"Key '{key}': missing 'native_translation' field. Found keys: {available_keys}. Must include 'native_translation'.",
                    )

        return True, ""

    def _call_text(self, messages: List[dict], *, temperature: float = 1) -> str:
        if self.profile is not None:
            assert self.gateway is not None
            result = self.gateway.complete(
                self.profile,
                LLMRequest(
                    messages=tuple(
                        LLMMessage(str(message["role"]), str(message["content"]))
                        for message in messages
                    ),
                    temperature=temperature,
                    metadata={"stage": "single_llm_translation", "role": "main"},
                ),
            )
            return result.text.strip()
        response = call_llm(messages=messages, model=self.model, temperature=temperature)
        return response.choices[0].message.content.strip()

    def _translate_chunk_single(
        self, subtitle_chunk: List[SubtitleProcessData]
    ) -> List[SubtitleProcessData]:
        """单条翻译模式"""
        single_prompt = get_prompt(
            "translate/single", target_language=self.target_language
        )

        for data in subtitle_chunk:
            try:
                translated_text = self._call_text(
                    [
                        {"role": "system", "content": single_prompt},
                        {"role": "user", "content": data.original_text},
                    ],
                    temperature=0.7,
                )
                data.translated_text = translated_text
            except Exception as e:
                logger.error(f"Single item translation failed {data.index}: {str(e)}")

        return subtitle_chunk

    def _get_cache_key(self, chunk: List[SubtitleProcessData]) -> str:
        """生成缓存键"""
        class_name = self.__class__.__name__
        chunk_key = generate_cache_key(chunk)
        lang = self.target_language.value
        semantic_inputs = {
            "model": self.model,
            "profile": self.profile.to_dict() if self.profile is not None else None,
            "custom_prompt": self.custom_prompt,
            "reflect": self.is_reflect,
        }
        digest = hashlib.sha256(
            json.dumps(semantic_inputs, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return f"{class_name}:v2:{chunk_key}:{lang}:{digest}"
