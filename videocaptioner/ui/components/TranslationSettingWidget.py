"""Three parallel translation settings pages and named LLM profile management."""

from __future__ import annotations

import json
from typing import Optional

from PyQt5.QtCore import QPoint, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLayout,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
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
    ScrollArea,
    SegmentedWidget,
    SettingCard,
    SettingCardGroup,
    SpinBox,
    StrongBodyLabel,
    SwitchSettingCard,
    TextEdit,
)
from qfluentwidgets import FluentIcon as FIF

from videocaptioner.core.entities import TranslatorServiceEnum
from videocaptioner.core.llm.check_llm import probe_model_profile_capabilities
from videocaptioner.core.llm.models import (
    LLMModelProfile,
    LLMTransport,
    OpenAIEndpoint,
    ProviderDialect,
    thaw_json_object,
)
from videocaptioner.core.llm.profiles import LLMModelProfileStore
from videocaptioner.core.llm.request_options import (
    known_thinking_budget,
    validate_profile_request_options,
)
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.components.LineEditSettingCard import LineEditSettingCard

_REQUEST_OPTION_TEMPLATES: dict[str, tuple[str, str, dict]] = {
    "blank": ("空白", "适用于所有接口；清空高级参数。", {}),
    "gpt-chat": (
        "GPT · Chat",
        "OpenAI-compatible Chat Completions 推理强度示例。",
        {"reasoning_effort": "high", "$omit": ["temperature"]},
    ),
    "gpt-responses": (
        "GPT · Responses",
        "标准 OpenAI Responses 推理强度示例。",
        {"reasoning": {"effort": "high"}, "$omit": ["temperature"]},
    ),
    "claude-manual": (
        "Claude · 手动思考",
        "Anthropic Messages 手动 thinking budget 示例。",
        {
            "thinking": {"type": "enabled", "budget_tokens": 4096},
            "$omit": ["temperature"],
        },
    ),
    "claude-adaptive": (
        "Claude · 自适应思考",
        "支持自适应 thinking 的 Anthropic 模型示例。",
        {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "high"},
            "$omit": ["temperature"],
        },
    ),
    "gemini": (
        "Gemini",
        "Gemini native generationConfig.thinkingConfig 示例。",
        {"generationConfig": {"thinkingConfig": {"thinkingBudget": 4096}}},
    ),
    "qwen": (
        "Qwen",
        "OpenAI-compatible 服务的 provider-native extra_body 示例。",
        {"extra_body": {"enable_thinking": True}, "$omit": ["temperature"]},
    ),
    "glm": (
        "GLM",
        "OpenAI-compatible GLM provider-native thinking 示例。",
        {
            "extra_body": {"thinking": {"type": "enabled"}},
            "$omit": ["temperature"],
        },
    ),
    "deepseek": (
        "DeepSeek",
        "OpenAI-compatible DeepSeek provider-native thinking budget 示例。",
        {
            "extra_body": {
                "thinking": {"type": "enabled", "budget_tokens": 4096}
            },
            "$omit": ["temperature"],
        },
    ),
    "kimi": (
        "Kimi",
        "Kimi provider-native thinking 示例；部分模型不允许关闭思考。",
        {"extra_body": {"thinking": {"type": "enabled"}}},
    ),
    "doubao": (
        "Doubao",
        "Doubao reasoning_effort 与 provider-native thinking 示例。",
        {
            "reasoning_effort": "high",
            "extra_body": {"thinking": {"type": "enabled"}},
        },
    ),
}

_INTERFACE_FORMATS: dict[str, tuple[str, LLMTransport, OpenAIEndpoint]] = {
    "openai-chat": (
        "OpenAI · Chat Completions",
        LLMTransport.OPENAI_COMPATIBLE,
        OpenAIEndpoint.CHAT_COMPLETIONS,
    ),
    "openai-responses": (
        "OpenAI · Responses",
        LLMTransport.OPENAI_COMPATIBLE,
        OpenAIEndpoint.RESPONSES,
    ),
    "anthropic-messages": (
        "Anthropic · Messages",
        LLMTransport.ANTHROPIC_MESSAGES,
        OpenAIEndpoint.CHAT_COMPLETIONS,
    ),
    "gemini": (
        "Google · Gemini",
        LLMTransport.GEMINI,
        OpenAIEndpoint.CHAT_COMPLETIONS,
    ),
}


class _PromptDialog(MessageBoxBase):
    def __init__(self, title: str, text: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.titleLabel = StrongBodyLabel(title, self)
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
        summary = (
            self.tr("使用自定义 Prompt")
            if value.strip()
            else self.tr("使用系统默认 Prompt")
        )
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
        self.titleLabel = StrongBodyLabel(
            self.tr("编辑模型方案") if profile else self.tr("新增模型方案"), self
        )
        self.nameEdit = LineEdit(self)
        self.interfaceCombo = ComboBox(self)
        self.dialectCombo = ComboBox(self)
        self.baseUrlEdit = LineEdit(self)
        self.apiKeyEdit = PasswordLineEdit(self)
        self.modelEdit = LineEdit(self)
        self.contextSpin = SpinBox(self)
        self.concurrencySpin = SpinBox(self)
        self.outputModeCombo = ComboBox(self)
        self.outputTokensSpin = SpinBox(self)
        self.advancedButton = PushButton(self.tr("显示高级请求参数"), self)
        self.templateCombo = ComboBox(self)
        self.applyTemplateButton = PushButton(self.tr("应用模板"), self)
        self.templateHint = CaptionLabel(self)
        self.requestOptionsEdit = TextEdit(self)
        self.probeButton = PushButton(self.tr("测试文本与结构化能力"), self)
        self.probeResultLabel = CaptionLabel(self.tr("尚未执行能力测试"), self)
        self.probeButton.setObjectName("modelCapabilityProbeButton")

        for key, (label, _transport, _endpoint) in _INTERFACE_FORMATS.items():
            self.interfaceCombo.addItem(self.tr(label), userData=key)
        self.interfaceCombo.setToolTip(
            self.tr("OpenAI 接口选项同样适用于实现对应协议的兼容服务")
        )
        self.dialectCombo.addItems([item.value for item in ProviderDialect])
        self.outputModeCombo.addItem(self.tr("自动"), userData="auto")
        self.outputModeCombo.addItem(self.tr("自定义"), userData="custom")
        self.contextSpin.setRange(16_384, 2_000_000)
        self.contextSpin.setSingleStep(1024)
        self.concurrencySpin.setRange(1, 50)
        self.outputTokensSpin.setRange(1, 1_999_999)
        self.outputTokensSpin.setSingleStep(512)
        self.outputTokensSpin.setValue(4096)
        self.apiKeyEdit.setClearButtonEnabled(True)
        self.baseUrlEdit.setPlaceholderText("https://api.example.com/v1")
        self.modelEdit.setPlaceholderText(self.tr("模型名称"))
        self.requestOptionsEdit.setPlaceholderText(
            self.tr('例如 {"reasoning": {"effort": "high"}}')
        )
        self.requestOptionsEdit.setMinimumHeight(170)
        self.requestOptionsEdit.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.MinimumExpanding
        )
        self.probeResultLabel.setWordWrap(True)
        self.templateHint.setWordWrap(True)
        for key, (label, _description, _value) in _REQUEST_OPTION_TEMPLATES.items():
            self.templateCombo.addItem(label, userData=key)

        if profile:
            self.nameEdit.setText(profile.name)
            interface_key = self._interface_key(
                profile.transport, profile.openai_endpoint
            )
            self.interfaceCombo.setCurrentIndex(
                max(self.interfaceCombo.findData(interface_key), 0)
            )
            self.dialectCombo.setCurrentText(profile.dialect.value)
            self.baseUrlEdit.setText(profile.base_url)
            self.apiKeyEdit.setText(profile.api_key)
            self.modelEdit.setText(profile.model)
            self.contextSpin.setValue(profile.work_context_tokens)
            self.concurrencySpin.setValue(profile.max_concurrency)
            if profile.max_output_tokens is None:
                self.outputModeCombo.setCurrentIndex(0)
            else:
                self.outputModeCombo.setCurrentIndex(1)
                self.outputTokensSpin.setValue(profile.max_output_tokens)
            self.requestOptionsEdit.setPlainText(
                json.dumps(
                    thaw_json_object(profile.request_options),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            self.interfaceCombo.setCurrentIndex(
                self.interfaceCombo.findData("openai-chat")
            )
            self.dialectCombo.setCurrentText(ProviderDialect.GENERIC.value)
            self.contextSpin.setValue(65_536)
            self.concurrencySpin.setValue(4)
            self.requestOptionsEdit.setPlainText("{}")

        outputRow = QWidget(self)
        outputLayout = QHBoxLayout(outputRow)
        outputLayout.setContentsMargins(0, 0, 0, 0)
        outputLayout.addWidget(self.outputModeCombo)
        outputLayout.addWidget(self.outputTokensSpin, 1)

        self.advancedWidget = QWidget(self)
        advancedLayout = QVBoxLayout(self.advancedWidget)
        advancedLayout.setContentsMargins(0, 0, 0, 0)
        advancedLayout.setSpacing(8)
        templateRow = QHBoxLayout()
        templateRow.setContentsMargins(0, 0, 0, 0)
        templateRow.addWidget(self.templateCombo, 1)
        templateRow.addWidget(self.applyTemplateButton)
        advancedLayout.addLayout(templateRow)
        advancedLayout.addWidget(self.templateHint)
        advancedLayout.addWidget(self.requestOptionsEdit)
        self.requestOptionsHint = CaptionLabel(
            self.tr(
                "这是最终请求体的附加 JSON，不是完整请求体。应用会保护输入、"
                "结构化输出、工具和 token 字段；未知非保护字段将原样传给服务端。"
            ),
            self.advancedWidget,
        )
        self.requestOptionsHint.setWordWrap(True)
        advancedLayout.addWidget(self.requestOptionsHint)
        self.advancedWidget.hide()

        probeWidget = QWidget(self)
        probeLayout = QVBoxLayout(probeWidget)
        probeLayout.setContentsMargins(0, 0, 0, 0)
        probeLayout.setSpacing(6)
        probeLayout.addWidget(self.probeButton)
        probeLayout.addWidget(self.probeResultLabel)

        formWidget = QWidget(self)
        form = QFormLayout(formWidget)
        form.setContentsMargins(0, 0, 0, 0)
        form.setVerticalSpacing(12)
        form.setSizeConstraint(QLayout.SetMinimumSize)  # type: ignore[attr-defined]
        form.addRow(BodyLabel(self.tr("方案名称"), formWidget), self.nameEdit)
        form.addRow(BodyLabel(self.tr("接口格式"), formWidget), self.interfaceCombo)
        form.addRow(BodyLabel(self.tr("供应商方言"), formWidget), self.dialectCombo)
        form.addRow(BodyLabel(self.tr("Base URL"), formWidget), self.baseUrlEdit)
        form.addRow(BodyLabel(self.tr("API Key"), formWidget), self.apiKeyEdit)
        form.addRow(BodyLabel(self.tr("模型"), formWidget), self.modelEdit)
        form.addRow(BodyLabel(self.tr("工作上下文"), formWidget), self.contextSpin)
        form.addRow(BodyLabel(self.tr("最大输出 token"), formWidget), outputRow)
        form.addRow(BodyLabel(self.tr("最大并发"), formWidget), self.concurrencySpin)
        form.addRow(self.advancedButton)
        form.addRow(self.advancedWidget)
        form.addRow(BodyLabel(self.tr("能力测试"), formWidget), probeWidget)
        self.formWidget = formWidget
        self.formLayout = form
        self.formScrollArea = ScrollArea(self)
        self.formScrollArea.setWidget(formWidget)
        self.formScrollArea.setWidgetResizable(True)
        self.formScrollArea.setHorizontalScrollBarPolicy(
            Qt.ScrollBarAlwaysOff  # type: ignore[attr-defined]
        )
        self.formScrollArea.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.formScrollArea.enableTransparentBackground()
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.formScrollArea)
        self.widget.setMinimumWidth(720)
        self._updateDialogViewportHeight()
        self.yesButton.setText(self.tr("保存"))
        self.cancelButton.setText(self.tr("取消"))
        self.probeButton.clicked.connect(self._requestProbe)
        self.outputModeCombo.currentIndexChanged.connect(self._onOutputModeChanged)
        self.contextSpin.valueChanged.connect(self._onContextChanged)
        self.advancedButton.clicked.connect(self._toggleAdvanced)
        self.templateCombo.currentIndexChanged.connect(self._updateTemplateHint)
        self.applyTemplateButton.clicked.connect(self._applyTemplate)
        for edit in (self.nameEdit, self.baseUrlEdit, self.modelEdit):
            edit.textChanged.connect(self._updateSaveState)
        self._onOutputModeChanged()
        self._onContextChanged(self.contextSpin.value())
        self._updateTemplateHint()
        self._updateSaveState()

    @staticmethod
    def _interface_key(
        transport: LLMTransport, endpoint: OpenAIEndpoint
    ) -> str:
        for key, (_label, candidate_transport, candidate_endpoint) in (
            _INTERFACE_FORMATS.items()
        ):
            if transport is candidate_transport and endpoint is candidate_endpoint:
                return key
        raise ValueError(
            f"unsupported interface format: {transport.value}/{endpoint.value}"
        )

    def _onOutputModeChanged(self, _index: int = -1) -> None:
        self.outputTokensSpin.setEnabled(self.outputModeCombo.currentData() == "custom")

    def _onContextChanged(self, value: int) -> None:
        self.outputTokensSpin.setMaximum(max(1, value - 1))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "formScrollArea"):
            self._updateDialogViewportHeight()

    def _updateDialogViewportHeight(self) -> None:
        self.widget.setMaximumHeight(max(1, self.height() - 32))
        view_margins = self.viewLayout.contentsMargins()
        available_form_height = (
            self.height()
            - 32
            - self.buttonGroup.height()
            - view_margins.top()
            - view_margins.bottom()
            - self.titleLabel.sizeHint().height()
            - self.viewLayout.spacing()
        )
        self.formScrollArea.setMinimumHeight(
            max(1, min(600, available_form_height))
        )
        self.widget.updateGeometry()

    def _toggleAdvanced(self) -> None:
        visible = self.advancedWidget.isHidden()
        self.advancedWidget.setVisible(visible)
        self.advancedButton.setText(
            self.tr("收起高级请求参数")
            if visible
            else self.tr("显示高级请求参数")
        )
        self.formLayout.activate()
        self.formWidget.updateGeometry()
        self.formScrollArea.updateGeometry()
        if visible:
            QTimer.singleShot(0, self._revealAdvancedEditor)

    def _revealAdvancedEditor(self) -> None:
        if self.advancedWidget.isHidden():
            return
        editor_bottom = self.requestOptionsEdit.mapTo(
            self.formWidget, QPoint(0, self.requestOptionsEdit.height())
        )
        self.formScrollArea.ensureVisible(editor_bottom.x(), editor_bottom.y(), 0, 12)
        self.requestOptionsEdit.setFocus()

    def _updateTemplateHint(self, _index: int = -1) -> None:
        key = str(self.templateCombo.currentData() or "blank")
        self.templateHint.setText(_REQUEST_OPTION_TEMPLATES[key][1])

    def _confirmTemplate(self, label: str) -> bool:
        confirm = MessageBox(
            self.tr("应用高级参数模板"),
            self.tr(
                "模板“{label}”会替换当前高级 JSON（包括 $omit），但不会修改连接、"
                "模型或接口格式。继续吗？"
            ).format(label=label),
            self,
        )
        return bool(confirm.exec())

    def _applyTemplate(self) -> None:
        key = str(self.templateCombo.currentData() or "blank")
        label, _description, value = _REQUEST_OPTION_TEMPLATES[key]
        if not self._confirmTemplate(label):
            return
        self.requestOptionsEdit.setPlainText(
            json.dumps(value, ensure_ascii=False, indent=2)
        )

    def requestOptions(self) -> dict:
        raw = self.requestOptionsEdit.toPlainText().strip() or "{}"
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                self.tr("高级请求参数 JSON 第 {line} 行第 {column} 列无效：{message}").format(
                    line=exc.lineno,
                    column=exc.colno,
                    message=exc.msg,
                )
            ) from exc
        if not isinstance(value, dict):
            raise ValueError(self.tr("高级请求参数 JSON 顶层必须是对象"))
        return value

    def _updateSaveState(self) -> None:
        self.yesButton.setEnabled(
            bool(
                self.nameEdit.text().strip()
                and self.baseUrlEdit.text().strip()
                and self.modelEdit.text().strip()
            )
        )

    def values(self) -> dict:
        interface_key = str(self.interfaceCombo.currentData() or "openai-chat")
        _label, transport, endpoint = _INTERFACE_FORMATS[interface_key]
        return {
            "name": self.nameEdit.text().strip(),
            "transport": transport,
            "dialect": ProviderDialect(self.dialectCombo.currentText()),
            "base_url": self.baseUrlEdit.text().strip(),
            "api_key": self.apiKeyEdit.text(),
            "model": self.modelEdit.text().strip(),
            "work_context_tokens": self.contextSpin.value(),
            "max_concurrency": self.concurrencySpin.value(),
            "openai_endpoint": endpoint,
            "request_options": self.requestOptions(),
            "max_output_tokens": (
                self.outputTokensSpin.value()
                if self.outputModeCombo.currentData() == "custom"
                else None
            ),
        }

    def temporaryProfile(self) -> LLMModelProfile:
        profile = LLMModelProfile(
            profile_id=self.profileId or "context-probe", **self.values()
        )
        validate_profile_request_options(profile)
        return profile

    @staticmethod
    def profileWarnings(profile: LLMModelProfile) -> tuple[str, ...]:
        warnings: list[str] = []
        cap = profile.max_output_tokens
        if cap is not None and cap < 1024:
            warnings.append("最大输出 token 小于 1024，可能没有足够空间返回译文")
        if cap is not None and cap > profile.work_context_tokens // 2:
            warnings.append("最大输出 token 超过工作上下文的一半")
        budget = known_thinking_budget(thaw_json_object(profile.request_options))
        if cap is not None and budget is not None and budget >= cap:
            warnings.append(
                f"识别到的 thinking budget（{budget}）不小于最大输出 token（{cap}）"
            )
        return tuple(warnings)

    def _confirmStore(self) -> bool:
        confirm = MessageBox(
            self.tr("允许服务端存储"),
            self.tr(
                "高级请求参数包含 store: true。该服务可能保存字幕提示词和回答；"
                "每次保存此方案都需要重新确认。仍要保存吗？"
            ),
            self,
        )
        return bool(confirm.exec())

    def validate(self) -> bool:
        try:
            profile = self.temporaryProfile()
        except ValueError as exc:
            InfoBar.warning(
                self.tr("无法保存模型方案"),
                str(exc),
                duration=6000,
                parent=self,
            )
            return False
        warnings = self.profileWarnings(profile)
        if warnings:
            InfoBar.warning(
                self.tr("模型方案提示"),
                "；".join(warnings),
                duration=7000,
                parent=self.window(),
            )
        if profile.request_options.get("store") is True:
            return self._confirmStore()
        return True

    def _confirmProbeCost(self) -> bool:
        confirm = MessageBox(
            self.tr("执行真实能力测试"),
            self.tr(
                "将使用当前未保存的全部配置分别发送一次文本请求和一次结构化输出"
                "请求，可能产生费用。继续吗？"
            ),
            self,
        )
        return bool(confirm.exec())

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
        if not self._confirmProbeCost():
            return
        self.probeButton.setEnabled(False)
        self.probeResultLabel.setText(self.tr("测试中…"))
        self.probeRequested.emit(profile)

    def showProbeResult(self, result) -> None:
        text_status = self.tr("通过") if result.text.success else self.tr("失败")
        structured_status = (
            self.tr("通过") if result.structured.success else self.tr("失败")
        )
        self.probeResultLabel.setText(
            self.tr(
                "文本能力：{text_status}（{text_message}）\n"
                "结构化输出：{structured_status}（{structured_message}）\n"
                "探测最大输出 token：{cap}"
            ).format(
                text_status=text_status,
                text_message=result.text.message,
                structured_status=structured_status,
                structured_message=result.structured.message,
                cap=result.max_output_tokens,
            )
        )


class ModelContextProbeThread(QThread):
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, profile: LLMModelProfile, parent=None):
        super().__init__(parent)
        self.profile = profile

    def run(self) -> None:
        try:
            self.completed.emit(probe_model_profile_capabilities(self.profile))
        except Exception as exc:  # pragma: no cover - defensive thread boundary
            self.failed.emit(str(exc))


class ProfileSelectionCard(SettingCard):
    createRequested = pyqtSignal(object)
    editRequested = pyqtSignal(object)
    deleteRequested = pyqtSignal(object)

    def __init__(self, config_item, title: str, content: str, parent=None):
        super().__init__(FIF.ROBOT, title, content, parent)
        self.configItem = config_item
        self.configuredContent = content
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
            self.configuredContent
            if configured
            else self.tr("未配置，相关 LLM 翻译模式不可用")
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
            "使用传统翻译服务快速处理字幕",
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
            self.tr("使用传统翻译服务快速处理字幕")
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
        self.titleLabel = StrongBodyLabel(self.tr("翻译设置"), self)
        self.subtitleLabel = CaptionLabel(
            self.tr("三种翻译方式独立配置；切换页签不会改变任务使用的翻译方式。"), self
        )
        self.subtitleLabel.setWordWrap(True)
        self.pivot = SegmentedWidget(self)
        self.pivot.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.stackedWidget = QStackedWidget(self)
        self.pages: dict[str, QWidget] = {}
        self.profileCards: list[ProfileSelectionCard] = []
        self._probeThreads: set[ModelContextProbeThread] = set()
        self._buildPages()

        self.rootLayout = QVBoxLayout(self)
        self.rootLayout.setContentsMargins(0, 0, 0, 0)
        self.rootLayout.setSpacing(8)
        self.rootLayout.addWidget(self.titleLabel)
        self.rootLayout.addWidget(self.subtitleLabel)
        self.rootLayout.addWidget(self.pivot, 0, Qt.AlignLeft)  # type: ignore
        self.rootLayout.addWidget(self.stackedWidget)
        self.stackedWidget.currentChanged.connect(self._onPageChanged)
        self.stackedWidget.setCurrentWidget(self.pages["non-llm"])
        self.pivot.setCurrentItem("non-llm")
        self._syncContentHeight()
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
        group = SettingCardGroup(self.tr("翻译服务"), non_llm)
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
        group = SettingCardGroup(self.tr("模型与翻译"), single)
        self.singleMainProfileCard = self._profileCard(
            cfg.main_llm_profile_id,
            self.tr("主翻译模型"),
            self.tr("负责正式翻译"),
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
        group = SettingCardGroup(self.tr("模型、术语与审计"), enhanced)
        self.enhancedMainProfileCard = self._profileCard(
            cfg.main_llm_profile_id,
            self.tr("主翻译模型"),
            self.tr("负责全文分析、术语初译和正式翻译"),
            group,
        )
        self.reviewProfileCard = self._profileCard(
            cfg.review_llm_profile_id,
            self.tr("高级校对模型"),
            self.tr("负责术语裁决和翻译质量审计"),
            group,
        )
        self.enhancedMainPromptCard = PromptSettingCard(
            cfg.main_translation_prompt,
            self.tr("主翻译 Prompt"),
            self.tr("约束主翻译的语气、用词和格式"),
            group,
        )
        self.reviewPromptCard = PromptSettingCard(
            cfg.review_translation_prompt,
            self.tr("高级校对 Prompt"),
            self.tr("约束术语裁决和质量审计标准"),
            group,
        )
        self.enhancedBatchCard = RangeSettingCard(
            cfg.enhanced_batch_size,
            FIF.ALIGNMENT,
            self.tr("正式翻译批处理上限"),
            self.tr("每批最多翻译的字幕数量；超出上下文时自动减少"),
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
            self.tr("独立任务可暂停确认；批量任务自动采用校对结果"),
            texts=[self.tr("自动确认"), self.tr("人工确认")],
            parent=group,
        )
        self.auditModeCard = ComboBoxSettingCard(
            cfg.translation_audit_mode,
            FIF.VIEW,
            self.tr("审计处理"),
            self.tr("独立任务可人工选择建议；自动模式会写回通过硬校验的校对修正"),
            texts=[self.tr("审计并人工确认"), self.tr("自动采纳校对修正")],
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

    def _profileCard(
        self, config_item, title: str, content: str, parent
    ) -> ProfileSelectionCard:
        card = ProfileSelectionCard(
            config_item,
            title,
            content,
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
            self._syncContentHeight()

    def _syncContentHeight(self) -> None:
        """Expose the current page's natural height to the parent ExpandLayout.

        qfluentwidgets' ``ExpandLayout`` preserves each child widget's existing
        height instead of consulting its size hint.  Without an explicit resize,
        this compound settings widget is embedded at Qt's initial 30 px height
        and all cards overlap.  Keeping the stack and wrapper at the active
        page's natural height also avoids reserving the much taller enhanced
        page while a compact page is selected.
        """
        page = self.stackedWidget.currentWidget()
        if page is None:
            return
        if page.layout() is not None:
            page.layout().activate()
        self.stackedWidget.setFixedHeight(page.sizeHint().height())
        self.rootLayout.activate()
        self.setFixedHeight(self.rootLayout.sizeHint().height())

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

        def cleanup() -> None:
            self._probeThreads.discard(thread)
            thread.deleteLater()

        def completed(result) -> None:
            dialog.probeButton.setEnabled(True)
            dialog.showProbeResult(result)
            if result.text.success and result.structured.success:
                InfoBar.success(
                    self.tr("能力测试完成"),
                    self.tr("文本与结构化输出能力均通过"),
                    duration=5000,
                    parent=dialog,
                )
            else:
                InfoBar.warning(
                    self.tr("部分能力测试未通过"),
                    self.tr("请查看编辑窗口中的两项详细结果"),
                    duration=5000,
                    parent=dialog,
                )
            cleanup()

        def failed(message: str) -> None:
            dialog.probeButton.setEnabled(True)
            dialog.probeResultLabel.setText(self.tr("能力测试失败：{0}").format(message))
            InfoBar.warning(
                self.tr("能力测试失败"),
                message,
                duration=5000,
                parent=dialog,
            )
            cleanup()

        thread.completed.connect(completed)
        thread.failed.connect(failed)
        thread.start()


__all__ = [
    "ModelContextProbeThread",
    "ProfileSelectionCard",
    "PromptSettingCard",
    "TranslationSettingWidget",
]
