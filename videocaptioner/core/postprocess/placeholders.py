"""F1 占位符清理 —— 删除 [Music]/[音乐]/♪ 等非语义生成字幕行。

保守规则：只有整行是占位符才处理；括号内含其他实义内容（标题、说话人、
引用）一律不动。移植自技能 ass-subtitle-optimizer 的 SKILL.md 占位符规则。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Iterable, Tuple

from ..utils.logger import setup_logger
from .config import PostprocessConfig
from .report import QualityReport

if TYPE_CHECKING:
    from ..asr.asr_data import ASRData

logger = setup_logger("postprocess.placeholders")

_MUSIC_SYMBOLS = "♪♫♬♩"
_BRACKETS = {"[": "]", "(": ")", "【": "】", "（": "）"}

# 默认占位符词表（大小写不敏感、整行匹配、去括号后比较）
DEFAULT_PLACEHOLDER_PATTERNS = frozenset(
    {
        "music",
        "applause",
        "laughter",
        "laughs",
        "laugh",
        "chuckles",
        "chuckle",
        "sighs",
        "sigh",
        "silence",
        "inaudible",
        "foreign language",
        "speaking foreign language",
        "音乐",
        "掌声",
        "笑声",
        "笑",
        "叹气",
        "咳嗽",
        "沉默",
        "听不清",
    }
)

_SPEAKING_RE = re.compile(r"^speaking [a-z ]+$")


def _strip_music_and_space(text: str) -> str:
    return "".join(ch for ch in text if ch not in _MUSIC_SYMBOLS and not ch.isspace())


def is_placeholder(text: str, extra_patterns: Iterable[str] = ()) -> bool:
    """判断单行/单字段文本是否为占位符（整行匹配）。"""
    if not text:
        return False
    s = text.replace("\\N", " ").replace("\n", " ").strip()
    if not s:
        return False
    # 仅由音乐符号与空白构成的行
    if _strip_music_and_space(s) == "":
        return True
    # 剥离首尾音乐符号与空白（如 "♪ Music"）
    s = s.strip(_MUSIC_SYMBOLS + " \t")
    # 去掉单层包裹的括号
    if len(s) >= 2 and s[0] in _BRACKETS and s[-1] == _BRACKETS[s[0]]:
        inner = s[1:-1].strip()
    else:
        inner = s
    key = inner.lower().strip()
    if not key:
        return False
    if key in DEFAULT_PLACEHOLDER_PATTERNS:
        return True
    if key in {p.lower().strip() for p in extra_patterns}:
        return True
    if _SPEAKING_RE.match(key):
        return True
    return False


def remove_placeholders(
    asr_data: "ASRData",
    cfg: PostprocessConfig,
    report: QualityReport,
) -> Tuple["ASRData", QualityReport]:
    """从字幕中删除占位符段 / 占位符译文侧。

    双字段语义：
    - text 与 translated_text 均为占位符（或另一侧为空）→ 删除整段。
    - 仅 translated_text 是占位符（原文有实义）→ 置空 translated_text。
    - 仅 text 是占位符而译文有实义（罕见）→ 不动，记入 QA 复查队列。
    """
    stage = report.stage("placeholders")
    extra = cfg.extra_placeholder_patterns

    kept = []
    for seg in asr_data.segments:
        has_text = bool(seg.text and seg.text.strip())
        has_trans = bool(seg.translated_text and seg.translated_text.strip())
        text_ph = has_text and is_placeholder(seg.text, extra)
        trans_ph = has_trans and is_placeholder(seg.translated_text, extra)

        # 删除整段的三种情形
        if (
            (text_ph and trans_ph)
            or (text_ph and not has_trans)
            or (trans_ph and not has_text)
        ):
            stage.add(sample=(seg.text or seg.translated_text).strip()[:40])
            continue

        # 仅译文侧是占位符 → 清空译文
        if trans_ph and has_text and not text_ph:
            stage.add(sample=seg.translated_text.strip()[:40])
            seg.translated_text = ""
            kept.append(seg)
            continue

        # 仅原文侧是占位符但译文有实义 → 保留，交人工复查
        if text_ph and has_trans and not trans_ph:
            report.placeholder_review.append(
                f"{seg.text.strip()[:30]} => {seg.translated_text.strip()[:30]}"
            )

        kept.append(seg)

    asr_data.segments = kept
    if stage.changed:
        logger.info("占位符清理：处理 %d 处", stage.changed)
    return asr_data, report
