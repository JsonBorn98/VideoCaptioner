"""规则型字幕后处理与审计。

统一收口占位符清理 / 文本规范化 / 时轴间隙闭合 / 阅读速度审计 / QA 报告，
供 CLI 与 GUI 两条字幕管线共用。无 Qt、无网络（F5 压缩重译除外，走现有 call_llm）。

管线插入点（见 docs/dev/subtitle-optimizer-integration-plan.md §3.2）：
- 加载后、断句前          → run_pre_stage       （占位符清理）
- 优化后 / 翻译后          → run_normalize_stage （取代 remove_punctuation）
- 保存前                   → run_post_stage      （压缩 → 闭合间隙 → 审计）

任何步骤内部异常均捕获后记 warning 并跳过，绝不阻断主管线。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Tuple

from ..utils.logger import setup_logger
from .config import PostprocessConfig
from .report import AuditResult, QualityReport, StageReport, build_qa_report

if TYPE_CHECKING:
    from ..asr.asr_data import ASRData

logger = setup_logger("postprocess")

__all__ = [
    "PostprocessConfig",
    "QualityReport",
    "StageReport",
    "AuditResult",
    "build_qa_report",
    "run_pre_stage",
    "run_normalize_stage",
    "run_post_stage",
]


def _new_report(asr_data: "ASRData", report: Optional[QualityReport]) -> QualityReport:
    if report is None:
        report = QualityReport()
    return report


def run_pre_stage(
    asr_data: "ASRData",
    cfg: PostprocessConfig,
    report: Optional[QualityReport] = None,
) -> Tuple["ASRData", QualityReport]:
    """加载后、断句/优化前：占位符清理。"""
    report = _new_report(asr_data, report)
    if cfg.remove_placeholders:
        try:
            from .placeholders import remove_placeholders

            asr_data, report = remove_placeholders(asr_data, cfg, report)
        except Exception as exc:  # noqa: BLE001 —— 后处理不得阻断管线
            logger.warning("占位符清理失败，已跳过: %s", exc)
    return asr_data, report


def run_normalize_stage(
    asr_data: "ASRData",
    cfg: PostprocessConfig,
    report: Optional[QualityReport] = None,
) -> Tuple["ASRData", QualityReport]:
    """优化后 / 翻译后：文本规范化（取代 remove_punctuation）。可重复调用。"""
    report = _new_report(asr_data, report)
    try:
        from .normalize import normalize_segments

        asr_data, report = normalize_segments(asr_data, cfg, report)
    except Exception as exc:  # noqa: BLE001
        logger.warning("文本规范化失败，已跳过: %s", exc)
    return asr_data, report


def run_post_stage(
    asr_data: "ASRData",
    cfg: PostprocessConfig,
    report: Optional[QualityReport] = None,
    llm_ctx: Optional[dict] = None,
) -> Tuple["ASRData", QualityReport]:
    """保存前：[压缩重译] → 闭合间隙 → 审计。

    顺序保证删除段/压缩产生的新间隙被闭合，且审计看到的是最终时轴。
    """
    report = _new_report(asr_data, report)

    if cfg.compress_fast_subtitles:
        try:
            from .compress import compress_fast_subtitles

            asr_data, report = compress_fast_subtitles(asr_data, cfg, report, llm_ctx)
        except Exception as exc:  # noqa: BLE001
            logger.warning("快速字幕压缩失败，已跳过: %s", exc)

    if cfg.fix_gaps:
        try:
            from .timing import close_gaps

            asr_data, report = close_gaps(asr_data, cfg, report)
        except Exception as exc:  # noqa: BLE001
            logger.warning("闭合间隙失败，已跳过: %s", exc)

    if cfg.audit_enabled():
        try:
            from .audit import audit

            asr_data, report = audit(asr_data, cfg, report)
        except Exception as exc:  # noqa: BLE001
            logger.warning("阅读速度审计失败，已跳过: %s", exc)

    report.segment_count = len(asr_data.segments)
    return asr_data, report
