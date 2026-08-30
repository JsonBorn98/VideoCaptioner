"""Optional text shortening for time-constrained dubbing."""

import json
from typing import Any, Iterable, Optional

from videocaptioner.core.llm import LLMGateway, LLMMessage, LLMModelProfile, LLMRequest
from videocaptioner.core.llm.utility import (
    UTILITY_PROFILE_CARD,
    UtilityProfileError,
    borrow_utility_gateway,
)
from videocaptioner.core.utils.logger import setup_logger
from videocaptioner.core.utils.text_utils import is_mainly_cjk

from .models import DubbingConfig, DubbingSegment

logger = setup_logger("dubbing.rewriter")

# Formal JSON Schema for the rewrite response {items:[{index,text}]}. Providers
# with native structured output enforce the shape; the generic dialect keeps
# bare JSON mode, where the prompt already restates it.
REWRITE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "text": {"type": "string"},
                },
                "required": ["index", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def should_rewrite(segment: DubbingSegment, threshold: float) -> bool:
    """Estimate whether text is likely too long for its target duration."""
    duration_s = max(segment.target_duration_ms / 1000, 0.1)
    text = segment.text.strip()
    if is_mainly_cjk(text):
        required = len(text) / duration_s
        comfortable = 5.5
    else:
        required = max(1, len(text.split())) / duration_s
        comfortable = 2.7
    return required > comfortable * threshold


def rewrite_segments_if_needed(
    segments: Iterable[DubbingSegment],
    config: DubbingConfig,
    profile: Optional[LLMModelProfile] = None,
    *,
    gateway: Optional[LLMGateway] = None,
) -> None:
    """Shorten long subtitle lines with the utility-role model profile.

    The profile and gateway follow the shared consumer seam: callers inject a
    resolved model profile (and, in tests, a fake gateway); a missing gateway
    is constructed lazily and released again when this call owns it.
    """
    if not config.rewrite_too_long:
        return
    if profile is None:
        raise UtilityProfileError(
            "配音改写已开启但未提供模型配置方案，"
            f"请到{UTILITY_PROFILE_CARD}选择或创建模型配置方案"
        )

    targets = [seg for seg in segments if should_rewrite(seg, config.rewrite_threshold)]
    if not targets:
        return

    logger.info("dubbing rewrite: %d segment(s) over duration threshold", len(targets))
    payload = [
        {
            "index": seg.index,
            "duration_seconds": round(seg.target_duration_ms / 1000, 2),
            "speaker": seg.speaker,
            "text": seg.text,
        }
        for seg in targets
    ]
    request = LLMRequest(
        messages=(
            LLMMessage(
                "system",
                "You shorten subtitle dubbing lines while preserving meaning, language, "
                "speaker intent, names, numbers, and key facts. Return only JSON.",
            ),
            LLMMessage(
                "user",
                "Rewrite only lines that are too long for the duration. Keep one output "
                "per input index. Make each line natural to speak and shorter. JSON format: "
                '{"items":[{"index":1,"text":"..."}]}\n\n'
                f"{json.dumps({'items': payload}, ensure_ascii=False)}",
            ),
        ),
        max_output_tokens=profile.max_output_tokens,
        response_schema=REWRITE_RESPONSE_SCHEMA,
        metadata={"stage": "llm_dub_rewrite", "role": "utility"},
    )
    with borrow_utility_gateway(gateway) as runtime:
        result = runtime.complete(profile, request)
    parsed = json.loads(result.text)
    items = parsed.get("items", []) if isinstance(parsed, dict) else []
    rewritten = {
        int(item["index"]): str(item["text"]).strip()
        for item in items
        if isinstance(item, dict) and item.get("text")
    }
    for seg in targets:
        new_text = rewritten.get(seg.index)
        if new_text:
            seg.rewritten_text = new_text

    applied = sum(1 for seg in targets if seg.rewritten_text)
    logger.info("dubbing rewrite: applied %d of %d", applied, len(targets))
    if applied < len(targets):
        logger.warning(
            "dubbing rewrite: LLM returned %d of %d requested lines", applied, len(targets)
        )
