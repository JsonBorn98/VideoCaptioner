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
    ExpandLayout,
    InfoBadge,
    InfoBar,
    InfoLevel,
    LineEdit,
    PushButton,
    ScrollArea,
    SegmentedWidget,
    SimpleCardWidget,
    TitleLabel,
    TransparentToolButton,
    isDarkTheme,
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
)
from videocaptioner.ui.thread.voice_preview_thread import (
    VoicePreviewThread,
    bundled_voice_preview,
)


class MiniTag(InfoBadge):
    def __init__(self, text: str, parent=None):
        super().__init__(parent=parent)
        self.setText(text)
        self.setLevel(InfoLevel.INFOAMTION)
        self.setFixedHeight(20)
        self.setMinimumWidth(max(46, len(text) * 12 + 18))
        setFont(self, 10)


class VoiceTile(SimpleCardWidget):
    def __init__(self, voice: DubbingVoiceOption, parent=None):
        super().__init__(parent)
        self.voice = voice
        self.setObjectName("voiceTile")
        self.setBorderRadius(8)
        self.setCursor(Qt.PointingHandCursor)  # type: ignore
        self.setClickEnabled(True)
        self.setMinimumHeight(92)
        self.setMaximumHeight(104)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(7)

        top = QHBoxLayout()
        top.setSpacing(10)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        self.titleLabel = BodyLabel(voice.title, self)
        self.descLabel = CaptionLabel(voice.description, self)
        self.descLabel.setWordWrap(True)
        title_box.addWidget(self.titleLabel)
        title_box.addWidget(self.descLabel)
        top.addLayout(title_box, 1)
        self.previewButton = PushButton(FIF.PLAY, self.tr("试听"), self)
        self.previewButton.setToolTip(self.tr("播放内置试听"))
        self.previewButton.setFixedHeight(30)
        self.previewButton.setMinimumWidth(72)
        top.addWidget(self.previewButton, 0, Qt.AlignTop)  # type: ignore
        self.stateTag = MiniTag("", self)
        self.stateTag.hide()
        top.addWidget(self.stateTag, 0, Qt.AlignTop)  # type: ignore
        layout.addLayout(top)

        tag_row = QHBoxLayout()
        tag_row.setSpacing(5)
        for tag in voice.tags[:3]:
            tag_row.addWidget(MiniTag(tag, self))
        tag_row.addStretch(1)
        self.selectButton = PushButton(self.tr("使用"), self)
        self.selectButton.setFixedHeight(30)
        self.selectButton.setMinimumWidth(76)
        self.selectButton.setMaximumWidth(92)
        tag_row.addWidget(self.selectButton)
        layout.addLayout(tag_row)

    def setCurrent(self, current: bool):
        self.stateTag.setText(self.tr("导出使用") if current else "")
        self.stateTag.setVisible(current)
        self.selectButton.setText(self.tr("已选择") if current else self.tr("使用"))
        self.selectButton.setEnabled(not current)
        self.setClickEnabled(not current)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:  # type: ignore
            self.selectButton.click()
        super().mousePressEvent(event)


class CurrentVoicePanel(SimpleCardWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("currentVoicePanel")
        self.setBorderRadius(8)
        self.setMinimumWidth(300)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        self.titleLabel = BodyLabel(self.tr("已选音色"), self)
        self.voiceLabel = BodyLabel("-", self)
        self.descLabel = CaptionLabel("", self)
        self.descLabel.setWordWrap(True)
        self.tagWidget = QWidget(self)
        self.tagRow = QHBoxLayout(self.tagWidget)
        self.tagRow.setContentsMargins(0, 0, 0, 0)
        self.tagRow.setSpacing(5)
        self.previewInput = LineEdit(self)
        self.previewInput.setPlaceholderText(self.tr("输入一句话，试听当前音色"))
        self.previewInput.setMinimumHeight(34)
        buttonRow = QHBoxLayout()
        buttonRow.setSpacing(8)
        self.previewButton = PushButton(FIF.PLAY, self.tr("内置试听"), self)
        self.customPreviewButton = PushButton(FIF.PLAY, self.tr("文本试听"), self)
        self.previewButton.setToolTip(self.tr("播放随软件内置的音色样例，不需要 API Key"))
        self.customPreviewButton.setToolTip(self.tr("用输入文字实时生成试听；非 Edge 服务需要 API Key"))
        self.previewButton.setFixedHeight(32)
        self.customPreviewButton.setFixedHeight(32)
        buttonRow.addWidget(self.previewButton)
        buttonRow.addWidget(self.customPreviewButton)

        layout.addWidget(self.titleLabel)
        layout.addWidget(self.voiceLabel)
        layout.addWidget(self.descLabel)
        layout.addWidget(self.tagWidget)
        layout.addWidget(self.previewInput)
        layout.addLayout(buttonRow)

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


class ProviderStatusPanel(SimpleCardWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("providerStatusPanel")
        self.setBorderRadius(8)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(8)
        self.titleLabel = BodyLabel(self.tr("当前服务"), self)
        self.statusTag = MiniTag("", self)
        header.addWidget(self.titleLabel)
        header.addStretch(1)
        header.addWidget(self.statusTag)
        self.descLabel = CaptionLabel("", self)
        self.descLabel.setWordWrap(True)
        self.modelLabel = CaptionLabel("", self)
        self.testButton = PushButton(FIF.PLAY, self.tr("测试服务"), self)
        self.testButton.setToolTip(self.tr("合成一句短文本，验证当前配音服务是否可用"))
        self.testButton.setFixedHeight(32)

        layout.addLayout(header)
        layout.addWidget(self.descLabel)
        layout.addWidget(self.modelLabel)
        layout.addWidget(self.testButton)

    def setStatus(self, provider_title: str, description: str, model: str, ready: bool, needs_key: bool):
        self.titleLabel.setText(provider_title)
        self.descLabel.setText(description)
        self.modelLabel.setText(model)
        if needs_key:
            self.statusTag.setText(self.tr("已配置") if ready else self.tr("缺 Key"))
            self.statusTag.setLevel(InfoLevel.SUCCESS if ready else InfoLevel.WARNING)
        else:
            self.statusTag.setText(self.tr("免 Key"))
            self.statusTag.setLevel(InfoLevel.SUCCESS)
        self.testButton.setEnabled(True)
        self.testButton.setText(self.tr("测试服务") if ready else self.tr("去设置填写 Key"))


class ClonePanel(SimpleCardWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("clonePanel")
        self.setBorderRadius(8)
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
        self.hintLabel = CaptionLabel(self.tr("上传一段自己的声音，可用输入文本试听相似音色。"), self)
        self.hintLabel.setWordWrap(True)

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
        self.textInput.setPlaceholderText(self.tr("参考音频里说了什么，例如：大家好，欢迎来到我的频道"))
        self.textInput.setMinimumHeight(34)
        self.clonePreviewButton = PushButton(FIF.PLAY, self.tr("试听克隆"), self)
        self.clonePreviewButton.setToolTip(self.tr("填写参考音频和原文后，用上方试听文本生成相似音色"))
        self.clonePreviewButton.setFixedHeight(34)

        layout.addLayout(header)
        layout.addWidget(self.hintLabel)
        layout.addLayout(file_row)
        layout.addWidget(self.textInput)
        layout.addWidget(self.clonePreviewButton)
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
        self.enableTransparentBackground()

        self.topPanel = QWidget(self.scrollWidget)
        topLayout = QHBoxLayout(self.topPanel)
        topLayout.setContentsMargins(0, 0, 0, 0)
        topLayout.setSpacing(8)
        self.providerSegment = SegmentedWidget(self.topPanel)
        for option in DUBBING_PROVIDERS:
            self.providerSegment.addItem(
                routeKey=option.key,
                text=option.title,
                onClick=lambda _checked=False, key=option.key: self._on_provider_changed(key),
            )
        setFont(self.providerSegment, 13)
        topLayout.addWidget(self.providerSegment)
        topLayout.addStretch(1)

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
        self.voiceGrid.setHorizontalSpacing(10)
        self.voiceGrid.setVerticalSpacing(8)
        self.voiceLayout.addLayout(voiceHeader)
        self.voiceLayout.addWidget(self.voiceGridWidget)

        self.sidePanel = QWidget(self.bodyPanel)
        self.sidePanel.setMinimumWidth(320)
        self.sidePanel.setMaximumWidth(360)
        sideLayout = QVBoxLayout(self.sidePanel)
        sideLayout.setContentsMargins(0, 0, 0, 0)
        sideLayout.setSpacing(12)
        self.currentPanel = CurrentVoicePanel(self.sidePanel)
        self.configPanel = ProviderStatusPanel(self.sidePanel)
        self.clonePanel = ClonePanel(self.sidePanel)
        sideLayout.addWidget(self.currentPanel)
        sideLayout.addWidget(self.configPanel)
        sideLayout.addWidget(self.clonePanel)
        sideLayout.addStretch(1)

        bodyLayout.addWidget(self.voicePanel, 5)
        bodyLayout.addWidget(self.sidePanel, 3)

        self.expandLayout.setSpacing(14)
        self.expandLayout.setContentsMargins(36, 6, 36, 0)
        self.expandLayout.addWidget(self.topPanel)
        self.expandLayout.addWidget(self.bodyPanel)

    def _connect_signals(self):
        self.currentPanel.previewButton.clicked.connect(self._preview_current)
        self.currentPanel.customPreviewButton.clicked.connect(self._preview_custom_text)
        self.clonePanel.clonePreviewButton.clicked.connect(self._preview_custom_text)
        self.configPanel.testButton.clicked.connect(self._on_service_button_clicked)

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_page_background()
        self._on_provider_changed(cfg.dubbing_provider.value)

    def _sync_page_background(self):
        color = "#202020" if isDarkTheme() else "#f5f5f5"
        self.setStyleSheet(f"QScrollArea {{ border: none; background: {color}; }}")
        self.scrollWidget.setStyleSheet(f"QWidget#scrollWidget {{ background: {color}; }}")

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
        if option.models and not cfg.dubbing_model.value:
            cfg.set(cfg.dubbing_model, option.models[0])

        self.clonePanel.setVisible(option.supports_clone)
        self._render_voice_cards(provider)
        self.expandLayout.update()

    def _refresh_provider_status(self, option=None):
        option = option or get_provider_option(cfg.dubbing_provider.value)
        api_key = cfg.dubbing_api_key.value.strip()
        preset = get_dubbing_preset(cfg.dubbing_preset.value)
        ready = not option.needs_api_key or bool(api_key)
        desc = option.description
        if option.needs_api_key and not api_key:
            desc = self.tr("内置试听可直接播放；用自己的文字试听或正式配音前，需要先到设置页填写 Key。")
        self.configPanel.setStatus(
            provider_title=option.title,
            description=desc,
            model=self.tr("当前声音引擎：{model}").format(model=cfg.dubbing_model.value or preset.model),
            ready=ready,
            needs_key=option.needs_api_key,
        )

    def _render_voice_cards(self, provider: str):
        self.voiceLayout.removeWidget(self.voiceGridWidget)
        self.voiceGridWidget.setParent(None)
        self.voiceGridWidget.deleteLater()
        self.voiceGridWidget = QWidget(self.voicePanel)
        self.voiceGrid = QGridLayout(self.voiceGridWidget)
        self.voiceGrid.setContentsMargins(0, 0, 0, 0)
        self.voiceGrid.setHorizontalSpacing(10)
        self.voiceGrid.setVerticalSpacing(8)
        self.voiceLayout.addWidget(self.voiceGridWidget)
        self.voice_cards = []
        voices = get_provider_voices(provider)
        self.voiceCountLabel.setText(self.tr("{count} 个音色").format(count=len(voices)))
        columns = 2
        rows = max(1, (len(voices) + columns - 1) // columns)
        grid_height = rows * 100 + (rows - 1) * 8
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
        self._refresh_provider_status()
        if show_tip:
            InfoBar.success(
                self.tr("已选择音色"),
                self.tr("{name} 已设为默认配音音色").format(name=get_voice_title(preset_name)),
                duration=INFOBAR_DURATION_SUCCESS,
                parent=self,
            )

    def _preview_current(self):
        self._preview(cfg.dubbing_preset.value, self.currentPanel.previewButton)

    def _on_service_button_clicked(self):
        option = get_provider_option(cfg.dubbing_provider.value)
        if option.needs_api_key and not cfg.dubbing_api_key.value.strip():
            self._open_settings_for_key()
            return
        self._preview_current()

    def _open_settings_for_key(self):
        window = self.window()
        setting_interface = getattr(window, "settingInterface", None)
        if setting_interface is not None and hasattr(window, "switchTo"):
            window.switchTo(setting_interface)
            InfoBar.info(
                self.tr("填写配音 Key"),
                self.tr("请在配音配置中填写当前服务的 API Key。"),
                duration=3500,
                parent=setting_interface,
            )
            return
        InfoBar.warning(
            self.tr("需要 API Key"),
            self.tr("请打开设置页，在配音配置中填写当前服务的 API Key。"),
            duration=3500,
            parent=self,
        )

    def _preview_custom_text(self):
        text = self.currentPanel.previewInput.text().strip()
        if not text:
            InfoBar.warning(
                self.tr("请输入试听文本"),
                self.tr("文本试听会使用你输入的内容实时生成音频。"),
                duration=3000,
                parent=self,
            )
            return
        self._preview(
            cfg.dubbing_preset.value,
            self.currentPanel.customPreviewButton,
            text=text,
            use_clone=True,
        )

    def _preview(
        self,
        preset_name: str,
        button: PushButton | TransparentToolButton | None = None,
        *,
        text: str = "",
        use_clone: bool = False,
    ):
        if self.preview_thread and self.preview_thread.isRunning():
            return
        preset = get_dubbing_preset(preset_name)
        clone_audio = ""
        clone_text = ""
        if use_clone and get_provider_option(preset.provider).supports_clone:
            clone_audio = self.clonePanel.audioInput.text().strip()
            clone_text = self.clonePanel.textInput.text().strip()
            if bool(clone_audio) != bool(clone_text):
                InfoBar.warning(
                    self.tr("克隆信息不完整"),
                    self.tr("音色克隆需要同时填写参考音频和参考音频原文。"),
                    duration=3500,
                    parent=self,
                )
                return
        if (
            preset.provider != "edge"
            and not cfg.dubbing_api_key.value.strip()
            and (text or clone_audio or not bundled_voice_preview(preset_name))
        ):
            InfoBar.warning(
                self.tr("需要 API Key"),
                self.tr("自定义文本或克隆试听需要真实请求，请先填写当前配音服务的 API Key。"),
                duration=3500,
                parent=self,
            )
            return
        self._active_preview_button = button
        if button:
            button.setEnabled(False)
            if hasattr(button, "setText"):
                button.setText(self.tr("试听中..."))
        self.configPanel.testButton.setEnabled(False)
        self.preview_thread = VoicePreviewThread(
            preset_name,
            text=text,
            clone_audio_path=clone_audio,
            clone_audio_text=clone_text,
        )
        self.preview_thread.finished.connect(self._on_preview_finished)
        self.preview_thread.error.connect(self._on_preview_error)
        self.preview_thread.start()

    def _reset_preview_buttons(self):
        self._refresh_provider_status()
        self.currentPanel.previewButton.setEnabled(True)
        self.currentPanel.previewButton.setText(self.tr("内置试听"))
        self.currentPanel.customPreviewButton.setEnabled(True)
        self.currentPanel.customPreviewButton.setText(self.tr("文本试听"))
        if self._active_preview_button and self._active_preview_button is not self.currentPanel.previewButton:
            self._active_preview_button.setEnabled(True)
            if hasattr(self._active_preview_button, "setText"):
                text = self.tr("文本试听") if self._active_preview_button is self.currentPanel.customPreviewButton else self.tr("试听")
                self._active_preview_button.setText(text)
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
