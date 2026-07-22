"""Full-size manual glossary confirmation workbench."""

from __future__ import annotations

from dataclasses import replace
from typing import Mapping, Optional, Sequence

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QHBoxLayout,
    QListWidgetItem,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    LineEdit,
    ListWidget,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    RadioButton,
    SimpleCardWidget,
    StrongBodyLabel,
)
from qfluentwidgets import FluentIcon as FIF

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
        root.setContentsMargins(0, 6, 0, 4)
        root.setSpacing(12)

        heading = QHBoxLayout()
        heading.setContentsMargins(8, 0, 8, 0)
        title_block = QVBoxLayout()
        title_block.setSpacing(2)
        title = StrongBodyLabel(self.tr("人工术语确认"), self)
        title.setObjectName("glossaryReviewTitle")
        subtitle = CaptionLabel(
            self.tr("核对主翻译建议与高级校对结论；当前默认采用校对结果"), self
        )
        title_block.addWidget(title)
        title_block.addWidget(subtitle)
        heading.addLayout(title_block)
        heading.addStretch(1)
        self.count_label = CaptionLabel(self)
        heading.addWidget(self.count_label, 0, Qt.AlignVCenter)  # type: ignore[arg-type]
        root.addLayout(heading)

        splitter = QSplitter(Qt.Horizontal, self)  # type: ignore
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(8)

        list_card = SimpleCardWidget(splitter)
        list_card.setBorderRadius(10)
        list_layout = QVBoxLayout(list_card)
        list_layout.setContentsMargins(12, 12, 12, 12)
        list_layout.setSpacing(8)
        list_layout.addWidget(BodyLabel(self.tr("疑难术语"), list_card))
        self.term_list = ListWidget(list_card)
        self.term_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.term_list.setMinimumWidth(220)
        self.term_list.currentRowChanged.connect(self._show_candidate)
        list_layout.addWidget(self.term_list, 1)
        splitter.addWidget(list_card)

        detail = SimpleCardWidget(splitter)
        detail.setBorderRadius(10)
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(18, 14, 18, 16)
        detail_layout.setSpacing(9)
        self.source_label = StrongBodyLabel(detail)
        self.source_label.setWordWrap(True)
        self.sense_label = CaptionLabel(detail)
        self.sense_label.setWordWrap(True)
        self.main_radio = RadioButton(detail)
        self.review_radio = RadioButton(detail)
        self.custom_radio = RadioButton(self.tr("自定义译法"), detail)
        self.ignore_radio = RadioButton(self.tr("不是术语／忽略"), detail)
        self.choice_group = QButtonGroup(self)
        for radio in (
            self.main_radio,
            self.review_radio,
            self.custom_radio,
            self.ignore_radio,
        ):
            self.choice_group.addButton(radio)
            radio.toggled.connect(self._store_current_choice)
        self.custom_edit = LineEdit(detail)
        self.custom_edit.setPlaceholderText(self.tr("输入最终译法（可留空）"))
        self.custom_edit.textEdited.connect(self._on_custom_edited)
        self.final_label = BodyLabel(detail)
        self.final_label.setWordWrap(True)
        self.occurrence_label = CaptionLabel(detail)
        self.context_view = PlainTextEdit(detail)
        self.context_view.setReadOnly(True)
        self.context_view.setPlaceholderText(self.tr("代表语境不可用"))

        detail_layout.addWidget(self.source_label)
        detail_layout.addWidget(self.sense_label)
        detail_layout.addSpacing(2)
        detail_layout.addWidget(self.main_radio)
        detail_layout.addWidget(self.review_radio)
        detail_layout.addWidget(self.custom_radio)
        detail_layout.addWidget(self.custom_edit)
        detail_layout.addWidget(self.ignore_radio)
        detail_layout.addSpacing(4)
        detail_layout.addWidget(self.final_label)
        detail_layout.addWidget(self.occurrence_label)
        detail_layout.addWidget(BodyLabel(self.tr("代表语境"), detail))
        detail_layout.addWidget(self.context_view, 1)
        splitter.addWidget(detail)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        root.addWidget(splitter, 1)

        actions = QHBoxLayout()
        actions.setContentsMargins(8, 0, 8, 0)
        actions.setSpacing(8)
        self.previous_button = PushButton(self)
        self.previous_button.setIcon(FIF.LEFT_ARROW)
        self.previous_button.setText(self.tr("上一项"))
        self.next_button = PushButton(self)
        self.next_button.setIcon(FIF.RIGHT_ARROW)
        self.next_button.setText(self.tr("下一项"))
        self.merge_button = PushButton(self)
        self.merge_button.setIcon(FIF.LINK)
        self.merge_button.setText(self.tr("合并所选候选"))
        self.continue_button = PrimaryPushButton(self)
        self.continue_button.setIcon(FIF.ACCEPT)
        self.continue_button.setText(self.tr("继续翻译"))
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
        self.count_label.setText(self.tr("{0} 个候选").format(len(self._candidates)))
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
                self._contexts.get(cue_id, self.tr("语境不可用"))
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
