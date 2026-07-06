"""F4/F6 阅读速度与时长异常审计（只读）。

阈值与口径移植自技能 ass-subtitle-optimizer 的 ``audit_reading_speed.py`` 与
``build_translator_qa_report.py``，数据源换为 ASRData。审计不修改任何段。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, List, Tuple

from ..utils.text_utils import is_mainly_cjk
from .config import PostprocessConfig
from .report import (
    AuditResult,
    DurationAnomaly,
    Overlap,
    QualityReport,
    SpeedWarning,
)

if TYPE_CHECKING:
    from ..asr.asr_data import ASRData, ASRDataSeg

_WS_RE = re.compile(r"\s+")


def _fmt(ms: int) -> str:
    """毫秒转 SRT 时间戳 HH:MM:SS,mmm。"""
    total_seconds, milliseconds = divmod(max(0, ms), 1000)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def _char_count(text: str, cjk: bool) -> int:
    """CJK 按去空白字符数计，其余按 strip 后字符数计（含词间空格）。"""
    if cjk:
        return len(_WS_RE.sub("", text))
    return len(text.strip())


def _context_entry(seg: "ASRDataSeg", index: int, current: bool) -> dict:
    return {
        "current": current,
        "index": index,
        "start": _fmt(seg.start_time),
        "end": _fmt(seg.end_time),
        "text": seg.text,
        "translated": seg.translated_text,
    }


def _collect_context(segments: List["ASRDataSeg"], center: int, radius: int) -> List[dict]:
    if radius <= 0:
        return []
    lo = max(0, center - radius)
    hi = min(len(segments), center + radius + 1)
    return [
        _context_entry(segments[i], i + 1, current=(i == center))
        for i in range(lo, hi)
    ]


def audit(
    asr_data: "ASRData",
    cfg: PostprocessConfig,
    report: QualityReport,
) -> Tuple["ASRData", QualityReport]:
    """执行只读审计，将结果写入 ``report.audit``。"""
    segments = asr_data.segments
    result = AuditResult(segment_count=len(segments))

    for i, seg in enumerate(segments):
        n = i + 1
        duration_ms = seg.end_time - seg.start_time
        duration_s = duration_ms / 1000

        main_chars = 0  # 用于短文本长显示判定（优先中文侧）
        has_cjk_field = False

        # 逐字段阅读速度
        if duration_s > 0:
            for value in (seg.text, seg.translated_text):
                if not value or not value.strip():
                    continue
                cjk = is_mainly_cjk(value)
                chars = _char_count(value, cjk)
                if cjk:
                    has_cjk_field = True
                    main_chars = max(main_chars, chars)
                cps = chars / duration_s
                hard_limit = cfg.max_cps_cjk if cjk else cfg.max_cps_latin
                comfort_limit = cfg.comfort_cps_cjk if cjk else cfg.comfort_cps_latin

                warning = SpeedWarning(
                    index=n,
                    start=_fmt(seg.start_time),
                    end=_fmt(seg.end_time),
                    duration_s=round(duration_s, 2),
                    is_cjk=cjk,
                    chars=chars,
                    cps=round(cps, 1),
                    limit=hard_limit,
                    over_by=round(cps - hard_limit, 1),
                    text=seg.text,
                    translated=seg.translated_text,
                )
                if cps > hard_limit:
                    warning.context = _collect_context(segments, i, radius=1)
                    result.hard.append(warning)
                elif cps > comfort_limit:
                    warning.limit = comfort_limit
                    warning.over_by = round(cps - comfort_limit, 1)
                    result.comfort.append(warning)

        if not has_cjk_field:
            main_chars = _char_count(seg.text, False)

        # 感知最短显示时长 → 舒适警告
        if 0 < duration_ms < cfg.min_duration_ms:
            result.comfort.append(
                SpeedWarning(
                    index=n,
                    start=_fmt(seg.start_time),
                    end=_fmt(seg.end_time),
                    duration_s=round(duration_s, 2),
                    is_cjk=has_cjk_field,
                    chars=main_chars,
                    cps=round(main_chars / duration_s, 1) if duration_s > 0 else 0.0,
                    limit=0.0,
                    over_by=0.0,
                    text=seg.text,
                    translated=seg.translated_text,
                )
            )

        # 长时长 / 短文本长显示异常（只报告）
        reasons: List[str] = []
        if duration_ms > cfg.max_duration_ms:
            reasons.append(f"时长>{cfg.max_duration_ms / 1000:g}s")
        if (
            duration_ms > cfg.short_text_max_duration_ms
            and 0 < main_chars <= cfg.short_text_max_chars
        ):
            reasons.append(
                f"短文本({main_chars}字)>{cfg.short_text_max_duration_ms / 1000:g}s"
            )
        if reasons:
            result.long_duration.append(
                DurationAnomaly(
                    index=n,
                    start=_fmt(seg.start_time),
                    end=_fmt(seg.end_time),
                    duration_s=round(duration_s, 2),
                    chars=main_chars,
                    reason="; ".join(reasons),
                    text=seg.text,
                    translated=seg.translated_text,
                )
            )

        # 时轴重叠（负间隙）结构警告
        if i > 0:
            prev_seg = segments[i - 1]
            if seg.start_time < prev_seg.end_time:
                result.overlaps.append(
                    Overlap(
                        index=n,
                        prev_index=n - 1,
                        overlap_ms=prev_seg.end_time - seg.start_time,
                        start=_fmt(seg.start_time),
                        text=seg.text,
                    )
                )

    result.hard.sort(key=lambda w: (-w.cps, w.index))
    result.comfort.sort(key=lambda w: (-w.cps, w.index))
    result.long_duration.sort(key=lambda d: (-d.duration_s, d.index))

    report.audit = result
    return asr_data, report
