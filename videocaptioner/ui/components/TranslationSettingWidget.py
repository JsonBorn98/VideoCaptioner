"""Three parallel translation settings pages and named LLM profile management."""

from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    ComboBox,
    ComboBoxSettingCard,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    MessageBox,
    MessageBoxBase,
    PasswordLineEdit,
    PushButton,
    RangeSettingCard,
    SegmentedWidget,
    SettingCard,
    SettingCardGroup,
    SpinBox,
    SubtitleLabel,
    SwitchSettingCard,
    TextEdit,
)
from qfluentwidgets import FluentIcon as FIF

from videocaptioner.core.entities import TranslatorServiceEnum
from videocaptioner.core.llm.check_llm import check_model_profile_connection
from videocaptioner.core.llm.models import (
    LLMModelProfile,
    LLMTransport,
    ProviderDialect,
)
from videocaptioner.core.llm.profiles import LLMModelProfileStore
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.components.LineEditSettingCard import LineEditSettingCard


class _PromptDialog(MessageBoxBase):
    def __init__(self, title: str, text: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.titleLabel = BodyLabel(title, self)
        self.promptEdit = TextEdit(self)
        self.promptEdit.setPlainText(text)
        self.promptEdit.setPlaceholderText(self.tr("输入该角色在翻译任务中使用的自定义 Prompt"))
        self.promptEdit.setMinimumSize(520, 280)
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.promptEdit)
        self.yesButton.setText(self.tr("保存"))
        self.cancelButton.setText(self.tr("取消"))


class PromptSettingCard(SettingCard):
    def __init__(self, config_item, title: str, content: str, parent=None):
        super().__init__(FIF.DOCUMENT, title, content, parent)
        self.configItem = config_item
        self.editButton = PushButton(self.tr("编辑 Prompt"), self)
        self.editButton.setObjectName("translationPromptEditButton")
        self.hBoxLayout.addWidget(self.editButton, 0, Qt.AlignRight)  # type: ignore
        self.hBoxLayout.addSpacing(16)
        self.editButton.clicked.connect(self._edit)
        config_item.valueChanged.connect(self._refreshSummary)
        self._refreshSummary(cfg.get(config_item))

    def _refreshSummary(self, value: str) -> None:
        summary = self.tr("已配置") if value.strip() else self.tr("使用系统默认 Prompt")
        self.contentLabel.setText(summary)

    def _edit(self) -> None:
        dialog = _PromptDialog(self.titleLabel.text(), cfg.get(self.configItem), self.window())
        if dialog.exec():
            cfg.set(self.configItem, dialog.promptEdit.toPlainText())


class _ProfileDialog(MessageBoxBase):
    probeRequested = pyqtSignal(object)

    def __init__(
        self,
        profile: Optional[LLMModelProfile] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.profileId = profile.profile_id if profile else ""
        self.titleLabel = BodyLabel(
            self.tr("编辑模型方案") if profile else self.tr("新增模型方案"), self
        )
        self.nameEdit = LineEdit(self)
        self.transportCombo = ComboBox(self)
        self.dialectCombo = ComboBox(self)
        self.baseUrlEdit = LineEdit(self)
        self.apiKeyEdit = PasswordLineEdit(self)
        self.modelEdit = LineEdit(self)
        self.contextSpin = SpinBox(self)
        self.concurrencySpin = SpinBox(self)
        self.probeButton = PushButton(self.tr("探查"), self)
        self.probeButton.setObjectName("modelContextProbeButton")

        self.transportCombo.addItems([item.value for item in LLMTransport])
        self.dialectCombo.addItems([item.value for item in ProviderDialect])
        self.contextSpin.setRange(16_384, 2_000_000)
        self.contextSpin.setSingleStep(1024)
        self.concurrencySpin.setRange(1, 50)
        self.apiKeyEdit.setClearButtonEnabled(True)
        self.baseUrlEdit.setPlaceholderText("https://api.example.com/v1")
        self.modelEdit.setPlaceholderText(self.tr("模型名称"))

        if profile:
            self.nameEdit.setText(profile.name)
            self.transportCombo.setCurrentText(profile.transport.value)
            self.dialectCombo.setCurrentText(profile.dialect.value)
            self.baseUrlEdit.setText(profile.base_url)
            self.apiKeyEdit.setText(profile.api_key)
            self.modelEdit.setText(profile.model)
            self.contextSpin.setValue(profile.work_context_tokens)
            self.concurrencySpin.setValue(profile.max_concurrency)
        else:
            self.transportCombo.setCurrentText(LLMTransport.OPENAI_COMPATIBLE.value)
            self.dialectCombo.setCurrentText(ProviderDialect.GENERIC.value)
            self.contextSpin.setValue(65_536)
            self.concurrencySpin.setValue(4)

        contextRow = QWidget(self)
        contextLayout = QHBoxLayout(contextRow)
        contextLayout.setContentsMargins(0, 0, 0, 0)
        contextLayout.addWidget(self.contextSpin, 1)
        contextLayout.addWidget(self.probeButton)
        formWidget = QWidget(self)
        form = QFormLayout(formWidget)
        form.setContentsMargins(0, 0, 0, 0)
        form.setVerticalSpacing(12)
        form.addRow(self.tr("方案名称"), self.nameEdit)
        form.addRow(self.tr("接口格式"), self.transportCombo)
        form.addRow(self.tr("供应商方言"), self.dialectCombo)
        form.addRow(self.tr("Base URL"), self.baseUrlEdit)
        form.addRow(self.tr("API Key"), self.apiKeyEdit)
        form.addRow(self.tr("模型"), self.modelEdit)
        form.addRow(self.tr("工作上下文上限"), contextRow)
        form.addRow(self.tr("最大并发"), self.concurrencySpin)
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(formWidget)
        self.widget.setMinimumWidth(620)
        self.yesButton.setText(self.tr("保存"))
        self.cancelButton.setText(self.tr("取消"))
        self.probeButton.clicked.connect(self._requestProbe)
        for edit in (self.nameEdit, self.baseUrlEdit, self.modelEdit):
            edit.textChanged.connect(self._updateSaveState)
        self._updateSaveState()

    def _updateSaveState(self) -> None:
        self.yesButton.setEnabled(
            bool(
                self.nameEdit.text().strip()
                and self.baseUrlEdit.text().strip()
                and self.modelEdit.text().strip()
            )
        )

    def values(self) -> dict:
        return {
            "name": self.nameEdit.text().strip(),
            "transport": LLMTransport(self.transportCombo.currentText()),
            "dialect": ProviderDialect(self.dialectCombo.currentText()),
            "base_url": self.baseUrlEdit.text().strip(),
            "api_key": self.apiKeyEdit.text(),
            "model": self.modelEdit.text().strip(),
            "work_context_tokens": self.contextSpin.value(),
            "max_concurrency": self.concurrencySpin.value(),
        }

    def temporaryProfile(self) -> LLMModelProfile:
        return LLMModelProfile(profile_id=self.profileId or "context-probe", **self.values())

    def _requestProbe(self) -> None:
        try:
            profile = self.temporaryProfile()
        except ValueError as exc:
            InfoBar.warning(
                self.tr("无法探查"),
                str(exc),
                duration=4000,
                parent=self,
            )
            return
        self.probeButton.setEnabled(False)
        self.probeRequested.emit(profile)


class ModelContextProbeThread(QThread):
    completed = pyqtSignal(bool, str)

    def __init__(self, profile: LLMModelProfile, parent=None):
        super().__init__(parent)
        self.profile = profile

    def run(self) -> None:
        success, message = check_model_profile_connection(self.profile)
        if not success:
            self.completed.emit(False, message)
            return
        self.completed.emit(
            True,
            self.tr("连接成功，但接口未提供可靠的上下文上限；当前填写值保持不变。"),
        )


class ProfileSelectionCard(SettingCard):
    createRequested = pyqtSignal(object)
    editRequested = pyqtSignal(object)
    deleteRequested = pyqtSignal(object)

    def __init__(self, config_item, title: str, content: str, parent=None):
        super().__init__(FIF.ROBOT, title, content, parent)
        self.configItem = config_item
        self.comboBox = ComboBox(self)
        self.comboBox.setMinimumWidth(170)
        self.createButton = PushButton(self.tr("新增"), self)
        self.editButton = PushButton(self.tr("编辑"), self)
        self.deleteButton = PushButton(self.tr("删除"), self)
        self.createButton.setObjectName("modelProfileCreateButton")
        self.editButton.setObjectName("modelProfileEditButton")
        self.deleteButton.setObjectName("modelProfileDeleteButton")
        for widget in (
            self.comboBox,
            self.createButton,
            self.editButton,
            self.deleteButton,
        ):
            self.hBoxLayout.addWidget(widget, 0, Qt.AlignRight)  # type: ignore
        self.hBoxLayout.addSpacing(16)
        self.comboBox.currentIndexChanged.connect(self._onSelectionChanged)
        self.createButton.clicked.connect(lambda: self.createRequested.emit(self))
        self.editButton.clicked.connect(lambda: self.editRequested.emit(self))
        self.deleteButton.clicked.connect(lambda: self.deleteRequested.emit(self))
        # Use a bound QObject method so Qt disconnects it with this card.  A
        # lambda capturing ``self`` keeps closed settings pages alive through
        # the process-wide config signal and can later call deleted widgets.
        config_item.valueChanged.connect(self._onConfigValueChanged)
        self._profiles: tuple[LLMModelProfile, ...] = ()

    def _onConfigValueChanged(self, _value) -> None:
        self.refresh(self._profiles)

    def refresh(self, profiles: tuple[LLMModelProfile, ...]) -> None:
        self._profiles = profiles
        selected_id = cfg.get(self.configItem)
        self.comboBox.blockSignals(True)
        try:
            self.comboBox.clear()
            self.comboBox.addItem(self.tr("未配置"), userData="")
            for profile in profiles:
                self.comboBox.addItem(profile.name, userData=profile.profile_id)
            selected_index = self.comboBox.findData(selected_id)
            if selected_id and selected_index < 0:
                self.comboBox.addItem(self.tr("缺失方案"), userData=selected_id)
                selected_index = self.comboBox.count() - 1
            self.comboBox.setCurrentIndex(max(selected_index, 0))
        finally:
            self.comboBox.blockSignals(False)
        configured = bool(selected_id and any(p.profile_id == selected_id for p in profiles))
        self.editButton.setEnabled(configured)
        self.deleteButton.setEnabled(configured)
        self.contentLabel.setText(
            self.tr("已配置") if configured else self.tr("未配置，相关 LLM 翻译模式不可用")
        )

    def selectedProfileId(self) -> str:
        return str(self.comboBox.currentData() or "")

    def _onSelectionChanged(self, _index: int) -> None:
        cfg.set(self.configItem, self.selectedProfileId())


class _NonLLMServiceCard(SettingCard):
    def __init__(self, parent=None):
        super().__init__(
            FIF.LANGUAGE,
            "翻译服务",
            "选择不使用 LLM 的翻译服务",
            parent,
        )
        self.comboBox = ComboBox(self)
        self.services = (
            TranslatorServiceEnum.BING,
            TranslatorServiceEnum.GOOGLE,
            TranslatorServiceEnum.DEEPLX,
        )
        self.comboBox.addItem(self.tr("未配置"), userData=None)
        for service in self.services:
            self.comboBox.addItem(service.value, userData=service)
        current = cfg.get(cfg.translator_service)
        index = self.comboBox.findData(current)
        self.comboBox.setCurrentIndex(max(index, 0))
        self.comboBox.currentIndexChanged.connect(self._changed)
        self.hBoxLayout.addWidget(self.comboBox, 0, Qt.AlignRight)  # type: ignore
        self.hBoxLayout.addSpacing(16)
        cfg.translator_service.valueChanged.connect(self._sync)
        self._updateContent()

    def _changed(self, _index: int) -> None:
        service = self.comboBox.currentData()
        if service is not None:
            cfg.set(cfg.translator_service, service)
        self._updateContent()

    def _sync(self, value) -> None:
        index = self.comboBox.findData(value)
        if index >= 0:
            self.comboBox.setCurrentIndex(index)
        else:
            self.comboBox.setCurrentIndex(0)
        self._updateContent()

    def _updateContent(self) -> None:
        self.contentLabel.setText(
            self.tr("已配置")
            if self.comboBox.currentData() is not None
            else self.tr("未配置，非 LLM 翻译模式不可用")
        )


class TranslationSettingWidget(QWidget):
    """Translation settings with navigation-only workflow tabs."""

    profilesChanged = pyqtSignal()

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        profile_store: Optional[LLMModelProfileStore] = None,
    ):
        super().__init__(parent)
        self.profileStore = profile_store or LLMModelProfileStore()
        self.titleLabel = BodyLabel(self.tr("翻译设置"), self)
        self.subtitleLabel = SubtitleLabel(
            self.tr("三种翻译方式独立配置；切换页签不会改变任务使用的翻译方式。"), self
        )
        self.pivot = SegmentedWidget(self)
        self.pivot.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.stackedWidget = QStackedWidget(self)
        self.pages: dict[str, QWidget] = {}
        self.profileCards: list[ProfileSelectionCard] = []
        self._probeThreads: set[ModelContextProbeThread] = set()
        self._buildPages()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.titleLabel)
        layout.addWidget(self.subtitleLabel)
        layout.addWidget(self.pivot, 0, Qt.AlignLeft)  # type: ignore
        layout.addWidget(self.stackedWidget)
        self.stackedWidget.currentChanged.connect(self._onPageChanged)
        self.stackedWidget.setCurrentWidget(self.pages["non-llm"])
        self.pivot.setCurrentItem("non-llm")
        self.profilesChanged.connect(self.refreshProfiles)
        self.refreshProfiles()

    def _addPage(self, route_key: str, title: str) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget(self)
        page.setObjectName(f"translation-{route_key}")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(16)
        self.pages[route_key] = page
        self.stackedWidget.addWidget(page)
        self.pivot.addItem(
            routeKey=route_key,
            text=title,
            onClick=lambda _checked=False, widget=page: self.stackedWidget.setCurrentWidget(
                widget
            ),
        )
        return page, layout

    def _buildPages(self) -> None:
        non_llm, non_llm_layout = self._addPage("non-llm", self.tr("非 LLM 翻译"))
        group = SettingCardGroup(self.tr("非 LLM 翻译"), non_llm)
        self.nonLLMServiceCard = _NonLLMServiceCard(group)
        self.deeplxEndpointCard = LineEditSettingCard(
            cfg.deeplx_endpoint,
            FIF.LINK,
            self.tr("DeepLX 后端"),
            self.tr("仅在选择 DeepLX 时使用"),
            "https://api.deeplx.org/translate",
            group,
        )
        group.addSettingCard(self.nonLLMServiceCard)
        group.addSettingCard(self.deeplxEndpointCard)
        non_llm_layout.addWidget(group)

        single, single_layout = self._addPage("single-llm", self.tr("LLM 翻译"))
        group = SettingCardGroup(self.tr("单模型 LLM 翻译"), single)
        self.singleMainProfileCard = self._profileCard(
            cfg.main_llm_profile_id,
            self.tr("主翻译模型"),
            group,
        )
        self.singlePromptCard = PromptSettingCard(
            cfg.main_translation_prompt,
            self.tr("主翻译 Prompt"),
            self.tr("单模型与增强型翻译共用同一主角色 Prompt"),
            group,
        )
        self.reflectCard = SwitchSettingCard(
            FIF.EDIT,
            self.tr("反思翻译"),
            self.tr("仅用于单模型 LLM 翻译"),
            cfg.need_reflect_translate,
            group,
        )
        self.singleBatchCard = RangeSettingCard(
            cfg.batch_size,
            FIF.ALIGNMENT,
            self.tr("批处理大小"),
            self.tr("单模型 LLM 每批处理的字幕数量"),
            group,
        )
        for card in (
            self.singleMainProfileCard,
            self.singlePromptCard,
            self.reflectCard,
            self.singleBatchCard,
        ):
            group.addSettingCard(card)
        single_layout.addWidget(group)

        enhanced, enhanced_layout = self._addPage(
            "enhanced-llm", self.tr("增强型 LLM 翻译")
        )
        group = SettingCardGroup(self.tr("增强型 LLM 翻译"), enhanced)
        self.enhancedMainProfileCard = self._profileCard(
            cfg.main_llm_profile_id,
            self.tr("主翻译模型"),
            group,
        )
        self.reviewProfileCard = self._profileCard(
            cfg.review_llm_profile_id,
            self.tr("高级校对模型"),
            group,
        )
        self.enhancedMainPromptCard = PromptSettingCard(
            cfg.main_translation_prompt,
            self.tr("主翻译 Prompt"),
            self.tr("在系统硬约束之后注入"),
            group,
        )
        self.reviewPromptCard = PromptSettingCard(
            cfg.review_translation_prompt,
            self.tr("高级校对 Prompt"),
            self.tr("独立于主翻译角色配置"),
            group,
        )
        self.enhancedBatchCard = RangeSettingCard(
            cfg.enhanced_batch_size,
            FIF.ALIGNMENT,
            self.tr("正式翻译批处理上限"),
            self.tr("仅限制翻译主体数量，token 规划器可以自动缩小"),
            group,
        )
        self.termContextCard = RangeSettingCard(
            cfg.term_context_radius,
            FIF.DOCUMENT,
            self.tr("术语上下文范围"),
            self.tr("提取疑难术语时默认读取前后字幕段数量"),
            group,
        )
        self.termConfirmationCard = ComboBoxSettingCard(
            cfg.term_confirmation_mode,
            FIF.ACCEPT,
            self.tr("术语确认"),
            self.tr("GUI 可人工确认；批量与 CLI 始终自动确认"),
            texts=[self.tr("自动确认"), self.tr("人工确认")],
            parent=group,
        )
        self.auditModeCard = ComboBoxSettingCard(
            cfg.translation_audit_mode,
            FIF.VIEW,
            self.tr("审计处理"),
            self.tr("仅报告保持只读；自动修复只处理客观高置信问题"),
            texts=[self.tr("审计仅报告"), self.tr("自动修复客观问题")],
            parent=group,
        )
        for card in (
            self.enhancedMainProfileCard,
            self.reviewProfileCard,
            self.enhancedMainPromptCard,
            self.reviewPromptCard,
            self.enhancedBatchCard,
            self.termContextCard,
            self.termConfirmationCard,
            self.auditModeCard,
        ):
            group.addSettingCard(card)
        enhanced_layout.addWidget(group)

    def _profileCard(self, config_item, title: str, parent) -> ProfileSelectionCard:
        card = ProfileSelectionCard(
            config_item,
            title,
            self.tr("选择器只显示用户定义的方案名称"),
            parent,
        )
        card.createRequested.connect(self._createProfile)
        card.editRequested.connect(self._editProfile)
        card.deleteRequested.connect(self._deleteProfile)
        self.profileCards.append(card)
        return card

    def _onPageChanged(self, index: int) -> None:
        widget = self.stackedWidget.widget(index)
        if widget:
            self.pivot.setCurrentItem(widget.objectName().removeprefix("translation-"))

    def refreshProfiles(self) -> None:
        profiles = self.profileStore.list()
        for card in self.profileCards:
            card.refresh(profiles)

    def _showError(self, action: str, error: Exception) -> None:
        InfoBar.error(
            self.tr("模型方案操作失败"),
            f"{action}: {error}",
            position=InfoBarPosition.TOP,
            duration=6000,
            parent=self.window(),
        )

    def _createProfile(self, card: ProfileSelectionCard) -> None:
        dialog = _ProfileDialog(parent=self.window())
        self._connectProbe(dialog)
        if not dialog.exec():
            return
        try:
            profile = self.profileStore.create(**dialog.values())
        except Exception as exc:
            self._showError(self.tr("无法新增方案"), exc)
            return
        cfg.set(card.configItem, profile.profile_id)
        self.profilesChanged.emit()

    def _editProfile(self, card: ProfileSelectionCard) -> None:
        try:
            profile = self.profileStore.get(card.selectedProfileId())
        except Exception as exc:
            self._showError(self.tr("无法读取方案"), exc)
            return
        dialog = _ProfileDialog(profile, self.window())
        self._connectProbe(dialog)
        if not dialog.exec():
            return
        try:
            self.profileStore.save(
                LLMModelProfile(profile_id=profile.profile_id, **dialog.values())
            )
        except Exception as exc:
            self._showError(self.tr("无法保存方案"), exc)
            return
        self.profilesChanged.emit()

    def _deleteProfile(self, card: ProfileSelectionCard) -> None:
        profile_id = card.selectedProfileId()
        try:
            profile = self.profileStore.get(profile_id)
        except Exception as exc:
            self._showError(self.tr("无法读取方案"), exc)
            return
        confirm = MessageBox(
            self.tr("删除模型方案"),
            self.tr("确定删除“{name}”吗？").format(name=profile.name),
            self.window(),
        )
        if not confirm.exec():
            return
        try:
            self.profileStore.delete(profile_id)
        except Exception as exc:
            self._showError(self.tr("无法删除方案"), exc)
            return
        for item in (cfg.main_llm_profile_id, cfg.review_llm_profile_id):
            if cfg.get(item) == profile_id:
                cfg.set(item, "")
        self.profilesChanged.emit()

    def _connectProbe(self, dialog: _ProfileDialog) -> None:
        dialog.probeRequested.connect(
            lambda profile, dialog=dialog: self._startProbe(dialog, profile)
        )

    def _startProbe(self, dialog: _ProfileDialog, profile: LLMModelProfile) -> None:
        thread = ModelContextProbeThread(profile, self)
        self._probeThreads.add(thread)

        def completed(success: bool, message: str) -> None:
            dialog.probeButton.setEnabled(True)
            if success:
                InfoBar.success(
                    self.tr("探查完成"), message, duration=5000, parent=dialog
                )
            else:
                InfoBar.warning(
                    self.tr("无法探查上下文能力"),
                    message,
                    duration=5000,
                    parent=dialog,
                )
            self._probeThreads.discard(thread)
            thread.deleteLater()

        thread.completed.connect(completed)
        thread.start()


__all__ = [
    "ModelContextProbeThread",
    "ProfileSelectionCard",
    "PromptSettingCard",
    "TranslationSettingWidget",
]
