# -*- coding: utf-8 -*-
from qfluentwidgets import (
    BodyLabel,
    ComboBoxSettingCard,
    MessageBoxBase,
    SwitchSettingCard,
)
from qfluentwidgets import FluentIcon as FIF

from videocaptioner.core.entities import TranscribeOutputFormatEnum
from videocaptioner.ui.common.config import cfg


class TranscriptionSettingDialog(MessageBoxBase):
    """转录设置对话框"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.titleLabel = BodyLabel(self.tr("转录设置"), self)

        # 创建输出格式选择卡片
        self.output_format_card = ComboBoxSettingCard(
            cfg.transcribe_output_format,
            FIF.SAVE,
            self.tr("输出格式"),
            self.tr("选择转录字幕的输出格式"),
            texts=[fmt.value for fmt in TranscribeOutputFormatEnum],
            parent=self,
        )
        self.audio_loudnorm_card = SwitchSettingCard(
            FIF.VOLUME,
            self.tr("音量标准化"),
            self.tr("抽取音频时使用 EBU R128 loudnorm，适合音量忽大忽小的素材"),
            cfg.audio_loudnorm,
            self,
        )

        # 添加到布局
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.output_format_card)
        self.viewLayout.addWidget(self.audio_loudnorm_card)
        # 设置间距
        self.viewLayout.setSpacing(10)

        # 设置窗口标题和宽度
        self.setWindowTitle(self.tr("转录设置"))
        self.widget.setMinimumWidth(380)

        # 只显示取消按钮
        self.yesButton.hide()
        self.cancelButton.setText(self.tr("关闭"))

