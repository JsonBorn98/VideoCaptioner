from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal
from qfluentwidgets import LineEdit, SettingCard
from qfluentwidgets.common.config import ConfigItem, qconfig


class LineEditSettingCard(SettingCard):
    """行输入卡片"""

    textChanged = pyqtSignal(str)

    def __init__(
        self,
        configItem: Optional[ConfigItem],
        icon,
        title: str,
        content: Optional[str] = None,
        placeholder: str = "",
        parent=None,
    ):
        super().__init__(icon, title, content, parent)

        self.configItem = configItem

        self.lineEdit = LineEdit(self)
        self.lineEdit.setPlaceholderText(placeholder)
        self.hBoxLayout.addWidget(self.lineEdit, 1, Qt.AlignRight)  # type: ignore
        self.hBoxLayout.addSpacing(16)

        self.lineEdit.setMinimumWidth(160)

        if configItem is not None:
            self.setValue(qconfig.get(configItem))

        self.lineEdit.textChanged.connect(self.__onTextChanged)
        if configItem is not None:
            configItem.valueChanged.connect(self.setValue)

    def __onTextChanged(self, text: str):
        self.setValue(text)
        self.textChanged.emit(text)

    def setValue(self, value: str):
        if self.configItem is not None:
            qconfig.set(self.configItem, value)
        self.lineEdit.setText(value)

    def setText(self, text: str):
        self.setValue(text)

    def text(self) -> str:
        return self.lineEdit.text()
