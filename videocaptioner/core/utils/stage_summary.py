"""Framework-agnostic stage summary + one-line renderer.

A ``StageSummary`` is the small structured object a pipeline stage returns to
describe, deterministically, what it did — independent of any log level. The
CLI (``cli/output``) and the GUI (Qt signals) render it themselves, so the
console and the log file never disagree. This module stays Qt-free and
importable from ``core``.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# (label, value) pairs kept in explicit order so rendering is deterministic.
Counts = List[Tuple[str, int]]

_SEP = " · "


@dataclass
class StageSummary:
    """Structured, level-independent report of what a pipeline stage did."""

    stage: str
    counts: Counts = field(default_factory=list)
    warnings: Tuple[str, ...] = ()
    status: Optional[str] = None


def format_stage_summary(summary: StageSummary) -> str:
    """Render a ``StageSummary`` as one concise line.

    Example: ``optimize · 120 段 · 3 重试 · ⚠ 2 [degraded]``. The middle
    segments are the ``counts`` (``{value} {label}``); ``· ⚠ N`` is appended
    when there are warnings, and ``status`` is shown in brackets when set.
    """

    parts = [summary.stage]
    parts.extend(f"{value} {label}" for label, value in summary.counts)
    line = _SEP.join(parts)
    if summary.warnings:
        line += f"{_SEP}⚠ {len(summary.warnings)}"
    if summary.status:
        line += f" [{summary.status}]"
    return line


def build_split_stage_summary(
    segment_count: int,
    *,
    use_llm: bool,
    fallback_count: int = 0,
) -> StageSummary:
    """Build the shared CLI/GUI summary for split or word-cue merge."""

    counts: Counts = [("段", segment_count)]
    if fallback_count:
        counts.append(("规则回退", fallback_count))
    return StageSummary(
        "split" if use_llm else "merge",
        counts,
        status="degraded" if fallback_count else None,
    )


def build_optimize_stage_summary(
    segment_count: int,
    *,
    failed_batches: int = 0,
    maxed_batches: int = 0,
) -> StageSummary:
    """Build the shared CLI/GUI subtitle optimization summary."""

    counts: Counts = [("段", segment_count)]
    if failed_batches:
        counts.append(("批失败", failed_batches))
    if maxed_batches:
        counts.append(("校验未过", maxed_batches))
    return StageSummary(
        "optimize",
        counts,
        status="degraded" if failed_batches else None,
    )


def build_translate_stage_summary(
    segment_count: int,
    *,
    failed_count: int = 0,
) -> StageSummary:
    """Build the shared CLI/GUI subtitle translation summary."""

    counts: Counts = [("段", segment_count)]
    if failed_count:
        counts.append(("翻译失败", failed_count))
    return StageSummary(
        "translate",
        counts,
        status="degraded" if failed_count else None,
    )
