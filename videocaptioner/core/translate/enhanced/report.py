"""Markdown rendering for the structured translation audit report."""

from __future__ import annotations

import os
import tempfile
from collections import Counter
from pathlib import Path

from .models import TranslationAuditReport

_CATEGORY_LABELS = {
    "empty_translation": "译文为空",
    "source_copied": "原文照抄",
    "protected_token_missing": "关键信息缺失",
    "semantic_accuracy": "语义准确性",
    "untranslated_content": "未翻译内容",
    "fact_number_unit": "事实、数字与单位",
    "negation_modality": "否定与情态",
    "reference": "指代关系",
    "name_or_title": "专名与称谓",
    "target_language_quality": "目标语言质量",
    "format_integrity": "格式完整性",
    "meaning": "语义错误",
    "omission": "漏译",
    "addition": "增译",
    "terminology": "术语",
    "continuity": "上下文连贯",
    "fluency": "表达质量",
    "format": "格式",
    "number": "数字与事实",
}

_DISPOSITION_LABELS = {
    "reported": "仅报告，无有效建议",
    "auto_fixed": "已自动采纳",
    "user_applied": "已由用户采纳",
    "user_rejected": "用户保留原译文",
    "fix_validation_failed": "修复校验失败",
}

_SELECTION_SOURCE_LABELS = {
    "main_model": "主翻译建议",
    "review_model_accepted": "高级校对接受",
    "review_model_corrected": "高级校对修正",
    "user_main": "用户采用主翻译",
    "user_review": "用户采用高级校对",
    "user_custom": "用户自定义",
    "source_fallback": "回退保留原文",
    "imported": "导入术语表",
}

_ROLE_LABELS = {"main": "主翻译", "review": "高级校对", "utility": "连接检查"}

_STAGE_LABELS = {
    "analysis_window": "全文分窗分析",
    "analysis_summary": "分析汇总",
    "term_proposal": "术语初译",
    "term_review": "术语校对",
    "term_review_final": "术语最终裁决",
    "translation": "正式翻译",
    "audit": "质量审计",
}


def render_audit_markdown(report: TranslationAuditReport) -> str:
    counts = Counter(issue.disposition.value for issue in report.issues)
    lines = [
        "# 翻译审计报告",
        "",
        "## 摘要",
        "",
        f"- 问题总数：{len(report.issues)}",
        f"- 已自动采纳：{counts.get('auto_fixed', 0)}",
        f"- 已由用户采纳：{counts.get('user_applied', 0)}",
        f"- 用户保留原译文：{counts.get('user_rejected', 0)}",
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
                        _ROLE_LABELS.get(usage.role, usage.role),
                        _STAGE_LABELS.get(usage.stage, usage.stage),
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
                        _SELECTION_SOURCE_LABELS.get(
                            term.selection_source.value, term.selection_source.value
                        ),
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
            categories = issue.categories or (issue.category,)
            category_text = "、".join(
                _CATEGORY_LABELS.get(category, category) for category in categories
            )
            final_translation = (
                issue.suggested_translation
                if issue.disposition.value in {"auto_fixed", "user_applied"}
                else issue.translated_text
            )
            lines.extend(
                (
                    f"### 字幕 {issue.cue_id} · {category_text}",
                    "",
                    f"- 处理结果：{_DISPOSITION_LABELS.get(issue.disposition.value, issue.disposition.value)}",
                    f"- 说明：{issue.message}",
                    f"- 原文：{issue.original_text}",
                    f"- 当前译文：{issue.translated_text}",
                    f"- 建议译文：{issue.suggested_translation or '—'}",
                    f"- 最终译文：{final_translation}",
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
