from typing import Optional, Union

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QIcon
from qfluentwidgets import CompactDoubleSpinBox, CompactSpinBox, FluentIconBase, SettingCard, Slider
from qfluentwidgets.common.config import ConfigItem, qconfig

_Icon = Union[str, QIcon, FluentIconBase]


class DoubleSpinBoxSettingCard(SettingCard):
    """小数输入设置卡片"""

    valueChanged = pyqtSignal(float)

    def __init__(
        self,
        configItem: ConfigItem,
        icon: _Icon,
        title: str,
        content: Optional[str] = None,
        minimum: float = 0.0,
        maximum: float = 100.0,
        decimals: int = 1,
        step: float = 0.1,
        parent=None,
    ):
        super().__init__(icon, title, content, parent)

        self.configItem = configItem

        # 创建CompactDoubleSpinBox
        self.spinBox = CompactDoubleSpinBox(self)
        self.spinBox.setRange(minimum, maximum)
        self.spinBox.setDecimals(decimals)
        self.spinBox.setMinimumWidth(60)
        self.spinBox.setSingleStep(step)  # 设置步长为0.2

        # 添加到布局
        self.hBoxLayout.addWidget(self.spinBox, 0, Qt.AlignRight)  # type: ignore
        self.hBoxLayout.addSpacing(8)

        # 设置初始值和连接信号
        self.setValue(qconfig.get(configItem))
        self.spinBox.valueChanged.connect(self.__onValueChanged)
        configItem.valueChanged.connect(self.setValue)

    def __onValueChanged(self, value: float):
        """数值改变时的槽函数"""
        self.setValue(value)
        self.valueChanged.emit(value)

    def setValue(self, value: float):
        """设置数值"""
        qconfig.set(self.configItem, value)
        self.spinBox.setValue(value)


class SpinBoxSettingCard(SettingCard):
    """数值输入设置卡片"""

    valueChanged = pyqtSignal(int)

    def __init__(
        self,
        configItem: ConfigItem,
        icon: _Icon,
        title: str,
        content: Optional[str] = None,
        minimum: int = 0,
        maximum: int = 100,
        parent=None,
    ):
        super().__init__(icon, title, content, parent)

        self.configItem = configItem

        # 创建SpinBox
        self.spinBox = CompactSpinBox(self)
        self.spinBox.setRange(minimum, maximum)
        self.spinBox.setMinimumWidth(60)

        # 添加到布局
        self.hBoxLayout.addWidget(self.spinBox, 0, Qt.AlignRight)  # type: ignore
        self.hBoxLayout.addSpacing(8)

        # 设置初始值和连接信号
        self.setValue(qconfig.get(configItem))
        self.spinBox.valueChanged.connect(self.__onValueChanged)
        configItem.valueChanged.connect(self.setValue)

    def __onValueChanged(self, value: int):
        """数值改变时的槽函数"""
        self.setValue(value)
        self.valueChanged.emit(value)

    def setValue(self, value: int):
        """设置数值"""
        qconfig.set(self.configItem, value)
        self.spinBox.setValue(value)


class SliderSpinBoxSettingCard(SettingCard):
    """滑块 + 数值输入设置卡片（可拖动，也可精确键入）。

    两个控件与同一 ConfigItem 三方同步；支持运行时 :meth:`setRange` 动态调整可选范围，
    用于多旋钮联动钳制（改一个，其余可选范围实时收紧）。
    """

    valueChanged = pyqtSignal(int)

    def __init__(
        self,
        configItem: ConfigItem,
        icon: _Icon,
        title: str,
        content: Optional[str] = None,
        minimum: int = 0,
        maximum: int = 100,
        step: int = 10,
        parent=None,
    ):
        super().__init__(icon, title, content, parent)

        self.configItem = configItem
        self._syncing = False

        self.slider = Slider(Qt.Horizontal, self)  # type: ignore[attr-defined]
        self.slider.setMinimumWidth(160)
        self.slider.setSingleStep(step)
        self.slider.setPageStep(step)
        self.slider.setRange(minimum, maximum)

        self.spinBox = CompactSpinBox(self)
        self.spinBox.setMinimumWidth(80)
        self.spinBox.setSingleStep(step)
        self.spinBox.setRange(minimum, maximum)

        self.hBoxLayout.addWidget(self.slider, 0, Qt.AlignRight)  # type: ignore
        self.hBoxLayout.addSpacing(12)
        self.hBoxLayout.addWidget(self.spinBox, 0, Qt.AlignRight)  # type: ignore
        self.hBoxLayout.addSpacing(8)

        self.setValue(qconfig.get(configItem))
        self.slider.valueChanged.connect(self.__onValueChanged)
        self.spinBox.valueChanged.connect(self.__onValueChanged)
        configItem.valueChanged.connect(self.setValue)

    def setRange(self, minimum: int, maximum: int) -> None:
        """动态调整滑块与输入框的可选范围（用于联动钳制）。"""
        if maximum < minimum:
            maximum = minimum
        self._syncing = True
        try:
            self.slider.setRange(minimum, maximum)
            self.spinBox.setRange(minimum, maximum)
        finally:
            self._syncing = False

    def __onValueChanged(self, value: int):
        if self._syncing:
            return
        self.setValue(value)
        self.valueChanged.emit(value)

    def setValue(self, value: int):
        """将三方（配置项、滑块、输入框）同步到同一数值。"""
        qconfig.set(self.configItem, value)
        self._syncing = True
        try:
            self.spinBox.setValue(value)
            self.slider.setValue(value)
        finally:
            self._syncing = False

