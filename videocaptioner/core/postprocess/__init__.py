"""规则型字幕后处理与审计。

统一收口占位符清理 / 文本规范化 / 时轴间隙闭合 / 阅读速度审计 / QA 报告，
供 CLI 与 GUI 两条字幕管线共用。无 Qt、无网络（F5 压缩重译除外，走现有 call_llm）。

管线插入点（见 docs/dev/subtitle-optimizer-integration-plan.md §3.2）：
- 加载后、断句前          → run_pre_stage       （占位符清理）
- 优化后 / 翻译后          → run_normalize_stage （取代 remove_punctuation）
- 保存前                   → run_post_stage      （必要时规范化 → 压缩 → 闭合间隙 → 速度优化 → 尾部补偿 → 审计）

任何步骤内部异常均捕获后记 warning 并跳过，绝不阻断主管线。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Optional, Tuple

from ..entities import SubtitleLayoutEnum
from ..utils.logger import setup_logger
from .config import PostprocessConfig
from .report import AuditResult, QualityReport, StageReport, build_qa_report

if TYPE_CHECKING:
    from ..asr.asr_data import ASRData
    from ..speed.timing_evidence import TimingEvidenceWindow

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
    "PostprocessLayoutMode",
    "PostprocessProfile",
    "PostprocessProfileStore",
    "PostprocessResult",
    "PostprocessTask",
    "run_postprocess_task",
]


def _new_report(asr_data: "ASRData", report: Optional[QualityReport]) -> QualityReport:
    if report is None:
        report = QualityReport()
    return report


def _stage_changed(report: QualityReport, key: str) -> int:
    """读取阶段累计变更数（不存在则 0），只读、不新建 StageReport。"""
    sr = report.stages.get(key)
    return sr.changed if sr is not None else 0


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
    layout: SubtitleLayoutEnum = SubtitleLayoutEnum.ORIGINAL_ON_TOP,
    primary_side_only: bool = False,
) -> Tuple["ASRData", QualityReport]:
    """优化后 / 翻译后：文本规范化（取代 remove_punctuation）。可重复调用。"""
    report = _new_report(asr_data, report)
    before_quotes = _stage_changed(report, "normalize_quotes")
    before_trim = _stage_changed(report, "trim_trailing")
    try:
        from .normalize import normalize_segments

        asr_data, report = normalize_segments(
            asr_data, cfg, report, layout, primary_side_only
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("文本规范化失败，已跳过: %s", exc)
    quote_delta = _stage_changed(report, "normalize_quotes") - before_quotes
    trim_delta = _stage_changed(report, "trim_trailing") - before_trim
    if quote_delta or trim_delta:
        logger.info(
            "文本规范化：引号 %d 处 / 弱尾标点 %d 处", quote_delta, trim_delta
        )
    return asr_data, report


def run_post_stage(
    asr_data: "ASRData",
    cfg: PostprocessConfig,
    report: Optional[QualityReport] = None,
    llm_ctx: Optional[dict] = None,
    layout: SubtitleLayoutEnum = SubtitleLayoutEnum.ORIGINAL_ON_TOP,
    timing_windows: Iterable["TimingEvidenceWindow"] = (),
) -> Tuple["ASRData", QualityReport]:
    """保存前：[必要时规范化] → [压缩重译] → 闭合间隙 → [速度优化] → [尾部补偿] → 审计。

    顺序保证删除段/压缩产生的新间隙被闭合，尾部补偿在速度优化确定最终时轴后执行，
    且审计看到的是最终时轴。
    """
    report = _new_report(asr_data, report)

    # 允许用户只开启规则后处理（例如 --normalize-quotes --no-optimize --no-translate）。
    # 默认 trim_trailing_punct=True 仅复刻旧 LLM 后处理路径，不在纯透传路径里单独改字节。
    if cfg.normalize_quotes or cfg.trim_trailing_punct or cfg.compress_fast_subtitles:
        asr_data, report = run_normalize_stage(
            asr_data, cfg, report, layout, primary_side_only=True
        )

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

    if cfg.speed_optimize:
        from ..speed.pipeline import optimize_speed
        from ..speed.profiles import load_speed_profile, resolve_speed_policy

        try:
            policy = resolve_speed_policy(
                cfg.speed_profile,
                cfg.speed_overrides,
                profile_file=cfg.speed_profile_file,
            )
            resolved_profile_id = (
                load_speed_profile(cfg.speed_profile_file).profile_id
                if cfg.speed_profile_file
                else cfg.speed_profile
            )
        except (KeyError, ValueError) as exc:
            logger.warning("速度方案不可用，回退到均衡方案: %s", exc)
            policy = resolve_speed_policy("balanced", cfg.speed_overrides)
            resolved_profile_id = "balanced"
        asr_data, report.speed = optimize_speed(
            asr_data,
            policy=policy,
            profile_id=resolved_profile_id,
            mode=cfg.speed_mode,
            layout=layout,
            primary_side=cfg.speed_primary,
            timing_windows=timing_windows,
            reference_audit=cfg.speed_reference_audit,
            optimize_both_sides=cfg.optimize_both_sides,
            semantic_repair=cfg.speed_semantic_repair,
            semantic_model=cfg.llm_model,
            semantic_window_size=cfg.speed_semantic_window,
            semantic_uncertain_review=cfg.speed_llm_uncertain_review,
        )
        report.segment_count = len(asr_data.segments)

    # 速度结构调整和语义修复都可能产生新文本。规范化是幂等操作，
    # 在所有文本改写结束后再次执行，保证最终交付仍满足尾标点策略。
    if cfg.normalize_quotes or cfg.trim_trailing_punct:
        asr_data, report = run_normalize_stage(
            asr_data, cfg, report, layout, primary_side_only=True
        )

    # 尾部补偿是最后一步时轴变换：在速度优化确定最终时轴后，对超过最大闭合间隙的间隙
    # 按补偿曲线为上一段结尾追加显示时长，使审计看到的是最终交付时轴。
    if cfg.tail_compensation:
        try:
            from .timing import apply_tail_compensation

            asr_data, report = apply_tail_compensation(asr_data, cfg, report, layout)
        except Exception as exc:  # noqa: BLE001
            logger.warning("尾部补偿失败，已跳过: %s", exc)

    if cfg.audit_enabled():
        try:
            from .audit import audit

            asr_data, report = audit(asr_data, cfg, report)
        except Exception as exc:  # noqa: BLE001
            logger.warning("阅读速度审计失败，已跳过: %s", exc)
        if report.audit is not None:
            c = report.audit.counts()
            logger.info(
                "阅读速度审计：段 %d，硬警告 %d，舒适警告 %d，长时长 %d，重叠 %d",
                report.audit.segment_count,
                c["hard"],
                c["comfort"],
                c["long_duration"],
                c["overlaps"],
            )

    report.segment_count = len(asr_data.segments)
    return asr_data, report


# Public stage API.  Imports live after the functions so runner can reuse this
# module without a circular initialization dependency.
from .models import PostprocessLayoutMode as PostprocessLayoutMode  # noqa: E402
from .models import PostprocessResult as PostprocessResult  # noqa: E402
from .models import PostprocessTask as PostprocessTask  # noqa: E402
from .profiles import PostprocessProfile as PostprocessProfile  # noqa: E402
from .profiles import PostprocessProfileStore as PostprocessProfileStore  # noqa: E402
from .runner import run_postprocess_task as run_postprocess_task  # noqa: E402
