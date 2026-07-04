from typing import List, Optional, Union

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QCompleter
from qfluentwidgets import EditableComboBox, SettingCard
from qfluentwidgets.common.config import ConfigItem, qconfig


class EditComboBoxSettingCard(SettingCard):
    """可编辑的下拉框设置卡片"""

    currentTextChanged = pyqtSignal(str)

    def __init__(
        self,
        configItem: Optional[ConfigItem],
        icon: Union[str, QIcon],
        title: str,
        content: Optional[str] = None,
        items: Optional[List[str]] = None,
        parent=None,
    ):
        super().__init__(icon, title, content, parent)

        self.configItem = configItem
        self.items = items or []

        # 创建可编辑的组合框
        self.comboBox = EditableComboBox(self)
        for item in self.items:
            self.comboBox.addItem(item)

        # 设置搜索功能
        self._setupCompleter()

        # 设置布局
        self.hBoxLayout.addWidget(self.comboBox, 1, Qt.AlignRight)  # type: ignore
        self.hBoxLayout.addSpacing(16)

        # 设置最小宽度
        self.comboBox.setMinimumWidth(160)

        # 设置初始值
        if configItem is not None:
            self.setValue(qconfig.get(configItem))

        # 连接信号
        self.comboBox.currentTextChanged.connect(self.__onTextChanged)
        if configItem is not None:
            configItem.valueChanged.connect(self.setValue)

    def _setupCompleter(self):
        """设置搜索自动完成功能"""
        if not self.items:
            self.comboBox.setCompleter(QCompleter([], self))
            return

        completer = QCompleter(self.items, self)
        completer.setCaseSensitivity(Qt.CaseInsensitive)  # type: ignore # 不区分大小写
        completer.setFilterMode(Qt.MatchContains)  # type: ignore # 包含匹配
        self.comboBox.setCompleter(completer)

    def __onTextChanged(self, text: str):
        """当文本改变时触发"""
        self.setValue(text)
        self.currentTextChanged.emit(text)

    def setValue(self, value: str):
        """设置值"""
        if self.configItem is not None:
            qconfig.set(self.configItem, value)
        self.comboBox.setText(value)

    def setCurrentText(self, text: str):
        """设置当前文本"""
        self.setValue(text)

    def currentText(self) -> str:
        """返回当前文本"""
        return self.comboBox.currentText()

    def addItem(self, item: str):
        """添加选项"""
        self.comboBox.addItem(item)
        self.items.append(item)
        self._setupCompleter()

    def addItems(self, items: List[str]):
        """添加选项"""
        for item in items:
            self.comboBox.addItem(item)
        self.items.extend(items)
        self._setupCompleter()

    def setItems(self, items: List[str]):
        """重新设置选项列表"""
        self.comboBox.clear()
        self.items = items
        for item in items:
            self.comboBox.addItem(item)
        self._setupCompleter()

    def clear(self):
        """清空所有选项"""
        self.comboBox.clear()
        self.items = []
        self._setupCompleter()
