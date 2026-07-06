"""F3 时轴间隙闭合 / 闪烁修复。

语义移植自技能 ass-subtitle-optimizer 的 ``fix_subtitle_blinks.py``：在数据层
一个 segment 即技能里的"双语块"，无需同时轴分组/容差逻辑。仅闭合正间隙；
重叠（负间隙）不动，交审计报告。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Tuple

from ..utils.logger import setup_logger
from .config import PostprocessConfig
from .report import QualityReport

if TYPE_CHECKING:
    from ..asr.asr_data import ASRData

logger = setup_logger("postprocess.timing")


def close_gaps(
    asr_data: "ASRData",
    cfg: PostprocessConfig,
    report: QualityReport,
) -> Tuple["ASRData", QualityReport]:
    """闭合相邻段之间 ``min_gap_ms <= gap <= max_gap_ms`` 的正间隙。

    - ``extend``（默认，技能语义）：前段结束时间延长到后段开始时间。
    - ``midpoint``（旧 optimize_timing 语义）：边界吸附到间隙 3/4 点。

    词级时间戳数据直接跳过（与 optimize_timing 一致）。
    """
    stage = report.stage("close_gaps")
    segments = asr_data.segments
    if len(segments) < 2 or asr_data.is_word_timestamp():
        return asr_data, report

    max_gap = cfg.max_gap_ms
    min_gap = cfg.min_gap_ms
    mode = cfg.gap_mode

    for i in range(len(segments) - 1):
        prev_seg = segments[i]
        next_seg = segments[i + 1]
        gap = next_seg.start_time - prev_seg.end_time
        if gap < min_gap or gap > max_gap:
            continue
        if mode == "midpoint":
            mid = (prev_seg.end_time + next_seg.start_time) // 2 + gap // 4
            prev_seg.end_time = mid
            next_seg.start_time = mid
        else:  # extend
            prev_seg.end_time = next_seg.start_time
        stage.add(sample=f"gap {gap}ms @seg{i + 1}")

    if stage.changed:
        logger.info("闭合间隙：处理 %d 处（mode=%s）", stage.changed, mode)
    return asr_data, report
