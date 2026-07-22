"""Task-level controls for the three subtitle translation workflows."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QBrush, QColor, QPainter, QPen
from PyQt5.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    ComboBox,
    IconWidget,
    LineEdit,
    MessageBoxBase,
    PushButton,
    SimpleCardWidget,
    SpinBox,
    StrongBodyLabel,
    SwitchButton,
    TextEdit,
    isDarkTheme,
    themeColor,
)
from qfluentwidgets import FluentIcon as FIF

from videocaptioner.core.entities import TranslatorServiceEnum
from videocaptioner.core.llm.profiles import LLMModelProfileStore, LLMProfileError
from videocaptioner.core.translate.enhanced.models import (
    TermConfirmationMode,
    TranslationAuditMode,
)
from videocaptioner.core.translate.types import TranslationMode
from videocaptioner.ui.common.config import cfg


class _ModeCard(CardWidget):
    """Fluent workflow card with a restrained selected-state accent."""

    def __init__(
        self,
        icon,
        title: str,
        description: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        # CardWidget queries the normal color while its base class is initializing.
        self._checked = False
        super().__init__(parent)
        self.title = title
        self.description = description
        self.status = ""
        self.setClickEnabled(True)
        self.setBorderRadius(10)
        self.setMinimumHeight(88)
        self.setCursor(Qt.PointingHandCursor)  # type: ignore

        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 14, 16, 14)
        layout.setSpacing(14)
        self.icon_widget = IconWidget(icon, self)
        self.icon_widget.setFixedSize(26, 26)
        layout.addWidget(self.icon_widget, 0, Qt.AlignTop)  # type: ignore[arg-type]

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(3)
        self.title_label = StrongBodyLabel(title, self)
        self.description_label = CaptionLabel(description, self)
        self.description_label.setWordWrap(True)
        self.status_label = CaptionLabel(self)
        text_layout.addWidget(self.title_label)
        text_layout.addWidget(self.description_label)
        text_layout.addWidget(self.status_label)
        layout.addLayout(text_layout, 1)

    def text(self) -> str:
        return "\n".join((self.title, self.description, self.status))

    def click(self) -> None:
        self.clicked.emit()

    def setChecked(self, checked: bool) -> None:
        if self._checked == checked:
            return
        self._checked = checked
        self._updateBackgroundColor()
        self.update()

    def isChecked(self) -> bool:
        return self._checked

    def set_status(self, status: str) -> None:
        self.status = status
        self.status_label.setText(status)
        self.status_label.setVisible(bool(status))

    def _normalBackgroundColor(self) -> QColor:
        if not self._checked:
            return super()._normalBackgroundColor()
        color = QColor(themeColor())
        color.setAlpha(36 if isDarkTheme() else 24)
        return color

    def _hoverBackgroundColor(self) -> QColor:
        if self._checked:
            color = QColor(themeColor())
            color.setAlpha(48 if isDarkTheme() else 34)
            return color
        return super()._hoverBackgroundColor()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().paintEvent(event)
        if not self._checked:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        accent = QColor(themeColor())
        painter.setBrush(QBrush())
        painter.setPen(QPen(accent, 1.4))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -2, -2), 10, 10)
        painter.setPen(QPen())
        painter.setBrush(accent)
        painter.drawRoundedRect(5, 16, 3, max(16, self.height() - 32), 2, 2)


class _PromptEditor(MessageBoxBase):
    def __init__(self, title: str, value: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.title_label = StrongBodyLabel(title, self)
        self.editor = TextEdit(self)
        self.editor.setPlainText(value)
        self.editor.setMinimumSize(560, 320)
        self.editor.setPlaceholderText(self.tr("输入该角色的翻译要求、语域和格式规则"))
        self.viewLayout.addWidget(self.title_label)
        self.viewLayout.addWidget(self.editor)
        self.yesButton.setText(self.tr("保存"))
        self.cancelButton.setText(self.tr("取消"))


class TranslationModeSelector(QWidget):
    """Three parallel mode cards plus controls scoped to the selected mode.

    Role selectors write the same persistent config IDs used by the settings
    page.  A selected but incomplete mode remains selectable and reports its
    missing role through :attr:`availability_changed`.
    """

    mode_changed = pyqtSignal(object)
    availability_changed = pyqtSignal(bool, str)
    glossary_path_changed = pyqtSignal(str)

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        profile_store: Optional[LLMModelProfileStore] = None,
    ) -> None:
        super().__init__(parent)
        self.profile_store = profile_store
        self.imported_glossary_path = ""
        self._profiles_error = ""
        self._profile_ids: set[str] = set()
        self._syncing = False
        self.cards: dict[TranslationMode, _ModeCard] = {}
        self._build_ui()
        self._connect_config_signals()
        self.refresh_profiles()
        self.set_mode(cfg.translation_mode.value, persist=False)

    @property
    def selected_mode(self) -> TranslationMode:
        value = cfg.translation_mode.value
        return value if isinstance(value, TranslationMode) else TranslationMode(str(value))

    @property
    def is_selected_mode_available(self) -> bool:
        return not self.missing_configuration(self.selected_mode)

    def missing_configuration(self, mode: TranslationMode) -> tuple[str, ...]:
        missing: list[str] = []
        if mode is TranslationMode.NON_LLM and cfg.translator_service.value not in {
            TranslatorServiceEnum.BING,
            TranslatorServiceEnum.GOOGLE,
            TranslatorServiceEnum.DEEPLX,
        }:
            missing.append(self.tr("非 LLM 翻译服务"))
        if mode in {TranslationMode.SINGLE_LLM, TranslationMode.ENHANCED_LLM}:
            if str(cfg.main_llm_profile_id.value).strip() not in self._profile_ids:
                missing.append(self.tr("主翻译模型"))
        if mode is TranslationMode.ENHANCED_LLM:
            if str(cfg.review_llm_profile_id.value).strip() not in self._profile_ids:
                missing.append(self.tr("高级校对模型"))
        return tuple(missing)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        card_row = QHBoxLayout()
        card_row.setContentsMargins(0, 0, 0, 0)
        card_row.setSpacing(10)
        definitions = (
            (
                TranslationMode.NON_LLM,
                FIF.LANGUAGE,
                self.tr("非 LLM 翻译"),
                self.tr("使用传统翻译服务快速处理"),
            ),
            (
                TranslationMode.SINGLE_LLM,
                FIF.ROBOT,
                self.tr("LLM 翻译"),
                self.tr("由主模型分批完成翻译"),
            ),
            (
                TranslationMode.ENHANCED_LLM,
                FIF.COMPLETED,
                self.tr("增强型 LLM 翻译"),
                self.tr("全文分析、术语裁决与质量审计"),
            ),
        )
        for mode, icon, title, description in definitions:
            card = _ModeCard(icon, title, description, self)
            card.clicked.connect(lambda value=mode: self.set_mode(value))
            self.cards[mode] = card
            card_row.addWidget(card, 1)
        root.addLayout(card_row)

        self.options_stack = QStackedWidget(self)
        self.non_llm_panel = self._build_non_llm_panel()
        self.single_llm_panel = self._build_single_llm_panel()
        self.enhanced_llm_panel = self._build_enhanced_llm_panel()
        for panel in (self.non_llm_panel, self.single_llm_panel, self.enhanced_llm_panel):
            self.options_stack.addWidget(panel)
        root.addWidget(self.options_stack)

    @staticmethod
    def _panel() -> tuple[SimpleCardWidget, QGridLayout]:
        panel = SimpleCardWidget()
        panel.setBorderRadius(10)
        layout = QGridLayout(panel)
        layout.setContentsMargins(20, 14, 20, 16)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(10)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(3, 1)
        return panel, layout

    @staticmethod
    def _prepare_combo(combo: ComboBox) -> None:
        combo.setMinimumWidth(160)
        combo.setMaxVisibleItems(8)

    def _build_non_llm_panel(self) -> QWidget:
        panel, grid = self._panel()
        self.non_llm_service_label = BodyLabel(panel)
        grid.addWidget(self.non_llm_service_label, 0, 0, 1, 4)
        return panel

    def _build_single_llm_panel(self) -> QWidget:
        panel, grid = self._panel()
        grid.addWidget(BodyLabel(self.tr("主翻译模型"), panel), 0, 0)
        self.main_profile_combo = ComboBox(panel)
        self._prepare_combo(self.main_profile_combo)
        self.main_profile_combo.currentIndexChanged.connect(self._on_main_profile_changed)
        grid.addWidget(self.main_profile_combo, 0, 1)

        self.main_prompt_button = PushButton(panel)
        self.main_prompt_button.setIcon(FIF.EDIT)
        self.main_prompt_button.setText(self.tr("编辑主翻译 Prompt"))
        self.main_prompt_button.clicked.connect(self.edit_main_prompt)
        grid.addWidget(self.main_prompt_button, 0, 2, 1, 2)

        grid.addWidget(BodyLabel(self.tr("反思翻译"), panel), 1, 0)
        self.reflect_checkbox = SwitchButton(panel)
        self.reflect_checkbox.setOnText(self.tr("开启"))
        self.reflect_checkbox.setOffText(self.tr("关闭"))
        self.reflect_checkbox.setChecked(bool(cfg.need_reflect_translate.value))
        self.reflect_checkbox.checkedChanged.connect(
            lambda checked: cfg.set(cfg.need_reflect_translate, checked)
        )
        grid.addWidget(self.reflect_checkbox, 1, 1, Qt.AlignLeft)  # type: ignore[arg-type]

        grid.addWidget(BodyLabel(self.tr("每批字幕"), panel), 1, 2)
        self.single_batch_spin = SpinBox(panel)
        self.single_batch_spin.setRange(5, 50)
        self.single_batch_spin.setValue(int(cfg.batch_size.value))
        self.single_batch_spin.valueChanged.connect(lambda value: cfg.set(cfg.batch_size, value))
        self.single_batch_spin.setMinimumWidth(120)
        grid.addWidget(self.single_batch_spin, 1, 3)
        return panel

    def _build_enhanced_llm_panel(self) -> QWidget:
        panel, grid = self._panel()
        # A separate widget mirrors the main-role binding while keeping one cfg ID.
        grid.addWidget(BodyLabel(self.tr("主翻译模型"), panel), 0, 0)
        self.enhanced_main_profile_combo = ComboBox(panel)
        self._prepare_combo(self.enhanced_main_profile_combo)
        self.enhanced_main_profile_combo.currentIndexChanged.connect(
            self._on_enhanced_main_profile_changed
        )
        grid.addWidget(self.enhanced_main_profile_combo, 0, 1)

        grid.addWidget(BodyLabel(self.tr("高级校对模型"), panel), 0, 2)
        self.review_profile_combo = ComboBox(panel)
        self._prepare_combo(self.review_profile_combo)
        self.review_profile_combo.currentIndexChanged.connect(self._on_review_profile_changed)
        grid.addWidget(self.review_profile_combo, 0, 3)

        prompt_row = QWidget(panel)
        prompt_layout = QHBoxLayout(prompt_row)
        prompt_layout.setContentsMargins(0, 0, 0, 0)
        prompt_layout.setSpacing(8)
        enhanced_main_prompt = PushButton(prompt_row)
        enhanced_main_prompt.setIcon(FIF.EDIT)
        enhanced_main_prompt.setText(self.tr("主翻译 Prompt"))
        enhanced_main_prompt.clicked.connect(self.edit_main_prompt)
        review_prompt = PushButton(prompt_row)
        review_prompt.setIcon(FIF.EDIT)
        review_prompt.setText(self.tr("高级校对 Prompt"))
        review_prompt.clicked.connect(self.edit_review_prompt)
        prompt_layout.addWidget(enhanced_main_prompt)
        prompt_layout.addWidget(review_prompt)
        grid.addWidget(BodyLabel(self.tr("角色 Prompt"), panel), 1, 0)
        grid.addWidget(prompt_row, 1, 1)

        grid.addWidget(BodyLabel(self.tr("术语确认"), panel), 1, 2)
        self.term_confirmation_combo = ComboBox(panel)
        self._prepare_combo(self.term_confirmation_combo)
        self.term_confirmation_combo.addItem(
            self.tr("自动采用校对结论"), userData=TermConfirmationMode.AUTOMATIC
        )
        self.term_confirmation_combo.addItem(
            self.tr("人工确认"), userData=TermConfirmationMode.MANUAL
        )
        self._select_data(self.term_confirmation_combo, cfg.term_confirmation_mode.value)
        self.term_confirmation_combo.currentIndexChanged.connect(
            lambda: cfg.set(cfg.term_confirmation_mode, self.term_confirmation_combo.currentData())
        )
        grid.addWidget(self.term_confirmation_combo, 1, 3)

        grid.addWidget(BodyLabel(self.tr("审计策略"), panel), 2, 0)
        self.audit_mode_combo = ComboBox(panel)
        self._prepare_combo(self.audit_mode_combo)
        self.audit_mode_combo.addItem(
            self.tr("审计并人工确认"), userData=TranslationAuditMode.REVIEW_AND_CONFIRM
        )
        self.audit_mode_combo.addItem(
            self.tr("自动采纳校对修正"),
            userData=TranslationAuditMode.AUTO_APPLY_REVIEW,
        )
        self._select_data(self.audit_mode_combo, cfg.translation_audit_mode.value)
        self.audit_mode_combo.currentIndexChanged.connect(
            lambda: cfg.set(cfg.translation_audit_mode, self.audit_mode_combo.currentData())
        )
        grid.addWidget(self.audit_mode_combo, 2, 1)

        glossary_row = QWidget(panel)
        glossary_layout = QHBoxLayout(glossary_row)
        glossary_layout.setContentsMargins(0, 0, 0, 0)
        glossary_layout.setSpacing(8)
        self.glossary_path_edit = LineEdit(glossary_row)
        self.glossary_path_edit.setReadOnly(True)
        self.glossary_path_edit.setPlaceholderText(self.tr("可选：导入已有项目术语表"))
        glossary_button = PushButton(glossary_row)
        glossary_button.setIcon(FIF.FOLDER)
        glossary_button.setText(self.tr("选择文件"))
        glossary_button.clicked.connect(self.choose_glossary)
        glossary_layout.addWidget(self.glossary_path_edit, 1)
        glossary_layout.addWidget(glossary_button)
        grid.addWidget(BodyLabel(self.tr("项目术语表"), panel), 2, 2)
        grid.addWidget(glossary_row, 2, 3)

        grid.addWidget(BodyLabel(self.tr("每批字幕"), panel), 3, 0)
        self.enhanced_batch_spin = SpinBox(panel)
        self.enhanced_batch_spin.setRange(1, 50)
        self.enhanced_batch_spin.setValue(int(cfg.enhanced_batch_size.value))
        self.enhanced_batch_spin.valueChanged.connect(
            lambda value: cfg.set(cfg.enhanced_batch_size, value)
        )
        self.enhanced_batch_spin.setMinimumWidth(120)
        grid.addWidget(self.enhanced_batch_spin, 3, 1)
        return panel

    @staticmethod
    def _select_data(combo: ComboBox, value: object) -> None:
        raw = getattr(value, "value", value)
        for index in range(combo.count()):
            candidate = combo.itemData(index)
            if getattr(candidate, "value", candidate) == raw:
                combo.setCurrentIndex(index)
                return

    def _connect_config_signals(self) -> None:
        cfg.translation_mode.valueChanged.connect(self._on_external_mode_changed)
        cfg.translator_service.valueChanged.connect(self._on_external_non_llm_service_changed)
        cfg.main_llm_profile_id.valueChanged.connect(self._on_external_main_profile_changed)
        cfg.review_llm_profile_id.valueChanged.connect(self._on_external_review_profile_changed)

    def refresh_profiles(self) -> None:
        self._profiles_error = ""
        if self.profile_store is None:
            try:
                self.profile_store = LLMModelProfileStore()
            except LLMProfileError as exc:
                self._profiles_error = str(exc)
        else:
            try:
                self.profile_store.reload()
            except LLMProfileError as exc:
                self._profiles_error = str(exc)

        profiles = self.profile_store.list() if self.profile_store and not self._profiles_error else ()
        self._profile_ids = {profile.profile_id for profile in profiles}
        self._syncing = True
        try:
            for combo, current in (
                (self.main_profile_combo, str(cfg.main_llm_profile_id.value)),
                (self.enhanced_main_profile_combo, str(cfg.main_llm_profile_id.value)),
                (self.review_profile_combo, str(cfg.review_llm_profile_id.value)),
            ):
                combo.clear()
                combo.addItem(self.tr("未选择"), userData="")
                for profile in profiles:
                    combo.addItem(profile.name, userData=profile.profile_id)
                index = combo.findData(current)
                combo.setCurrentIndex(index if index >= 0 else 0)
        finally:
            self._syncing = False
        self._refresh_status()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.refresh_profiles()
        super().showEvent(event)

    def set_mode(self, mode: TranslationMode | str, *, persist: bool = True) -> None:
        selected = mode if isinstance(mode, TranslationMode) else TranslationMode(str(mode))
        if persist and cfg.translation_mode.value != selected:
            cfg.set(cfg.translation_mode, selected)
        index = {
            TranslationMode.NON_LLM: 0,
            TranslationMode.SINGLE_LLM: 1,
            TranslationMode.ENHANCED_LLM: 2,
        }[selected]
        for card_mode, card in self.cards.items():
            card.setChecked(card_mode is selected)
        self.options_stack.setCurrentIndex(index)
        self._refresh_status()
        self.mode_changed.emit(selected)

    def _refresh_status(self) -> None:
        for mode, card in self.cards.items():
            missing = self.missing_configuration(mode)
            if self._profiles_error and mode is not TranslationMode.NON_LLM:
                status = self.tr("配置读取失败")
            elif missing:
                status = self.tr("缺少：") + "、".join(missing)
            else:
                status = ""
            card.set_status(status)
            card.status_label.setStyleSheet(
                f"color: {themeColor().name()};" if not missing else "color: #d89614;"
            )
        service_name = getattr(cfg.translator_service.value, "value", cfg.translator_service.value)
        self.non_llm_service_label.setText(self.tr("当前服务：") + str(service_name))
        missing = self.missing_configuration(self.selected_mode)
        reason = self.tr("缺少：") + "、".join(missing) if missing else ""
        self.availability_changed.emit(not missing, reason)

    def _on_external_mode_changed(self, value: object) -> None:
        self.set_mode(str(getattr(value, "value", value)), persist=False)

    def _on_external_non_llm_service_changed(self, _value: object) -> None:
        self._refresh_status()

    def _set_profile_combo_value(self, combo: ComboBox, profile_id: str) -> None:
        index = combo.findData(profile_id)
        self._syncing = True
        combo.setCurrentIndex(index if index >= 0 else 0)
        self._syncing = False

    def _on_external_main_profile_changed(self, value: object) -> None:
        profile_id = str(value)
        if profile_id and profile_id not in self._profile_ids:
            self.refresh_profiles()
            return
        self._set_profile_combo_value(self.main_profile_combo, profile_id)
        self._set_profile_combo_value(self.enhanced_main_profile_combo, profile_id)
        self._refresh_status()

    def _on_external_review_profile_changed(self, value: object) -> None:
        profile_id = str(value)
        if profile_id and profile_id not in self._profile_ids:
            self.refresh_profiles()
            return
        self._set_profile_combo_value(self.review_profile_combo, profile_id)
        self._refresh_status()

    def _on_main_profile_changed(self) -> None:
        if self._syncing:
            return
        cfg.set(cfg.main_llm_profile_id, str(self.main_profile_combo.currentData() or ""))

    def _on_enhanced_main_profile_changed(self) -> None:
        if self._syncing:
            return
        cfg.set(
            cfg.main_llm_profile_id,
            str(self.enhanced_main_profile_combo.currentData() or ""),
        )

    def _on_review_profile_changed(self) -> None:
        if self._syncing:
            return
        cfg.set(cfg.review_llm_profile_id, str(self.review_profile_combo.currentData() or ""))

    def edit_main_prompt(self) -> None:
        dialog = _PromptEditor(
            self.tr("主翻译 Prompt"), str(cfg.main_translation_prompt.value), self
        )
        if dialog.exec():
            cfg.set(cfg.main_translation_prompt, dialog.editor.toPlainText())

    def edit_review_prompt(self) -> None:
        dialog = _PromptEditor(
            self.tr("高级校对 Prompt"), str(cfg.review_translation_prompt.value), self
        )
        if dialog.exec():
            cfg.set(cfg.review_translation_prompt, dialog.editor.toPlainText())

    def choose_glossary(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("导入项目术语表"),
            str(Path(self.imported_glossary_path).parent)
            if self.imported_glossary_path
            else "",
            self.tr("VideoCaptioner 项目术语表 (*.vcglossary.json)"),
        )
        if path:
            self.set_imported_glossary_path(path)

    def set_imported_glossary_path(self, path: str) -> None:
        self.imported_glossary_path = path
        self.glossary_path_edit.setText(path)
        self.glossary_path_changed.emit(path)


__all__ = ["TranslationModeSelector"]
