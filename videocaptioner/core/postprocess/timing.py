"""F3 时轴间隙闭合 / 闪烁修复 + 尾部补偿。

``close_gaps`` 语义移植自技能 ass-subtitle-optimizer 的 ``fix_subtitle_blinks.py``：
在数据层一个 segment 即技能里的"双语块"，无需同时轴分组/容差逻辑。仅闭合正间隙；
重叠（负间隙）不动，交审计报告。

``apply_tail_compensation`` 补齐一个既有管线缺失的能力：对**超过最大闭合间隙**
（``max_gap_ms``）的间隙，按一条单调钳制补偿曲线为上一段结尾追加显示时长，避免速度
优化后字幕在长间隔前过快消失；补偿量与留白均随间隙单调不降（见 docs/adr/0005）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Set, Tuple

from ..entities import SubtitleLayoutEnum
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


def _protected_indices(
    asr_data: "ASRData",
    cfg: PostprocessConfig,
    layout: SubtitleLayoutEnum,
) -> Set[int]:
    """复用速度管线的保护探测（音乐/歌词、孤立标题卡、短文本长显示）。

    探测失败绝不阻断尾部补偿；仅退化为"无保护段"。
    策略解析优先沿用配置的速度方案；不可用时回退到均衡策略仅作阈值来源。
    """
    try:
        from ..speed.layout import resolve_primary_text
        from ..speed.protection import detect_protected_cues

        try:
            from ..speed.profiles import resolve_speed_policy

            policy = resolve_speed_policy(
                cfg.speed_profile,
                cfg.speed_overrides,
                profile_file=cfg.speed_profile_file,
            )
        except Exception:  # noqa: BLE001 —— 保护探测的策略来源不可用时回退
            from ..speed.policy import get_speed_policy

            policy = get_speed_policy("balanced")

        texts = [
            resolve_primary_text(segment, layout, cfg.speed_primary)
            for segment in asr_data.segments
        ]
        matches = detect_protected_cues(asr_data.segments, texts, policy)
        return {match.index for match in matches}
    except Exception as exc:  # noqa: BLE001 —— 后处理不得阻断管线
        logger.warning("尾部补偿保护探测失败，已按无保护段处理: %s", exc)
        return set()


def compensation_for_gap(gap: int, cfg: PostprocessConfig) -> int:
    """补偿曲线在间隙 ``gap`` 处的取值（见 docs/adr/0005）。

    - ``gap <= max_gap_ms``：0（归闪轴闭合）。
    - ``max_gap_ms < gap < max_compensation_gap_ms``：自 ``min_compensation_ms`` 线性上升。
    - ``gap >= max_compensation_gap_ms``：``max_compensation_ms``（封顶）。

    配置约束（``min_compensation_ms <= max_gap_ms`` 且斜率 <= 1）保证返回值恒 <= gap。
    """
    if gap <= cfg.max_gap_ms:
        return 0
    if gap >= cfg.max_compensation_gap_ms:
        return cfg.max_compensation_ms
    span = cfg.max_compensation_gap_ms - cfg.max_gap_ms
    rise = cfg.max_compensation_ms - cfg.min_compensation_ms
    return cfg.min_compensation_ms + round(rise * (gap - cfg.max_gap_ms) / span)


def apply_tail_compensation(
    asr_data: "ASRData",
    cfg: PostprocessConfig,
    report: QualityReport,
    layout: SubtitleLayoutEnum = SubtitleLayoutEnum.ORIGINAL_ON_TOP,
) -> Tuple["ASRData", QualityReport]:
    """按补偿曲线为每段结尾追加显示时长（见 :func:`compensation_for_gap`）。

    间隙 = 下一段开始 − 上一段结束。补偿量由曲线给出：不超过 ``max_gap_ms`` 的间隙不
    补偿（归 :func:`close_gaps`）；跨过即给 ``min_compensation_ms``，随间隙线性升到
    ``max_compensation_ms`` 后封顶。补偿量与留白均随间隙单调不降（由配置约束保证）。

    两重防过度延长：延长后不超过 ``max_duration_ms``；受保护段（音乐/歌词、孤立标题卡、
    短文本长显示）跳过。只延长上一段结尾、绝不缩短；曲线约束已保证补偿量 <= 间隙，故
    留白恒正、时轴保持有序不重叠。

    词级时间戳数据整体跳过。单次执行的时轴变换。
    """
    stage = report.stage("tail_compensation")
    segments = asr_data.segments
    if len(segments) < 2 or asr_data.is_word_timestamp():
        return asr_data, report

    max_duration = cfg.max_duration_ms
    protected = _protected_indices(asr_data, cfg, layout)

    for i in range(len(segments) - 1):
        prev_seg = segments[i]
        next_seg = segments[i + 1]
        gap = next_seg.start_time - prev_seg.end_time
        comp = compensation_for_gap(gap, cfg)
        if comp <= 0 or i in protected:
            continue
        comp = min(comp, gap)  # 防御：曲线约束已保证，仍夹一次绝不越过下一段
        room = max_duration - (prev_seg.end_time - prev_seg.start_time)
        comp = min(comp, room)  # 不超过最长显示时长
        if comp <= 0:
            continue
        prev_seg.end_time += comp
        stage.add(sample=f"gap {gap}ms +{comp}ms @seg{i + 1}")

    if stage.changed:
        logger.info("尾部补偿：处理 %d 处", stage.changed)
    return asr_data, report
