"""后处理报告数据结构与 QA 报告渲染。

``QualityReport`` 是贯穿整条后处理管线的累加器；各规则步骤写入
``StageReport``（变更计数 + 有界样本），审计步骤写入 ``AuditResult``。
``build_qa_report`` 将其渲染为单一 Markdown 交付（对齐技能 S8 结构）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional

from ..utils.text_utils import is_mainly_cjk

if TYPE_CHECKING:
    from ..speed.pipeline import SpeedOptimizationResult

_MAX_SAMPLES = 20
_MAX_TABLE_ROWS = 40


@dataclass
class StageReport:
    """单个规则步骤的处理摘要。"""

    name: str
    changed: int = 0
    samples: List[str] = field(default_factory=list)

    def add(self, count: int = 1, sample: Optional[str] = None) -> None:
        self.changed += count
        if sample is not None and len(self.samples) < _MAX_SAMPLES:
            self.samples.append(sample)


@dataclass
class SpeedWarning:
    """阅读速度警告（针对单个字段：中文主行或外文辅行）。"""

    index: int
    start: str
    end: str
    duration_s: float
    is_cjk: bool
    chars: int
    cps: float
    limit: float
    over_by: float
    text: str
    translated: str
    context: List[dict] = field(default_factory=list)


@dataclass
class DurationAnomaly:
    """长时长 / 短文本长显示异常（只报告，不自动修改）。"""

    index: int
    start: str
    end: str
    duration_s: float
    chars: int
    reason: str
    text: str
    translated: str


@dataclass
class Overlap:
    """时轴重叠（负间隙）结构警告。"""

    index: int
    prev_index: int
    overlap_ms: int
    start: str
    text: str


@dataclass
class AuditResult:
    """只读审计结果。"""

    segment_count: int = 0
    hard: List[SpeedWarning] = field(default_factory=list)
    comfort: List[SpeedWarning] = field(default_factory=list)
    long_duration: List[DurationAnomaly] = field(default_factory=list)
    overlaps: List[Overlap] = field(default_factory=list)

    def counts(self) -> dict:
        return {
            "hard": len(self.hard),
            "comfort": len(self.comfort),
            "long_duration": len(self.long_duration),
            "overlaps": len(self.overlaps),
        }


@dataclass
class QualityReport:
    """贯穿后处理管线的累加器。"""

    source_path: str = ""
    output_path: str = ""
    segment_count: int = 0
    stages: Dict[str, StageReport] = field(default_factory=dict)
    audit: Optional[AuditResult] = None
    #  text 是占位符但译文有实义的段（数据模型无法只留译文，交人工复查）
    placeholder_review: List[str] = field(default_factory=list)
    #  压缩重译未能自动完成、已保留原文的条目
    compress_failures: List[str] = field(default_factory=list)
    speed: Optional["SpeedOptimizationResult"] = None

    def stage(self, name: str) -> StageReport:
        report = self.stages.get(name)
        if report is None:
            report = StageReport(name)
            self.stages[name] = report
        return report


def _md_escape(value: object) -> str:
    return str(value).replace("\n", " ").replace("|", "\\|")


def _md_row(values: List[object]) -> str:
    return "| " + " | ".join(_md_escape(v) for v in values) + " |\n"


_STAGE_LABELS = {
    "placeholders": "占位符清理",
    "normalize_quotes": "引号规范化",
    "trim_trailing": "弱尾标点清理",
    "close_gaps": "闭合间隙",
    "tail_compensation": "尾部补偿",
    "compress": "快速字幕压缩",
}


def build_qa_report(report: QualityReport) -> str:
    """将 QualityReport 渲染为 Markdown QA 报告。"""

    if report.speed is not None:
        from ..speed.report import build_speed_qa

        speed_report = build_speed_qa(report.speed)
    else:
        speed_report = ""

    lines: List[str] = ["# 字幕质量 QA 报告\n\n"]

    # 1. 文件信息
    lines.append("## 文件信息\n\n")
    lines.append(f"- 输入: `{report.source_path}`\n")
    lines.append(f"- 输出: `{report.output_path}`\n")
    lines.append(f"- 段数: {report.segment_count}\n\n")

    # 2. 处理摘要
    lines.append("## 处理摘要\n\n")
    stage_lines = []
    for key, sr in report.stages.items():
        if sr.changed <= 0:
            continue
        label = _STAGE_LABELS.get(key, key)
        stage_lines.append(f"- {label}: {sr.changed} 处\n")
    if report.compress_failures:
        stage_lines.append(f"- 快速字幕压缩失败（已保留原文）: {len(report.compress_failures)} 条\n")
    if report.placeholder_review:
        stage_lines.append(
            f"- 占位符复查（原文疑似占位符但译文有实义）: {len(report.placeholder_review)} 条\n"
        )
    lines.extend(stage_lines or ["- 未启用规则型清理步骤。\n"])

    # 3. 校验摘要
    audit = report.audit
    lines.append("\n## 校验摘要\n\n")
    if audit is None:
        if report.speed is not None:
            lines.append("- 已启用统一字幕速度优化，详细 M3 结果见文末。\n")
        else:
            lines.append("- 未启用阅读速度审计。\n")
    else:
        c = audit.counts()
        cjk_hard = sum(1 for w in audit.hard if w.is_cjk)
        latin_hard = c["hard"] - cjk_hard
        cjk_comfort = sum(1 for w in audit.comfort if w.is_cjk)
        latin_comfort = c["comfort"] - cjk_comfort
        lines.append(f"- 段数: {audit.segment_count}\n")
        lines.append(f"- 硬警告: {c['hard']}（中文 {cjk_hard} / 外文 {latin_hard}）\n")
        lines.append(f"- 舒适警告: {c['comfort']}（中文 {cjk_comfort} / 外文 {latin_comfort}）\n")
        lines.append(f"- 长时长异常: {c['long_duration']}\n")
        lines.append(f"- 时轴重叠: {c['overlaps']}\n")

    # 4. 译者复查队列
    if audit is not None:
        lines.append("\n## 译者复查队列\n\n")
        lines.append(
            "以下条目不一定有错，只是自动修复方向不明确、最值得人工检查的位置。\n\n"
        )

        lines.append("### 长时长 / 短文本长显示\n\n")
        if audit.long_duration:
            lines.append("| 段 | 时间 | 时长(s) | 字符 | 原因 | 原文 | 译文 |\n")
            lines.append("| --- | --- | ---: | ---: | --- | --- | --- |\n")
            for item in audit.long_duration[:_MAX_TABLE_ROWS]:
                lines.append(
                    _md_row([
                        item.index,
                        f"{item.start}->{item.end}",
                        item.duration_s,
                        item.chars,
                        item.reason,
                        item.text,
                        item.translated,
                    ])
                )
            if len(audit.long_duration) > _MAX_TABLE_ROWS:
                lines.append(
                    f"\n_省略 {len(audit.long_duration) - _MAX_TABLE_ROWS} 条长时长样本。_\n"
                )
        else:
            lines.append("- 无。\n")

        lines.append("\n### 快速中文行\n\n")
        _write_speed_table(lines, [w for w in audit.hard if w.is_cjk])

        lines.append("\n### 快速外文行\n\n")
        _write_speed_table(lines, [w for w in audit.hard if not w.is_cjk])

    # 5. 人工 QA 注意事项
    lines.append("\n## 人工 QA 注意事项\n\n")
    lines.append("- 优先检查中文主行的可读性。\n")
    lines.append("- 外文辅行常因源时轴天然偏快，可能属正常现象。\n")
    lines.append("- 长时长行需人工判断是否为有意为之（标题卡 / 歌词 / 持续在屏文本）。\n")

    if speed_report:
        lines.extend(["\n---\n\n", speed_report])

    return "".join(lines)


def _write_speed_table(lines: List[str], warnings: List[SpeedWarning]) -> None:
    if not warnings:
        lines.append("- 无超过硬限的条目。\n")
        return
    lines.append("| 段 | 时间 | CPS | 限值 | 时长(s) | 文本 |\n")
    lines.append("| --- | --- | ---: | ---: | ---: | --- |\n")
    for w in warnings[:_MAX_TABLE_ROWS]:
        shown = _speed_warning_display_text(w)
        lines.append(
            _md_row([
                w.index,
                f"{w.start}->{w.end}",
                w.cps,
                w.limit,
                w.duration_s,
                shown,
            ])
        )
    if len(warnings) > _MAX_TABLE_ROWS:
        lines.append(f"\n_省略 {len(warnings) - _MAX_TABLE_ROWS} 条。_\n")


def _speed_warning_display_text(warning: SpeedWarning) -> str:
    """Show the actual field that triggered the CJK/non-CJK speed warning."""
    fields = [warning.text, warning.translated]
    for value in fields:
        if value and bool(is_mainly_cjk(value)) == warning.is_cjk:
            return value
    return warning.text or warning.translated
