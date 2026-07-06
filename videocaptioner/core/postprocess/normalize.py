"""F2 中文文本规范化 —— 引号规范化 + 弱尾标点清理。

算法移植自技能 ass-subtitle-optimizer 的 ``normalize_chinese_subtitle_text.py``
（去掉 ASS tag 处理，改为在 ASRData 纯文本上工作）。

默认路径（``normalize_quotes`` 关、``trim_trailing_punct`` 开）复刻现有
``ASRData.remove_punctuation`` 行为（仅删末尾 ，。 并 strip），保证输出逐字节一致；
仅当用户开启 ``normalize_quotes`` 时才对中文行启用扩展弱标点集与闭合符处理。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Tuple

from ..utils.logger import setup_logger
from ..utils.text_utils import is_mainly_cjk
from .config import PostprocessConfig
from .report import QualityReport

if TYPE_CHECKING:
    from ..asr.asr_data import ASRData

logger = setup_logger("postprocess.normalize")

WEAK_TRAILING_PUNCTUATION = set("，,。．.、；;：:")
STRONG_TRAILING_PUNCTUATION = set("？！?!…")
CLOSERS = set("」』）)]】》〉”’")

_LEGACY_TRAILING_RE = re.compile(r"[，。]+$")


@dataclass
class QuoteState:
    """引号开闭状态；由调用方持有并跨段传递（引号常跨条开闭）。"""

    double_open: bool = True
    single_open: bool = True


def normalize_quotes(text: str, state: QuoteState) -> Tuple[str, int]:
    """规范化中文引号：“”->「」、‘’->『』，直引号按开闭状态翻转。

    英文词内撇号（前后均为 ASCII 字母，如 ``don't``）保持不动。
    """
    out: List[str] = []
    changed = 0
    for index, char in enumerate(text):
        if char == "“":
            out.append("「")
            state.double_open = False
            changed += 1
        elif char == "”":
            out.append("」")
            state.double_open = True
            changed += 1
        elif char == '"':
            out.append("「" if state.double_open else "」")
            state.double_open = not state.double_open
            changed += 1
        elif char == "「":
            out.append(char)
            state.double_open = False
        elif char == "」":
            out.append(char)
            state.double_open = True
        elif char == "‘":
            out.append("『")
            state.single_open = False
            changed += 1
        elif char == "’":
            out.append("』")
            state.single_open = True
            changed += 1
        elif char == "『":
            out.append(char)
            state.single_open = False
        elif char == "』":
            out.append(char)
            state.single_open = True
        elif char == "'":
            prev_char = text[index - 1] if index > 0 else ""
            next_char = text[index + 1] if index + 1 < len(text) else ""
            if (
                prev_char.isascii()
                and prev_char.isalpha()
                and next_char.isascii()
                and next_char.isalpha()
            ):
                out.append(char)
            else:
                out.append("『" if state.single_open else "』")
                state.single_open = not state.single_open
                changed += 1
        else:
            out.append(char)
    return "".join(out), changed


def _trim_weak_trailing_line(text: str) -> Tuple[str, int]:
    """处理单行行尾：先剥闭合符，循环删弱标点（遇强标点停止），再拼回闭合符。"""
    stripped_right = len(text) - len(text.rstrip())
    suffix = text[len(text) - stripped_right:] if stripped_right else ""
    core = text[: len(text) - stripped_right] if stripped_right else text
    changed = 0

    trailing_closers = ""
    while core and core[-1] in CLOSERS:
        trailing_closers = core[-1] + trailing_closers
        core = core[:-1].rstrip()

    while core:
        last = core[-1]
        if last in STRONG_TRAILING_PUNCTUATION:
            break
        if last in WEAK_TRAILING_PUNCTUATION:
            core = core[:-1].rstrip()
            changed += 1
            continue
        break
    return core + trailing_closers + suffix, changed


def trim_weak_trailing(text: str) -> Tuple[str, int]:
    """按 ``\\n`` 分行处理每行行尾弱标点（扩展集 + 闭合符感知）。"""
    parts = text.split("\n")
    changed = 0
    for index, part in enumerate(parts):
        parts[index], count = _trim_weak_trailing_line(part)
        changed += count
    return "\n".join(parts), changed


def _legacy_trim(text: str) -> str:
    """复刻 ASRData.remove_punctuation：strip + 删末尾 ，。。"""
    return _LEGACY_TRAILING_RE.sub("", text.strip())


def normalize_segments(
    asr_data: "ASRData",
    cfg: PostprocessConfig,
    report: QualityReport,
) -> Tuple["ASRData", QualityReport]:
    """规范化所有段的 text / translated_text 双字段。

    可安全重复调用（引号已规范化后再跑不产生变化）。
    """
    if not (cfg.normalize_quotes or cfg.trim_trailing_punct):
        return asr_data, report

    quote_stage = report.stage("normalize_quotes")
    trim_stage = report.stage("trim_trailing")

    # 每个字段流独立维护引号状态（跨段持续）
    text_quote_state = QuoteState()
    trans_quote_state = QuoteState()

    for seg in asr_data.segments:
        for field_name, state in (
            ("text", text_quote_state),
            ("translated_text", trans_quote_state),
        ):
            value = getattr(seg, field_name)
            if not value:
                continue
            cjk = is_mainly_cjk(value)
            enhanced = cfg.normalize_quotes and cjk
            new_value = value

            if enhanced:
                new_value, qc = normalize_quotes(new_value, state)
                if qc:
                    quote_stage.add(qc, sample=value.strip()[:40])

            if cfg.trim_trailing_punct:
                if enhanced:
                    new_value, tc = trim_weak_trailing(new_value)
                    if tc:
                        trim_stage.add(tc)
                else:
                    trimmed = _legacy_trim(new_value)
                    if trimmed != new_value:
                        trim_stage.add(sample=value.strip()[:40])
                    new_value = trimmed

            setattr(seg, field_name, new_value)

    return asr_data, report
