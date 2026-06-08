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
    QFrame,
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    BodyLabel,
    CaptionLabel,
    ComboBox,
    CommandBar,
    DropDownPushButton,
    InfoBar,
    InfoBarPosition,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    RoundMenu,
    ScrollArea,
    SwitchButton,
    ToolTipFilter,
    ToolTipPosition,
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
from videocaptioner.core.subtitle.ass_renderer import ffmpeg_supports_ass_filter
from videocaptioner.core.utils.platform_utils import open_folder
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.common.dubbing_options import (
    DUBBING_VOICES,
    DubbingVoiceOption,
)
from videocaptioner.ui.common.theme_tokens import app_palette
from videocaptioner.ui.components.workflow_widgets import (
    CONTENT_GAP,
    CONTROL_RADIUS,
    PANEL_RADIUS,
    SECTION_GAP,
    FileRow,
    ModernPanel,
    OutputCard,
    SmallActionButton,
    WorkflowSettingRow,
)
from videocaptioner.ui.task_factory import TaskFactory
from videocaptioner.ui.thread.dubbing_thread import DubbingThread
from videocaptioner.ui.thread.video_synthesis_thread import VideoSynthesisThread
from videocaptioner.ui.thread.voice_preview_thread import VoicePreviewThread, bundled_voice_preview

DUBBING_PRESET_LABELS = {
    voice.preset: voice.title
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
        self.main_layout.setContentsMargins(42, 26, 42, 28)
        self.main_layout.setSpacing(18)

        self.command_bar = CommandBar(self)
        self.command_bar.hide()

        self._setup_command_bar()

        top_layout = QHBoxLayout()
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(24)
        top_layout.addStretch(1)

        self.mode_label = CaptionLabel("", self)
        self.mode_label.hide()
        self.open_folder_button = SmallActionButton(self.tr("打开文件夹"), self, FIF.FOLDER)
        self.open_folder_button.setFixedWidth(128)
        self.open_folder_button.clicked.connect(self.open_video_folder)

        self.synthesize_button = PrimaryPushButton(
            self.tr("生成成片"), self, icon=FIF.PLAY
        )
        self.synthesize_button.setObjectName("synthesisPrimaryButton")
        self.synthesize_button.setFixedHeight(40)
        self.synthesize_button.setFixedWidth(150)
        setFont(self.synthesize_button, 13, 820)
        top_layout.addWidget(self.open_folder_button, 0, Qt.AlignBottom)  # type: ignore
        top_layout.addWidget(self.synthesize_button, 0, Qt.AlignBottom)  # type: ignore
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
        self.content_layout.setSpacing(0)

        self.config_card = self._create_input_card()
        self.config_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.content_layout.addWidget(self.config_card, 0)
        self.content_layout.addStretch(1)

        self.scroll_area.setWidget(self.scroll_widget)
        self.main_layout.addWidget(self.scroll_area, 1)

        self.bottom_layout = QHBoxLayout()
        self.bottom_layout.setContentsMargins(0, 0, 0, 0)
        self.bottom_layout.setSpacing(14)
        self.progress_bar = ProgressBar(self)
        self.status_label = BodyLabel(self.tr("就绪"), self)
        self.status_label.setFixedWidth(132)
        self.status_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)  # type: ignore
        setFont(self.status_label, 12, 720)
        self.bottom_layout.addWidget(self.progress_bar, 1)
        self.bottom_layout.addWidget(self.status_label)
        self.main_layout.addLayout(self.bottom_layout)
        self._set_progress_visible(False)
        self._sync_page_style()

    def _create_input_card(self):
        card = QWidget(self)
        layout = QHBoxLayout(card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(CONTENT_GAP)

        left_stack = QWidget(card)
        left_stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        left_layout = QVBoxLayout(left_stack)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(SECTION_GAP)

        self.output_panel = ModernPanel(self.tr("导出内容"), left_stack)
        output_grid = QHBoxLayout()
        output_grid.setContentsMargins(0, 0, 0, 0)
        output_grid.setSpacing(12)
        self.output_subtitle_button = OutputCard(
            self.tr("字幕视频"),
            self.tr("把字幕合成到视频里，支持软字幕或样式硬字幕。"),
            FIF.FONT,
            self.output_panel.body,
        )
        self.output_dubbing_button = OutputCard(
            self.tr("配音音轨"),
            self.tr("按字幕生成配音，可单独导出音频或合入视频。"),
            FIF.VOLUME,
            self.output_panel.body,
        )
        output_grid.addWidget(self.output_subtitle_button, 1)
        output_grid.addWidget(self.output_dubbing_button, 1)
        self.output_panel.bodyLayout.addLayout(output_grid)

        self.files_panel = ModernPanel(self.tr("输入文件"), left_stack)
        self.subtitle_row = FileRow(self.tr("字幕文件"), "", self.tr("选择或者拖拽字幕文件"), self.files_panel.body)
        self.video_row = FileRow(self.tr("视频文件"), "", self.tr("选择或者拖拽视频文件"), self.files_panel.body)
        self.subtitle_label = self.subtitle_row.titleLabel
        self.subtitle_input = self.subtitle_row.lineEdit
        self.subtitle_button = self.subtitle_row.button
        self.video_label = self.video_row.titleLabel
        self.video_input = self.video_row.lineEdit
        self.video_button = self.video_row.button
        self.files_panel.bodyLayout.addWidget(self.subtitle_row)
        self.files_panel.bodyLayout.addWidget(self.video_row)
        self.requirement_hint = CaptionLabel("", self.files_panel.body)
        self.requirement_hint.setWordWrap(True)
        self.requirement_hint.hide()

        left_layout.addWidget(self.output_panel)
        left_layout.addWidget(self.files_panel)
        left_layout.addStretch(1)

        right_stack = QWidget(card)
        right_stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        right_layout = QVBoxLayout(right_stack)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(SECTION_GAP)
        self.settings_panel = ModernPanel(self.tr("合成参数"), right_stack)
        self._setup_settings_panel()
        right_layout.addWidget(self.settings_panel)
        self._setup_dubbing_card(right_stack, right_layout)

        layout.addWidget(left_stack, 112, Qt.AlignTop)  # type: ignore[arg-type]
        layout.addWidget(right_stack, 88, Qt.AlignTop)  # type: ignore[arg-type]
        layout.setStretch(0, 112)
        layout.setStretch(1, 88)
        return card

    def _setup_settings_panel(self):
        self.subtitle_type_button = DropDownPushButton(
            self.tr("硬字幕"), self.settings_panel.body
        )
        self.subtitle_type_button.setFixedHeight(34)
        self.subtitle_type_button.setMinimumWidth(140)
        self.subtitle_type_menu = RoundMenu(parent=self)
        hard_action = Action(text=self.tr("硬字幕"))
        soft_action = Action(text=self.tr("软字幕"))
        hard_action.triggered.connect(lambda _checked=False: self.on_soft_subtitle_action_triggered(False))
        soft_action.triggered.connect(lambda _checked=False: self.on_soft_subtitle_action_triggered(True))
        self.subtitle_type_menu.addAction(hard_action)
        self.subtitle_type_menu.addAction(soft_action)
        self.subtitle_type_button.setMenu(self.subtitle_type_menu)

        self.use_style_switch = SwitchButton("", self.settings_panel.body)
        self.use_style_switch.setOnText("")
        self.use_style_switch.setOffText("")
        self.use_style_switch.setFixedWidth(46)
        self.use_style_switch.setCheckedIndicatorColor(app_palette().accent, app_palette().accent)

        self.render_mode_button = DropDownPushButton(
            cfg.subtitle_render_mode.value.value, self.settings_panel.body
        )
        self.render_mode_menu = RoundMenu(parent=self)
        for mode in SubtitleRenderModeEnum:
            action = Action(text=mode.value)
            action.triggered.connect(
                lambda checked, m=mode.value: self.on_render_mode_changed(m)
            )
            self.render_mode_menu.addAction(action)
        self.render_mode_button.setMenu(self.render_mode_menu)

        self.video_quality_button = DropDownPushButton(
            cfg.video_quality.value.value, self.settings_panel.body
        )
        self.video_quality_menu = RoundMenu(parent=self)
        for quality in VideoQualityEnum:
            action = Action(text=quality.value)
            action.triggered.connect(
                lambda checked, q=quality.value: self.on_video_quality_action_changed(q)
            )
            self.video_quality_menu.addAction(action)
        self.video_quality_button.setMenu(self.video_quality_menu)
        self.render_mode_button.setFixedHeight(34)
        self.video_quality_button.setFixedHeight(34)
        self.subtitle_type_button.setFixedWidth(140)
        self.render_mode_button.setFixedWidth(140)
        self.video_quality_button.setFixedWidth(140)

        self.subtitle_type_row = WorkflowSettingRow(
            self.tr("字幕方式"),
            self.tr("硬字幕会直接烧录到画面"),
            self.subtitle_type_button,
            self.settings_panel.body,
        )
        self.use_style_row = WorkflowSettingRow(
            self.tr("字幕样式"),
            self.tr("使用样式页的 ASS 样式"),
            self.use_style_switch,
            self.settings_panel.body,
        )
        self.render_mode_row = WorkflowSettingRow(
            self.tr("渲染模式"),
            self.tr("使用 ASS 样式渲染"),
            self.render_mode_button,
            self.settings_panel.body,
        )
        self.video_quality_row = WorkflowSettingRow(
            self.tr("视频质量"),
            self.tr("质量越高，生成越慢"),
            self.video_quality_button,
            self.settings_panel.body,
        )
        self.settings_panel.bodyLayout.addWidget(self.subtitle_type_row)
        self.settings_panel.bodyLayout.addWidget(self.use_style_row)
        self.settings_panel.bodyLayout.addWidget(self.render_mode_row)
        self.settings_panel.bodyLayout.addWidget(self.video_quality_row)

    def _setup_command_bar(self):
        """设置顶部命令栏"""
        # Keep these actions as internal state mirrors for existing signal handlers.
        # The visible output switches live in the input card to avoid duplicate
        # "字幕视频 / 配音音轨" controls in the same page.
        self.add_subtitle_action = Action(
            FIF.FONT,
            self.tr("字幕视频"),
            triggered=self.on_add_subtitle_action_triggered,
            checkable=True,
        )
        self.add_subtitle_action.setToolTip(self.tr("把字幕合成到视频里"))

        self.add_dubbing_action = Action(
            FIF.VOLUME,
            self.tr("配音音轨"),
            triggered=self.on_add_dubbing_action_triggered,
            checkable=True,
        )
        self.add_dubbing_action.setToolTip(self.tr("生成配音音轨并合入视频"))

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
        self.use_style_switch.checkedChanged.connect(self.on_use_style_action_triggered)

        # 合成和文件夹相关信号
        self.synthesize_button.clicked.connect(
            lambda: self.start_output_generation(need_create_task=True)
        )
        cfg.soft_subtitle.valueChanged.connect(self.on_soft_subtitle_changed)
        cfg.need_video.valueChanged.connect(self.on_need_video_changed)
        cfg.dubbing_enabled.valueChanged.connect(self.on_dubbing_enabled_changed)
        cfg.video_quality.valueChanged.connect(lambda value: self.on_video_quality_changed(value.value))
        cfg.use_subtitle_style.valueChanged.connect(self.on_use_style_changed)
        cfg.subtitle_render_mode.valueChanged.connect(
            lambda value: self.on_render_mode_changed_external(value.value)
        )

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
        self.use_style_switch.blockSignals(True)
        self.use_style_switch.setChecked(cfg.use_subtitle_style.value)
        self.use_style_switch.blockSignals(False)
        self.render_mode_button.setText(cfg.subtitle_render_mode.value.value)
        self._set_combo_key(self.dubbing_preset_combo, DUBBING_PRESET_LABELS, cfg.dubbing_preset.value)
        self._set_combo_key(self.text_track_combo, TEXT_TRACK_LABELS, cfg.dubbing_text_track.value)
        self._sync_subtitle_type_button()
        self._sync_voice_summary()
        self._update_output_action_text()
        self._update_synthesis_controls_state()
        self._update_dubbing_controls_state()
        self._update_input_requirements()
        self._sync_page_style()

    def showEvent(self, event):
        super().showEvent(event)
        self.set_value()

    def _setup_dubbing_card(self, parent: QWidget, parent_layout: QVBoxLayout):
        self.dubbing_card = ModernPanel(self.tr("配音"), parent)
        self.voice_box = QFrame(self.dubbing_card.body)
        self.voice_box.setObjectName("voiceBox")
        voice_layout = QVBoxLayout(self.voice_box)
        voice_layout.setContentsMargins(12, 12, 12, 12)
        voice_layout.setSpacing(10)

        voice_title_row = QHBoxLayout()
        voice_title_row.setContentsMargins(0, 0, 0, 0)
        voice_title_row.setSpacing(10)
        self.voice_title_label = BodyLabel("-", self.voice_box)
        setFont(self.voice_title_label, 13, 760)
        voice_title_row.addWidget(self.voice_title_label)
        voice_title_row.addStretch(1)

        self.dubbing_preset_combo = ComboBox(self.voice_box)
        self.dubbing_preset_combo.addItems(list(DUBBING_PRESET_LABELS.values()))
        self.dubbing_preset_combo.setMinimumWidth(220)
        self.dubbing_preset_combo.hide()
        self.text_track_combo = ComboBox(self.voice_box)
        self.text_track_combo.addItems(list(TEXT_TRACK_LABELS.values()))
        self.text_track_combo.setMinimumWidth(128)
        self.voice_preview_button = PushButton(FIF.PLAY, self.tr("试听"), self.voice_box)
        self.voice_dialog_button = PushButton(FIF.MUSIC, self.tr("音色库"), self.voice_box)
        self.voice_preview_button.setFixedHeight(34)
        self.voice_dialog_button.setFixedHeight(34)

        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(8)
        action_row.addWidget(self.text_track_combo, 1)
        action_row.addWidget(self.voice_preview_button)
        action_row.addWidget(self.voice_dialog_button)

        voice_layout.addLayout(voice_title_row)
        voice_layout.addWidget(self.dubbing_preset_combo)
        voice_layout.addLayout(action_row)
        self.dubbing_card.bodyLayout.addWidget(self.voice_box)
        self.dubbing_card.bodyLayout.addStretch(1)
        parent_layout.addWidget(self.dubbing_card, 1)

    def on_soft_subtitle_action_triggered(self, checked: bool):
        """处理软字幕按钮点击。"""
        cfg.set(cfg.soft_subtitle, checked)
        self.soft_subtitle_action.setChecked(checked)
        self._sync_subtitle_type_button()

        if checked:
            if self.use_style_action.isChecked():
                self.use_style_action.setChecked(False)
                cfg.set(cfg.use_subtitle_style, False)
                self.use_style_switch.blockSignals(True)
                self.use_style_switch.setChecked(False)
                self.use_style_switch.blockSignals(False)
                self._update_style_controls_state()
        self._update_style_controls_state()

    def on_soft_subtitle_changed(self, checked: bool):
        """处理外部软字幕配置变更（仅更新UI状态）"""
        self.soft_subtitle_action.setChecked(checked)
        self._sync_subtitle_type_button()

    def on_need_video_action_triggered(self, checked: bool):
        """处理视频合成按钮点击。"""
        cfg.set(cfg.need_video, checked)
        self._update_output_action_text()
        self._update_synthesis_controls_state()
        self._update_start_button_text()
        self._update_input_requirements()

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
        self._sync_summary_state()

    def on_video_quality_changed(self, quality_text: str):
        """处理外部质量配置变更（仅更新UI状态）"""
        self.video_quality_button.setText(quality_text)
        self._sync_summary_state()

    def on_use_style_action_triggered(self, checked: bool):
        """处理使用样式开关点击"""
        cfg.set(cfg.use_subtitle_style, checked)
        self.use_style_action.setChecked(checked)
        self.use_style_switch.blockSignals(True)
        self.use_style_switch.setChecked(checked)
        self.use_style_switch.blockSignals(False)
        self._update_style_controls_state()

        if checked:
            if self.soft_subtitle_action.isChecked():
                self.soft_subtitle_action.setChecked(False)
                cfg.set(cfg.soft_subtitle, False)
                self._sync_subtitle_type_button()

    def on_use_style_changed(self, checked: bool):
        """处理外部使用样式配置变更（仅更新 UI）"""
        self.use_style_action.setChecked(checked)
        self.use_style_switch.blockSignals(True)
        self.use_style_switch.setChecked(checked)
        self.use_style_switch.blockSignals(False)
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
            self._sync_summary_state()

    def on_render_mode_changed_external(self, mode_text: str):
        """处理外部渲染模式变更（仅更新 UI）"""
        self.render_mode_button.setText(mode_text)
        self._sync_summary_state()

    def _update_synthesis_controls_state(self):
        """更新所有合成相关控件的启用/禁用状态"""
        need_video = cfg.need_video.value

        # 合成视频关闭时，禁用所有相关选项
        self.soft_subtitle_action.setEnabled(need_video)
        self.subtitle_type_button.setEnabled(need_video)
        self.use_style_action.setEnabled(need_video)
        self.video_quality_button.setEnabled(need_video)

        # 渲染模式按钮需要同时满足：合成视频开启 且 使用样式开启
        self._update_style_controls_state()

    def _update_style_controls_state(self):
        """更新样式相关控件的启用/禁用状态"""
        need_video = cfg.need_video.value
        use_style = self.use_style_action.isChecked()
        self.use_style_switch.setEnabled(need_video)
        # 渲染模式按钮：需要合成视频开启 且 使用样式开启
        self.render_mode_button.setEnabled(need_video and use_style)
        if hasattr(self, "use_style_row"):
            self.use_style_row.setDescription(
                self.tr("使用样式页的 ASS 样式") if use_style else self.tr("使用默认字幕样式")
            )
        if hasattr(self, "render_mode_row"):
            self.render_mode_row.setDescription(
                self.tr("使用 ASS 样式渲染") if use_style else self.tr("开启字幕样式后可选择渲染模式")
            )
        if hasattr(self, "subtitle_type_row") and not need_video:
            self.subtitle_type_row.setDescription(self.tr("选择字幕视频后可设置字幕方式"))
        self._sync_summary_state()

    def _update_dubbing_controls_state(self):
        enabled = cfg.dubbing_enabled.value
        self.dubbing_card.setEnabled(enabled)
        self._update_start_button_text()
        self._sync_summary_state()

    def _update_output_action_text(self):
        self.add_subtitle_action.setChecked(cfg.need_video.value)
        self.add_dubbing_action.setChecked(cfg.dubbing_enabled.value)
        self.output_subtitle_button.setChecked(cfg.need_video.value)
        self.output_dubbing_button.setChecked(cfg.dubbing_enabled.value)
        self._sync_summary_state()

    def _update_start_button_text(self):
        add_subtitle = cfg.need_video.value
        add_dubbing = cfg.dubbing_enabled.value
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
        self._sync_start_button_enabled(add_subtitle, add_dubbing)
        self._sync_summary_state()

    def _sync_start_button_enabled(self, add_subtitle: bool, add_dubbing: bool):
        self.synthesize_button.setEnabled(self._has_required_inputs(add_subtitle, add_dubbing))

    def _has_required_inputs(self, add_subtitle: bool, add_dubbing: bool) -> bool:
        if not add_subtitle and not add_dubbing:
            return False

        subtitle_path = self.subtitle_input.text().strip()
        if not subtitle_path or not Path(subtitle_path).is_file():
            return False

        video_path = self.video_input.text().strip()
        if add_subtitle:
            return bool(video_path) and Path(video_path).is_file()

        return not video_path or Path(video_path).is_file()

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
        self._sync_file_status()
        self._sync_summary_state()

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
        self._update_output_action_text()
        self._update_synthesis_controls_state()
        self._update_start_button_text()
        self._update_input_requirements()

    def on_output_dubbing_button_clicked(self, checked: bool):
        cfg.set(cfg.dubbing_enabled, checked)
        self.add_dubbing_action.setChecked(checked)
        self._update_output_action_text()
        self._update_dubbing_controls_state()
        self._update_start_button_text()
        self._update_input_requirements()

    def _planned_outputs_text(self) -> str:
        add_subtitle = cfg.need_video.value
        add_dubbing = cfg.dubbing_enabled.value
        has_video = bool(self.video_input.text().strip())
        if add_subtitle and add_dubbing:
            return self.tr("字幕视频 + 配音音频")
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
        self._sync_voice_summary()
        self._show_provider_tip(preset)

    def on_text_track_changed(self, text: str):
        cfg.set(cfg.dubbing_text_track, self._key_from_label(TEXT_TRACK_LABELS, text))
        self._sync_summary_state()

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
        self._sync_voice_summary()

    def _sync_page_style(self):
        palette = app_palette()
        accent = palette.accent
        self.setStyleSheet(
            f"""
            QWidget#VideoSynthesisInterface {{
                background: {palette.bg};
            }}
            QScrollArea {{
                border: none;
                background: transparent;
            }}
            QWidget#videoSynthesisScrollWidget {{
                background: transparent;
            }}
            QFrame#voiceBox {{
                background: {palette.field};
                border: 1px solid {palette.line_soft};
                border-radius: {PANEL_RADIUS}px;
            }}
            LineEdit, QLineEdit {{
                color: {palette.text};
                background: {palette.field};
                border: 1px solid {palette.line};
                border-radius: {CONTROL_RADIUS}px;
                padding: 0 10px;
            }}
            ComboBox, PushButton, DropDownPushButton {{
                color: {palette.text};
                background: {palette.field};
                border: 1px solid {palette.line};
                border-radius: {CONTROL_RADIUS}px;
                font-weight: 760;
            }}
            PrimaryPushButton#synthesisPrimaryButton {{
                color: {palette.accent_fg};
                background: {accent};
                border: 1px solid {accent};
                border-radius: {CONTROL_RADIUS}px;
                font-weight: 880;
            }}
            ProgressBar {{
                min-height: 8px;
                max-height: 8px;
            }}
            """
        )
        for panel in self.findChildren(ModernPanel):
            panel.syncStyle()
        for row in self.findChildren(FileRow):
            row.syncStyle()
        for row in self.findChildren(WorkflowSettingRow):
            row.syncStyle()
        for card in self.findChildren(OutputCard):
            card.syncStyle()
        self.open_folder_button.syncStyle()

    def _sync_subtitle_type_button(self):
        if cfg.soft_subtitle.value:
            self.subtitle_type_button.setText(self.tr("软字幕"))
            self.subtitle_type_row.setDescription(self.tr("作为独立字幕轨道嵌入视频"))
        else:
            self.subtitle_type_button.setText(self.tr("硬字幕"))
            self.subtitle_type_row.setDescription(self.tr("硬字幕会直接烧录到画面"))

    def _sync_file_status(self):
        subtitle_ok = Path(self.subtitle_input.text().strip()).is_file()
        video_text = self.video_input.text().strip()
        if hasattr(self, "subtitle_row"):
            self.subtitle_row.setReady(subtitle_ok)
        if hasattr(self, "video_row"):
            self.video_row.setReady(bool(video_text) and Path(video_text).is_file())

    def _sync_summary_state(self):
        subtitle_path = self.subtitle_input.text().strip()
        video_path = self.video_input.text().strip()
        if not cfg.need_video.value and not cfg.dubbing_enabled.value:
            status = self.tr("请选择导出内容")
        elif not subtitle_path:
            status = self.tr("等待字幕文件")
        elif not Path(subtitle_path).is_file():
            status = self.tr("字幕文件不存在")
        elif cfg.need_video.value and not video_path:
            status = self.tr("等待视频文件")
        elif video_path and not Path(video_path).is_file():
            status = self.tr("视频文件不存在")
        else:
            status = self.tr("可以开始生成")
        if hasattr(self, "status_label"):
            self.status_label.setText(self.tr("就绪") if status == self.tr("可以开始生成") else status)
        self._sync_start_button_enabled(cfg.need_video.value, cfg.dubbing_enabled.value)

    def _set_progress_visible(self, visible: bool) -> None:
        self.progress_bar.setVisible(visible)
        self.status_label.setVisible(visible)

    def _begin_generation_progress(self) -> None:
        self._set_progress_visible(True)
        self.progress_bar.resume()
        self.progress_bar.reset()
        self.status_label.setText(self.tr("准备生成"))

    def _finish_generation_progress(self) -> None:
        self._set_progress_visible(True)
        self.progress_bar.resume()
        self.progress_bar.setValue(100)
        self.status_label.setText(self.tr("已完成"))

    def _sync_voice_summary(self):
        if not hasattr(self, "voice_title_label"):
            return
        preset = cfg.dubbing_preset.value
        voice_option = self._voice_option_from_preset(preset)
        label = voice_option.title if voice_option else DUBBING_PRESET_LABELS.get(preset, preset).split("（", 1)[0]
        self.voice_title_label.setText(label)

    @staticmethod
    def _voice_option_from_preset(preset: str) -> DubbingVoiceOption | None:
        for voices in DUBBING_VOICES.values():
            for voice in voices:
                if voice.preset == preset:
                    return voice
        return None

    @staticmethod
    def _set_combo_key(combo: ComboBox, mapping: dict[str, str], key: str):
        combo.setCurrentText(mapping.get(key, key))

    def _show_provider_tip(self, preset: str):
        return

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
        self._begin_generation_progress()
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
            self._set_progress_visible(False)

    def process(self):
        self.start_output_generation(need_create_task=False)

    def start_output_generation(self, need_create_task=True):
        add_subtitle = cfg.need_video.value
        add_dubbing = cfg.dubbing_enabled.value
        if not self._validate_before_generation(add_subtitle, add_dubbing):
            return

        self.synthesize_button.setEnabled(False)
        self._begin_generation_progress()
        self._pending_synthesis_after_dubbing = False
        self._final_synthesis_task = None

        if add_dubbing:
            if add_subtitle:
                final_task = self.create_task() if need_create_task else self.task
                if not final_task or not final_task.video_path or not final_task.output_path:
                    self.synthesize_button.setEnabled(True)
                    self._set_progress_visible(False)
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
                self._set_progress_visible(False)
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
        if (
            add_subtitle
            and cfg.subtitle_render_mode.value == SubtitleRenderModeEnum.ASS_STYLE
            and not ffmpeg_supports_ass_filter()
        ):
            self._show_preflight_error(
                self.tr("FFmpeg 不支持 ASS 硬字幕"),
                self.tr("请安装带 libass 的完整 FFmpeg，或在字幕样式里切换为圆角背景。"),
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
        self._finish_generation_progress()
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
        self._finish_generation_progress()
        self.open_video_folder()
        InfoBar.success(
            self.tr("成功"),
            self.tr("已生成：") + self._planned_outputs_text(),
            duration=INFOBAR_DURATION_SUCCESS,
            position=InfoBarPosition.TOP,
            parent=self,
        )

    def on_dubbing_progress(self, progress: int, message: str):
        self._set_progress_visible(True)
        if self._pending_synthesis_after_dubbing:
            progress = int(progress * 0.55)
        self.progress_bar.setValue(progress)
        self.status_label.setText(message)

    def on_dubbing_error(self, error: str):
        self.synthesize_button.setEnabled(True)
        self._set_progress_visible(True)
        self.progress_bar.error()
        self.status_label.setText(self.tr("失败"))
        InfoBar.error(
            self.tr("配音失败"),
            str(error),
            duration=INFOBAR_DURATION_ERROR,
            position=InfoBarPosition.TOP,
            parent=self,
        )

    def on_video_synthesis_progress(self, progress, message):
        self._set_progress_visible(True)
        self.progress_bar.setValue(progress)
        self.status_label.setText(message)

    def on_video_synthesis_error(self, error):
        self.synthesize_button.setEnabled(True)
        self._set_progress_visible(True)
        self.progress_bar.error()
        self.status_label.setText(self.tr("失败"))
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
