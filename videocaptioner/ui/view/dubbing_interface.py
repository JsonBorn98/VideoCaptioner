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
    ExpandLayout,
    InfoBar,
    InfoLevel,
    ScrollArea,
    SimpleCardWidget,
    TitleLabel,
    setFont,
)

from videocaptioner.config import CACHE_PATH
from videocaptioner.core.constant import INFOBAR_DURATION_ERROR, INFOBAR_DURATION_SUCCESS
from videocaptioner.core.dubbing import get_dubbing_preset
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.common.dubbing_options import (
    DUBBING_PROVIDERS,
    DubbingProviderOption,
    DubbingVoiceOption,
    get_provider_option,
    get_provider_voices,
)
from videocaptioner.ui.common.theme_tokens import app_palette, is_dark_theme
from videocaptioner.ui.thread.voice_preview_thread import (
    VoicePreviewThread,
    bundled_voice_preview,
    playable_voice_preview,
)

CONTROL_RADIUS = 7
PANEL_RADIUS = 8
PAGE_MARGIN_X = 34
SECTION_GAP = 14
BODY_GAP = 18
PROVIDER_HEIGHT = 96
FILTER_HEIGHT = 60
TABLE_HEADER_HEIGHT = 44
VOICE_ROW_HEIGHT = 68
SQUARE_BUTTON_SIZE = 40
AUDITION_BUTTON_WIDTH = 92
PREVIEW_PANEL_WIDTH = 376
PREVIEW_PANEL_HEIGHT = 486
PREVIEW_PANEL_COMPACT_HEIGHT = 360
PREVIEW_PANEL_NO_CLONE_HEIGHT = 268
GENDER_FILTER_TAGS = {"女声", "男声"}


class MiniTag(QFrame):
    def __init__(self, text: str, parent=None, level: InfoLevel = InfoLevel.INFOAMTION):
        super().__init__(parent)
        self._level = level
        self.setObjectName("miniTag")
        self.setFixedHeight(24)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 10, 0)
        layout.setSpacing(0)

        self.label = QLabel(self)
        self.label.setObjectName("miniTagLabel")
        self.label.setAlignment(Qt.AlignCenter)  # type: ignore
        setFont(self.label, 11, 750)
        layout.addWidget(self.label)

        self.setText(text)
        self.setLevel(level)

    def text(self) -> str:
        return self.label.text()

    def setText(self, text: str):
        self.label.setText(text)
        self.setMinimumWidth(max(46, len(text) * 13 + 20))

    def setLevel(self, level: InfoLevel):
        self._level = level
        self._sync_style()

    def _sync_style(self):
        palette = app_palette()
        if self._level == InfoLevel.SUCCESS:
            bg, fg, border = palette.accent, palette.accent_fg, palette.accent
        elif self._level == InfoLevel.WARNING:
            bg, fg, border = palette.disabled, palette.muted, palette.disabled
        elif is_dark_theme():
            bg, fg, border = palette.disabled, palette.muted, palette.disabled
        else:
            bg, fg, border = palette.field, palette.muted, palette.line
        self.setStyleSheet(
            f"""
            QFrame#miniTag {{
                background: {bg};
                border: 1px solid {border};
                border-radius: 12px;
            }}
            QLabel#miniTagLabel {{
                color: {fg};
                background: transparent;
                border: none;
            }}
            """
        )


def _blend_color(foreground: str, background: str, alpha: float) -> QColor:
    fg = QColor(foreground)
    bg = QColor(background)
    if not fg.isValid():
        fg = QColor("#28f08b")
    if not bg.isValid():
        bg = QColor("#292b2b")
    alpha = max(0.0, min(1.0, alpha))
    return QColor(
        int(fg.red() * alpha + bg.red() * (1 - alpha)),
        int(fg.green() * alpha + bg.green() * (1 - alpha)),
        int(fg.blue() * alpha + bg.blue() * (1 - alpha)),
    )


class ThemedSimpleCard(SimpleCardWidget):
    """Simple card with project-owned colors instead of qfluent theme globals."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._selected_visual = False

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
        radius = self.getBorderRadius()
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), radius, radius)


class ProviderCard(ThemedSimpleCard):
    def __init__(self, option: DubbingProviderOption, parent=None):
        super().__init__(parent)
        self.option = option
        self.setObjectName("providerCard")
        self.setAttribute(Qt.WA_StyledBackground, True)  # type: ignore
        self.setBorderRadius(PANEL_RADIUS)
        self.setClickEnabled(True)
        self.setCursor(Qt.PointingHandCursor)  # type: ignore
        self.setFixedHeight(PROVIDER_HEIGHT)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(7)

        top = QHBoxLayout()
        top.setSpacing(10)
        self.titleLabel = BodyLabel(self.tr(option.title), self)
        setFont(self.titleLabel, 13, 700)
        self.badge = MiniTag("", self)
        self.badge.hide()
        top.addWidget(self.titleLabel)
        top.addStretch(1)
        top.addWidget(self.badge)

        self.descLabel = CaptionLabel(self.tr(option.description), self)
        self.descLabel.setWordWrap(True)
        self.descLabel.setFixedHeight(34)

        layout.addLayout(top)
        layout.addWidget(self.descLabel)

    def setSelected(self, selected: bool):
        self.setSelectedVisual(selected)
        if selected:
            self.badge.setText(self.tr("已选"))
            self.badge.setLevel(InfoLevel.SUCCESS)
        else:
            self.badge.setText("")
        self.badge.setVisible(selected)
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)
        palette = app_palette()
        self.titleLabel.setStyleSheet(f"color: {palette.text}; background: transparent;")
        self.descLabel.setStyleSheet(f"color: {palette.muted}; background: transparent;")

class ClickableFrame(QFrame):
    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)  # type: ignore

    def mousePressEvent(self, event):
        if self.isEnabled() and event.button() == Qt.LeftButton:  # type: ignore
            self.clicked.emit()
        super().mousePressEvent(event)


class FilterButton(ClickableFrame):
    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self._checked = False
        self.setObjectName("filterButton")
        self.setFixedHeight(34)
        self.setMinimumWidth(58)

        layout = QGridLayout(self)
        layout.setContentsMargins(12, 0, 12, 0)
        self.label = QLabel(text, self)
        self.label.setObjectName("filterButtonLabel")
        self.label.setAlignment(Qt.AlignCenter)  # type: ignore
        setFont(self.label, 13, 750)
        layout.addWidget(self.label, 0, 0, Qt.AlignCenter)  # type: ignore
        self._sync_style()

    def setChecked(self, checked: bool):
        self._checked = checked
        self._sync_style()

    def isChecked(self) -> bool:
        return self._checked

    def text(self) -> str:
        return self.label.text()

    def _sync_style(self):
        palette = app_palette()
        if self._checked:
            bg, fg, border = palette.accent, palette.accent_fg, palette.accent
        else:
            bg, fg, border = palette.field, palette.muted, palette.line
        self.setStyleSheet(
            f"""
            QFrame#filterButton {{
                background: {bg};
                border: 1px solid {border};
                border-radius: {CONTROL_RADIUS}px;
            }}
            QLabel#filterButtonLabel {{
                color: {fg};
                background: transparent;
                border: none;
            }}
            """
        )


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
        setFont(self.label, 13, 750)
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


class PrimaryActionButton(ClickableFrame):
    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setObjectName("primaryActionButton")
        self.setFixedHeight(SQUARE_BUTTON_SIZE)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addStretch(1)
        self.label = QLabel(text, self)
        self.label.setObjectName("primaryActionLabel")
        setFont(self.label, 13, 850)
        layout.addWidget(self.label)
        layout.addStretch(1)
        self._sync_style()

    def setText(self, text: str):
        self.label.setText(text)

    def text(self) -> str:
        return self.label.text()

    def setEnabled(self, enabled: bool):
        super().setEnabled(enabled)
        self._sync_style()

    def _sync_style(self):
        palette = app_palette()
        if self.isEnabled():
            bg, fg, border = palette.accent, palette.accent_fg, palette.accent
        else:
            bg, fg, border = palette.disabled, palette.subtle, palette.line
        self.setStyleSheet(
            f"""
            QFrame#primaryActionButton {{
                background: {bg};
                border: 1px solid {border};
                border-radius: {CONTROL_RADIUS}px;
            }}
            QLabel#primaryActionLabel {{
                color: {fg};
                background: transparent;
                border: none;
            }}
            """
        )


class SecondaryActionButton(ClickableFrame):
    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setObjectName("secondaryActionButton")
        self.setFixedHeight(SQUARE_BUTTON_SIZE)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addStretch(1)
        self.label = QLabel(text, self)
        self.label.setObjectName("secondaryActionLabel")
        setFont(self.label, 13, 800)
        layout.addWidget(self.label)
        layout.addStretch(1)
        self._sync_style()

    def setText(self, text: str):
        self.label.setText(text)

    def text(self) -> str:
        return self.label.text()

    def setEnabled(self, enabled: bool):
        super().setEnabled(enabled)
        self._sync_style()

    def _sync_style(self):
        palette = app_palette()
        if not self.isEnabled():
            bg, fg, border = palette.disabled, palette.subtle, palette.line
        else:
            bg, fg, border = palette.field, palette.text, palette.line
        self.setStyleSheet(
            f"""
            QFrame#secondaryActionButton {{
                background: {bg};
                border: 1px solid {border};
                border-radius: {CONTROL_RADIUS}px;
            }}
            QLabel#secondaryActionLabel {{
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
        setFont(self.titleLabel, 13, 700)
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
        header = QFrame(self)
        header.setObjectName("voiceHeader")
        header.setFixedHeight(TABLE_HEADER_HEIGHT)
        layout = QGridLayout(header)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setHorizontalSpacing(18)
        layout.setColumnStretch(0, 1)
        layout.setColumnMinimumWidth(1, AUDITION_BUTTON_WIDTH)
        for column, text in [(0, self.tr("音色")), (1, self.tr("试听"))]:
            label = CaptionLabel(text, header)
            setFont(label, 11, 700)
            align = Qt.AlignRight | Qt.AlignVCenter if column == 1 else Qt.AlignVCenter
            layout.addWidget(label, 0, column, align)  # type: ignore
        self.layout.addWidget(header)

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
        self.setFixedHeight(PREVIEW_PANEL_HEIGHT)
        self._clone_available = True
        self._clone_audio_path = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 15, 16, 15)
        layout.setSpacing(9)

        header = QHBoxLayout()
        self.titleLabel = BodyLabel(self.tr("配音文案"), self)
        setFont(self.titleLabel, 16, 700)
        self.hintLabel = CaptionLabel(self.tr("用户可自行输入"), self)
        self.hintLabel.setObjectName("sampleHintLabel")
        setFont(self.hintLabel, 11)
        self.hintLabel.hide()
        header.addWidget(self.titleLabel)
        header.addStretch(1)
        header.addWidget(self.hintLabel)

        self.previewInput = QTextEdit(self)
        self.previewInput.setObjectName("previewInput")
        self.previewInput.setPlaceholderText(self.tr("输入一句话，试听选中的音色"))
        self.previewInput.setFixedHeight(104)
        self.previewInput.setText(self.tr("你好，这是我想用于测试的配音文案。请用自然清晰的语气朗读这一句话。"))
        setFont(self.previewInput, 13, 700)

        meta = QHBoxLayout()
        meta.setContentsMargins(0, 0, 0, 0)
        meta.setSpacing(8)
        self.metaLabel = CaptionLabel(self.tr("建议 10-80 字，试听更快"), self)
        self.countLabel = CaptionLabel("", self)
        self.metaLabel.setObjectName("sampleMetaLabel")
        self.countLabel.setObjectName("sampleMetaLabel")
        self.countLabel.setMinimumWidth(50)
        self.countLabel.setAlignment(Qt.AlignRight | Qt.AlignVCenter)  # type: ignore
        setFont(self.metaLabel, 11)
        setFont(self.countLabel, 11)
        meta.addStretch(1)
        meta.addWidget(self.countLabel)
        self.metaLabel.hide()

        self.customPreviewButton = PrimaryActionButton(self.tr("试听这句话"), self)

        self.cloneSection = QFrame(self)
        self.cloneSection.setObjectName("cloneSection")
        cloneLayout = QVBoxLayout(self.cloneSection)
        cloneLayout.setContentsMargins(12, 12, 12, 12)
        cloneLayout.setSpacing(8)

        cloneHeader = QHBoxLayout()
        cloneTitle = BodyLabel(self.tr("声音克隆"), self.cloneSection)
        setFont(cloneTitle, 15, 700)
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

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        self.chooseButton = SecondaryActionButton(self.tr("上传音频"), self.cloneSection)
        self.playButton = AuditionButton(self.tr("试听"), self.cloneSection)
        self.recordButton = AuditionButton(self.tr("录制"), self.cloneSection)
        self.clearButton = AuditionButton(self.tr("清除"), self.cloneSection)
        self.playButton.setFixedWidth(74)
        self.recordButton.setFixedWidth(92)
        self.clearButton.setFixedWidth(74)
        actions.addWidget(self.chooseButton, 1)
        actions.addWidget(self.playButton)
        actions.addWidget(self.recordButton)
        actions.addWidget(self.clearButton)

        self.cloneTextLabel = CaptionLabel(self.tr("参考文本"), self.cloneSection)
        self.cloneTextLabel.setObjectName("sampleMetaLabel")
        self.cloneTextInput = QTextEdit(self.cloneSection)
        self.cloneTextInput.setObjectName("cloneTextInput")
        self.cloneTextInput.setPlaceholderText(self.tr("输入参考音频里实际朗读的文字"))
        self.cloneTextInput.setFixedHeight(48)
        setFont(self.cloneTextInput, 12, 650)

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
            self.setFixedHeight(PREVIEW_PANEL_NO_CLONE_HEIGHT)
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
            self.setFixedHeight(PREVIEW_PANEL_NO_CLONE_HEIGHT)
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
        self.customPreviewButton._sync_style()
        self.chooseButton._sync_style()
        self.playButton._sync_style()
        self.recordButton._sync_style()
        self.clearButton._sync_style()

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
            self.setFixedHeight(PREVIEW_PANEL_NO_CLONE_HEIGHT)
            self.updateGeometry()
            return
        has_audio = bool(self._clone_audio_path)
        self._update_clone_hint(self._clone_audio_path)
        self.cloneTextLabel.setVisible(has_audio)
        self.cloneTextInput.setVisible(has_audio)
        self.cloneHintLabel.setVisible(bool(self.cloneHintLabel.text()))
        self.playButton.setEnabled(self._clone_audio_exists())
        self.clearButton.setEnabled(has_audio)
        self.setFixedHeight(PREVIEW_PANEL_HEIGHT if has_audio else PREVIEW_PANEL_COMPACT_HEIGHT)
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
        self.recorder = QAudioRecorder(self)
        self._recording_output_path: Path | None = None
        self.scrollWidget = QWidget()
        self.expandLayout = ExpandLayout(self.scrollWidget)
        self.titleLabel = TitleLabel(self.tr("配音"), self)
        self.subtitleLabel = CaptionLabel(self.tr("选择提供商和音色，输入一句自己的试听文案。"), self)
        self.providerCards: dict[str, ProviderCard] = {}
        self.genderFilters: dict[str, FilterButton] = {}
        self.genderFilter = "全部"
        self._active_preview_button: QWidget | None = None
        self._active_preview_cache_key: tuple[str, ...] | None = None
        self._preview_cache: dict[tuple[str, ...], str] = {}

        self._init_ui()
        self._connect_signals()
        self._setup_recorder()
        self._on_provider_changed(cfg.dubbing_provider.value)

    def _init_ui(self):
        self.resize(1200, 820)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)  # type: ignore
        self.setViewportMargins(0, 46, 0, 20)
        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        self.setObjectName("dubbingInterface")
        self.scrollWidget.setObjectName("scrollWidget")
        self.titleLabel.setObjectName("settingLabel")
        self.titleLabel.move(36, 10)
        self.subtitleLabel.hide()
        self.enableTransparentBackground()

        self.providerPanel = QWidget(self.scrollWidget)
        self.providerPanel.setFixedHeight(PROVIDER_HEIGHT)
        providerLayout = QHBoxLayout(self.providerPanel)
        providerLayout.setContentsMargins(0, 0, 0, 0)
        providerLayout.setSpacing(12)
        for option in DUBBING_PROVIDERS:
            card = ProviderCard(option, self.providerPanel)
            card.clicked.connect(lambda key=option.key: self._on_provider_changed(key))
            providerLayout.addWidget(card, 1)
            self.providerCards[option.key] = card

        self.filterPanel = QFrame(self.scrollWidget)
        self.filterPanel.setObjectName("filterPanel")
        self.filterPanel.setFixedHeight(FILTER_HEIGHT)
        filterLayout = QHBoxLayout(self.filterPanel)
        filterLayout.setContentsMargins(12, 12, 12, 12)
        filterLayout.setSpacing(10)
        self.genderFilterLabel = CaptionLabel(self.tr("声线"), self.filterPanel)
        filterLayout.addWidget(self.genderFilterLabel)
        for value in ("全部", "女声", "男声"):
            button = self._create_filter_button(value, self.filterPanel)
            button.clicked.connect(lambda _checked=False, v=value: self._on_gender_filter(v))
            self.genderFilters[value] = button
            filterLayout.addWidget(button)
        filterLayout.addStretch(1)

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

        self.expandLayout.setSpacing(SECTION_GAP)
        self.expandLayout.setContentsMargins(PAGE_MARGIN_X, 0, PAGE_MARGIN_X, 0)
        self.expandLayout.addWidget(self.providerPanel)
        self.expandLayout.addWidget(self.filterPanel)
        self.expandLayout.addWidget(self.bodyPanel)

    def _create_filter_button(self, text: str, parent: QWidget) -> FilterButton:
        button = FilterButton(text, parent)
        return button

    def _connect_signals(self):
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
                border-radius: 8px;
            }}
            #providerCard[selected="true"] {{
                background: {palette.selected};
                border: 1px solid {palette.accent};
            }}
            QFrame#filterPanel, QFrame#voiceTable {{
                background: {palette.panel};
                border: 1px solid {palette.line_soft};
                border-radius: 8px;
            }}
            QFrame#voiceHeader {{
                background: {palette.header};
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
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
                border-radius: 8px;
                padding: 14px;
            }}
            QTextEdit#cloneTextInput {{
                color: {palette.text};
                background: {palette.field};
                border: 1px solid {palette.line};
                border-radius: 8px;
                padding: 10px;
            }}
            QFrame#cloneSection {{
                background: {palette.field};
                border: 1px solid {palette.line_soft};
                border-radius: 8px;
            }}
            QFrame#cloneFileBox {{
                background: transparent;
                border: 1px solid {palette.line_soft};
                border-radius: 8px;
            }}
            QFrame#playerBar {{
                background: {palette.field};
                border: 1px solid {palette.line_soft};
                border-radius: 8px;
            }}
            QLabel {{
                color: {palette.text};
                background: transparent;
            }}
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
            card.setSelected(key == provider)
        self._sync_filter_visibility(presets)
        self._refresh_filters()
        self._render_voice_table()
        self.expandLayout.update()

    def _sync_filter_visibility(self, voices: tuple[DubbingVoiceOption, ...]):
        supports_gender = any(GENDER_FILTER_TAGS.intersection(voice.tags) for voice in voices)
        if not supports_gender:
            self.genderFilter = "全部"
        self.filterPanel.setVisible(supports_gender)

    def _refresh_filters(self):
        for value, button in self.genderFilters.items():
            selected = value == self.genderFilter
            button.setChecked(selected)

    def _on_gender_filter(self, value: str):
        self.genderFilter = value
        self._refresh_filters()
        self._render_voice_table()

    def _filtered_voices(self) -> list[DubbingVoiceOption]:
        voices = list(get_provider_voices(cfg.dubbing_provider.value))
        if self.genderFilter != "全部":
            voices = [voice for voice in voices if self.genderFilter in voice.tags]
        return voices

    def _render_voice_table(self):
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
        self._play_audio_file(path)

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

    def _preview(
        self,
        preset_name: str,
        button: QWidget | None = None,
        *,
        text: str = "",
        clone_audio_path: str = "",
        clone_audio_text: str = "",
    ):
        if self.preview_thread and self.preview_thread.isRunning():
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
            self._play_audio_file(cached_path)
            return
        self._active_preview_button = button
        self._active_preview_cache_key = cache_key
        if button:
            button.setEnabled(False)
            if hasattr(button, "setText"):
                button.setText(self.tr("试听中..."))
        self.preview_thread = VoicePreviewThread(
            preset_name,
            text=text,
            clone_audio_path=clone_audio_path,
            clone_audio_text=clone_audio_text,
        )
        self.preview_thread.finished.connect(self._on_preview_finished)
        self.preview_thread.error.connect(self._on_preview_error)
        self.preview_thread.start()

    def _reset_preview_buttons(self):
        self.previewPanel.customPreviewButton.setEnabled(True)
        self.previewPanel.customPreviewButton.setText(self.tr("试听这句话"))
        if self._active_preview_button and self._active_preview_button is not self.previewPanel.customPreviewButton:
            self._active_preview_button.setEnabled(True)
            if hasattr(self._active_preview_button, "setText"):
                self._active_preview_button.setText(self.tr("试听"))
        self._active_preview_button = None

    def _on_preview_finished(self, path: str):
        if self._active_preview_cache_key:
            self._preview_cache[self._active_preview_cache_key] = path
            self._active_preview_cache_key = None
        self._reset_preview_buttons()
        self._play_audio_file(path)
        InfoBar.success(
            self.tr("开始播放"),
            self.tr("正在播放：{name}").format(name=Path(path).name),
            duration=INFOBAR_DURATION_SUCCESS,
            parent=self,
        )

    def _on_preview_error(self, message: str):
        self._active_preview_cache_key = None
        self._reset_preview_buttons()
        InfoBar.error(
            self.tr("试听失败"),
            message,
            duration=INFOBAR_DURATION_ERROR,
            parent=self,
        )

    def _play_audio_file(self, path: str):
        playable_path = playable_voice_preview(Path(path))
        self.player.stop()
        self.player.setMedia(QMediaContent(QUrl.fromLocalFile(str(playable_path))))
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
