"""Fluent review and application page for enhanced translation audits."""

from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    InfoBadge,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    SimpleCardWidget,
    StrongBodyLabel,
    TableWidget,
    setCustomStyleSheet,
)
from qfluentwidgets import FluentIcon as FIF

from videocaptioner.core.translate.enhanced.audit import validate_suggested_translation
from videocaptioner.core.translate.enhanced.models import (
    TranslationAuditIssue,
    TranslationAuditReport,
)

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


class TranslationAuditPage(QWidget):
    """Review consolidated suggestions and return the user's accepted cue IDs."""

    closed = pyqtSignal()
    confirmed = pyqtSignal(object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.report = TranslationAuditReport()
        self._interactive = False
        self._accepted_ids: set[int] = set()
        self._eligible_ids: set[int] = set()
        self._validation_errors: dict[int, str] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 6, 0, 0)
        root.setSpacing(12)

        heading = QHBoxLayout()
        heading.setContentsMargins(8, 0, 8, 0)
        heading.setSpacing(10)
        title_block = QVBoxLayout()
        title_block.setSpacing(2)
        self.title_label = StrongBodyLabel(self.tr("翻译质量审计"), self)
        self.summary_label = CaptionLabel(self.tr("查看高级校对建议及其最终处理结果"), self)
        title_block.addWidget(self.title_label)
        title_block.addWidget(self.summary_label)
        heading.addLayout(title_block)
        heading.addStretch(1)
        self.issue_badge = InfoBadge.info(self.tr("0 项问题"), self)
        heading.addWidget(self.issue_badge, 0, Qt.AlignVCenter)  # type: ignore[arg-type]
        root.addLayout(heading)

        self.issue_table = TableWidget(self)
        self.issue_table.setColumnCount(6)
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
        self.issue_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.issue_table.setAlternatingRowColors(False)
        self.issue_table.setWordWrap(False)
        self.issue_table.setBorderVisible(True)
        self.issue_table.setBorderRadius(8)
        self.issue_table.verticalHeader().hide()
        self.issue_table.verticalHeader().setDefaultSectionSize(38)
        header = self.issue_table.horizontalHeader()
        header.setMinimumSectionSize(68)
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        header.setSectionResizeMode(5, QHeaderView.Fixed)
        self.issue_table.setColumnWidth(0, 70)
        self.issue_table.setColumnWidth(1, 118)
        self.issue_table.setColumnWidth(5, 112)
        item_qss = "QTableView::item { padding-left: 10px; padding-right: 10px; }"
        setCustomStyleSheet(self.issue_table, item_qss, item_qss)
        root.addWidget(self.issue_table, 1)

        self.comparison_card = SimpleCardWidget(self)
        self.comparison_card.setBorderRadius(10)
        comparison_layout = QVBoxLayout(self.comparison_card)
        comparison_layout.setContentsMargins(18, 10, 18, 12)
        comparison_layout.setSpacing(6)
        comparison_layout.addWidget(BodyLabel(self.tr("原文与校对建议"), self.comparison_card))
        self.comparison_view = PlainTextEdit(self.comparison_card)
        self.comparison_view.setReadOnly(True)
        self.comparison_view.setMinimumHeight(92)
        self.comparison_view.setMaximumHeight(120)
        comparison_layout.addWidget(self.comparison_view)
        root.addWidget(self.comparison_card)

        self.details_card = SimpleCardWidget(self)
        self.details_card.setBorderRadius(10)
        details_layout = QVBoxLayout(self.details_card)
        details_layout.setContentsMargins(18, 12, 18, 14)
        details_layout.setSpacing(7)
        details_heading = QHBoxLayout()
        details_heading.setSpacing(8)
        details_heading.addWidget(BodyLabel(self.tr("术语、警告与用量"), self.details_card))
        details_heading.addStretch(1)
        self.details_summary = CaptionLabel(self.details_card)
        details_heading.addWidget(self.details_summary)
        details_layout.addLayout(details_heading)
        self.details_view = PlainTextEdit(self.details_card)
        self.details_view.setReadOnly(True)
        self.details_view.setMinimumHeight(130)
        self.details_view.setMaximumHeight(180)
        self.details_view.setPlaceholderText(self.tr("本次审计没有附加术语、警告或用量信息"))
        details_layout.addWidget(self.details_view)
        root.addWidget(self.details_card)

        actions = QHBoxLayout()
        actions.setContentsMargins(8, 0, 8, 4)
        actions.setSpacing(8)
        self.keep_button = PushButton(self.tr("保留原译文"), self)
        self.keep_button.clicked.connect(self._keep_selected)
        actions.addWidget(self.keep_button)
        self.apply_button = PushButton(self.tr("采纳当前建议"), self)
        self.apply_button.clicked.connect(self._apply_selected)
        actions.addWidget(self.apply_button)
        self.apply_all_button = PushButton(self.tr("采纳全部有效建议"), self)
        self.apply_all_button.clicked.connect(self._apply_all)
        actions.addWidget(self.apply_all_button)
        actions.addStretch(1)
        self.finish_button = PrimaryPushButton(self.tr("完成并写回字幕"), self)
        self.finish_button.clicked.connect(self._finish_confirmation)
        actions.addWidget(self.finish_button)
        self.close_button = PushButton(self)
        self.close_button.setIcon(FIF.RETURN)
        self.close_button.setText(self.tr("返回字幕工作区"))
        self.close_button.clicked.connect(self.closed)
        actions.addWidget(self.close_button)
        root.addLayout(actions)
        self.issue_table.itemSelectionChanged.connect(self._refresh_decision_controls)

    @staticmethod
    def _display_category(category: str) -> str:
        return _CATEGORY_LABELS.get(category, category.replace("_", " "))

    @staticmethod
    def _display_disposition(disposition: str) -> str:
        return _DISPOSITION_LABELS.get(disposition, disposition.replace("_", " "))

    @staticmethod
    def _display_message(category: str, message: str) -> str:
        """Localize deterministic messages saved by earlier app versions."""

        if message == "Translation is empty.":
            return "译文为空。"
        if message == "Translation is identical to the source.":
            return "译文与原文完全相同，可能未翻译。"
        marker = "Protected source tokens are missing:"
        if category == "protected_token_missing" and message.startswith(marker):
            return "译文缺少原文中的关键信息：" + message.removeprefix(marker).strip()
        return message

    def set_report(self, report: TranslationAuditReport, *, interactive: bool = False) -> None:
        self.report = report
        self._interactive = interactive
        self._accepted_ids.clear()
        self._eligible_ids.clear()
        self._validation_errors.clear()
        for issue in report.issues:
            eligible, reason = self._suggestion_validation(issue)
            if eligible:
                self._eligible_ids.add(issue.cue_id)
            elif reason:
                self._validation_errors[issue.cue_id] = reason
        self.summary_label.setText(
            self.tr("选择要写回字幕的高级校对建议")
            if interactive
            else self.tr("查看高级校对建议及其最终处理结果")
        )
        issue_count = len(report.issues)
        self.issue_badge.setText(self.tr("{0} 项问题").format(issue_count))
        self.issue_table.setRowCount(issue_count)
        for row, issue in enumerate(report.issues):
            categories = issue.categories or (issue.category,)
            values = (
                str(issue.cue_id),
                "、".join(self._display_category(value) for value in categories),
                self._display_message(issue.category, issue.message),
                issue.translated_text,
                issue.suggested_translation or self.tr("—"),
                self._decision_text(issue),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)  # type: ignore
                item.setToolTip(value)
                if column in {0, 5}:
                    item.setTextAlignment(Qt.AlignCenter)  # type: ignore[arg-type]
                self.issue_table.setItem(row, column, item)

        details: list[str] = []
        if report.authoritative_terms:
            details.append(self.tr("权威术语 · 最终译法 / 选择来源"))
            details.extend(
                f"{term.source_term}  →  {term.translation}  /  "
                f"{_SELECTION_SOURCE_LABELS.get(term.selection_source.value, term.selection_source.value)}"
                for term in report.authoritative_terms
            )
        if report.warnings:
            if details:
                details.append("")
            details.append(self.tr("警告"))
            details.extend(f"• {warning}" for warning in report.warnings)
        if report.usages:
            if details:
                details.append("")
            details.append(self.tr("模型用量 · 接口实际返回"))
            unavailable = self.tr("不可用")
            for usage in report.usages:
                role = _ROLE_LABELS.get(usage.role, usage.role)
                stage = _STAGE_LABELS.get(usage.stage, usage.stage)
                details.append(
                    f"{role} / {stage}  ·  调用 {usage.calls}  ·  "
                    f"输入 {usage.input_tokens if usage.input_tokens is not None else unavailable}  ·  "
                    f"输出 {usage.output_tokens if usage.output_tokens is not None else unavailable}  ·  "
                    f"缓存读取 {usage.cache_read_tokens if usage.cache_read_tokens is not None else unavailable}  ·  "
                    f"缓存写入 {usage.cache_write_tokens if usage.cache_write_tokens is not None else unavailable}"
                )
        auxiliary_count = (
            len(report.authoritative_terms) + len(report.warnings) + len(report.usages)
        )
        self.details_summary.setText(
            self.tr("{0} 条附加记录").format(auxiliary_count) if auxiliary_count else ""
        )
        self.details_view.setPlainText("\n".join(details))
        self.details_card.setVisible(bool(auxiliary_count))
        self.keep_button.setVisible(interactive)
        self.apply_button.setVisible(interactive)
        self.apply_all_button.setVisible(interactive)
        self.finish_button.setVisible(interactive)
        self.finish_button.setEnabled(interactive)
        self.close_button.setVisible(not interactive)
        if issue_count:
            self.issue_table.selectRow(0)
        self._refresh_decision_controls()

    def _suggestion_validation(self, issue: TranslationAuditIssue) -> tuple[bool, str]:
        suggestion = issue.suggested_translation.strip()
        if not suggestion:
            return False, self.tr("高级校对未提供建议译文")
        if suggestion == issue.translated_text.strip():
            return False, self.tr("建议译文与当前译文相同")
        return validate_suggested_translation(
            issue.original_text,
            suggestion,
            self.report.authoritative_terms,
            issue.translated_text,
        )

    def _decision_text(self, issue: TranslationAuditIssue) -> str:
        if not self._interactive:
            return self._display_disposition(issue.disposition.value)
        if issue.cue_id not in self._eligible_ids:
            return self.tr("不可采纳")
        if issue.cue_id in self._accepted_ids:
            return self.tr("将采纳")
        return self.tr("保留原译文")

    def _selected_issue(self) -> Optional[TranslationAuditIssue]:
        row = self.issue_table.currentRow()
        if row < 0 or row >= len(self.report.issues):
            return None
        return self.report.issues[row]

    def _refresh_decision_controls(self) -> None:
        issue = self._selected_issue()
        eligible = bool(
            self._interactive and issue is not None and issue.cue_id in self._eligible_ids
        )
        self.apply_button.setEnabled(eligible)
        self.keep_button.setEnabled(eligible)
        self.apply_all_button.setEnabled(bool(self._interactive and self._eligible_ids))
        if issue is None:
            self.comparison_view.clear()
            self.comparison_card.setVisible(False)
        else:
            self.comparison_card.setVisible(True)
            comparison = self.tr("原文：{0}\n\n当前译文：{1}\n\n建议译文：{2}").format(
                issue.original_text,
                issue.translated_text,
                issue.suggested_translation or self.tr("—"),
            )
            validation_error = self._validation_errors.get(issue.cue_id)
            if self._interactive and validation_error:
                comparison += self.tr("\n\n不可采纳原因：{0}").format(validation_error)
            self.comparison_view.setPlainText(comparison)
        if self._interactive:
            for row, row_issue in enumerate(self.report.issues):
                item = self.issue_table.item(row, 5)
                if item is not None:
                    item.setText(self._decision_text(row_issue))

    def _apply_selected(self) -> None:
        issue = self._selected_issue()
        if issue is not None and issue.cue_id in self._eligible_ids:
            self._accepted_ids.add(issue.cue_id)
            self._refresh_decision_controls()

    def _keep_selected(self) -> None:
        issue = self._selected_issue()
        if issue is not None:
            self._accepted_ids.discard(issue.cue_id)
            self._refresh_decision_controls()

    def _apply_all(self) -> None:
        self._accepted_ids = set(self._eligible_ids)
        self._refresh_decision_controls()

    def _finish_confirmation(self) -> None:
        if not self._interactive:
            return
        self.finish_button.setEnabled(False)
        self.keep_button.setEnabled(False)
        self.apply_button.setEnabled(False)
        self.apply_all_button.setEnabled(False)
        self._interactive = False
        self.summary_label.setText(self.tr("正在写回审计结果…"))
        self.confirmed.emit(tuple(sorted(self._accepted_ids)))


__all__ = ["TranslationAuditPage"]
