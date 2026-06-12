from pathlib import Path

from PyQt5.QtCore import Qt, QUrl, pyqtSignal
from PyQt5.QtGui import QColor, QPainter
from PyQt5.QtMultimedia import (
    QAudioEncoderSettings,
    QAudioRecorder,
    QMediaContent,
    QMediaPlayer,
    QMediaRecorder,
    QMultimedia,
)
from PyQt5.QtWidgets import (
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    InfoBar,
    ScrollArea,
)

from videocaptioner.config import CACHE_PATH
from videocaptioner.core.constant import INFOBAR_DURATION_ERROR, INFOBAR_DURATION_SUCCESS
from videocaptioner.core.dubbing import get_dubbing_preset
from videocaptioner.ui.common.app_icons import AppIcon
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.common.dubbing_options import (
    DUBBING_PROVIDERS,
    DubbingVoiceOption,
    get_provider_option,
    get_provider_voices,
)
from videocaptioner.ui.common.theme_tokens import app_palette
from videocaptioner.ui.components.workbench import (
    ClickableFrame,
    CompactButton,
    FilterTabs,
    SelectableCard,
    WorkbenchButton,
    apply_font,
)
from videocaptioner.ui.thread.voice_preview_thread import (
    VoicePreviewThread,
    bundled_voice_preview,
    playable_voice_preview,
)

CONTROL_RADIUS = 9   # 与 workbench CompactButton 一致
PANEL_RADIUS = 14  # 与 workbench .panel 一致
PAGE_MARGIN_X = 26  # 与批量/诊断等独立 nav 页根边距统一为 (26,20,26,22)
SECTION_GAP = 14
BODY_GAP = 18
PROVIDER_HEIGHT = 88
TABLE_HEADER_HEIGHT = 44
VOICE_ROW_HEIGHT = 68
SQUARE_BUTTON_SIZE = 40
AUDITION_BUTTON_WIDTH = 92

# 提供商卡左侧图标（与批量处理页模式卡同款图标盒）：均为音频族图标，
# edge=扬声器、gemini=音符、siliconflow=麦克风（克隆录音语义贴切）。
_PROVIDER_ICONS = {
    "edge": AppIcon.VOLUME,
    "gemini": AppIcon.MUSIC,
    "siliconflow": AppIcon.MICROPHONE,
}
PREVIEW_PANEL_WIDTH = 376
GENDER_FILTER_TAGS = {"女声", "男声"}


def _blend_color(foreground: str, background: str, alpha: float) -> QColor:
    # foreground/background 恒为 app_palette() 的有效色；旧的无效兜底写死了非主题绿，
    # 自定义主题时反而错，且从不触发，去掉。
    fg = QColor(foreground)
    bg = QColor(background)
    alpha = max(0.0, min(1.0, alpha))
    return QColor(
        int(fg.red() * alpha + bg.red() * (1 - alpha)),
        int(fg.green() * alpha + bg.green() * (1 - alpha)),
        int(fg.blue() * alpha + bg.blue() * (1 - alpha)),
    )


class ThemedSimpleCard(QFrame):
    """项目自绘卡：palette 颜色 + 可选选中态描边，不依赖 qfluent SimpleCardWidget
    （它本就完全覆盖 paintEvent，qfluent 基类的视觉从未被用到）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._selected_visual = False
        self._radius = PANEL_RADIUS

    def setBorderRadius(self, radius: int):
        self._radius = radius

    def setSelectedVisual(self, selected: bool):
        self._selected_visual = selected
        self.update()

    def paintEvent(self, event):  # noqa: N802
        palette = app_palette()
        painter = QPainter(self)
        painter.setRenderHints(QPainter.Antialiasing)
        background = (
            _blend_color(palette.accent, palette.panel, 0.14)
            if self._selected_visual
            else QColor(palette.panel)
        )
        border = QColor(palette.accent if self._selected_visual else palette.line)
        painter.setPen(border)
        painter.setBrush(background)
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), self._radius, self._radius)


class AuditionButton(ClickableFrame):
    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setObjectName("auditionButton")
        self.setFixedSize(AUDITION_BUTTON_WIDTH, 36)

        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel(text, self)
        self.label.setObjectName("auditionButtonLabel")
        self.label.setAlignment(Qt.AlignCenter)  # type: ignore
        apply_font(self.label, 13, 750)
        layout.addWidget(self.label, 0, 0, Qt.AlignCenter)  # type: ignore
        self._sync_style()

    def setText(self, text: str):
        self.label.setText(text)

    def text(self) -> str:
        return self.label.text()

    def setEnabled(self, enabled: bool):
        super().setEnabled(enabled)
        self._sync_style()

    def mousePressEvent(self, event):
        if self.isEnabled() and event.button() == Qt.LeftButton:  # type: ignore
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def _sync_style(self):
        palette = app_palette()
        if not self.isEnabled():
            bg, fg, border = palette.disabled, palette.subtle, palette.line
        else:
            bg, fg, border = palette.field, palette.text, palette.line
        self.setStyleSheet(
            f"""
            QFrame#auditionButton {{
                background: {bg};
                border: 1px solid {border};
                border-radius: {CONTROL_RADIUS}px;
            }}
            QLabel#auditionButtonLabel {{
                color: {fg};
                background: transparent;
                border: none;
            }}
            """
        )


class VoiceRow(QFrame):
    previewRequested = pyqtSignal(str, object)
    selectedRequested = pyqtSignal(str)

    def __init__(self, voice: DubbingVoiceOption, parent=None):
        super().__init__(parent)
        self.voice = voice
        self.setObjectName("voiceRow")
        self.setFixedHeight(VOICE_ROW_HEIGHT)
        self.setCursor(Qt.PointingHandCursor)  # type: ignore

        layout = QGridLayout(self)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setHorizontalSpacing(18)
        layout.setVerticalSpacing(0)
        layout.setColumnStretch(0, 1)
        layout.setColumnMinimumWidth(1, AUDITION_BUTTON_WIDTH)

        titleWidget = QWidget(self)
        titleWidget.setFixedHeight(40)
        titleBox = QVBoxLayout(titleWidget)
        titleBox.setContentsMargins(0, 0, 0, 0)
        titleBox.setSpacing(4)
        self.titleLabel = BodyLabel(self.tr(voice.title), self)
        self.titleLabel.setFixedHeight(18)
        apply_font(self.titleLabel, 13, 700)
        self.descLabel = CaptionLabel(self.tr(voice.description), self)
        self.descLabel.setFixedHeight(16)
        self.descLabel.setWordWrap(False)
        titleBox.addWidget(self.titleLabel)
        titleBox.addWidget(self.descLabel)
        layout.addWidget(titleWidget, 0, 0, Qt.AlignVCenter)  # type: ignore

        self.previewButton = AuditionButton(self.tr("试听"), self)
        layout.addWidget(self.previewButton, 0, 1, Qt.AlignRight | Qt.AlignVCenter)  # type: ignore
        self.previewButton.clicked.connect(lambda: self.previewRequested.emit(self.voice.preset, self.previewButton))

    def setSelected(self, selected: bool):
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:  # type: ignore
            self.selectedRequested.emit(self.voice.preset)
            event.accept()
            return
        super().mousePressEvent(event)


class VoiceTable(QFrame):
    previewRequested = pyqtSignal(str, object)
    selectedRequested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("voiceTable")
        self.rows: list[VoiceRow] = []
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(1, 1, 1, 1)
        self.layout.setSpacing(1)
        self._add_header()

    def _add_header(self):
        """表头即筛选行：与批量处理页队列头同款的分段筛选组件。"""
        self.header = QFrame(self)
        self.header.setObjectName("voiceHeader")
        self.header.setFixedHeight(52)
        layout = QHBoxLayout(self.header)
        layout.setContentsMargins(10, 0, 16, 0)
        layout.setSpacing(10)
        self.filterTabs = FilterTabs(
            [("全部", "全部"), ("女声", "女声"), ("男声", "男声")], self.header
        )
        layout.addWidget(self.filterTabs)
        layout.addStretch(1)
        self.layout.addWidget(self.header)

    def setFilterVisible(self, visible: bool):
        self.header.setVisible(visible)

    def setVoices(self, voices: list[DubbingVoiceOption], current: str):
        for row in self.rows:
            self.layout.removeWidget(row)
            row.hide()
            row.setParent(None)
            row.deleteLater()
        self.rows = []
        for voice in voices:
            row = VoiceRow(voice, self)
            row.setSelected(voice.preset == current)
            row.previewRequested.connect(self.previewRequested)
            row.selectedRequested.connect(self.selectedRequested)
            self.layout.addWidget(row)
            self.rows.append(row)
        self.setFixedHeight(TABLE_HEADER_HEIGHT + len(self.rows) * (VOICE_ROW_HEIGHT + 1) + 2)
        self.updateGeometry()


class PreviewPanel(ThemedSimpleCard):
    layoutChanged = pyqtSignal()
    customPreviewRequested = pyqtSignal()
    chooseAudioRequested = pyqtSignal()
    playAudioRequested = pyqtSignal()
    recordRequested = pyqtSignal()
    clearRequested = pyqtSignal()
    cloneTextChanged = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("previewPanel")
        self.setAttribute(Qt.WA_StyledBackground, True)  # type: ignore
        self.setBorderRadius(PANEL_RADIUS)
        self.setFixedWidth(PREVIEW_PANEL_WIDTH)
        self._clone_available = True
        self._clone_audio_path = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 15, 16, 15)
        layout.setSpacing(9)

        header = QHBoxLayout()
        self.titleLabel = BodyLabel(self.tr("配音文案"), self)
        apply_font(self.titleLabel, 16, 700)
        self.hintLabel = CaptionLabel(self.tr("用户可自行输入"), self)
        self.hintLabel.setObjectName("sampleHintLabel")
        apply_font(self.hintLabel, 11, 400)
        self.hintLabel.hide()
        header.addWidget(self.titleLabel)
        header.addStretch(1)
        header.addWidget(self.hintLabel)

        self.previewInput = QTextEdit(self)
        self.previewInput.setObjectName("previewInput")
        self.previewInput.setPlaceholderText(self.tr("输入一句话，试听选中的音色"))
        self.previewInput.setFixedHeight(104)
        self.previewInput.setText(self.tr("你好，这是我想用于测试的配音文案。请用自然清晰的语气朗读这一句话。"))
        apply_font(self.previewInput, 13, 700)

        meta = QHBoxLayout()
        meta.setContentsMargins(0, 0, 0, 0)
        meta.setSpacing(8)
        self.metaLabel = CaptionLabel(self.tr("建议 10-80 字，试听更快"), self)
        self.countLabel = CaptionLabel("", self)
        self.metaLabel.setObjectName("sampleMetaLabel")
        self.countLabel.setObjectName("sampleMetaLabel")
        self.countLabel.setMinimumWidth(50)
        self.countLabel.setAlignment(Qt.AlignRight | Qt.AlignVCenter)  # type: ignore
        apply_font(self.metaLabel, 11, 400)
        apply_font(self.countLabel, 11, 400)
        meta.addStretch(1)
        meta.addWidget(self.countLabel)
        self.metaLabel.hide()

        self.customPreviewButton = WorkbenchButton(
            self.tr("试听这句话"), AppIcon.PLAY, primary=True, height=40, parent=self
        )

        self.cloneSection = QFrame(self)
        self.cloneSection.setObjectName("cloneSection")
        cloneLayout = QVBoxLayout(self.cloneSection)
        cloneLayout.setContentsMargins(12, 12, 12, 12)
        cloneLayout.setSpacing(8)

        cloneHeader = QHBoxLayout()
        cloneTitle = BodyLabel(self.tr("声音克隆"), self.cloneSection)
        apply_font(cloneTitle, 15, 700)
        cloneHeader.addWidget(cloneTitle)
        cloneHeader.addStretch(1)

        self.fileBox = QFrame(self.cloneSection)
        self.fileBox.setObjectName("cloneFileBox")
        self.fileBox.setFixedHeight(38)
        fileLayout = QHBoxLayout(self.fileBox)
        fileLayout.setContentsMargins(12, 0, 12, 0)
        self.fileLabel = CaptionLabel(self.tr("未选择参考音频"), self.fileBox)
        self.fileLabel.setWordWrap(False)
        fileLayout.addWidget(self.fileLabel, 1)

        # 操作按钮统一 workbench 紧凑按钮，自适应宽度不挤压
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        self.chooseButton = CompactButton(self.tr("上传"), AppIcon.FOLDER_ADD, self.cloneSection)
        self.playButton = CompactButton(self.tr("试听"), AppIcon.PLAY, self.cloneSection)
        self.recordButton = CompactButton(self.tr("录制"), AppIcon.MICROPHONE, self.cloneSection)
        self.clearButton = CompactButton(self.tr("清除"), AppIcon.DELETE, self.cloneSection)
        actions.addWidget(self.chooseButton)
        actions.addWidget(self.playButton)
        actions.addWidget(self.recordButton)
        actions.addWidget(self.clearButton)
        actions.addStretch(1)

        self.cloneTextLabel = CaptionLabel(self.tr("参考文本"), self.cloneSection)
        self.cloneTextLabel.setObjectName("sampleMetaLabel")
        self.cloneTextInput = QTextEdit(self.cloneSection)
        self.cloneTextInput.setObjectName("cloneTextInput")
        self.cloneTextInput.setPlaceholderText(self.tr("输入参考音频里实际朗读的文字"))
        self.cloneTextInput.setFixedHeight(48)
        apply_font(self.cloneTextInput, 12, 650)

        self.cloneHintLabel = CaptionLabel(self.tr("未上传参考音频时，会直接用上方文案试听当前音色。"), self.cloneSection)
        self.cloneHintLabel.setObjectName("sampleMetaLabel")
        self.cloneHintLabel.setWordWrap(True)
        self.cloneHintLabel.hide()

        cloneLayout.addLayout(cloneHeader)
        cloneLayout.addWidget(self.fileBox)
        cloneLayout.addLayout(actions)
        cloneLayout.addWidget(self.cloneTextLabel)
        cloneLayout.addWidget(self.cloneTextInput)
        cloneLayout.addWidget(self.cloneHintLabel)

        layout.addLayout(header)
        layout.addWidget(self.previewInput)
        layout.addLayout(meta)
        layout.addWidget(self.cloneSection)
        layout.addWidget(self.customPreviewButton)

        self.previewInput.textChanged.connect(self._update_count)
        self.customPreviewButton.clicked.connect(self.customPreviewRequested)
        self.chooseButton.clicked.connect(self.chooseAudioRequested)
        self.playButton.clicked.connect(self.playAudioRequested)
        self.recordButton.clicked.connect(self.recordRequested)
        self.clearButton.clicked.connect(self.clearRequested)
        self.cloneTextInput.textChanged.connect(
            lambda: self.cloneTextChanged.emit(self.cloneTextInput.toPlainText().strip())
        )
        self._update_count()

    def text(self) -> str:
        return self.previewInput.toPlainText().strip()

    def setCloneAvailable(self, available: bool):
        self._clone_available = available
        self.cloneSection.setVisible(available)
        if not available:
            self.layoutChanged.emit()
        else:
            self._sync_clone_state()
        self.updateGeometry()

    def setAudioPath(self, path: str):
        self._clone_audio_path = path.strip()
        self.fileLabel.setText(Path(path).name if path else self.tr("未选择参考音频"))
        if not self._clone_available:
            self.playButton.setEnabled(False)
            self.clearButton.setEnabled(False)
            self.updateGeometry()
            self.layoutChanged.emit()
            return
        self._sync_clone_state()

    def setCloneText(self, text: str):
        if self.cloneTextInput.toPlainText() == text:
            return
        self.cloneTextInput.blockSignals(True)
        self.cloneTextInput.setText(text)
        self.cloneTextInput.blockSignals(False)

    def setRecording(self, recording: bool):
        self.recordButton.setText(self.tr("停止") if recording else self.tr("录制"))
        self.chooseButton.setEnabled(not recording)
        self.playButton.setEnabled(False if recording else self._clone_audio_exists())
        self.clearButton.setEnabled(False if recording else bool(self._clone_audio_path))

    def syncStyle(self):
        self.customPreviewButton.syncStyle()
        self.chooseButton.syncStyle()
        self.playButton.syncStyle()
        self.recordButton.syncStyle()
        self.clearButton.syncStyle()

    def _update_clone_hint(self, path: str):
        if path:
            if Path(path).exists():
                self.cloneHintLabel.clear()
            else:
                self.cloneHintLabel.setText(self.tr("参考音频文件不存在，请重新选择或清除。"))
        else:
            self.cloneHintLabel.clear()

    def _sync_clone_state(self):
        if not self._clone_available:
            self.cloneSection.hide()
            self.updateGeometry()
            return
        has_audio = bool(self._clone_audio_path)
        self._update_clone_hint(self._clone_audio_path)
        self.cloneTextLabel.setVisible(has_audio)
        self.cloneTextInput.setVisible(has_audio)
        self.cloneHintLabel.setVisible(bool(self.cloneHintLabel.text()))
        self.playButton.setEnabled(self._clone_audio_exists())
        self.clearButton.setEnabled(has_audio)
        self.cloneSection.updateGeometry()
        self.updateGeometry()
        self.layoutChanged.emit()

    def _clone_audio_exists(self) -> bool:
        return bool(self._clone_audio_path) and Path(self._clone_audio_path).exists()

    def _update_count(self):
        self.countLabel.setText(self.tr("{count} 字").format(count=len(self.text())))


class DubbingInterface(ScrollArea):
    """配音音色库与试听页。"""

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setWindowTitle(self.tr("配音"))
        self.preview_thread: VoicePreviewThread | None = None
        self.player = QMediaPlayer(self)
        self.player.stateChanged.connect(self._on_player_state_changed)
        self.recorder = QAudioRecorder(self)
        self._recording_output_path: Path | None = None
        self.scrollWidget = QWidget()
        self.contentLayout = QVBoxLayout(self.scrollWidget)
        self.providerCards: dict[str, SelectableCard] = {}
        self.genderFilter = "全部"
        self._active_preview_button: QWidget | None = None  # 合成中的按钮
        self._playing_button: QWidget | None = None  # 播放中的按钮（可点停止）
        self._active_preview_cache_key: tuple[str, ...] | None = None
        self._preview_cache: dict[tuple[str, ...], str] = {}

        self._init_ui()
        self._connect_signals()
        self._setup_recorder()
        self._on_provider_changed(cfg.dubbing_provider.value)

    def _init_ui(self):
        self.resize(1200, 820)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)  # type: ignore
        self.setViewportMargins(0, 0, 0, 0)
        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        self.setObjectName("dubbingInterface")
        self.scrollWidget.setObjectName("scrollWidget")
        self.enableTransparentBackground()

        # 页头：标题 + 描述，随内容滚动（与批量处理页 pageTitle/pageSubtitle
        # 同款）；不再用浮动绝对定位 + viewport 顶边距的旧写法。
        self.headerWidget = QWidget(self.scrollWidget)
        headLayout = QVBoxLayout(self.headerWidget)
        headLayout.setContentsMargins(0, 0, 0, 0)
        headLayout.setSpacing(3)
        self.titleLabel = QLabel(self.tr("配音"), self.headerWidget)
        self.titleLabel.setObjectName("pageTitle")
        apply_font(self.titleLabel, 26, 860)
        self.subtitleLabel = QLabel(
            self.tr("选择提供商和音色，输入一句自己的试听文案。"), self.headerWidget
        )
        self.subtitleLabel.setObjectName("pageSubtitle")
        apply_font(self.subtitleLabel, 13, 720)
        headLayout.addWidget(self.titleLabel)
        headLayout.addWidget(self.subtitleLabel)

        self.providerPanel = QWidget(self.scrollWidget)
        self.providerPanel.setFixedHeight(PROVIDER_HEIGHT)
        providerLayout = QHBoxLayout(self.providerPanel)
        providerLayout.setContentsMargins(0, 0, 0, 0)
        providerLayout.setSpacing(12)
        for option in DUBBING_PROVIDERS:
            card = SelectableCard(
                option.key,
                self.tr(option.title),
                self.tr(option.description),
                _PROVIDER_ICONS.get(option.key),
                self.providerPanel,
            )
            card.clicked.connect(self._on_provider_changed)
            providerLayout.addWidget(card, 1)
            self.providerCards[option.key] = card

        self.bodyPanel = QWidget(self.scrollWidget)
        bodyLayout = QHBoxLayout(self.bodyPanel)
        bodyLayout.setContentsMargins(0, 0, 0, 0)
        bodyLayout.setSpacing(BODY_GAP)
        self.voiceTable = VoiceTable(self.bodyPanel)
        self.sidePanel = QWidget(self.bodyPanel)
        sideLayout = QVBoxLayout(self.sidePanel)
        sideLayout.setContentsMargins(0, 0, 0, 0)
        sideLayout.setSpacing(SECTION_GAP)
        self.previewPanel = PreviewPanel(self.sidePanel)
        sideLayout.addWidget(self.previewPanel)
        sideLayout.addStretch(1)
        bodyLayout.addWidget(self.voiceTable, 1, Qt.AlignTop)  # type: ignore
        bodyLayout.addWidget(self.sidePanel, 0, Qt.AlignTop)  # type: ignore

        self.contentLayout.setSpacing(SECTION_GAP)
        self.contentLayout.setContentsMargins(PAGE_MARGIN_X, 20, PAGE_MARGIN_X, 22)
        self.contentLayout.addWidget(self.headerWidget)
        self.contentLayout.addWidget(self.providerPanel)
        self.contentLayout.addWidget(self.bodyPanel)
        self.contentLayout.addStretch(1)  # 内容不足一屏时顶部对齐，不被 QVBoxLayout 撑开

    def _connect_signals(self):
        self.voiceTable.filterTabs.changed.connect(self._on_gender_filter)
        self.voiceTable.previewRequested.connect(self._preview)
        self.voiceTable.selectedRequested.connect(self._apply_preset)
        self.previewPanel.customPreviewRequested.connect(self._preview_custom_text)
        self.previewPanel.chooseAudioRequested.connect(self._choose_clone_audio)
        self.previewPanel.playAudioRequested.connect(self._play_clone_audio)
        self.previewPanel.recordRequested.connect(self._toggle_clone_recording)
        self.previewPanel.clearRequested.connect(self._clear_clone_audio)
        self.previewPanel.layoutChanged.connect(self._refresh_body_layout)
        self.previewPanel.cloneTextChanged.connect(
            lambda text: cfg.set(cfg.dubbing_clone_text, text, save=False)
        )

    def _setup_recorder(self):
        settings = QAudioEncoderSettings()
        settings.setCodec("audio/pcm")
        settings.setSampleRate(16000)
        settings.setChannelCount(1)
        settings.setQuality(QMultimedia.NormalQuality)
        self.recorder.setEncodingSettings(settings)
        self.recorder.stateChanged.connect(self._on_recording_state_changed)

    def closeEvent(self, event):
        # 退出/切走时停掉试听网络线程、播放器与录音：main_window.closeEvent 会 close()
        # 本页，若 preview_thread 仍在跑，销毁 running QThread 会触发 qFatal。
        # 这是只读网络线程，terminate 安全。
        if self.preview_thread is not None and self.preview_thread.isRunning():
            self.preview_thread.terminate()
            self.preview_thread.wait(1000)
        self.player.stop()
        if self.recorder.state() == QMediaRecorder.RecordingState:
            self.recorder.stop()
        super().closeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_page_background()
        self._on_provider_changed(cfg.dubbing_provider.value)

    def _sync_page_background(self):
        palette = app_palette()
        style = f"""
            QScrollArea {{
                border: none;
                background: {palette.bg};
            }}
            QWidget#scrollWidget {{
                background: {palette.bg};
            }}
            #providerCard, #previewPanel {{
                background: {palette.panel};
                border: 1px solid {palette.line};
                border-radius: 14px;
            }}
            #providerCard[selected="true"] {{
                background: {palette.selected};
                border: 1px solid {palette.accent};
            }}
            QFrame#voiceTable {{
                background: {palette.panel};
                border: 1px solid {palette.line_soft};
                border-radius: 14px;
            }}
            QFrame#voiceHeader {{
                background: transparent;
                border: none;
                border-bottom: 1px solid {palette.line_soft};
            }}
            QFrame#voiceRow {{
                background: {palette.panel};
                border: 1px solid {palette.line_soft};
            }}
            QFrame#voiceRow[selected="true"] {{
                background: {palette.selected};
                border: 1px solid {palette.accent};
            }}
            QTextEdit#previewInput {{
                color: {palette.text};
                background: {palette.field};
                border: 1px solid {palette.line};
                border-radius: 12px;
                padding: 14px;
            }}
            QTextEdit#cloneTextInput {{
                color: {palette.text};
                background: {palette.field};
                border: 1px solid {palette.line};
                border-radius: 12px;
                padding: 10px;
            }}
            QFrame#cloneSection {{
                background: {palette.field};
                border: 1px solid {palette.line_soft};
                border-radius: 12px;
            }}
            QFrame#cloneFileBox {{
                background: transparent;
                border: 1px solid {palette.line_soft};
                border-radius: 12px;
            }}
            QFrame#playerBar {{
                background: {palette.field};
                border: 1px solid {palette.line_soft};
                border-radius: 12px;
            }}
            QLabel {{
                color: {palette.text};
                background: transparent;
            }}
            QLabel#pageTitle {{ color: {palette.text}; background: transparent; }}
            QLabel#pageSubtitle {{ color: {palette.muted}; background: transparent; }}
            CaptionLabel {{
                color: {palette.muted};
            }}
            CaptionLabel#sampleHintLabel, CaptionLabel#sampleMetaLabel {{
                color: {palette.subtle};
            }}
        """
        self.setStyleSheet(style)
        self.scrollWidget.setStyleSheet(f"QWidget#scrollWidget {{ background: {palette.bg}; }}")
        self.previewPanel.syncStyle()

    def _on_provider_changed(self, provider: str):
        cfg.set(cfg.dubbing_provider, provider)
        option = get_provider_option(provider)
        if option.models and not cfg.dubbing_model.value:
            cfg.set(cfg.dubbing_model, option.models[0])
        self.previewPanel.setCloneAvailable(option.supports_clone)
        self.previewPanel.setAudioPath(cfg.dubbing_clone_audio.value)
        self.previewPanel.setCloneText(cfg.dubbing_clone_text.value)

        presets = get_provider_voices(provider)
        current = cfg.dubbing_preset.value
        if current not in {voice.preset for voice in presets}:
            preset = get_dubbing_preset(presets[0].preset)
            cfg.set(cfg.dubbing_preset, presets[0].preset)
            cfg.set(cfg.dubbing_voice, preset.voice)
            cfg.set(cfg.dubbing_model, preset.model)

        for key, card in self.providerCards.items():
            card.setActive(key == provider)
        self._sync_filter_visibility(presets)
        self._render_voice_table()
        self.contentLayout.update()

    def _sync_filter_visibility(self, voices: tuple[DubbingVoiceOption, ...]):
        supports_gender = any(GENDER_FILTER_TAGS.intersection(voice.tags) for voice in voices)
        if not supports_gender:
            self.genderFilter = "全部"
            self.voiceTable.filterTabs.setCurrent("全部")
        self.voiceTable.setFilterVisible(supports_gender)

    def _on_gender_filter(self, value: str):
        self.genderFilter = value
        self._render_voice_table()

    def _filtered_voices(self) -> list[DubbingVoiceOption]:
        voices = list(get_provider_voices(cfg.dubbing_provider.value))
        if self.genderFilter != "全部":
            voices = [voice for voice in voices if self.genderFilter in voice.tags]
        return voices

    def _render_voice_table(self):
        # 行控件即将重建：先释放对旧行试听按钮的引用，避免悬挂指针
        if isinstance(self._playing_button, AuditionButton):
            self._stop_playback()
        if isinstance(self._active_preview_button, AuditionButton):
            self._active_preview_button = None
        voices = self._filtered_voices()
        self.voiceTable.setVoices(voices, cfg.dubbing_preset.value)
        self._refresh_body_layout()

    def _refresh_body_layout(self):
        side_height = self.sidePanel.sizeHint().height()
        self.bodyPanel.setFixedHeight(max(self.voiceTable.height(), side_height))
        self.bodyPanel.updateGeometry()
        self.scrollWidget.adjustSize()
        self.viewport().update()

    def _choose_clone_audio(self):
        if not get_provider_option(cfg.dubbing_provider.value).supports_clone:
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("选择参考音频"),
            "",
            self.tr("音频文件 (*.wav *.mp3 *.m4a *.aac *.flac *.ogg *.opus);;所有文件 (*.*)"),
        )
        if not path:
            return
        cfg.set(cfg.dubbing_clone_audio, path, save=False)
        self.previewPanel.setAudioPath(path)
        self._discard_clone_preview_cache()
        self._refresh_body_layout()

    def _toggle_clone_recording(self):
        if not get_provider_option(cfg.dubbing_provider.value).supports_clone:
            return
        if self.recorder.state() == QMediaRecorder.RecordingState:
            self.recorder.stop()
            return

        output_dir = CACHE_PATH / "dubbing-clone"
        output_dir.mkdir(parents=True, exist_ok=True)
        self._recording_output_path = output_dir / "reference.wav"
        if self._recording_output_path.exists():
            self._recording_output_path.unlink()
        self.recorder.setOutputLocation(QUrl.fromLocalFile(str(self._recording_output_path)))
        self.recorder.record()
        self.previewPanel.setRecording(True)

    def _on_recording_state_changed(self, state: QMediaRecorder.State):
        recording = state == QMediaRecorder.RecordingState
        self.previewPanel.setRecording(recording)
        if recording or not self._recording_output_path:
            return
        if self._recording_output_path.exists() and self._recording_output_path.stat().st_size > 0:
            path = str(self._recording_output_path)
            cfg.set(cfg.dubbing_clone_audio, path, save=False)
            self.previewPanel.setAudioPath(path)
            self._discard_clone_preview_cache()
            self._refresh_body_layout()
            InfoBar.success(
                self.tr("录制完成"),
                self.tr("已保存为参考音频"),
                duration=INFOBAR_DURATION_SUCCESS,
                parent=self,
            )
        self._recording_output_path = None

    def _clear_clone_audio(self):
        if self.recorder.state() == QMediaRecorder.RecordingState:
            self.recorder.stop()
        cfg.set(cfg.dubbing_clone_audio, "", save=False)
        cfg.set(cfg.dubbing_clone_text, "", save=False)
        self.previewPanel.setAudioPath("")
        self.previewPanel.setCloneText("")
        self._discard_clone_preview_cache()
        self._refresh_body_layout()

    def _play_clone_audio(self):
        path = cfg.dubbing_clone_audio.value.strip()
        if not path or not Path(path).exists():
            InfoBar.warning(
                self.tr("参考音频不存在"),
                self.tr("请重新上传或录制参考音频。"),
                duration=3000,
                parent=self,
            )
            self.previewPanel.setAudioPath("")
            cfg.set(cfg.dubbing_clone_audio, "", save=False)
            self._refresh_body_layout()
            return
        if self.previewPanel.playButton is self._playing_button:
            self._stop_playback()
            return
        self._play_audio_file(path, self.previewPanel.playButton)

    def _apply_preset(self, preset_name: str):
        preset = get_dubbing_preset(preset_name)
        cfg.set(cfg.dubbing_provider, preset.provider)
        cfg.set(cfg.dubbing_preset, preset_name)
        cfg.set(cfg.dubbing_voice, preset.voice)
        cfg.set(cfg.dubbing_model, preset.model)
        if preset.api_base and not cfg.dubbing_api_base.value:
            cfg.set(cfg.dubbing_api_base, preset.api_base)
        self._on_provider_changed(preset.provider)

    def _preview_custom_text(self):
        text = self.previewPanel.text()
        if not text:
            InfoBar.warning(
                self.tr("请输入试听文本"),
                self.tr("文本试听会使用你输入的内容实时生成音频。"),
                duration=3000,
                parent=self,
            )
            return
        option = get_provider_option(cfg.dubbing_provider.value)
        clone_audio_path = cfg.dubbing_clone_audio.value.strip() if option.supports_clone else ""
        clone_audio_text = cfg.dubbing_clone_text.value.strip() if clone_audio_path else ""
        if clone_audio_path and not clone_audio_text:
            InfoBar.warning(
                self.tr("缺少参考文本"),
                self.tr("请填写参考音频里实际朗读的文字，或清除参考音频后普通试听。"),
                duration=3500,
                parent=self,
            )
            return
        self._preview(
            cfg.dubbing_preset.value,
            self.previewPanel.customPreviewButton,
            text=text,
            clone_audio_path=clone_audio_path,
            clone_audio_text=clone_audio_text,
        )

    # ------------------------------------------------- 试听按钮状态机
    # idle（试听）→ loading（合成中…，禁用）→ playing（停止，可点）→ idle。
    # 同一时刻只有一个按钮处于 loading 或 playing；点击播放中的按钮即停止。

    def _preview_idle_text(self, button: QWidget) -> str:
        if button is self.previewPanel.customPreviewButton:
            return self.tr("试听这句话")
        return self.tr("试听")

    def _set_preview_button(self, button: QWidget | None, state: str):
        if button is None:
            return
        if state == "loading":
            button.setEnabled(False)
            if hasattr(button, "setText"):
                button.setText(self.tr("合成中…"))
        elif state == "playing":
            button.setEnabled(True)
            if hasattr(button, "setText"):
                button.setText(self.tr("停止"))
            if hasattr(button, "setIcon"):
                button.setIcon(AppIcon.CANCEL)
        else:
            button.setEnabled(True)
            if hasattr(button, "setText"):
                button.setText(self._preview_idle_text(button))
            if hasattr(button, "setIcon"):
                button.setIcon(AppIcon.PLAY)

    def _stop_playback(self):
        if self._playing_button is not None:
            self._set_preview_button(self._playing_button, "idle")
            self._playing_button = None
        self.player.stop()

    def _on_player_state_changed(self, state):
        # 自然播完（或外部停止）：把"停止"复原成"试听"
        if state == QMediaPlayer.StoppedState and self._playing_button is not None:
            self._set_preview_button(self._playing_button, "idle")
            self._playing_button = None

    def _preview(
        self,
        preset_name: str,
        button: QWidget | None = None,
        *,
        text: str = "",
        clone_audio_path: str = "",
        clone_audio_text: str = "",
    ):
        if button is not None and button is self._playing_button:
            # 播放中点同一按钮 = 停止
            self._stop_playback()
            return
        if self.preview_thread and self.preview_thread.isRunning():
            InfoBar.info(
                self.tr("请稍候"),
                self.tr("正在合成另一段试听。"),
                duration=2000,
                parent=self,
            )
            return
        preset = get_dubbing_preset(preset_name)
        requires_api = text or clone_audio_path or clone_audio_text or not bundled_voice_preview(preset_name)
        if preset.provider != "edge" and not cfg.dubbing_api_key.value.strip() and requires_api:
            InfoBar.warning(
                self.tr("需要 API Key"),
                self.tr("自定义文本试听需要真实请求，请先填写当前配音服务的 API Key。"),
                duration=3500,
                parent=self,
            )
            return
        cache_key = self._preview_cache_key(
            preset_name,
            text=text,
            clone_audio_path=clone_audio_path,
            clone_audio_text=clone_audio_text,
        )
        cached_path = self._preview_cache.get(cache_key, "")
        if cached_path and Path(cached_path).exists():
            self._play_audio_file(cached_path, button)
            return
        self._active_preview_button = button
        self._active_preview_cache_key = cache_key
        self._set_preview_button(button, "loading")
        self.preview_thread = VoicePreviewThread(
            preset_name,
            text=text,
            clone_audio_path=clone_audio_path,
            clone_audio_text=clone_audio_text,
        )
        self.preview_thread.finished.connect(self._on_preview_finished)
        self.preview_thread.error.connect(self._on_preview_error)
        self.preview_thread.start()

    def _on_preview_finished(self, path: str):
        if self._active_preview_cache_key:
            self._preview_cache[self._active_preview_cache_key] = path
            self._active_preview_cache_key = None
        button = self._active_preview_button
        self._active_preview_button = None
        self._set_preview_button(button, "idle")
        # 播放状态由 _play_audio_file 接管（按钮翻成"停止"），不再弹成功通知
        self._play_audio_file(path, button)

    def _on_preview_error(self, message: str):
        self._active_preview_cache_key = None
        self._set_preview_button(self._active_preview_button, "idle")
        self._active_preview_button = None
        InfoBar.error(
            self.tr("试听失败"),
            message,
            duration=INFOBAR_DURATION_ERROR,
            parent=self,
        )

    def _play_audio_file(self, path: str, button: QWidget | None = None):
        playable_path = playable_voice_preview(Path(path))
        self._stop_playback()
        self.player.setMedia(QMediaContent(QUrl.fromLocalFile(str(playable_path))))
        self._playing_button = button
        self._set_preview_button(button, "playing")
        self.player.play()

    def _preview_cache_key(
        self,
        preset_name: str,
        *,
        text: str = "",
        clone_audio_path: str = "",
        clone_audio_text: str = "",
    ) -> tuple[str, ...]:
        audio_signature = ""
        if clone_audio_path:
            audio_file = Path(clone_audio_path)
            if audio_file.exists():
                stat = audio_file.stat()
                audio_signature = f"{audio_file.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
            else:
                audio_signature = clone_audio_path
        return (
            preset_name,
            cfg.dubbing_provider.value.strip(),
            cfg.dubbing_model.value.strip(),
            cfg.dubbing_voice.value.strip(),
            text.strip(),
            audio_signature,
            clone_audio_text.strip(),
        )

    def _discard_clone_preview_cache(self):
        self._preview_cache.clear()
