# -*- coding: utf-8 -*-

from pathlib import Path

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, IconWidget, LineEdit, PushButton, setFont

from videocaptioner.ui.common.theme_tokens import app_palette, is_dark_theme, rgba

CONTROL_RADIUS = 7
PANEL_RADIUS = 8
CONTENT_GAP = 18
SECTION_GAP = 14


def _success_text_color() -> str:
    return app_palette().accent


class ClickableFrame(QFrame):
    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)  # type: ignore

    def mousePressEvent(self, event):
        if self.isEnabled() and event.button() == Qt.LeftButton:  # type: ignore
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


class PillLabel(QFrame):
    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("pillLabel")
        self._locked_width: int | None = None
        self.setFixedHeight(28)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(9, 0, 9, 0)
        layout.setSpacing(0)

        self.label = QLabel(text, self)
        self.label.setObjectName("pillText")
        self.label.setAlignment(Qt.AlignCenter)  # type: ignore
        setFont(self.label, 12, 860)
        layout.addWidget(self.label)
        self.syncStyle()

    def setText(self, text: str):
        self.label.setText(text)
        if self._locked_width is None:
            self.setMinimumWidth(max(54, len(text) * 13 + 22))
        else:
            super().setFixedWidth(self._locked_width)

    def setFixedWidth(self, width: int):
        self._locked_width = width
        super().setFixedWidth(width)

    def text(self) -> str:
        return self.label.text()

    def syncStyle(self):
        palette = app_palette()
        text_color = _success_text_color()
        self.setStyleSheet(
            f"""
            QFrame#pillLabel {{
                background: {palette.selected};
                border: 1px solid {palette.accent};
                border-radius: 14px;
            }}
            QLabel#pillText {{
                color: {text_color};
                background: transparent;
                border: none;
                font-weight: 860;
            }}
            """
        )


class StatusBadge(QLabel):
    """Compact status badge shared by modernized pages."""

    def __init__(self, text: str = "", parent=None, level: str = "neutral"):
        super().__init__(text, parent)
        self.setObjectName("statusBadge")
        self._level = level
        self._locked_width: int | None = None
        self.setAlignment(Qt.AlignCenter)  # type: ignore
        self.setFixedHeight(24)
        setFont(self, 12, 820)
        self.setText(text)
        self.syncStyle()

    def setText(self, text: str):
        super().setText(text)
        if self._locked_width is None:
            self.setMinimumWidth(max(70, len(text) * 13 + 22))
        else:
            super().setFixedWidth(self._locked_width)

    def setFixedWidth(self, width: int):
        self._locked_width = width
        super().setFixedWidth(width)

    def setLevel(self, level: str):
        self._level = level
        self.syncStyle()

    def syncStyle(self):
        palette = app_palette()
        success_text = _success_text_color()
        styles = {
            "success": (palette.selected, palette.accent, success_text),
            "danger": (rgba(palette.danger, 0.16), rgba(palette.danger, 0.42), palette.danger_fg),
            "warning": (palette.disabled, palette.line_soft, palette.muted),
            "neutral": (palette.disabled, palette.line_soft, palette.muted),
        }
        bg, border, fg = styles.get(self._level, styles["neutral"])
        self.setStyleSheet(
            f"""
            QLabel#statusBadge {{
                color: {fg};
                background: {bg};
                border: 1px solid {border};
                border-radius: 12px;
                font-weight: 820;
                padding: 0 9px;
            }}
            """
        )


class FilePathLineEdit(LineEdit):
    """Display only a file name while preserving the full path for business logic."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._actual_text = ""
        self._updating_display = False
        self.textChanged.connect(self._on_display_text_changed)

    def setText(self, text: str):
        self._actual_text = text
        display_text = Path(text).name if text and ("/" in text or "\\" in text) else text
        self._updating_display = True
        super().setText(display_text)
        self._updating_display = False
        self.setToolTip(text)

    def text(self) -> str:
        return self._actual_text or super().text()

    def clear(self):
        self._actual_text = ""
        super().clear()
        self.setToolTip("")

    def _on_display_text_changed(self, text: str):
        if not self._updating_display:
            self._actual_text = text
            self.setToolTip(text)


class ModernPanel(QFrame):
    def __init__(self, title: str, parent=None, pill: str = ""):
        super().__init__(parent)
        self.setObjectName("modernPanel")
        self.header = QFrame(self)
        self.header.setObjectName("panelHeader")
        self.body = QWidget(self)
        self.body.setObjectName("panelBody")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(14, 0, 14, 0)
        header_layout.setSpacing(10)
        self.titleLabel = BodyLabel(title, self.header)
        setFont(self.titleLabel, 15, 750)
        self.pill = PillLabel(pill, self.header)
        self.pill.setVisible(bool(pill))
        header_layout.addWidget(self.titleLabel)
        header_layout.addStretch(1)
        header_layout.addWidget(self.pill)
        self.header.setFixedHeight(48)

        self.bodyLayout = QVBoxLayout(self.body)
        self.bodyLayout.setContentsMargins(14, 14, 14, 14)
        self.bodyLayout.setSpacing(12)
        root.addWidget(self.header)
        root.addWidget(self.body)
        self.syncStyle()

    def setPillText(self, text: str):
        self.pill.setText(text)
        self.pill.setVisible(bool(text))

    def syncStyle(self):
        palette = app_palette()
        self.pill.syncStyle()
        self.setStyleSheet(
            f"""
            QFrame#modernPanel {{
                background: {palette.panel};
                border: 1px solid {palette.line_soft};
                border-radius: {PANEL_RADIUS}px;
            }}
            QFrame#panelHeader {{
                background: transparent;
                border-bottom: 1px solid {palette.line_soft};
            }}
            QWidget#panelBody {{
                background: transparent;
            }}
            """
        )


class SmallActionButton(ClickableFrame):
    def __init__(self, text: str, parent=None, icon=None, primary: bool = False):
        super().__init__(parent)
        self.primary = primary
        self.setObjectName("smallActionButton")
        self.setFixedHeight(40)
        self.setMinimumWidth(92)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(13, 0, 13, 0)
        layout.setSpacing(8)
        layout.addStretch(1)
        self.iconWidget = IconWidget(icon, self) if icon else None
        if self.iconWidget:
            self.iconWidget.setFixedSize(18, 18)
            layout.addWidget(self.iconWidget)
        self.label = QLabel(text, self)
        self.label.setObjectName("smallActionText")
        setFont(self.label, 13, 820)
        layout.addWidget(self.label)
        layout.addStretch(1)
        self.syncStyle()

    def setText(self, text: str):
        self.label.setText(text)

    def text(self) -> str:
        return self.label.text()

    def setEnabled(self, enabled: bool):
        super().setEnabled(enabled)
        self.syncStyle()

    def syncStyle(self):
        palette = app_palette()
        if not self.isEnabled():
            bg, fg, border = palette.disabled, palette.subtle, palette.line
        elif self.primary:
            bg, fg, border = palette.accent, palette.accent_fg, palette.accent
        elif is_dark_theme():
            bg, fg, border = "#303333", palette.text, palette.line
        else:
            bg, fg, border = "#f8faf9", palette.text, palette.line
        self.setStyleSheet(
            f"""
            QFrame#smallActionButton {{
                background: {bg};
                border: 1px solid {border};
                border-radius: {CONTROL_RADIUS}px;
            }}
            QLabel#smallActionText {{
                color: {fg};
                background: transparent;
                border: none;
            }}
            """
        )


class OutputCard(ClickableFrame):
    clicked = pyqtSignal(bool)

    def __init__(self, title: str, desc: str, icon, parent=None):
        super().__init__(parent)
        self._checked = False
        self.setObjectName("outputCard")
        self.setFixedHeight(92)

        layout = QGridLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(3)

        self.iconBox = QFrame(self)
        self.iconBox.setObjectName("outputIconBox")
        self.iconBox.setFixedSize(38, 38)
        icon_layout = QGridLayout(self.iconBox)
        icon_layout.setContentsMargins(7, 7, 7, 7)
        self.icon = IconWidget(icon, self.iconBox)
        self.icon.setFixedSize(22, 22)
        icon_layout.addWidget(self.icon, 0, 0, Qt.AlignCenter)  # type: ignore

        self.titleLabel = BodyLabel(title, self)
        self.descLabel = CaptionLabel(desc, self)
        self.descLabel.setWordWrap(True)
        setFont(self.titleLabel, 14, 760)
        setFont(self.descLabel, 12)
        layout.addWidget(self.iconBox, 0, 0, 2, 1, Qt.AlignTop)  # type: ignore
        layout.addWidget(self.titleLabel, 0, 1)
        layout.addWidget(self.descLabel, 1, 1)
        layout.setColumnStretch(1, 1)
        self.syncStyle()

    def setChecked(self, checked: bool):
        self._checked = checked
        self.syncStyle()

    def isChecked(self) -> bool:
        return self._checked

    def mousePressEvent(self, event):
        if self.isEnabled() and event.button() == Qt.LeftButton:  # type: ignore
            self.setChecked(not self._checked)
            self.clicked.emit(self._checked)
            event.accept()
            return
        super().mousePressEvent(event)

    def syncStyle(self):
        palette = app_palette()
        bg = palette.field
        border = palette.accent if self._checked else palette.line
        self.setStyleSheet(
            f"""
            QFrame#outputCard {{
                background: {bg};
                border: 1px solid {border};
                border-radius: {PANEL_RADIUS}px;
            }}
            QFrame#outputIconBox {{
                background: {palette.field};
                border: 1px solid {palette.line};
                border-radius: {CONTROL_RADIUS}px;
            }}
            """
        )


class FileRow(QFrame):
    def __init__(self, title: str, meta: str, placeholder: str, parent=None):
        super().__init__(parent)
        self.setObjectName("fileRow")
        self.setFixedHeight(58)

        layout = QGridLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(4)

        self.titleLabel = BodyLabel(title, self)
        self.metaLabel = CaptionLabel(meta, self)
        self.lineEdit = FilePathLineEdit(self)
        self.lineEdit.setPlaceholderText(placeholder)
        self.lineEdit.setAcceptDrops(True)
        self.button = PushButton(self.tr("浏览"), self)
        self.button.setFixedHeight(34)
        self.titleLabel.setFixedWidth(90)
        self.metaLabel.setFixedWidth(90)
        setFont(self.titleLabel, 13, 760)
        setFont(self.metaLabel, 11)

        if meta:
            layout.addWidget(self.titleLabel, 0, 0)
            layout.addWidget(self.metaLabel, 1, 0)
        else:
            self.metaLabel.hide()
            layout.addWidget(self.titleLabel, 0, 0, 2, 1, Qt.AlignVCenter)  # type: ignore
        layout.addWidget(self.lineEdit, 0, 1, 2, 1)
        layout.addWidget(self.button, 0, 2, 2, 1)
        layout.setColumnStretch(1, 1)
        self._ready = False
        self.syncStyle()

    def setReady(self, ready: bool):
        self._ready = ready
        self.syncStyle()

    def syncStyle(self):
        palette = app_palette()
        self.setStyleSheet(
            f"""
            QFrame#fileRow {{
                background: {palette.field};
                border: 1px solid {palette.line_soft};
                border-radius: {PANEL_RADIUS}px;
            }}
            """
        )


class WorkflowSettingRow(QFrame):
    def __init__(self, title: str, desc: str, control: QWidget, parent=None):
        super().__init__(parent)
        self.setObjectName("settingRow")
        self.setFixedHeight(54)

        layout = QGridLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(3)
        self.titleLabel = BodyLabel(title, self)
        self.descLabel = CaptionLabel(desc, self)
        self.descLabel.setWordWrap(True)
        setFont(self.titleLabel, 13, 760)
        setFont(self.descLabel, 11)
        layout.addWidget(self.titleLabel, 0, 0)
        layout.addWidget(self.descLabel, 1, 0)
        layout.addWidget(control, 0, 1, 2, 1, Qt.AlignRight | Qt.AlignVCenter)  # type: ignore
        layout.setColumnStretch(0, 1)
        self.syncStyle()

    def setDescription(self, text: str):
        self.descLabel.setText(text)

    def syncStyle(self):
        palette = app_palette()
        self.setStyleSheet(
            f"""
            QFrame#settingRow {{
                background: {palette.field};
                border: 1px solid {palette.line_soft};
                border-radius: {PANEL_RADIUS}px;
            }}
            """
        )
