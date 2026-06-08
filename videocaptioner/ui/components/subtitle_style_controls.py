# coding:utf-8
from typing import List, Optional, Union

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor, QIcon
from PyQt5.QtWidgets import QColorDialog, QToolButton
from qfluentwidgets import ComboBox, CompactDoubleSpinBox, CompactSpinBox, LineEdit
from qfluentwidgets.common.icon import FluentIconBase

from videocaptioner.ui.common.theme_tokens import app_palette, rgba
from videocaptioner.ui.components.form_cards import FormCard

WIDE_CONTROL_WIDTH = 190
SHORT_CONTROL_WIDTH = 112
CONTROL_HEIGHT = 40


class SubtitleStyleRow(FormCard):
    """Base row for subtitle-style settings."""

    def __init__(
        self,
        icon: Union[str, QIcon, FluentIconBase],
        title: str,
        content: Optional[str] = None,
        parent=None,
    ):
        super().__init__(icon, title, "", parent)

    def setTitle(self, title: str):
        self.titleLabel.setText(title)

    def setValue(self, value):
        pass

    def setIconSize(self, width: int, height: int):
        self.iconWidget.setFixedSize(width, height)

    def syncStyle(self) -> None:  # noqa: N802
        palette = app_palette()
        self.setStyleSheet(
            f"""
            QFrame#formCard {{
                border: 0;
                border-radius: 0;
                background: transparent;
            }}
            QFrame#formCard:hover {{
                background: transparent;
            }}
            QLabel {{
                color: {palette.text};
                background: transparent;
            }}
            CaptionLabel {{
                color: {palette.muted};
            }}
            ComboBox, CompactSpinBox, CompactDoubleSpinBox, LineEdit {{
                color: {palette.text};
                background: {palette.field};
                border: 1px solid {palette.line_soft};
                border-radius: 8px;
                font-weight: 720;
            }}
            ComboBox:hover, CompactSpinBox:hover, CompactDoubleSpinBox:hover, LineEdit:hover {{
                border-color: {rgba(palette.accent, 0.70)};
            }}
            """
        )


class SubtitleStyleDoubleSpinRow(SubtitleStyleRow):
    """Float spin-box row."""

    valueChanged = pyqtSignal(float)

    def __init__(
        self,
        icon: Union[str, QIcon, FluentIconBase],
        title: str,
        content: Optional[str] = None,
        minimum: float = 0.0,
        maximum: float = 100.0,
        decimals: int = 1,
        parent=None,
    ):
        super().__init__(icon, title, content, parent)
        self.spinBox = CompactDoubleSpinBox(self)
        self.spinBox.setRange(minimum, maximum)
        self.spinBox.setDecimals(decimals)
        self.spinBox.setFixedWidth(SHORT_CONTROL_WIDTH)
        self.spinBox.setFixedHeight(CONTROL_HEIGHT)
        self.spinBox.setSingleStep(0.2)
        self.controlLayout.addWidget(self.spinBox)
        self.spinBox.valueChanged.connect(lambda value: self.valueChanged.emit(value))

    def setValue(self, value: float):
        self.spinBox.blockSignals(True)
        self.spinBox.setValue(float(value))
        self.spinBox.blockSignals(False)


class SubtitleStyleSpinRow(SubtitleStyleRow):
    """Integer spin-box row."""

    valueChanged = pyqtSignal(int)

    def __init__(
        self,
        icon: Union[str, QIcon, FluentIconBase],
        title: str,
        content: Optional[str] = None,
        minimum: int = 0,
        maximum: int = 100,
        step: int = 2,
        parent=None,
    ):
        super().__init__(icon, title, content, parent)
        self.spinBox = CompactSpinBox(self)
        self.spinBox.setRange(minimum, maximum)
        self.spinBox.setFixedWidth(SHORT_CONTROL_WIDTH)
        self.spinBox.setFixedHeight(CONTROL_HEIGHT)
        self.spinBox.setSingleStep(step)
        self.controlLayout.addWidget(self.spinBox)
        self.spinBox.valueChanged.connect(lambda value: self.valueChanged.emit(value))

    def setValue(self, value: int):
        self.spinBox.blockSignals(True)
        self.spinBox.setValue(int(value))
        self.spinBox.blockSignals(False)


class SubtitleStyleComboRow(SubtitleStyleRow):
    """Combo-box row."""

    currentTextChanged = pyqtSignal(str)
    currentIndexChanged = pyqtSignal(int)

    def __init__(
        self,
        icon: Union[str, QIcon, FluentIconBase],
        title: str,
        content: Optional[str] = None,
        texts: Optional[List[str]] = None,
        parent=None,
    ):
        super().__init__(icon, title, content, parent)
        self.comboBox = ComboBox(self)
        self.comboBox.setFixedWidth(WIDE_CONTROL_WIDTH)
        self.comboBox.setFixedHeight(CONTROL_HEIGHT)
        self.controlLayout.addWidget(self.comboBox)
        if texts:
            self.comboBox.addItems(texts)
        self.comboBox.currentTextChanged.connect(lambda text: self.currentTextChanged.emit(text))
        self.comboBox.currentIndexChanged.connect(lambda index: self.currentIndexChanged.emit(index))

    def setCurrentText(self, text: str):
        self.comboBox.setCurrentText(text)

    def setCurrentIndex(self, index: int):
        self.comboBox.setCurrentIndex(index)

    def addItem(self, text: str):
        self.comboBox.addItem(text)

    def addItems(self, texts: List[str]):
        self.comboBox.addItems(texts)

    def clear(self):
        self.comboBox.clear()


class SubtitleStyleLineEditRow(SubtitleStyleRow):
    """Single-line text row for preview or lightweight settings."""

    textChanged = pyqtSignal(str)

    def __init__(
        self,
        icon: Union[str, QIcon, FluentIconBase],
        title: str,
        content: Optional[str] = None,
        placeholder: str = "",
        parent=None,
    ):
        super().__init__(icon, title, content, parent)
        self.lineEdit = LineEdit(self)
        self.lineEdit.setFixedWidth(WIDE_CONTROL_WIDTH)
        self.lineEdit.setFixedHeight(CONTROL_HEIGHT)
        self.lineEdit.setPlaceholderText(placeholder)
        self.controlLayout.addWidget(self.lineEdit)
        self.lineEdit.textChanged.connect(lambda text: self.textChanged.emit(text))

    def text(self) -> str:
        return self.lineEdit.text()

    def setText(self, text: str) -> None:  # noqa: N802
        self.lineEdit.blockSignals(True)
        self.lineEdit.setText(text)
        self.lineEdit.blockSignals(False)


class SubtitleStyleColorRow(SubtitleStyleRow):
    """Color-picker row."""

    colorChanged = pyqtSignal(QColor)

    def __init__(
        self,
        color: QColor,
        icon: Union[str, QIcon, FluentIconBase],
        title: str,
        content: Optional[str] = None,
        parent=None,
        enableAlpha=False,
    ):
        super().__init__(icon, title, content, parent)
        self.colorPicker = SubtitleStyleColorButton(color, title, self, enableAlpha)
        self.controlLayout.addWidget(self.colorPicker)
        self.colorPicker.colorChanged.connect(self.colorChanged)

    def setColor(self, color: QColor):
        self.colorPicker.setColor(color)


class SubtitleStyleColorButton(QToolButton):
    colorChanged = pyqtSignal(QColor)

    def __init__(self, color: QColor, title: str, parent=None, enableAlpha=False):
        super().__init__(parent=parent)
        self.title = title
        self.enableAlpha = enableAlpha
        self.color = QColor(color)
        self.setFixedSize(SHORT_CONTROL_WIDTH, CONTROL_HEIGHT)
        self.setCursor(Qt.PointingHandCursor)
        self.clicked.connect(self._show_color_dialog)
        self._apply_color()

    def _show_color_dialog(self):
        dialog = QColorDialog(self.color, self.window())
        dialog.setWindowTitle(self.tr("Choose ") + self.title)
        if self.enableAlpha:
            dialog.setOption(QColorDialog.ShowAlphaChannel, True)
        if dialog.exec_() != QColorDialog.Accepted:
            return
        self._set_user_color(dialog.selectedColor())

    def setColor(self, color: QColor):
        self.color = QColor(color)
        self._apply_color()

    def _set_user_color(self, color: QColor):
        self.setColor(color)
        self.colorChanged.emit(self.color)

    def _apply_color(self):
        palette = app_palette()
        visible_color = QColor(self.color)
        if not self.enableAlpha:
            visible_color.setAlpha(255)
        self.setStyleSheet(
            "QToolButton {"
            f"background: {rgba(visible_color.name(QColor.HexRgb), visible_color.alphaF())};"
            f"border: 1px solid {palette.line};"
            "border-radius: 8px;"
            "}"
            "QToolButton:hover {"
            f"border: 1px solid {palette.accent};"
            "}"
        )
