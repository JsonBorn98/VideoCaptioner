"""Utility-role model profile resolution shared by the GUI and the CLI.

工具角色（字幕断句、字幕优化、字幕后处理、配音改写、连接测试）的模型与连接统一
从模型配置方案体系解析：独立工具绑定优先，无绑定则从主翻译方案派生，两者都无则
抛带指引的专用异常——绝不静默回退（ADR-0014）。无论派生还是独立绑定，解析器一律
剥离翻译专属调优三字段，工具请求形态由解析器统一保证，与方案来源无关。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Optional

from .gateway import LLMGateway
from .models import LLMModelProfile, OpenAIEndpoint
from .profiles import LLMModelProfileStore, LLMProfileNotFoundError
from .request_options import (
    validate_profile_request_options,
    validate_structured_output_compatibility,
)

UTILITY_PROFILE_CARD = "翻译设置页·工具模型卡"

# 解析器与 GUI 启动预检共用的「无可解析方案」报错文案（原为三处字面量复制）。
UTILITY_PROFILE_UNRESOLVED_MESSAGE = (
    "未找到可用的模型配置方案：主翻译方案与工具模型绑定均为空，"
    f"请到{UTILITY_PROFILE_CARD}选择或创建模型配置方案"
)
# LLM 翻译缺主翻译方案时的报错文案（GUI 任务线程与重翻译线程共用）。
MAIN_LLM_PROFILE_MISSING_MESSAGE = (
    "未配置主翻译模型配置方案，无法使用 LLM 翻译；"
    "请到翻译设置页选择或创建模型配置方案"
)


class UtilityProfileError(ValueError):
    """Raised when no usable utility-role model profile can be resolved."""


def _stripped(profile: LLMModelProfile) -> LLMModelProfile:
    """Return a copy without the translation-only tuning fields."""

    return replace(
        profile,
        openai_endpoint=OpenAIEndpoint.CHAT_COMPLETIONS,
        request_options={},
        max_output_tokens=None,
    )


def _lookup(
    store: LLMModelProfileStore,
    profile_id: Optional[str],
    *,
    description: str,
) -> Optional[LLMModelProfile]:
    identifier = (profile_id or "").strip()
    if not identifier:
        return None
    try:
        return store.get(identifier)
    except LLMProfileNotFoundError:
        raise UtilityProfileError(
            f"{description}模型配置方案「{identifier}」已不存在，请到{UTILITY_PROFILE_CARD}"
            "重新绑定或恢复该方案"
        ) from None


def resolve_utility_profile(
    store: LLMModelProfileStore,
    main_profile_id: Optional[str],
    utility_profile_id: Optional[str] = None,
) -> LLMModelProfile:
    """Resolve the model profile for utility roles.

    Resolution order: an explicit utility binding wins; without one the profile
    is derived from the main translation profile. A lost binding (deleted
    profile) is an error, never a silent fallback to derivation. Neither a
    binding nor a main profile means there is nothing to serve utility roles
    from, which is also an error pointing at the utility model card. Profile
    ids are stripped before resolution, so a blank id reads as unbound.
    """

    bound = _lookup(store, utility_profile_id, description="工具角色绑定的")
    if bound is not None:
        return _stripped(bound)

    main = _lookup(store, main_profile_id, description="主翻译")
    if main is not None:
        return _stripped(main)

    raise UtilityProfileError(UTILITY_PROFILE_UNRESOLVED_MESSAGE)


def validate_utility_profile(profile: LLMModelProfile) -> None:
    """Local-only startup preflight for a resolved utility profile.

    Reuses the existing request-option validation, checks the tuning fields
    are at their stripped defaults, and keeps the structured-output bottom-line
    check (which always passes on stripped profiles) as a defensive bound.
    No real request is ever sent — matching the translation-path preflight.
    """

    validate_profile_request_options(profile)
    if profile.openai_endpoint is not OpenAIEndpoint.CHAT_COMPLETIONS:
        raise UtilityProfileError("utility profile must use the chat_completions endpoint")
    if profile.request_options:
        raise UtilityProfileError("utility profile must not carry request_options")
    if profile.max_output_tokens is not None:
        raise UtilityProfileError("utility profile must not cap output tokens")
    validate_structured_output_compatibility(profile)


@contextmanager
def borrow_utility_gateway(gateway: Optional[LLMGateway]) -> Iterator[LLMGateway]:
    """Yield a gateway for one functional consumer call, closing it if owned.

    Shared seam for the function-shaped utility consumers (dub rewrite,
    compress, semantic repair). An injected gateway is used as-is and never
    closed — the caller keeps its lifecycle. A missing one is constructed
    lazily here and released on exit, so self-built gateways can never leak.
    Long-lived constructor-held consumers (split/optimize, translation paths)
    hold their gateway for the object's lifetime and stay out of this seam.
    """

    if gateway is not None:
        yield gateway
        return
    runtime = LLMGateway()
    try:
        yield runtime
    finally:
        runtime.close()


__all__ = [
    "MAIN_LLM_PROFILE_MISSING_MESSAGE",
    "UTILITY_PROFILE_CARD",
    "UTILITY_PROFILE_UNRESOLVED_MESSAGE",
    "UtilityProfileError",
    "borrow_utility_gateway",
    "resolve_utility_profile",
    "validate_utility_profile",
]
