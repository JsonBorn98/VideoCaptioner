from qfluentwidgets import BodyLabel, MessageBoxBase, SwitchSettingCard
from qfluentwidgets import FluentIcon as FIF

from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.components.SpinBoxSettingCard import SpinBoxSettingCard


class SubtitleSettingDialog(MessageBoxBase):
    """Upstream subtitle segmentation settings.

    These controls intentionally stay with subtitle optimization and are not
    part of the downstream postprocess profile.
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
        self.word_count_cjk_card = SpinBoxSettingCard(
            cfg.max_word_count_cjk,
            FIF.TILES,  # type: ignore[arg-type]
            self.tr("中文最大字数"),
            self.tr("上游单条字幕的最大中日韩字符数"),
            minimum=8,
            maximum=100,
            parent=self,
        )
        self.word_count_english_card = SpinBoxSettingCard(
            cfg.max_word_count_english,
            FIF.TILES,  # type: ignore[arg-type]
            self.tr("英文最大单词数"),
            self.tr("上游单条英文字幕的最大单词数"),
            minimum=8,
            maximum=100,
            parent=self,
        )
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.split_card)
        self.viewLayout.addWidget(self.word_count_cjk_card)
        self.viewLayout.addWidget(self.word_count_english_card)
        self.viewLayout.setSpacing(10)
        self.setWindowTitle(self.tr("字幕优化设置"))
        self.widget.setMinimumWidth(420)
        self.yesButton.hide()
        self.cancelButton.setText(self.tr("关闭"))
