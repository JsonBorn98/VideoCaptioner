"""Full-size manual glossary confirmation workbench."""

from __future__ import annotations

from dataclasses import replace
from typing import Mapping, Optional, Sequence

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from videocaptioner.core.translate.enhanced.models import (
    GlossarySelectionSource,
    TermCandidate,
)


class GlossaryReviewPage(QWidget):
    """Master/detail editor that trusts the user's current selection as-is."""

    confirmed = pyqtSignal(object)
    cancelled = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._candidates: list[TermCandidate] = []
        self._contexts: Mapping[int, str] = {}
        self._updating = False
        self._build_ui()

    @property
    def candidates(self) -> tuple[TermCandidate, ...]:
        self._store_current_choice()
        return tuple(self._candidates)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        title = QLabel(self.tr("人工术语确认"), self)
        title.setObjectName("glossaryReviewTitle")
        root.addWidget(title)

        splitter = QSplitter(Qt.Horizontal, self)  # type: ignore
        self.term_list = QListWidget(splitter)
        self.term_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.term_list.currentRowChanged.connect(self._show_candidate)
        splitter.addWidget(self.term_list)

        detail = QWidget(splitter)
        detail_layout = QVBoxLayout(detail)
        self.source_label = QLabel(detail)
        self.sense_label = QLabel(detail)
        self.main_radio = QRadioButton(detail)
        self.review_radio = QRadioButton(detail)
        self.custom_radio = QRadioButton(self.tr("自定义译法"), detail)
        self.ignore_radio = QRadioButton(self.tr("不是术语／忽略"), detail)
        self.choice_group = QButtonGroup(self)
        for radio in (
            self.main_radio,
            self.review_radio,
            self.custom_radio,
            self.ignore_radio,
        ):
            self.choice_group.addButton(radio)
            radio.toggled.connect(self._store_current_choice)
        from qfluentwidgets import LineEdit

        self.custom_edit = LineEdit(detail)
        self.custom_edit.setPlaceholderText(self.tr("允许留空；系统将原样采用"))
        self.custom_edit.textEdited.connect(self._on_custom_edited)
        self.final_label = QLabel(detail)
        self.occurrence_label = QLabel(detail)
        self.context_view = QTextBrowser(detail)
        self.context_view.setOpenExternalLinks(False)

        detail_layout.addWidget(self.source_label)
        detail_layout.addWidget(self.sense_label)
        detail_layout.addWidget(self.main_radio)
        detail_layout.addWidget(self.review_radio)
        detail_layout.addWidget(self.custom_radio)
        detail_layout.addWidget(self.custom_edit)
        detail_layout.addWidget(self.ignore_radio)
        detail_layout.addWidget(self.final_label)
        detail_layout.addWidget(self.occurrence_label)
        detail_layout.addWidget(QLabel(self.tr("代表语境"), detail))
        detail_layout.addWidget(self.context_view, 1)
        splitter.addWidget(detail)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        root.addWidget(splitter, 1)

        actions = QHBoxLayout()
        self.previous_button = QPushButton(self.tr("上一项"), self)
        self.next_button = QPushButton(self.tr("下一项"), self)
        self.merge_button = QPushButton(self.tr("合并所选候选"), self)
        self.continue_button = QPushButton(self.tr("继续翻译"), self)
        self.previous_button.clicked.connect(lambda: self._move(-1))
        self.next_button.clicked.connect(lambda: self._move(1))
        self.merge_button.clicked.connect(self.merge_selected)
        self.continue_button.clicked.connect(self._confirm)
        actions.addWidget(self.previous_button)
        actions.addWidget(self.next_button)
        actions.addWidget(self.merge_button)
        actions.addStretch(1)
        actions.addWidget(self.continue_button)
        root.addLayout(actions)

    def set_candidates(
        self,
        candidates: Sequence[TermCandidate],
        contexts: Optional[Mapping[int, str]] = None,
    ) -> None:
        self._candidates = list(candidates)
        self._contexts = contexts or {}
        self.term_list.clear()
        for candidate in self._candidates:
            item = QListWidgetItem(candidate.source_term)
            item.setToolTip(candidate.sense)
            self.term_list.addItem(item)
        if self._candidates:
            self.term_list.setCurrentRow(0)

    def _show_candidate(self, row: int) -> None:
        if row < 0 or row >= len(self._candidates):
            return
        candidate = self._candidates[row]
        self._updating = True
        try:
            self.source_label.setText(self.tr("源术语：") + candidate.source_term)
            self.sense_label.setText(self.tr("义项：") + candidate.sense)
            self.main_radio.setText(self.tr("主翻译建议：") + candidate.main_translation)
            self.review_radio.setText(self.tr("高级校对结论：") + candidate.review_translation)
            self.custom_edit.setText(
                candidate.final_translation
                if candidate.selection_source is GlossarySelectionSource.USER_CUSTOM
                else ""
            )
            if candidate.ignored or not candidate.is_term:
                self.ignore_radio.setChecked(True)
            elif candidate.selection_source is GlossarySelectionSource.USER_MAIN:
                self.main_radio.setChecked(True)
            elif candidate.selection_source is GlossarySelectionSource.USER_CUSTOM:
                self.custom_radio.setChecked(True)
            else:
                self.review_radio.setChecked(True)
            self.final_label.setText(self.tr("当前最终译法：") + candidate.final_translation)
            ids = ", ".join(str(value) for value in candidate.occurrence_ids)
            self.occurrence_label.setText(self.tr("出现位置：") + ids)
            representative_ids = (
                candidate.representative_context_ids or candidate.occurrence_ids[:5]
            )
            blocks = [
                f"#{cue_id}  {self._contexts.get(cue_id, self.tr('语境不可用'))}"
                for cue_id in representative_ids
            ]
            self.context_view.setPlainText("\n\n".join(blocks))
        finally:
            self._updating = False
        self.previous_button.setEnabled(row > 0)
        self.next_button.setEnabled(row + 1 < len(self._candidates))

    def _store_current_choice(self) -> None:
        if self._updating:
            return
        row = self.term_list.currentRow()
        if row < 0 or row >= len(self._candidates):
            return
        candidate = self._candidates[row]
        if self.ignore_radio.isChecked():
            updated = replace(candidate, is_term=False, ignored=True)
        elif self.main_radio.isChecked():
            updated = replace(
                candidate,
                final_translation=candidate.main_translation,
                selection_source=GlossarySelectionSource.USER_MAIN,
                is_term=True,
                ignored=False,
            )
        elif self.custom_radio.isChecked():
            updated = replace(
                candidate,
                final_translation=self.custom_edit.text(),
                selection_source=GlossarySelectionSource.USER_CUSTOM,
                is_term=True,
                ignored=False,
            )
        else:
            updated = replace(
                candidate,
                final_translation=candidate.review_translation,
                selection_source=GlossarySelectionSource.USER_REVIEW,
                is_term=True,
                ignored=False,
            )
        self._candidates[row] = updated
        self.final_label.setText(self.tr("当前最终译法：") + updated.final_translation)

    def _on_custom_edited(self, _text: str) -> None:
        if not self.custom_radio.isChecked():
            self.custom_radio.setChecked(True)
        self._store_current_choice()

    def _move(self, offset: int) -> None:
        self._store_current_choice()
        row = self.term_list.currentRow() + offset
        if 0 <= row < len(self._candidates):
            self.term_list.setCurrentRow(row)

    def merge_selected(self) -> None:
        """Merge aliases, occurrences and contexts into the current master item."""

        self._store_current_choice()
        rows = sorted({self.term_list.row(item) for item in self.term_list.selectedItems()})
        master_row = self.term_list.currentRow()
        if master_row not in rows:
            rows.append(master_row)
            rows.sort()
        rows = [row for row in rows if 0 <= row < len(self._candidates)]
        if len(rows) < 2:
            return
        master = self._candidates[master_row]
        merged_aliases = list(master.aliases)
        merged_occurrences = set(master.occurrence_ids)
        merged_context_ids = set(master.representative_context_ids)
        for row in rows:
            if row == master_row:
                continue
            other = self._candidates[row]
            merged_aliases.extend((other.source_term, *other.aliases))
            merged_occurrences.update(other.occurrence_ids)
            merged_context_ids.update(other.representative_context_ids)
        master = replace(
            master,
            aliases=tuple(dict.fromkeys(merged_aliases)),
            occurrence_ids=tuple(sorted(merged_occurrences)),
            representative_context_ids=tuple(sorted(merged_context_ids)),
        )
        self._candidates[master_row] = master
        for row in reversed(rows):
            if row != master_row:
                del self._candidates[row]
        self.set_candidates(self._candidates, self._contexts)
        new_row = next(
            index
            for index, candidate in enumerate(self._candidates)
            if candidate.candidate_id == master.candidate_id
        )
        self.term_list.setCurrentRow(new_row)

    def _confirm(self) -> None:
        # Deliberately no pending-state or non-empty custom-value validation.
        self._store_current_choice()
        self.confirmed.emit(tuple(self._candidates))


__all__ = ["GlossaryReviewPage"]
