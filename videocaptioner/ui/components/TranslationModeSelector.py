"""Task-level controls for the three subtitle translation workflows."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from videocaptioner.core.entities import TranslatorServiceEnum
from videocaptioner.core.llm.profiles import LLMModelProfileStore, LLMProfileError
from videocaptioner.core.translate.enhanced.models import (
    TermConfirmationMode,
    TranslationAuditMode,
)
from videocaptioner.core.translate.types import TranslationMode
from videocaptioner.ui.common.config import cfg


class _ModeCard(QPushButton):
    """A checkable card with an always-visible configuration status."""

    def __init__(self, title: str, description: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.title = title
        self.description = description
        self.status = ""
        self.setCheckable(True)
        self.setMinimumHeight(92)
        self.setCursor(Qt.PointingHandCursor)  # type: ignore
        self._render()

    def set_status(self, status: str) -> None:
        self.status = status
        self._render()

    def _render(self) -> None:
        status = f"\n{self.status}" if self.status else ""
        self.setText(f"{self.title}\n{self.description}{status}")


class _PromptEditor(QDialog):
    def __init__(self, title: str, value: str, parent: QWidget):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(640, 460)
        layout = QVBoxLayout(self)
        self.editor = QTextEdit(self)
        self.editor.setPlainText(value)
        layout.addWidget(self.editor)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


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
        root.setSpacing(8)

        card_row = QHBoxLayout()
        self.card_group = QButtonGroup(self)
        self.card_group.setExclusive(True)
        definitions = (
            (TranslationMode.NON_LLM, self.tr("非 LLM 翻译"), self.tr("快速调用传统翻译服务")),
            (TranslationMode.SINGLE_LLM, self.tr("LLM 翻译"), self.tr("主模型分批翻译")),
            (
                TranslationMode.ENHANCED_LLM,
                self.tr("增强型 LLM 翻译"),
                self.tr("全文分析、术语裁决与质量审计"),
            ),
        )
        for mode, title, description in definitions:
            card = _ModeCard(title, description, self)
            card.clicked.connect(lambda _checked, value=mode: self.set_mode(value))
            self.card_group.addButton(card)
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
    def _panel() -> tuple[QFrame, QFormLayout]:
        panel = QFrame()
        panel.setFrameShape(QFrame.StyledPanel)
        layout = QFormLayout(panel)
        layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)  # type: ignore
        return panel, layout

    def _build_non_llm_panel(self) -> QWidget:
        panel, form = self._panel()
        self.non_llm_service_label = QLabel(self.tr("使用“翻译设置”中的传统翻译服务"), panel)
        form.addRow(self.tr("服务"), self.non_llm_service_label)
        return panel

    def _build_single_llm_panel(self) -> QWidget:
        panel, form = self._panel()
        self.main_profile_combo = QComboBox(panel)
        self.main_profile_combo.currentIndexChanged.connect(self._on_main_profile_changed)
        form.addRow(self.tr("主翻译方案"), self.main_profile_combo)

        self.main_prompt_button = QPushButton(self.tr("编辑主翻译 Prompt"), panel)
        self.main_prompt_button.clicked.connect(self.edit_main_prompt)
        form.addRow(self.tr("Prompt"), self.main_prompt_button)

        self.reflect_checkbox = QCheckBox(self.tr("启用反思翻译"), panel)
        self.reflect_checkbox.setChecked(bool(cfg.need_reflect_translate.value))
        self.reflect_checkbox.toggled.connect(
            lambda checked: cfg.set(cfg.need_reflect_translate, checked)
        )
        form.addRow(self.tr("反思"), self.reflect_checkbox)

        self.single_batch_spin = QSpinBox(panel)
        self.single_batch_spin.setRange(5, 50)
        self.single_batch_spin.setValue(int(cfg.batch_size.value))
        self.single_batch_spin.valueChanged.connect(lambda value: cfg.set(cfg.batch_size, value))
        form.addRow(self.tr("批处理大小"), self.single_batch_spin)
        return panel

    def _build_enhanced_llm_panel(self) -> QWidget:
        panel, form = self._panel()

        # A separate widget mirrors the main-role binding while keeping one cfg ID.
        self.enhanced_main_profile_combo = QComboBox(panel)
        self.enhanced_main_profile_combo.currentIndexChanged.connect(
            self._on_enhanced_main_profile_changed
        )
        form.addRow(self.tr("主翻译方案"), self.enhanced_main_profile_combo)

        self.review_profile_combo = QComboBox(panel)
        self.review_profile_combo.currentIndexChanged.connect(self._on_review_profile_changed)
        form.addRow(self.tr("高级校对方案"), self.review_profile_combo)

        prompt_row = QWidget(panel)
        prompt_layout = QHBoxLayout(prompt_row)
        prompt_layout.setContentsMargins(0, 0, 0, 0)
        enhanced_main_prompt = QPushButton(self.tr("主翻译 Prompt"), prompt_row)
        enhanced_main_prompt.clicked.connect(self.edit_main_prompt)
        review_prompt = QPushButton(self.tr("高级校对 Prompt"), prompt_row)
        review_prompt.clicked.connect(self.edit_review_prompt)
        prompt_layout.addWidget(enhanced_main_prompt)
        prompt_layout.addWidget(review_prompt)
        form.addRow(self.tr("角色 Prompt"), prompt_row)

        self.term_confirmation_combo = QComboBox(panel)
        self.term_confirmation_combo.addItem(self.tr("自动采用校对结论"), TermConfirmationMode.AUTOMATIC)
        self.term_confirmation_combo.addItem(self.tr("人工确认"), TermConfirmationMode.MANUAL)
        self._select_data(self.term_confirmation_combo, cfg.term_confirmation_mode.value)
        self.term_confirmation_combo.currentIndexChanged.connect(
            lambda: cfg.set(cfg.term_confirmation_mode, self.term_confirmation_combo.currentData())
        )
        form.addRow(self.tr("术语确认"), self.term_confirmation_combo)

        self.audit_mode_combo = QComboBox(panel)
        self.audit_mode_combo.addItem(self.tr("审计仅报告"), TranslationAuditMode.REPORT_ONLY)
        self.audit_mode_combo.addItem(
            self.tr("自动修复客观问题"), TranslationAuditMode.AUTO_FIX_OBJECTIVE
        )
        self._select_data(self.audit_mode_combo, cfg.translation_audit_mode.value)
        self.audit_mode_combo.currentIndexChanged.connect(
            lambda: cfg.set(cfg.translation_audit_mode, self.audit_mode_combo.currentData())
        )
        form.addRow(self.tr("审计策略"), self.audit_mode_combo)

        glossary_row = QWidget(panel)
        glossary_layout = QHBoxLayout(glossary_row)
        glossary_layout.setContentsMargins(0, 0, 0, 0)
        self.glossary_path_edit = QLineEdit(glossary_row)
        self.glossary_path_edit.setReadOnly(True)
        glossary_button = QPushButton(self.tr("导入"), glossary_row)
        glossary_button.clicked.connect(self.choose_glossary)
        glossary_layout.addWidget(self.glossary_path_edit, 1)
        glossary_layout.addWidget(glossary_button)
        form.addRow(self.tr("项目术语表"), glossary_row)

        self.enhanced_batch_spin = QSpinBox(panel)
        self.enhanced_batch_spin.setRange(1, 50)
        self.enhanced_batch_spin.setValue(int(cfg.enhanced_batch_size.value))
        self.enhanced_batch_spin.valueChanged.connect(
            lambda value: cfg.set(cfg.enhanced_batch_size, value)
        )
        form.addRow(self.tr("正式翻译批量"), self.enhanced_batch_spin)
        return panel

    @staticmethod
    def _select_data(combo: QComboBox, value: object) -> None:
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
                combo.addItem(self.tr("未选择"), "")
                for profile in profiles:
                    combo.addItem(profile.name, profile.profile_id)
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
        self.cards[selected].setChecked(True)
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
                status = self.tr("配置完整")
            card.set_status(status)
        missing = self.missing_configuration(self.selected_mode)
        reason = self.tr("缺少：") + "、".join(missing) if missing else ""
        self.availability_changed.emit(not missing, reason)

    def _on_external_mode_changed(self, value: object) -> None:
        self.set_mode(str(getattr(value, "value", value)), persist=False)

    def _on_external_non_llm_service_changed(self, _value: object) -> None:
        self._refresh_status()

    def _set_profile_combo_value(self, combo: QComboBox, profile_id: str) -> None:
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
        if dialog.exec_():
            cfg.set(cfg.main_translation_prompt, dialog.editor.toPlainText())

    def edit_review_prompt(self) -> None:
        dialog = _PromptEditor(
            self.tr("高级校对 Prompt"), str(cfg.review_translation_prompt.value), self
        )
        if dialog.exec_():
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
