# -*- coding: utf-8 -*-

import os
import shutil
import sys
from pathlib import Path

from PyQt5.QtCore import Qt, QUrl, pyqtSignal
from PyQt5.QtGui import QDropEvent
from PyQt5.QtMultimedia import QMediaContent, QMediaPlayer
from PyQt5.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    BodyLabel,
    CaptionLabel,
    CardWidget,
    ComboBox,
    CommandBar,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    RoundMenu,
    ScrollArea,
    TogglePushButton,
    ToolTipFilter,
    ToolTipPosition,
    TransparentDropDownPushButton,
    setFont,
)
from qfluentwidgets import FluentIcon as FIF

from videocaptioner.core.constant import (
    INFOBAR_DURATION_ERROR,
    INFOBAR_DURATION_SUCCESS,
    INFOBAR_DURATION_WARNING,
)
from videocaptioner.core.dubbing import get_dubbing_preset
from videocaptioner.core.entities import (
    DubbingTask,
    SubtitleRenderModeEnum,
    SupportedSubtitleFormats,
    SupportedVideoFormats,
    SynthesisTask,
    VideoQualityEnum,
)
from videocaptioner.core.utils.platform_utils import open_folder
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.common.dubbing_options import DUBBING_VOICES, get_provider_option
from videocaptioner.ui.common.signal_bus import signalBus
from videocaptioner.ui.task_factory import TaskFactory
from videocaptioner.ui.thread.dubbing_thread import DubbingThread
from videocaptioner.ui.thread.video_synthesis_thread import VideoSynthesisThread
from videocaptioner.ui.thread.voice_preview_thread import VoicePreviewThread, bundled_voice_preview

DUBBING_PRESET_LABELS = {
    voice.preset: f"{voice.title}（{get_provider_option(provider).title}）"
    for provider, voices in DUBBING_VOICES.items()
    for voice in voices
}

TEXT_TRACK_LABELS = {
    "auto": "自动选择",
    "first": "第一行",
    "second": "第二行",
}


class VideoSynthesisInterface(QWidget):
    finished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("VideoSynthesisInterface")
        self.setAttribute(Qt.WA_StyledBackground, True)  # type: ignore
        self.setAcceptDrops(True)  # 启用拖放功能
        self.setup_ui()
        self.set_value()
        self.setup_signals()
        self.task = None
        self.dubbing_task: DubbingTask | None = None
        self._pending_synthesis_after_dubbing = False
        self._final_synthesis_task: SynthesisTask | None = None
        self.preview_player = QMediaPlayer(self)

        self.installEventFilter(ToolTipFilter(self, 100, ToolTipPosition.BOTTOM))

    def setup_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setSpacing(20)

        # 创建顶部布局
        top_layout = QHBoxLayout()

        # 添加顶部命令栏
        self.command_bar = CommandBar(self)
        self.command_bar.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)  # type: ignore
        top_layout.addWidget(self.command_bar, 1)  # 设置stretch为1，使其尽可能占用空间

        # 设置命令栏
        self._setup_command_bar()

        self.mode_label = CaptionLabel("", self)
        self.mode_label.setMinimumWidth(220)
        self.mode_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)  # type: ignore
        top_layout.addWidget(self.mode_label)

        # 添加开始合成按钮到水平布局
        self.synthesize_button = PrimaryPushButton(
            self.tr("开始合成"), self, icon=FIF.PLAY
        )
        self.synthesize_button.setFixedHeight(34)
        self.synthesize_button.setMinimumWidth(118)
        setFont(self.synthesize_button, 14)
        top_layout.addWidget(self.synthesize_button)

        self.main_layout.addLayout(top_layout)

        self.scroll_area = ScrollArea(self)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)  # type: ignore
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet(
            "QScrollArea { border: none; background-color: transparent; }"
        )
        self.scroll_widget = QWidget(self.scroll_area)
        self.scroll_widget.setObjectName("videoSynthesisScrollWidget")
        self.scroll_widget.setStyleSheet(
            "QWidget#videoSynthesisScrollWidget { background-color: transparent; }"
        )
        self.content_layout = QVBoxLayout(self.scroll_widget)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(20)

        self.config_card = self._create_input_card()
        self.content_layout.addWidget(self.config_card)

        self._setup_dubbing_card()

        self.content_layout.addStretch(1)
        self.scroll_area.setWidget(self.scroll_widget)
        self.main_layout.addWidget(self.scroll_area, 1)

        # 底部进度条和状态信息
        self.bottom_layout = QHBoxLayout()
        self.progress_bar = ProgressBar(self)
        self.status_label = BodyLabel(self.tr("就绪"), self)
        self.status_label.setMinimumWidth(100)  # 设置最小宽度
        self.status_label.setAlignment(Qt.AlignCenter)  # type: ignore  # 设置文本居中对齐
        self.bottom_layout.addWidget(self.progress_bar, 1)  # 进度条使用剩余空间
        self.bottom_layout.addWidget(self.status_label)  # 状态标签使用固定宽度
        self.main_layout.addLayout(self.bottom_layout)

    def _create_input_card(self):
        card = QWidget(self)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.input_panel = CardWidget(card)
        self.input_panel.setObjectName("inputPanel")
        self.input_panel_layout = QVBoxLayout(self.input_panel)
        self.input_panel_layout.setContentsMargins(16, 14, 16, 14)
        self.input_panel_layout.setSpacing(12)

        output_row = QHBoxLayout()
        output_row.setSpacing(10)
        output_label = BodyLabel(self.tr("导出内容"), self.input_panel)
        output_label.setFixedWidth(76)
        self.output_subtitle_button = TogglePushButton(FIF.FONT, self.tr("字幕视频"), self.input_panel)
        self.output_dubbing_button = TogglePushButton(FIF.VOLUME, self.tr("配音音轨"), self.input_panel)
        self.output_subtitle_button.setFixedHeight(34)
        self.output_dubbing_button.setFixedHeight(34)
        output_row.addWidget(output_label)
        output_row.addWidget(self.output_subtitle_button)
        output_row.addWidget(self.output_dubbing_button)
        output_row.addStretch(1)
        self.input_panel_layout.addLayout(output_row)

        (
            self.subtitle_layout,
            self.subtitle_label,
            self.subtitle_input,
            self.subtitle_button,
        ) = self._create_file_row(
            self.input_panel,
            self.tr("字幕"),
            self.tr("选择或者拖拽字幕文件"),
        )
        self.video_layout, self.video_label, self.video_input, self.video_button = (
            self._create_file_row(
                self.input_panel,
                self.tr("视频"),
                self.tr("选择或者拖拽视频文件"),
            )
        )
        self.input_panel_layout.addLayout(self.subtitle_layout)
        self.input_panel_layout.addLayout(self.video_layout)
        self.requirement_hint = CaptionLabel("", self.input_panel)
        self.requirement_hint.setWordWrap(True)
        self.input_panel_layout.addWidget(self.requirement_hint)
        layout.addWidget(self.input_panel)
        return card

    def _create_file_row(self, parent, label_text: str, placeholder: str):
        row = QHBoxLayout()
        row.setSpacing(12)
        label = BodyLabel(label_text, parent)
        label.setFixedWidth(76)
        line_edit = LineEdit(parent)
        line_edit.setPlaceholderText(placeholder)
        line_edit.setAcceptDrops(True)
        button = PushButton(self.tr("浏览"), parent)
        row.addWidget(label)
        row.addWidget(line_edit, 1)
        row.addWidget(button)
        return row, label, line_edit, button

    def _setup_command_bar(self):
        """设置顶部命令栏"""
        self.add_subtitle_action = Action(
            FIF.FONT,
            self.tr("字幕视频"),
            triggered=self.on_add_subtitle_action_triggered,
            checkable=True,
        )
        self.add_subtitle_action.setToolTip(self.tr("把字幕合成到视频里"))
        self.command_bar.addAction(self.add_subtitle_action)

        self.add_dubbing_action = Action(
            FIF.VOLUME,
            self.tr("配音音轨"),
            triggered=self.on_add_dubbing_action_triggered,
            checkable=True,
        )
        self.add_dubbing_action.setToolTip(self.tr("生成配音音轨并合入视频"))
        self.command_bar.addAction(self.add_dubbing_action)

        self.command_bar.addSeparator()

        # 添加软字幕选项
        self.soft_subtitle_action = Action(
            FIF.FONT,
            self.tr("软字幕"),
            triggered=self.on_soft_subtitle_action_triggered,
            checkable=True,
        )
        self.soft_subtitle_action.setToolTip(self.tr("使用软字幕嵌入视频"))
        self.command_bar.addAction(self.soft_subtitle_action)

        # 添加分隔符
        self.command_bar.addSeparator()

        # 添加使用样式开关
        self.use_style_action = Action(
            FIF.PALETTE,
            self.tr("使用样式"),
            triggered=self.on_use_style_action_triggered,
            checkable=True,
        )
        self.use_style_action.setToolTip(self.tr("启用字幕样式渲染"))
        self.command_bar.addAction(self.use_style_action)

        self.command_bar.addSeparator()

        # 添加渲染模式下拉按钮
        self.render_mode_button = TransparentDropDownPushButton(
            self.tr("渲染模式"), self, FIF.FONT_SIZE
        )
        self.render_mode_button.setFixedHeight(34)
        self.render_mode_button.setMinimumWidth(140)
        self.render_mode_menu = RoundMenu(parent=self)
        for mode in SubtitleRenderModeEnum:
            action = Action(text=mode.value)
            action.triggered.connect(
                lambda checked, m=mode.value: self.on_render_mode_changed(m)
            )
            self.render_mode_menu.addAction(action)
        self.render_mode_button.setMenu(self.render_mode_menu)
        self.command_bar.addWidget(self.render_mode_button)

        self.command_bar.addSeparator()

        # 添加视频质量选择下拉按钮
        self.video_quality_button = TransparentDropDownPushButton(
            self.tr("视频质量"), self, FIF.SPEED_HIGH
        )
        self.video_quality_button.setFixedHeight(34)
        self.video_quality_button.setMinimumWidth(125)
        self.video_quality_menu = RoundMenu(parent=self)
        for quality in VideoQualityEnum:
            action = Action(text=quality.value)
            action.triggered.connect(
                lambda checked, q=quality.value: self.on_video_quality_action_changed(q)
            )
            self.video_quality_menu.addAction(action)
        self.video_quality_button.setMenu(self.video_quality_menu)
        self.command_bar.addWidget(self.video_quality_button)

        # 添加分隔符
        self.command_bar.addSeparator()

        # 保留兼容设置页信号的 Action；实际输出选择使用顶部“添加字幕/添加配音”。
        self.need_video_action = Action(
            FIF.VIDEO,
            self.tr("合成视频"),
            triggered=self.on_need_video_action_triggered,
            checkable=True,
        )
        self.need_video_action.setToolTip(self.tr("是否生成新的视频文件"))

        # 添加打开文件夹按钮
        folder_action = Action(FIF.FOLDER, "", triggered=self.open_video_folder)
        folder_action.setToolTip(self.tr("打开输出文件夹"))
        self.command_bar.addAction(folder_action)

    def setup_signals(self):
        # 文件选择相关信号
        self.subtitle_button.clicked.connect(self.choose_subtitle_file)
        self.video_button.clicked.connect(self.choose_video_file)
        self.subtitle_input.textChanged.connect(self._update_input_requirements)
        self.video_input.textChanged.connect(self._update_input_requirements)
        self.output_subtitle_button.clicked.connect(self.on_output_subtitle_button_clicked)
        self.output_dubbing_button.clicked.connect(self.on_output_dubbing_button_clicked)

        # 合成和文件夹相关信号
        self.synthesize_button.clicked.connect(
            lambda: self.start_output_generation(need_create_task=True)
        )
        # 全局 signalBus
        signalBus.soft_subtitle_changed.connect(self.on_soft_subtitle_changed)
        signalBus.need_video_changed.connect(self.on_need_video_changed)
        signalBus.dubbing_enabled_changed.connect(self.on_dubbing_enabled_changed)
        signalBus.video_quality_changed.connect(self.on_video_quality_changed)
        signalBus.use_subtitle_style_changed.connect(self.on_use_style_changed)
        signalBus.subtitle_render_mode_changed.connect(self.on_render_mode_changed_external)

        self.dubbing_preset_combo.currentTextChanged.connect(self.on_dubbing_preset_changed)
        self.text_track_combo.currentTextChanged.connect(self.on_text_track_changed)
        self.voice_preview_button.clicked.connect(self.preview_current_voice)
        self.voice_dialog_button.clicked.connect(self.show_voice_dialog)

    def set_value(self):
        """设置初始值"""
        self.soft_subtitle_action.setChecked(cfg.soft_subtitle.value)
        self.need_video_action.setChecked(cfg.need_video.value)
        self.video_quality_button.setText(cfg.video_quality.value.value)

        # 设置样式相关初始值
        self.use_style_action.setChecked(cfg.use_subtitle_style.value)
        self.render_mode_button.setText(cfg.subtitle_render_mode.value.value)
        self._set_combo_key(self.dubbing_preset_combo, DUBBING_PRESET_LABELS, cfg.dubbing_preset.value)
        self._set_combo_key(self.text_track_combo, TEXT_TRACK_LABELS, cfg.dubbing_text_track.value)
        self._update_output_action_text()
        self._update_synthesis_controls_state()
        self._update_dubbing_controls_state()
        self._update_input_requirements()

    def showEvent(self, event):
        super().showEvent(event)
        self.set_value()

    def _setup_dubbing_card(self):
        self.dubbing_card = QWidget(self.input_panel)
        self.dubbing_card.setMinimumHeight(72)
        dubbing_layout = QHBoxLayout(self.dubbing_card)
        dubbing_layout.setContentsMargins(0, 4, 0, 0)
        dubbing_layout.setSpacing(10)
        dubbing_layout.addWidget(BodyLabel(self.tr("配音"), self.dubbing_card))
        self.dubbing_preset_combo = ComboBox(self.dubbing_card)
        self.dubbing_preset_combo.addItems(list(DUBBING_PRESET_LABELS.values()))
        self.dubbing_preset_combo.setMinimumWidth(260)
        self.text_track_combo = ComboBox(self.dubbing_card)
        self.text_track_combo.addItems(list(TEXT_TRACK_LABELS.values()))
        self.text_track_combo.setMinimumWidth(140)
        self.voice_preview_button = PushButton(FIF.PLAY, self.tr("试听"), self.dubbing_card)
        self.voice_dialog_button = PrimaryPushButton(FIF.MUSIC, self.tr("音色库"), self.dubbing_card)
        self.voice_preview_button.setFixedHeight(34)
        self.voice_dialog_button.setFixedHeight(34)
        dubbing_layout.addWidget(CaptionLabel(self.tr("音色"), self.dubbing_card))
        dubbing_layout.addWidget(self.dubbing_preset_combo, 1)
        dubbing_layout.addWidget(CaptionLabel(self.tr("朗读"), self.dubbing_card))
        dubbing_layout.addWidget(self.text_track_combo)
        dubbing_layout.addWidget(self.voice_preview_button)
        dubbing_layout.addWidget(self.voice_dialog_button)
        self.input_panel_layout.addWidget(self.dubbing_card)

    def on_soft_subtitle_action_triggered(self, checked: bool):
        """处理软字幕按钮点击（更新配置+显示InfoBar）"""
        cfg.set(cfg.soft_subtitle, checked)

        # 显示说明信息
        if checked:
            # 开启软字幕时自动关闭使用样式
            if self.use_style_action.isChecked():
                self.use_style_action.setChecked(False)
                cfg.set(cfg.use_subtitle_style, False)
                self._update_style_controls_state()
            InfoBar.info(
                self.tr("开启软字幕"),
                self.tr("字幕作为独立轨道嵌入视频，不包含字幕样式"),
                duration=3000,
                position=InfoBarPosition.BOTTOM,
                parent=self,
            )
        else:
            InfoBar.info(
                self.tr("开启硬烧录字幕"),
                self.tr("字幕直接烧录到视频画面中，包含字幕样式"),
                duration=3000,
                position=InfoBarPosition.BOTTOM,
                parent=self,
            )

    def on_soft_subtitle_changed(self, checked: bool):
        """处理外部软字幕配置变更（仅更新UI状态）"""
        self.soft_subtitle_action.setChecked(checked)

    def on_need_video_action_triggered(self, checked: bool):
        """处理视频合成按钮点击（更新配置+显示InfoBar）"""
        cfg.set(cfg.need_video, checked)
        self._update_output_action_text()
        self._update_synthesis_controls_state()
        self._update_start_button_text()
        self._update_input_requirements()

        # 显示说明信息
        if checked:
            InfoBar.info(
                self.tr("开启视频合成"),
                self.tr("将进行视频与字幕的合成操作"),
                duration=3000,
                position=InfoBarPosition.BOTTOM,
                parent=self,
            )
        else:
            InfoBar.info(
                self.tr("关闭视频合成"),
                self.tr("仅生成字幕文件，不生成新的视频文件"),
                duration=3000,
                position=InfoBarPosition.BOTTOM,
                parent=self,
            )

    def on_need_video_changed(self, checked: bool):
        """处理外部视频合成配置变更（仅更新UI状态）"""
        self.need_video_action.setChecked(checked)
        self._update_output_action_text()
        self._update_synthesis_controls_state()
        self._update_input_requirements()

    def on_add_subtitle_action_triggered(self, checked: bool | None = None):
        checked = not cfg.need_video.value if checked is None else checked
        cfg.set(cfg.need_video, checked)
        self.need_video_action.setChecked(checked)
        self._update_output_action_text()
        self._update_synthesis_controls_state()
        self._update_start_button_text()
        self._update_input_requirements()

    def on_add_dubbing_action_triggered(self, checked: bool | None = None):
        checked = not cfg.dubbing_enabled.value if checked is None else checked
        cfg.set(cfg.dubbing_enabled, checked)
        self._update_output_action_text()
        self._update_dubbing_controls_state()
        self._update_input_requirements()

    def on_dubbing_enabled_changed(self, checked: bool):
        self._update_output_action_text()
        self._update_dubbing_controls_state()
        self._update_input_requirements()

    def on_video_quality_action_changed(self, quality_text: str):
        """处理质量选择"""
        # 根据文本找到对应的枚举
        quality_enum = None
        for e in VideoQualityEnum:
            if e.value == quality_text:
                quality_enum = e
                break

        if quality_enum is None:
            return

        cfg.set(cfg.video_quality, quality_enum)
        self.video_quality_button.setText(quality_text)

    def on_video_quality_changed(self, quality_text: str):
        """处理外部质量配置变更（仅更新UI状态）"""
        self.video_quality_button.setText(quality_text)

    def on_use_style_action_triggered(self, checked: bool):
        """处理使用样式开关点击"""
        cfg.set(cfg.use_subtitle_style, checked)
        self._update_style_controls_state()

        if checked:
            # 启用样式时自动关闭软字幕
            if self.soft_subtitle_action.isChecked():
                self.soft_subtitle_action.setChecked(False)
                cfg.set(cfg.soft_subtitle, False)
            InfoBar.info(
                self.tr("启用字幕样式"),
                self.tr("已自动切换为硬字幕渲染"),
                duration=3000,
                position=InfoBarPosition.BOTTOM,
                parent=self,
            )
        else:
            InfoBar.info(
                self.tr("关闭字幕样式"),
                self.tr("将使用默认字幕渲染"),
                duration=3000,
                position=InfoBarPosition.BOTTOM,
                parent=self,
            )

    def on_use_style_changed(self, checked: bool):
        """处理外部使用样式配置变更（仅更新 UI）"""
        self.use_style_action.setChecked(checked)
        self._update_style_controls_state()

    def on_render_mode_changed(self, mode_text: str):
        """处理渲染模式选择（本界面触发）"""
        mode_enum = None
        for e in SubtitleRenderModeEnum:
            if e.value == mode_text:
                mode_enum = e
                break
        if mode_enum:
            cfg.set(cfg.subtitle_render_mode, mode_enum)
            self.render_mode_button.setText(mode_text)
            signalBus.subtitle_render_mode_changed.emit(mode_text)

    def on_render_mode_changed_external(self, mode_text: str):
        """处理外部渲染模式变更（仅更新 UI）"""
        self.render_mode_button.setText(mode_text)

    def _update_synthesis_controls_state(self):
        """更新所有合成相关控件的启用/禁用状态"""
        need_video = cfg.need_video.value

        # 合成视频关闭时，禁用所有相关选项
        self.soft_subtitle_action.setEnabled(need_video)
        self.use_style_action.setEnabled(need_video)
        self.video_quality_button.setEnabled(need_video)

        # 渲染模式按钮需要同时满足：合成视频开启 且 使用样式开启
        self._update_style_controls_state()

    def _update_style_controls_state(self):
        """更新样式相关控件的启用/禁用状态"""
        need_video = cfg.need_video.value
        use_style = self.use_style_action.isChecked()
        # 渲染模式按钮：需要合成视频开启 且 使用样式开启
        self.render_mode_button.setEnabled(need_video and use_style)

    def _update_dubbing_controls_state(self):
        enabled = cfg.dubbing_enabled.value
        self.dubbing_card.setVisible(enabled)
        self._update_start_button_text()

    def _update_output_action_text(self):
        self.add_subtitle_action.setChecked(cfg.need_video.value)
        self.add_dubbing_action.setChecked(cfg.dubbing_enabled.value)
        self.output_subtitle_button.setChecked(cfg.need_video.value)
        self.output_dubbing_button.setChecked(cfg.dubbing_enabled.value)

    def _update_start_button_text(self):
        add_subtitle = cfg.need_video.value
        add_dubbing = cfg.dubbing_enabled.value
        self.synthesize_button.setEnabled(add_subtitle or add_dubbing)
        if add_subtitle and add_dubbing:
            self.synthesize_button.setText(self.tr("生成成片"))
            self.mode_label.setText(self.tr("输出：字幕 + 配音视频"))
        elif add_dubbing:
            if self.video_input.text().strip():
                self.synthesize_button.setText(self.tr("生成配音视频"))
                self.mode_label.setText(self.tr("输出：配音视频"))
            else:
                self.synthesize_button.setText(self.tr("生成配音音频"))
                self.mode_label.setText(self.tr("输出：配音音频"))
        elif add_subtitle:
            self.synthesize_button.setText(self.tr("生成字幕视频"))
            self.mode_label.setText(self.tr("输出：字幕视频"))
        else:
            self.synthesize_button.setText(self.tr("先选择输出"))
            self.mode_label.setText(self.tr("请选择“字幕视频”或“配音音轨”"))

    def _update_input_requirements(self):
        add_subtitle = cfg.need_video.value
        add_dubbing = cfg.dubbing_enabled.value

        if add_subtitle and add_dubbing:
            self.video_label.setText(self.tr("视频文件"))
            self.video_input.setPlaceholderText(self.tr("必填：选择或者拖拽视频文件"))
            self.requirement_hint.setText(self.tr("将生成带字幕和配音的视频；字幕和视频都需要提供。"))
        elif add_subtitle:
            self.video_label.setText(self.tr("视频文件"))
            self.video_input.setPlaceholderText(self.tr("必填：选择或者拖拽视频文件"))
            self.requirement_hint.setText(self.tr("将把字幕合成到视频中；请提供字幕和视频文件。"))
        elif add_dubbing:
            self.video_label.setText(self.tr("参考视频"))
            self.video_input.setPlaceholderText(
                self.tr("可选：选择视频可把配音合入视频")
            )
            self.requirement_hint.setText(self.tr("仅提供字幕会生成配音音频；同时提供视频会生成配音视频。"))
        else:
            self.video_label.setText(self.tr("视频文件"))
            self.video_input.setPlaceholderText(self.tr("选择或者拖拽视频文件"))
            self.requirement_hint.setText(self.tr("请选择要导出的内容：字幕视频、配音音轨，或两者都生成。"))
        self._update_start_button_text()
        self.synthesize_button.setToolTip(
            self.tr("{mode}；将生成：{outputs}").format(
                mode=self._current_mode_text(),
                outputs=self._planned_outputs_text(),
            )
        )

    def _current_mode_text(self) -> str:
        add_subtitle = cfg.need_video.value
        add_dubbing = cfg.dubbing_enabled.value
        has_video = bool(self.video_input.text().strip())
        if add_subtitle and add_dubbing:
            return self.tr("字幕 + 配音成片")
        if add_subtitle:
            return self.tr("字幕视频合成")
        if add_dubbing and has_video:
            return self.tr("配音视频")
        if add_dubbing:
            return self.tr("配音音频")
        return self.tr("未选择输出")

    def on_output_subtitle_button_clicked(self, checked: bool):
        cfg.set(cfg.need_video, checked)
        self.add_subtitle_action.setChecked(checked)
        signalBus.need_video_changed.emit(checked)
        self._update_output_action_text()
        self._update_synthesis_controls_state()
        self._update_start_button_text()
        self._update_input_requirements()

    def on_output_dubbing_button_clicked(self, checked: bool):
        cfg.set(cfg.dubbing_enabled, checked)
        self.add_dubbing_action.setChecked(checked)
        signalBus.dubbing_enabled_changed.emit(checked)
        self._update_output_action_text()
        self._update_dubbing_controls_state()
        self._update_start_button_text()
        self._update_input_requirements()

    def _planned_outputs_text(self) -> str:
        add_subtitle = cfg.need_video.value
        add_dubbing = cfg.dubbing_enabled.value
        has_video = bool(self.video_input.text().strip())
        if add_subtitle and add_dubbing:
            return self.tr("最终成片视频 + 配音音频")
        if add_subtitle:
            return self.tr("带字幕视频")
        if add_dubbing and has_video:
            return self.tr("配音视频 + 配音音频")
        if add_dubbing:
            return self.tr("配音音频")
        return self.tr("请选择添加字幕或添加配音")

    def on_dubbing_preset_changed(self, text: str):
        preset = self._preset_from_label(text)
        cfg.set(cfg.dubbing_preset, preset)
        cfg.set(cfg.dubbing_voice, self._voice_from_preset(preset))
        self._show_provider_tip(preset)

    def on_text_track_changed(self, text: str):
        cfg.set(cfg.dubbing_text_track, self._key_from_label(TEXT_TRACK_LABELS, text))

    def show_voice_dialog(self):
        window = self.window()
        if hasattr(window, "dubbingInterface"):
            window.switchTo(window.dubbingInterface)  # type: ignore[attr-defined]

    def preview_current_voice(self):
        if hasattr(self, "voice_preview_thread") and self.voice_preview_thread.isRunning():
            return
        preset = self._key_from_label(DUBBING_PRESET_LABELS, self.dubbing_preset_combo.currentText())
        preset_config = get_dubbing_preset(preset)
        if (
            preset_config.provider != "edge"
            and not cfg.dubbing_api_key.value.strip()
            and not bundled_voice_preview(preset)
        ):
            InfoBar.warning(
                self.tr("需要 API Key"),
                self.tr("该音色没有内置试听音频，请先在配音设置里填写 API Key。"),
                duration=3500,
                position=InfoBarPosition.TOP,
                parent=self,
            )
            return
        self.voice_preview_button.setEnabled(False)
        self.voice_preview_button.setText(self.tr("试听中..."))
        self.voice_preview_thread = VoicePreviewThread(preset)
        self.voice_preview_thread.finished.connect(self.on_voice_preview_finished)
        self.voice_preview_thread.error.connect(self.on_voice_preview_error)
        self.voice_preview_thread.start()

    def on_voice_preview_finished(self, path: str):
        self.voice_preview_button.setEnabled(True)
        self.voice_preview_button.setText(self.tr("试听当前"))
        self.preview_player.setMedia(QMediaContent(QUrl.fromLocalFile(path)))
        self.preview_player.play()

    def on_voice_preview_error(self, message: str):
        self.voice_preview_button.setEnabled(True)
        self.voice_preview_button.setText(self.tr("试听当前"))
        InfoBar.error(
            self.tr("试听失败"),
            message,
            duration=INFOBAR_DURATION_ERROR,
            position=InfoBarPosition.TOP,
            parent=self,
        )

    def apply_voice_preset(self, preset: str):
        label = DUBBING_PRESET_LABELS.get(preset, preset)
        self.dubbing_preset_combo.setCurrentText(label)
        cfg.set(cfg.dubbing_preset, preset)
        cfg.set(cfg.dubbing_voice, self._voice_from_preset(preset))

    @staticmethod
    def _set_combo_key(combo: ComboBox, mapping: dict[str, str], key: str):
        combo.setCurrentText(mapping.get(key, key))

    def _show_provider_tip(self, preset: str):
        if preset.startswith("edge"):
            InfoBar.info(
                self.tr("Edge 免费配音"),
                self.tr("无需 API Key，不支持音色克隆。"),
                duration=2500,
                position=InfoBarPosition.BOTTOM,
                parent=self,
            )
        elif preset.startswith("gemini"):
            InfoBar.warning(
                self.tr("Gemini 配音"),
                self.tr("需要在设置中填写配音 API Key，不支持音色克隆。"),
                duration=3000,
                position=InfoBarPosition.BOTTOM,
                parent=self,
            )
        elif preset.startswith("siliconflow"):
            InfoBar.info(
                self.tr("SiliconFlow 配音"),
                self.tr("需要 API Key，支持音色克隆。"),
                duration=3000,
                position=InfoBarPosition.BOTTOM,
                parent=self,
            )

    @staticmethod
    def _preset_from_label(label: str) -> str:
        for key, value in DUBBING_PRESET_LABELS.items():
            if value == label:
                return key
        return label

    @staticmethod
    def _key_from_label(mapping: dict[str, str], label: str) -> str:
        for key, value in mapping.items():
            if value == label:
                return key
        return next(iter(mapping))

    @staticmethod
    def _voice_from_preset(preset: str) -> str:
        from videocaptioner.core.dubbing.presets import get_dubbing_preset

        try:
            return get_dubbing_preset(preset).voice
        except ValueError:
            return ""

    def choose_subtitle_file(self):
        # 构建文件过滤器
        subtitle_formats = " ".join(
            f"*.{fmt.value}" for fmt in SupportedSubtitleFormats
        )
        filter_str = f"{self.tr('字幕文件')} ({subtitle_formats})"

        file_path, _ = QFileDialog.getOpenFileName(
            self, self.tr("选择字幕文件"), "", filter_str
        )
        if file_path:
            self.subtitle_input.setText(file_path)

    def choose_video_file(self):
        # 构建文件过滤器
        video_formats = " ".join(f"*.{fmt.value}" for fmt in SupportedVideoFormats)
        filter_str = f"{self.tr('视频文件')} ({video_formats})"

        file_path, _ = QFileDialog.getOpenFileName(
            self, self.tr("选择视频文件"), "", filter_str
        )
        if file_path:
            self.video_input.setText(file_path)

    def create_task(self):
        subtitle_file = self.subtitle_input.text()
        video_file = self.video_input.text()
        if not subtitle_file or not video_file:
            InfoBar.error(
                self.tr("错误"),
                self.tr("请选择字幕文件和视频文件"),
                duration=INFOBAR_DURATION_ERROR,
                position=InfoBarPosition.TOP,
                parent=self,
            )
            return None
        if not Path(subtitle_file).is_file():
            InfoBar.error(
                self.tr("错误"),
                self.tr("字幕文件不存在"),
                duration=INFOBAR_DURATION_ERROR,
                position=InfoBarPosition.TOP,
                parent=self,
            )
            return None
        if not Path(video_file).is_file():
            InfoBar.error(
                self.tr("错误"),
                self.tr("视频文件不存在"),
                duration=INFOBAR_DURATION_ERROR,
                position=InfoBarPosition.TOP,
                parent=self,
            )
            return None
        return TaskFactory.create_synthesis_task(video_file, subtitle_file)

    def create_dubbing_task(self, output_video_path: str | None = None):
        subtitle_file = self.subtitle_input.text()
        video_file = self.video_input.text()
        if not subtitle_file:
            InfoBar.error(
                self.tr("错误"),
                self.tr("请选择字幕文件"),
                duration=INFOBAR_DURATION_ERROR,
                position=InfoBarPosition.TOP,
                parent=self,
            )
            return None
        if not Path(subtitle_file).is_file():
            InfoBar.error(
                self.tr("错误"),
                self.tr("字幕文件不存在"),
                duration=INFOBAR_DURATION_ERROR,
                position=InfoBarPosition.TOP,
                parent=self,
            )
            return None
        if not video_file and cfg.need_video.value:
            InfoBar.error(
                self.tr("错误"),
                self.tr("生成视频时需要选择视频文件"),
                duration=INFOBAR_DURATION_ERROR,
                position=InfoBarPosition.TOP,
                parent=self,
            )
            return None
        if video_file and not Path(video_file).is_file():
            InfoBar.error(
                self.tr("错误"),
                self.tr("视频文件不存在"),
                duration=INFOBAR_DURATION_ERROR,
                position=InfoBarPosition.TOP,
                parent=self,
            )
            return None
        return TaskFactory.create_dubbing_task(
            video_file,
            subtitle_file,
            output_video_path=output_video_path,
        )

    def set_task(self, task: SynthesisTask):
        self.task = task
        self.update_info()

    def update_info(self):
        if self.task:
            self.video_input.setText(self.task.video_path)
            self.subtitle_input.setText(self.task.subtitle_path)

    def start_video_synthesis(self, need_create_task=True):
        self.synthesize_button.setEnabled(False)
        self.progress_bar.resume()
        self.progress_bar.reset()
        if need_create_task:
            self.task = self.create_task()

        if self.task:
            self.video_synthesis_thread = VideoSynthesisThread(self.task)
            self.video_synthesis_thread.finished.connect(
                self.on_video_synthesis_finished
            )
            self.video_synthesis_thread.progress.connect(
                self.on_video_synthesis_progress
            )
            self.video_synthesis_thread.error.connect(self.on_video_synthesis_error)
            self.video_synthesis_thread.start()
        else:
            self.synthesize_button.setEnabled(True)

    def process(self):
        self.start_output_generation(need_create_task=False)

    def start_output_generation(self, need_create_task=True):
        add_subtitle = cfg.need_video.value
        add_dubbing = cfg.dubbing_enabled.value
        if not self._validate_before_generation(add_subtitle, add_dubbing):
            return

        self.synthesize_button.setEnabled(False)
        self.progress_bar.resume()
        self.progress_bar.reset()
        self._pending_synthesis_after_dubbing = False
        self._final_synthesis_task = None

        if add_dubbing:
            if add_subtitle:
                final_task = self.create_task() if need_create_task else self.task
                if not final_task or not final_task.video_path or not final_task.output_path:
                    self.synthesize_button.setEnabled(True)
                    return
                temp_video = str(Path(final_task.output_path).with_suffix(".dub.tmp.mp4"))
                self._final_synthesis_task = final_task
                self._pending_synthesis_after_dubbing = True
                self.dubbing_task = self.create_dubbing_task(output_video_path=temp_video)
            else:
                self.dubbing_task = self.create_dubbing_task()
            if self.dubbing_task:
                self.start_dubbing_task(self.dubbing_task)
            else:
                self.synthesize_button.setEnabled(True)
            return

        self.start_video_synthesis(need_create_task=need_create_task)

    def _validate_before_generation(self, add_subtitle: bool, add_dubbing: bool) -> bool:
        subtitle_file = self.subtitle_input.text().strip()
        video_file = self.video_input.text().strip()
        if not add_subtitle and not add_dubbing:
            self._show_preflight_error(
                self.tr("请选择输出内容"),
                self.tr("请在“输出内容”里至少打开“字幕视频”或“配音”。"),
            )
            return False
        if not subtitle_file:
            self._show_preflight_error(
                self.tr("缺少字幕文件"),
                self.tr("请选择 srt、ass 或 vtt 字幕文件。"),
            )
            return False
        if not Path(subtitle_file).is_file():
            self._show_preflight_error(self.tr("字幕文件不存在"), subtitle_file)
            return False
        if Path(subtitle_file).suffix.lower().lstrip(".") not in {
            fmt.value for fmt in SupportedSubtitleFormats
        }:
            self._show_preflight_error(
                self.tr("字幕格式不支持"),
                self.tr("请选择 srt、ass 或 vtt 文件。"),
            )
            return False
        if add_subtitle and not video_file:
            self._show_preflight_error(
                self.tr("缺少视频文件"),
                self.tr("添加字幕或生成最终成片时需要选择视频文件。"),
            )
            return False
        if video_file:
            if not Path(video_file).is_file():
                self._show_preflight_error(self.tr("视频文件不存在"), video_file)
                return False
            if Path(video_file).suffix.lower().lstrip(".") not in {
                fmt.value for fmt in SupportedVideoFormats
            }:
                self._show_preflight_error(
                    self.tr("视频格式不支持"),
                    self.tr("请选择常见视频文件，例如 mp4、mov、mkv。"),
                )
                return False
        if (add_subtitle or add_dubbing) and not shutil.which("ffmpeg"):
            self._show_preflight_error(
                self.tr("缺少 FFmpeg"),
                self.tr("请先安装 FFmpeg 并确保 ffmpeg 在 PATH 中。"),
            )
            return False
        if add_dubbing and not shutil.which("ffprobe"):
            self._show_preflight_error(
                self.tr("缺少 FFprobe"),
                self.tr("配音需要 ffprobe 读取音频时长，请确认 FFmpeg 套件安装完整。"),
            )
            return False
        if add_dubbing:
            provider = cfg.dubbing_provider.value
            if provider != "edge" and not cfg.dubbing_api_key.value.strip():
                self._show_preflight_error(
                    self.tr("缺少配音 API Key"),
                    self.tr("当前音色需要 API Key；可切换到 Edge 免费音色，或在设置中填写 Key。"),
                )
                return False
        return True

    def _show_preflight_error(self, title: str, message: str):
        InfoBar.error(
            title,
            message,
            duration=INFOBAR_DURATION_ERROR,
            position=InfoBarPosition.TOP,
            parent=self,
        )

    def start_dubbing_task(self, task: DubbingTask):
        self.dubbing_thread = DubbingThread(task)
        self.dubbing_thread.finished.connect(self.on_dubbing_finished)
        self.dubbing_thread.progress.connect(self.on_dubbing_progress)
        self.dubbing_thread.error.connect(self.on_dubbing_error)
        self.dubbing_thread.start()

    def on_video_synthesis_finished(self, task):
        self.synthesize_button.setEnabled(True)
        self.progress_bar.setValue(100)
        if self._pending_synthesis_after_dubbing and task.video_path:
            temp_video = Path(task.video_path)
            if ".dub.tmp" in temp_video.name:
                temp_video.unlink(missing_ok=True)
            self._pending_synthesis_after_dubbing = False
        self.open_video_folder()
        InfoBar.success(
            self.tr("成功"),
            self.tr("已生成：") + self._planned_outputs_text(),
            duration=INFOBAR_DURATION_SUCCESS,
            position=InfoBarPosition.TOP,
            parent=self,
        )

    def on_dubbing_finished(self, task: DubbingTask):
        if self._pending_synthesis_after_dubbing and self._final_synthesis_task:
            if not task.output_video_path:
                self.on_dubbing_error(self.tr("配音视频输出路径为空"))
                return
            self.task = self._final_synthesis_task
            self.task.video_path = task.output_video_path
            self.video_synthesis_thread = VideoSynthesisThread(self.task)
            self.video_synthesis_thread.finished.connect(self.on_video_synthesis_finished)
            self.video_synthesis_thread.progress.connect(
                lambda progress, message: self.on_video_synthesis_progress(55 + int(progress * 0.45), message)
            )
            self.video_synthesis_thread.error.connect(self.on_video_synthesis_error)
            self.video_synthesis_thread.start()
            return

        self.synthesize_button.setEnabled(True)
        self.progress_bar.setValue(100)
        self.open_video_folder()
        InfoBar.success(
            self.tr("成功"),
            self.tr("已生成：") + self._planned_outputs_text(),
            duration=INFOBAR_DURATION_SUCCESS,
            position=InfoBarPosition.TOP,
            parent=self,
        )

    def on_dubbing_progress(self, progress: int, message: str):
        if self._pending_synthesis_after_dubbing:
            progress = int(progress * 0.55)
        self.progress_bar.setValue(progress)
        self.status_label.setText(message)

    def on_dubbing_error(self, error: str):
        self.synthesize_button.setEnabled(True)
        self.progress_bar.error()
        InfoBar.error(
            self.tr("配音失败"),
            str(error),
            duration=INFOBAR_DURATION_ERROR,
            position=InfoBarPosition.TOP,
            parent=self,
        )

    def on_video_synthesis_progress(self, progress, message):
        self.progress_bar.setValue(progress)
        self.status_label.setText(message)

    def on_video_synthesis_error(self, error):
        self.synthesize_button.setEnabled(True)
        self.progress_bar.error()
        InfoBar.error(
            self.tr("错误"),
            str(error),
            duration=INFOBAR_DURATION_ERROR,
            position=InfoBarPosition.TOP,
            parent=self,
        )

    def open_video_folder(self):
        if self.task and self.task.output_path:
            file_path = Path(self.task.output_path)
            target_dir = str(
                file_path.parent
                if file_path.exists()
                else (
                    Path(str(self.task.video_path)).parent
                    if self.task.video_path
                    else file_path.parent
                )
            )
            # Cross-platform folder opening
            open_folder(target_dir)
        elif self.dubbing_task and (self.dubbing_task.output_video_path or self.dubbing_task.output_audio_path):
            file_path = Path(self.dubbing_task.output_video_path or self.dubbing_task.output_audio_path or "")
            open_folder(str(file_path.parent))
        else:
            InfoBar.warning(
                self.tr("警告"),
                self.tr("没有可用的视频文件夹"),
                duration=INFOBAR_DURATION_WARNING,
                position=InfoBarPosition.TOP,
                parent=self,
            )

    def dragEnterEvent(self, event):
        """拖拽进入事件处理"""
        event.accept() if event.mimeData().hasUrls() else event.ignore()

    def dropEvent(self, event: QDropEvent):
        """拖拽放下事件处理"""
        files = [u.toLocalFile() for u in event.mimeData().urls()]
        for file_path in files:
            if not os.path.isfile(file_path):
                continue

            file_ext = os.path.splitext(file_path)[1][1:].lower()

            # 检查文件格式是否支持
            if file_ext in {fmt.value for fmt in SupportedSubtitleFormats}:
                self.subtitle_input.setText(file_path)
                InfoBar.success(
                    self.tr("导入成功"),
                    self.tr("字幕文件已放入输入框"),
                    duration=INFOBAR_DURATION_SUCCESS,
                    parent=self,
                )
                break
            elif file_ext in {fmt.value for fmt in SupportedVideoFormats}:
                self.video_input.setText(file_path)
                InfoBar.success(
                    self.tr("导入成功"),
                    self.tr("视频文件已输入框"),
                    duration=INFOBAR_DURATION_SUCCESS,
                    parent=self,
                )
                break
            else:
                InfoBar.error(
                    self.tr("格式错误") + file_ext,
                    self.tr("请拖入视频或者字幕文件"),
                    duration=INFOBAR_DURATION_ERROR,
                    parent=self,
                )


if __name__ == "__main__":
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)  # type: ignore
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)  # type: ignore

    app = QApplication(sys.argv)
    window = VideoSynthesisInterface()
    window.resize(600, 400)  # 设置窗口大小
    window.show()
    sys.exit(app.exec_())
