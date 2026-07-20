"""Markdown rendering for the structured translation audit report."""

from __future__ import annotations

import os
import tempfile
from collections import Counter
from pathlib import Path

from .models import TranslationAuditReport


def render_audit_markdown(report: TranslationAuditReport) -> str:
    counts = Counter(issue.disposition.value for issue in report.issues)
    lines = [
        "# 翻译审计报告",
        "",
        "## 摘要",
        "",
        f"- 问题总数：{len(report.issues)}",
        f"- 已自动修复：{counts.get('auto_fixed', 0)}",
        f"- 仅报告：{counts.get('reported', 0)}",
        f"- 修复校验失败：{counts.get('fix_validation_failed', 0)}",
        "",
        "## Usage",
        "",
        "| 角色 | 阶段 | 调用 | 输入 token | 输出 token | 缓存读取 | 缓存写入 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]

    def display(value: object) -> str:
        return "不可用" if value is None else str(value)

    def cell(value: object) -> str:
        return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")

    if report.usages:
        for usage in report.usages:
            lines.append(
                "| "
                + " | ".join(
                    (
                        usage.role,
                        usage.stage,
                        str(usage.calls),
                        display(usage.input_tokens),
                        display(usage.output_tokens),
                        display(usage.cache_read_tokens),
                        display(usage.cache_write_tokens),
                    )
                )
                + " |"
            )
    else:
        lines.append("| — | — | 0 | 不可用 | 不可用 | 不可用 | 不可用 |")

    lines.extend(
        (
            "",
            "## 权威术语",
            "",
            "| 源术语 | 义项 | 最终译法 | 选择来源 | 高风险 |",
            "|---|---|---|---|---:|",
        )
    )
    if report.authoritative_terms:
        for term in report.authoritative_terms:
            lines.append(
                "| "
                + " | ".join(
                    (
                        cell(term.source_term),
                        cell(term.sense),
                        cell(term.translation),
                        term.selection_source.value,
                        "是" if term.high_risk else "否",
                    )
                )
                + " |"
            )
    else:
        lines.append("| — | — | — | — | 否 |")

    lines.extend(("", "## 问题", ""))
    if not report.issues:
        lines.append("未发现需要报告的翻译问题。")
    else:
        for issue in report.issues:
            lines.extend(
                (
                    f"### 字幕 {issue.cue_id} · {issue.category}",
                    "",
                    f"- 处理结果：{issue.disposition.value}",
                    f"- 说明：{issue.message}",
                    f"- 原文：{issue.original_text}",
                    f"- 当前译文：{issue.translated_text}",
                    f"- 建议译文：{issue.suggested_translation or '—'}",
                    "",
                )
            )
    if report.warnings:
        lines.extend(("## 警告", ""))
        lines.extend(f"- {warning}" for warning in report.warnings)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def save_audit_markdown(path: str | Path, report: TranslationAuditReport) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = render_audit_markdown(report)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination
