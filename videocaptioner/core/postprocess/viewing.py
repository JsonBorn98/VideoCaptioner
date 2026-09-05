"""每侧显示长度策略：折算字符数、混合语言阈值与单行限长扫描。

显示侧长度策略（见 CONTEXT.md「显示侧长度策略」，设计记录 D02-D04/D19/D25）：
原文侧与译文侧各自选择单行限长（single_line）或自动换行（auto_wrap），默认
两侧均单行限长。单行限长侧按折算字符数执行目标上限（软目标）与绝对上限
（长度验收硬条件）；自动换行侧完全跳过长度与行数扫描，最终换行与渲染交给
播放器或下游渲染器，但不关闭阅读速度、语义质量等其他后处理能力。

折算字符数权重：CJK/全角字符计 1，拉丁字母、数字、半角标点计 0.5，
空白及零宽格式字符计 0，其他符号计 1。混合语言按 CJK 权重占比在中文与
英文两套上限之间线性插值得到有效阈值。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Literal

from ..entities import SubtitleLayoutEnum
from .config import PostprocessConfig

if TYPE_CHECKING:
    from ..asr.asr_data import ASRData

Side = Literal["original", "translated"]

SINGLE_LINE = "single_line"
AUTO_WRAP = "auto_wrap"
DISPLAY_MODES = (SINGLE_LINE, AUTO_WRAP)

# CJK / 全角：按 1 计。表意空格 U+3000 属空白，先于本表按 0 处理。
_CJK_RANGES = (
    (0x3001, 0x303F),  # CJK 标点（。，、「」等）
    (0x3040, 0x30FF),  # 平假名 / 片假名
    (0x3400, 0x4DBF),  # CJK 扩展 A
    (0x4E00, 0x9FFF),  # CJK 统一表意
    (0xAC00, 0xD7AF),  # 谚文
    (0xF900, 0xFAFF),  # CJK 兼容表意
    (0xFF01, 0xFF60),  # 全角形式（！＂＃…＀）
)
# 拉丁字母 / 数字 / 半角标点：按 0.5 计。
_HALFWIDTH_RANGES = (
    (0x0021, 0x007E),  # ASCII 可见字符（字母、数字、半角标点）
    (0x00C0, 0x00FF),  # Latin-1 补充字母（à é ü 等；× ÷ 等符号除外）
    (0x0100, 0x017F),  # Latin Extended-A
)


def _in_ranges(code: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(low <= code <= high for low, high in ranges)


def _char_weight(char: str) -> float:
    """单个字符的折算权重（D04/D25）。"""
    # 空白及零宽格式字符不增加长度。
    if char.isspace() or unicodedata.category(char) == "Cf":
        return 0.0
    code = ord(char)
    if _in_ranges(code, _CJK_RANGES):
        return 1.0
    if _in_ranges(code, _HALFWIDTH_RANGES):
        # Latin-1 中的非字母符号（× ÷ 等）按其他符号计 1。
        return 0.5 if (char.isascii() or char.isalpha()) else 1.0
    # 其他符号（emoji、数学符号、半角假名等）计 1。
    return 1.0


def _is_cjk_char(char: str) -> bool:
    if char.isspace() or unicodedata.category(char) == "Cf":
        return False
    return _in_ranges(ord(char), _CJK_RANGES)


def weighted_length(text: str) -> float:
    """折算字符数：按 D04/D25 权重累加的显示长度估算。"""
    return sum(_char_weight(char) for char in text)


def effective_length_limit(
    text: str, *, cjk_limit: float, latin_limit: float
) -> float:
    """混合语言有效阈值：按 CJK 权重占比在中文与英文上限之间线性插值（D25）。

    全 CJK 文本得到 ``cjk_limit``，全拉丁文本得到 ``latin_limit``；
    无有效计权内容时按 ``latin_limit`` 处理。
    """
    cjk_weight = 0.0
    total_weight = 0.0
    for char in text:
        weight = _char_weight(char)
        total_weight += weight
        if weight > 0 and _is_cjk_char(char):
            cjk_weight += weight
    if total_weight <= 0:
        return float(latin_limit)
    cjk_share = cjk_weight / total_weight
    return latin_limit + cjk_share * (cjk_limit - latin_limit)


@dataclass
class ViewingProblem:
    """单行限长侧扫描出的观看长度问题。

    供修复规划（问题主体/上下文）消费：``resolved`` 只在对应区域通过
    长度验收后置 True；修复耗尽回退的区域保持 False，不记为通过（D10/D14）。
    """

    problem_id: str
    side: Side
    segment_index: int
    text: str
    weighted_length: float
    absolute_limit: float
    target_limit: float
    reason: str
    resolved: bool = False


def sides_for_layout(layout: SubtitleLayoutEnum) -> tuple[Side, ...]:
    """布局包含的显示侧（CONTEXT.md「显示侧」）。"""
    if layout == SubtitleLayoutEnum.ONLY_ORIGINAL:
        return ("original",)
    if layout == SubtitleLayoutEnum.ONLY_TRANSLATE:
        return ("translated",)
    return ("original", "translated")


def scan_viewing_lengths(
    asr_data: "ASRData", cfg: PostprocessConfig, layout: SubtitleLayoutEnum
) -> List[ViewingProblem]:
    """扫描单行限长侧的长度与行数问题（只读，不修改字幕）。

    自动换行侧跳过长度与行数约束（D19）；单行侧超出有效绝对上限或
    行数超过单行时形成 ``ViewingProblem``。目标上限是软目标，只随
    问题记录供修复规划参考，不单独形成问题。
    """
    problems: List[ViewingProblem] = []
    for side in sides_for_layout(layout):
        if cfg.display_mode_for(side) != SINGLE_LINE:
            continue
        for index, segment in enumerate(asr_data.segments):
            text = segment.text if side == "original" else segment.translated_text
            if not text or not text.strip():
                continue
            length = weighted_length(text)
            absolute = effective_length_limit(
                text,
                cjk_limit=cfg.single_line_absolute_cjk,
                latin_limit=cfg.single_line_absolute_latin,
            )
            target = effective_length_limit(
                text,
                cjk_limit=cfg.single_line_target_cjk,
                latin_limit=cfg.single_line_target_latin,
            )
            if "\n" in text.strip():
                problems.append(
                    ViewingProblem(
                        problem_id=f"lines:{side}:{index}",
                        side=side,
                        segment_index=index,
                        text=text,
                        weighted_length=length,
                        absolute_limit=absolute,
                        target_limit=target,
                        reason="单行限长模式下显示侧行数超过单行",
                    )
                )
            if length > absolute:
                problems.append(
                    ViewingProblem(
                        problem_id=f"length:{side}:{index}",
                        side=side,
                        segment_index=index,
                        text=text,
                        weighted_length=length,
                        absolute_limit=absolute,
                        target_limit=target,
                        reason=(
                            f"折算字符数 {length:g} 超过有效绝对上限 {absolute:g}"
                        ),
                    )
                )
    return problems


__all__ = [
    "AUTO_WRAP",
    "DISPLAY_MODES",
    "SINGLE_LINE",
    "Side",
    "ViewingProblem",
    "effective_length_limit",
    "scan_viewing_lengths",
    "sides_for_layout",
    "weighted_length",
]
