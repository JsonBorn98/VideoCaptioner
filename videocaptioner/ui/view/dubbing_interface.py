from pathlib import Path

from PyQt5.QtCore import Qt, QUrl
from PyQt5.QtMultimedia import QMediaContent, QMediaPlayer
from PyQt5.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    ComboBox,
    ExpandLayout,
    IconWidget,
    InfoBadge,
    InfoBar,
    InfoLevel,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    SegmentedWidget,
    SubtitleLabel,
    TitleLabel,
    TransparentToolButton,
    setFont,
)
from qfluentwidgets import FluentIcon as FIF

from videocaptioner.core.constant import INFOBAR_DURATION_ERROR, INFOBAR_DURATION_SUCCESS
from videocaptioner.core.dubbing import get_dubbing_preset
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.common.dubbing_options import (
    DUBBING_PROVIDERS,
    DubbingVoiceOption,
    get_provider_option,
    get_provider_voices,
    get_voice_title,
    is_provider_default_base,
)
from videocaptioner.ui.thread.voice_preview_thread import VoicePreviewThread


class MiniTag(InfoBadge):
    def __init__(self, text: str, parent=None):
        super().__init__(parent=parent)
        self.setText(text)
        self.setLevel(InfoLevel.INFOAMTION)
        self.setFixedHeight(20)
        self.setMinimumWidth(max(46, len(text) * 12 + 18))
        setFont(self, 10)


class FieldRow(QWidget):
    def __init__(self, title: str, placeholder: str = "", parent=None, password: bool = False):
        super().__init__(parent)
        self.setMinimumHeight(38)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.label = CaptionLabel(title, self)
        self.label.setFixedWidth(58)
        self.lineEdit = LineEdit(self)
        self.lineEdit.setPlaceholderText(placeholder)
        if password:
            self.lineEdit.setEchoMode(LineEdit.Password)
        self.lineEdit.setMinimumHeight(34)
        layout.addWidget(self.label)
        layout.addWidget(self.lineEdit, 1)


class VoiceTile(CardWidget):
    def __init__(self, voice: DubbingVoiceOption, parent=None):
        super().__init__(parent)
        self.voice = voice
        self.setObjectName("voiceTile")
        self.setCursor(Qt.PointingHandCursor)  # type: ignore
        self.setMinimumHeight(116)
        self.setMaximumHeight(128)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.avatar = IconWidget(self)
        self.avatar.setObjectName("voiceAvatar")
        self.avatar.setIcon(FIF.MICROPHONE)
        self.avatar.setFixedSize(34, 34)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        self.titleLabel = BodyLabel(voice.title, self)
        self.descLabel = CaptionLabel(voice.description, self)
        self.descLabel.setWordWrap(True)
        title_box.addWidget(self.titleLabel)
        title_box.addWidget(self.descLabel)
        top.addWidget(self.avatar)
        top.addLayout(title_box, 1)
        layout.addLayout(top)

        tag_row = QHBoxLayout()
        tag_row.setSpacing(5)
        for tag in voice.tags[:3]:
            tag_row.addWidget(MiniTag(tag, self))
        tag_row.addStretch(1)
        self.stateTag = MiniTag("", self)
        self.stateTag.hide()
        tag_row.addWidget(self.stateTag)
        layout.addLayout(tag_row)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.previewButton = TransparentToolButton(FIF.PLAY, self)
        self.previewButton.setToolTip(self.tr("试听音色"))
        self.previewButton.setFixedSize(32, 32)
        self.selectButton = PrimaryPushButton(self.tr("使用"), self)
        self.selectButton.setFixedHeight(32)
        self.selectButton.setMinimumWidth(88)
        self.selectButton.setMaximumWidth(112)
        actions.addWidget(self.previewButton)
        actions.addStretch(1)
        actions.addWidget(self.selectButton)
        layout.addLayout(actions)

    def setCurrent(self, current: bool):
        self.stateTag.setText(self.tr("当前") if current else "")
        self.stateTag.setVisible(current)
        self.selectButton.setText(self.tr("已选择") if current else self.tr("使用"))
        self.selectButton.setEnabled(not current)
        self.setProperty("current", current)
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:  # type: ignore
            self.selectButton.click()
        super().mousePressEvent(event)


class CurrentVoicePanel(CardWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("currentVoicePanel")
        self.setMinimumWidth(300)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        self.titleLabel = SubtitleLabel(self.tr("当前音色"), self)
        self.voiceLabel = BodyLabel("-", self)
        self.descLabel = CaptionLabel("", self)
        self.descLabel.setWordWrap(True)
        self.tagWidget = QWidget(self)
        self.tagRow = QHBoxLayout(self.tagWidget)
        self.tagRow.setContentsMargins(0, 0, 0, 0)
        self.tagRow.setSpacing(5)
        self.previewButton = PrimaryPushButton(FIF.PLAY, self.tr("试听当前"), self)
        self.previewButton.setFixedHeight(32)

        layout.addWidget(self.titleLabel)
        layout.addWidget(self.voiceLabel)
        layout.addWidget(self.descLabel)
        layout.addWidget(self.tagWidget)
        layout.addWidget(self.previewButton)

    def setVoice(self, voice: DubbingVoiceOption):
        self.voiceLabel.setText(voice.title)
        self.descLabel.setText(voice.description)
        while self.tagRow.count():
            item = self.tagRow.takeAt(0)
            if widget := item.widget():
                widget.deleteLater()
        for tag in voice.tags[:3]:
            self.tagRow.addWidget(MiniTag(tag, self))
        self.tagRow.addStretch(1)


class ProviderConfigPanel(CardWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("providerConfigPanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(9)

        self.titleLabel = BodyLabel(self.tr("服务配置"), self)
        self.descLabel = CaptionLabel("", self)
        self.descLabel.setWordWrap(True)
        self.keyField = FieldRow(self.tr("API Key"), self.tr("填写当前配音服务的 API Key"), self, password=True)
        self.baseField = FieldRow(self.tr("Base URL"), self.tr("默认接口地址，可按需修改"), self)
        self.modelLabel = CaptionLabel(self.tr("模型"), self)
        self.modelCombo = ComboBox(self)
        self.modelCombo.setMinimumHeight(34)
        self.testButton = PushButton(FIF.PLAY, self.tr("测试配音"), self)
        self.testButton.setFixedHeight(32)

        layout.addWidget(self.titleLabel)
        layout.addWidget(self.descLabel)
        layout.addWidget(self.keyField)
        layout.addWidget(self.baseField)
        layout.addWidget(self.modelLabel)
        layout.addWidget(self.modelCombo)
        layout.addWidget(self.testButton)


class ClonePanel(CardWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("clonePanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(8)
        self.titleLabel = BodyLabel(self.tr("音色克隆"), self)
        self.badge = MiniTag(self.tr("SiliconFlow"), self)
        header.addWidget(self.titleLabel)
        header.addWidget(self.badge)
        header.addStretch(1)

        file_row = QHBoxLayout()
        file_row.setSpacing(8)
        self.audioInput = LineEdit(self)
        self.audioInput.setPlaceholderText(self.tr("参考音频：wav / mp3 / m4a / flac"))
        self.audioInput.setMinimumHeight(34)
        self.browseButton = PushButton(FIF.FOLDER, self.tr("浏览"), self)
        self.browseButton.setFixedHeight(34)
        file_row.addWidget(self.audioInput, 1)
        file_row.addWidget(self.browseButton)

        self.textInput = LineEdit(self)
        self.textInput.setPlaceholderText(self.tr("参考音频中的原文，越准确越稳定"))
        self.textInput.setMinimumHeight(34)

        layout.addLayout(header)
        layout.addLayout(file_row)
        layout.addWidget(self.textInput)
        self.audioInput.setText(cfg.dubbing_clone_audio.value)
        self.textInput.setText(cfg.dubbing_clone_text.value)
        self.audioInput.textChanged.connect(lambda text: cfg.set(cfg.dubbing_clone_audio, text))
        self.textInput.textChanged.connect(lambda text: cfg.set(cfg.dubbing_clone_text, text))
        self.browseButton.clicked.connect(self._choose_file)

    def _choose_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("选择参考音频"),
            "",
            self.tr("音频文件 (*.wav *.mp3 *.m4a *.flac *.ogg *.opus)"),
        )
        if file_path:
            self.audioInput.setText(file_path)


class DubbingInterface(ScrollArea):
    """配音音色库与服务配置页。"""

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setWindowTitle(self.tr("配音"))
        self.preview_thread: VoicePreviewThread | None = None
        self.player = QMediaPlayer(self)
        self.scrollWidget = QWidget()
        self.expandLayout = ExpandLayout(self.scrollWidget)
        self.titleLabel = TitleLabel(self.tr("配音"), self)
        self.voice_cards: list[VoiceTile] = []
        self._active_preview_button: PushButton | TransparentToolButton | None = None

        self._init_ui()
        self._connect_signals()
        self._on_provider_changed(cfg.dubbing_provider.value)

    def _init_ui(self):
        self.resize(1000, 800)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)  # type: ignore
        self.setViewportMargins(0, 68, 0, 20)
        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        self.setObjectName("dubbingInterface")
        self.scrollWidget.setObjectName("scrollWidget")
        self.titleLabel.setObjectName("settingLabel")
        self.titleLabel.move(36, 26)
        self.setStyleSheet(
            """
            DubbingInterface, #scrollWidget { background-color: transparent; }
            QScrollArea { border: none; background-color: transparent; }
            CardWidget#heroPanel, CardWidget#currentVoicePanel, CardWidget#providerConfigPanel, CardWidget#clonePanel,
            CardWidget#voiceTile { border-radius: 10px; background: #303030; border: 1px solid #4a4a4a; }
            CardWidget#voiceTile[current="true"] { border: 1px solid rgba(67, 217, 154, 0.58); background: #303a34; }
            """
        )

        self.heroPanel = CardWidget(self.scrollWidget)
        self.heroPanel.setObjectName("heroPanel")
        self.heroPanel.setMinimumHeight(64)
        heroLayout = QHBoxLayout(self.heroPanel)
        heroLayout.setContentsMargins(16, 8, 16, 8)
        heroLayout.setSpacing(14)
        heroText = QVBoxLayout()
        heroText.setSpacing(2)
        self.heroTitle = SubtitleLabel(self.tr("音色库"), self.heroPanel)
        self.heroDesc = CaptionLabel(self.tr("选择服务、试听音色，并保存默认配音。"), self.heroPanel)
        self.heroDesc.setWordWrap(True)
        heroText.addWidget(self.heroTitle)
        heroText.addWidget(self.heroDesc)
        self.providerSegment = SegmentedWidget(self.heroPanel)
        for option in DUBBING_PROVIDERS:
            self.providerSegment.addItem(
                routeKey=option.key,
                text=option.title,
                onClick=lambda _checked=False, key=option.key: self._on_provider_changed(key),
            )
        setFont(self.providerSegment, 13)
        heroLayout.addLayout(heroText, 1)
        heroLayout.addWidget(self.providerSegment, 0, Qt.AlignVCenter)  # type: ignore

        self.bodyPanel = QWidget(self.scrollWidget)
        bodyLayout = QHBoxLayout(self.bodyPanel)
        bodyLayout.setContentsMargins(0, 0, 0, 0)
        bodyLayout.setSpacing(18)

        self.voicePanel = QWidget(self.bodyPanel)
        self.voicePanel.setMinimumWidth(560)
        self.voiceLayout = QVBoxLayout(self.voicePanel)
        self.voiceLayout.setContentsMargins(0, 0, 0, 0)
        self.voiceLayout.setSpacing(10)
        voiceHeader = QHBoxLayout()
        voiceHeader.addWidget(BodyLabel(self.tr("可用音色"), self.voicePanel))
        voiceHeader.addStretch(1)
        self.voiceCountLabel = CaptionLabel("", self.voicePanel)
        voiceHeader.addWidget(self.voiceCountLabel)
        self.voiceGridWidget = QWidget(self.voicePanel)
        self.voiceGrid = QGridLayout(self.voiceGridWidget)
        self.voiceGrid.setContentsMargins(0, 0, 0, 0)
        self.voiceGrid.setHorizontalSpacing(12)
        self.voiceGrid.setVerticalSpacing(10)
        self.voiceLayout.addLayout(voiceHeader)
        self.voiceLayout.addWidget(self.voiceGridWidget)

        self.sidePanel = QWidget(self.bodyPanel)
        self.sidePanel.setMinimumWidth(320)
        self.sidePanel.setMaximumWidth(360)
        sideLayout = QVBoxLayout(self.sidePanel)
        sideLayout.setContentsMargins(0, 0, 0, 0)
        sideLayout.setSpacing(12)
        self.currentPanel = CurrentVoicePanel(self.sidePanel)
        self.configPanel = ProviderConfigPanel(self.sidePanel)
        self.clonePanel = ClonePanel(self.sidePanel)
        sideLayout.addWidget(self.currentPanel)
        sideLayout.addWidget(self.configPanel)
        sideLayout.addWidget(self.clonePanel)
        sideLayout.addStretch(1)

        bodyLayout.addWidget(self.voicePanel, 5)
        bodyLayout.addWidget(self.sidePanel, 3)

        self.expandLayout.setSpacing(14)
        self.expandLayout.setContentsMargins(36, 6, 36, 0)
        self.expandLayout.addWidget(self.heroPanel)
        self.expandLayout.addWidget(self.bodyPanel)

    def _connect_signals(self):
        self.currentPanel.previewButton.clicked.connect(self._preview_current)
        self.configPanel.testButton.clicked.connect(self._preview_current)
        self.configPanel.keyField.lineEdit.textChanged.connect(self._on_api_key_changed)
        self.configPanel.baseField.lineEdit.textChanged.connect(lambda text: cfg.set(cfg.dubbing_api_base, text))
        self.configPanel.modelCombo.currentTextChanged.connect(lambda text: cfg.set(cfg.dubbing_model, text))

    def showEvent(self, event):
        super().showEvent(event)
        self._on_provider_changed(cfg.dubbing_provider.value)

    def _on_api_key_changed(self, text: str):
        cfg.set(cfg.dubbing_api_key, text)
        self._refresh_provider_status()

    def _on_provider_changed(self, provider: str):
        cfg.set(cfg.dubbing_provider, provider)
        option = get_provider_option(provider)
        self.providerSegment.setCurrentItem(provider)
        if item := self.providerSegment.widget(provider):
            self.providerSegment.slideAni.stop()
            self.providerSegment.slideAni.setValue(item.x())

        presets = get_provider_voices(provider)
        current = cfg.dubbing_preset.value
        if current not in {voice.preset for voice in presets}:
            preset = get_dubbing_preset(presets[0].preset)
            cfg.set(cfg.dubbing_preset, presets[0].preset)
            cfg.set(cfg.dubbing_voice, preset.voice)
            cfg.set(cfg.dubbing_model, preset.model)

        self._refresh_provider_status(option)
        self.configPanel.setMinimumHeight(236 if option.needs_api_key else 126)
        self.configPanel.keyField.setVisible(option.needs_api_key)
        self.configPanel.baseField.setVisible(option.needs_api_key)
        self.configPanel.modelLabel.setVisible(option.needs_api_key)
        self.configPanel.modelCombo.setVisible(option.needs_api_key)

        self.configPanel.keyField.lineEdit.setText(cfg.dubbing_api_key.value)
        self.configPanel.baseField.lineEdit.setText(cfg.dubbing_api_base.value)
        self.configPanel.modelCombo.blockSignals(True)
        self.configPanel.modelCombo.clear()
        self.configPanel.modelCombo.addItems(list(option.models))
        if cfg.dubbing_model.value:
            self.configPanel.modelCombo.setCurrentText(cfg.dubbing_model.value)
        elif option.models:
            self.configPanel.modelCombo.setCurrentText(option.models[0])
            cfg.set(cfg.dubbing_model, option.models[0])
        self.configPanel.modelCombo.blockSignals(False)

        if not option.default_base and is_provider_default_base(cfg.dubbing_api_base.value):
            cfg.set(cfg.dubbing_api_base, "")
            self.configPanel.baseField.lineEdit.setText("")
        elif option.default_base and is_provider_default_base(cfg.dubbing_api_base.value):
            cfg.set(cfg.dubbing_api_base, option.default_base)
            self.configPanel.baseField.lineEdit.setText(option.default_base)
            self.configPanel.baseField.lineEdit.setCursorPosition(0)

        self.clonePanel.setVisible(option.supports_clone)
        self._render_voice_cards(provider)
        self.expandLayout.update()

    def _refresh_provider_status(self, option=None):
        option = option or get_provider_option(cfg.dubbing_provider.value)
        api_key = cfg.dubbing_api_key.value.strip()
        if option.needs_api_key and not api_key:
            self.configPanel.descLabel.setText(
                self.tr("{desc} 请先填写 API Key 后再测试真实服务。").format(desc=option.description)
            )
            self.configPanel.testButton.setText(self.tr("填写 Key 后测试"))
            self.configPanel.testButton.setEnabled(False)
        else:
            self.configPanel.descLabel.setText(option.description)
            self.configPanel.testButton.setText(self.tr("测试配音"))
            self.configPanel.testButton.setEnabled(True)

    def _render_voice_cards(self, provider: str):
        self.voiceLayout.removeWidget(self.voiceGridWidget)
        self.voiceGridWidget.setParent(None)
        self.voiceGridWidget.deleteLater()
        self.voiceGridWidget = QWidget(self.voicePanel)
        self.voiceGrid = QGridLayout(self.voiceGridWidget)
        self.voiceGrid.setContentsMargins(0, 0, 0, 0)
        self.voiceGrid.setHorizontalSpacing(12)
        self.voiceGrid.setVerticalSpacing(10)
        self.voiceLayout.addWidget(self.voiceGridWidget)
        self.voice_cards = []
        voices = get_provider_voices(provider)
        self.voiceCountLabel.setText(self.tr("{count} 个音色").format(count=len(voices)))
        columns = 2
        rows = max(1, (len(voices) + columns - 1) // columns)
        grid_height = rows * 126 + (rows - 1) * 10
        content_height = grid_height + 42
        self.voiceGridWidget.setFixedHeight(grid_height)
        self.voicePanel.setFixedHeight(content_height)
        self.bodyPanel.setFixedHeight(max(content_height, self.sidePanel.sizeHint().height()))
        for index, voice in enumerate(voices):
            card = VoiceTile(voice, self.voiceGridWidget)
            card.previewButton.clicked.connect(lambda _=False, p=voice.preset, b=card.previewButton: self._preview(p, b))
            card.selectButton.clicked.connect(lambda _=False, p=voice.preset: self._apply_preset(p))
            card.setCurrent(voice.preset == cfg.dubbing_preset.value)
            self.voiceGrid.addWidget(card, index // columns, index % columns)
            self.voice_cards.append(card)
        self._sync_current_panel()
        self.voiceGridWidget.updateGeometry()
        self.voicePanel.updateGeometry()
        self.bodyPanel.updateGeometry()
        self.scrollWidget.adjustSize()
        self.viewport().update()

    def _sync_current_panel(self):
        current = cfg.dubbing_preset.value
        for voices in (get_provider_voices(cfg.dubbing_provider.value),):
            for voice in voices:
                if voice.preset == current:
                    self.currentPanel.setVoice(voice)
                    return

    def _apply_preset(self, preset_name: str, *, show_tip: bool = True):
        preset = get_dubbing_preset(preset_name)
        cfg.set(cfg.dubbing_provider, preset.provider)
        cfg.set(cfg.dubbing_preset, preset_name)
        cfg.set(cfg.dubbing_voice, preset.voice)
        cfg.set(cfg.dubbing_model, preset.model)
        if preset.api_base and not cfg.dubbing_api_base.value:
            cfg.set(cfg.dubbing_api_base, preset.api_base)
        self._render_voice_cards(preset.provider)
        if self.configPanel.modelCombo.currentText() != preset.model:
            self.configPanel.modelCombo.setCurrentText(preset.model)
        if show_tip:
            InfoBar.success(
                self.tr("已选择音色"),
                self.tr("{name} 已设为默认配音音色").format(name=get_voice_title(preset_name)),
                duration=INFOBAR_DURATION_SUCCESS,
                parent=self,
            )

    def _preview_current(self):
        self._preview(cfg.dubbing_preset.value, self.currentPanel.previewButton)

    def _preview(self, preset_name: str, button: PushButton | TransparentToolButton | None = None):
        if self.preview_thread and self.preview_thread.isRunning():
            return
        self._active_preview_button = button
        if button:
            button.setEnabled(False)
            if hasattr(button, "setText"):
                button.setText(self.tr("试听中..."))
        self.configPanel.testButton.setEnabled(False)
        self.preview_thread = VoicePreviewThread(preset_name)
        self.preview_thread.finished.connect(self._on_preview_finished)
        self.preview_thread.error.connect(self._on_preview_error)
        self.preview_thread.start()

    def _reset_preview_buttons(self):
        self._refresh_provider_status()
        self.currentPanel.previewButton.setEnabled(True)
        self.currentPanel.previewButton.setText(self.tr("试听当前"))
        if self._active_preview_button and self._active_preview_button is not self.currentPanel.previewButton:
            self._active_preview_button.setEnabled(True)
            if hasattr(self._active_preview_button, "setText"):
                self._active_preview_button.setText(self.tr("试听"))
        self._active_preview_button = None

    def _on_preview_finished(self, path: str):
        self._reset_preview_buttons()
        self.player.setMedia(QMediaContent(QUrl.fromLocalFile(path)))
        self.player.play()
        InfoBar.success(
            self.tr("开始播放"),
            self.tr("正在播放：{name}").format(name=Path(path).name),
            duration=INFOBAR_DURATION_SUCCESS,
            parent=self,
        )

    def _on_preview_error(self, message: str):
        self._reset_preview_buttons()
        InfoBar.error(
            self.tr("试听失败"),
            message,
            duration=INFOBAR_DURATION_ERROR,
            parent=self,
        )
