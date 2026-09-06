from qfluentwidgets import BodyLabel, MessageBoxBase, SwitchSettingCard
from qfluentwidgets import FluentIcon as FIF

from videocaptioner.ui.common.config import cfg


class SubtitleSettingDialog(MessageBoxBase):
    """Upstream subtitle segmentation settings.

    These controls intentionally stay with subtitle optimization and are not
    part of the downstream postprocess profile. Viewing length constraints
    are owned exclusively by postprocessing (ADR-0020): no upstream length
    limits are exposed here anymore.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.titleLabel = BodyLabel(self.tr("字幕优化设置"), self)
        self.split_card = SwitchSettingCard(
            FIF.ALIGNMENT,
            self.tr("字幕分割"),
            self.tr("字幕是否使用大语言模型进行智能断句"),
            cfg.need_split,
            self,
        )
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.split_card)
        self.viewLayout.setSpacing(10)
        self.setWindowTitle(self.tr("字幕优化设置"))
        self.widget.setMinimumWidth(420)
        self.yesButton.hide()
        self.cancelButton.setText(self.tr("关闭"))
