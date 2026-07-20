"""Strictly read-only GUI presentation for translation audit results."""

from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from videocaptioner.core.translate.enhanced.models import TranslationAuditReport


class TranslationAuditPage(QWidget):
    """Display audit findings without any apply/edit action."""

    closed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.report = TranslationAuditReport()
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(self.tr("翻译质量审计"), self))
        self.summary_label = QLabel(self)
        layout.addWidget(self.summary_label)
        self.issue_table = QTableWidget(0, 6, self)
        self.issue_table.setHorizontalHeaderLabels(
            [
                self.tr("字幕"),
                self.tr("类别"),
                self.tr("问题"),
                self.tr("当前译文"),
                self.tr("建议译文"),
                self.tr("处理结果"),
            ]
        )
        self.issue_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.issue_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.issue_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.issue_table, 1)
        self.details_view = QTextBrowser(self)
        self.details_view.setMaximumHeight(150)
        layout.addWidget(self.details_view)
        self.close_button = QPushButton(self.tr("返回字幕工作区"), self)
        self.close_button.clicked.connect(self.closed)
        layout.addWidget(self.close_button)

    def set_report(self, report: TranslationAuditReport) -> None:
        self.report = report
        self.summary_label.setText(self.tr("共发现 {0} 项问题").format(len(report.issues)))
        self.issue_table.setRowCount(len(report.issues))
        for row, issue in enumerate(report.issues):
            values = (
                str(issue.cue_id),
                issue.category,
                issue.message,
                issue.translated_text,
                issue.suggested_translation,
                issue.disposition.value,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)  # type: ignore
                self.issue_table.setItem(row, column, item)

        details: list[str] = []
        if report.authoritative_terms:
            details.append(self.tr("权威术语（最终译法 / 选择来源）"))
            details.extend(
                f"{term.source_term} → {term.translation} / {term.selection_source.value}"
                for term in report.authoritative_terms
            )
        if report.warnings:
            details.append(self.tr("警告") + "\n" + "\n".join(report.warnings))
        if report.usages:
            details.append(self.tr("Usage（接口实际返回）"))
            for usage in report.usages:
                unavailable = self.tr("不可用")
                details.append(
                    f"{usage.role}/{usage.stage}: calls={usage.calls}, "
                    f"input={usage.input_tokens if usage.input_tokens is not None else unavailable}, "
                    f"output={usage.output_tokens if usage.output_tokens is not None else unavailable}, "
                    f"cache-read={usage.cache_read_tokens if usage.cache_read_tokens is not None else unavailable}, "
                    f"cache-write={usage.cache_write_tokens if usage.cache_write_tokens is not None else unavailable}"
                )
        self.details_view.setPlainText("\n\n".join(details))


__all__ = ["TranslationAuditPage"]
