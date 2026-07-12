# -*- coding: utf-8 -*-

import os
import sys
from pathlib import Path

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QDropEvent
from PyQt5.QtWidgets import QApplication, QFileDialog, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    Action,
    BodyLabel,
    CardWidget,
    ComboBox,
    CommandBar,
    EditableComboBox,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    RoundMenu,
    SingleDirectionScrollArea,
    Slider,
    SwitchButton,
    TextEdit,
    ToolTipFilter,
    ToolTipPosition,
    TransparentDropDownPushButton,
)
from qfluentwidgets import FluentIcon as FIF

from videocaptioner.core.constant import (
    INFOBAR_DURATION_ERROR,
    INFOBAR_DURATION_SUCCESS,
    INFOBAR_DURATION_WARNING,
)
from videocaptioner.core.entities import (
    SubtitleRenderModeEnum,
    SupportedSubtitleFormats,
    SupportedVideoFormats,
    SynthesisTask,
    VideoQualityEnum,
)
from videocaptioner.core.utils.platform_utils import open_folder
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.common.signal_bus import signalBus
from videocaptioner.ui.task_factory import TaskFactory
from videocaptioner.ui.thread.video_synthesis_thread import VideoSynthesisThread


class VideoSynthesisInterface(QWidget):
    finished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("VideoSynthesisInterface")
        self.setAttribute(Qt.WA_StyledBackground, True)  # type: ignore
        self.setAcceptDrops(True)  # 启用拖放功能
        self.setup_ui()
        self.setup_style()
        self.set_value()
        self.setup_signals()
        self.task = None

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

        # 添加开始合成按钮到水平布局
        self.synthesize_button = PrimaryPushButton(
            self.tr("开始合成"), self, icon=FIF.PLAY
        )
        self.synthesize_button.setFixedHeight(34)
        top_layout.addWidget(self.synthesize_button)

        self.main_layout.addLayout(top_layout)

        # 配置卡片
        self.config_card = CardWidget(self)
        self.config_layout = QVBoxLayout(self.config_card)
        self.config_layout.setContentsMargins(20, 20, 20, 20)
        self.config_layout.setSpacing(20)

        # 字幕文件选择
        self.subtitle_layout = QHBoxLayout()
        self.subtitle_layout.setSpacing(15)
        self.subtitle_label = BodyLabel(self.tr("字幕文件"), self)
        self.subtitle_input = LineEdit(self)
        self.subtitle_input.setPlaceholderText(self.tr("选择或者拖拽字幕文件"))
        self.subtitle_input.setAcceptDrops(True)  # 启用拖放
        self.subtitle_button = PushButton(self.tr("浏览"))
        self.subtitle_layout.addWidget(self.subtitle_label)
        self.subtitle_layout.addWidget(self.subtitle_input)
        self.subtitle_layout.addWidget(self.subtitle_button)
        self.config_layout.addLayout(self.subtitle_layout)

        # 视频文件选择
        self.video_layout = QHBoxLayout()
        self.video_layout.setSpacing(15)
        self.video_label = BodyLabel(self.tr("视频文件"), self)
        self.video_input = LineEdit(self)
        self.video_input.setPlaceholderText(self.tr("选择或者拖拽视频文件"))
        self.video_input.setAcceptDrops(True)  # 启用拖放
        self.video_button = PushButton(self.tr("浏览"))
        self.video_layout.addWidget(self.video_label)
        self.video_layout.addWidget(self.video_input)
        self.video_layout.addWidget(self.video_button)
        self.config_layout.addLayout(self.video_layout)

        # 视频编码区（新引擎）
        self._setup_encode_section()
        # 编码器选项 / 分辨率与帧率 / 音频 / 其他·高级（见方案 §5、§4、§11）
        self._setup_encoder_options_section()
        self._setup_resolution_fps_section()
        self._setup_audio_section()
        self._setup_advanced_section()
        self._setup_command_preview_section()

        # 配置卡放入竖向滚动区（页面较长，顶部工具栏/文件与底部进度保持在滚动区外）
        self.scroll_area = SingleDirectionScrollArea(self, orient=Qt.Vertical)  # type: ignore
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setWidget(self.config_card)
        self.scroll_area.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        if hasattr(self.scroll_area, "enableTransparentBackground"):
            self.scroll_area.enableTransparentBackground()
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

    def _setup_command_bar(self):
        """设置顶部命令栏"""
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

        # 添加是否合成视频选项
        self.need_video_action = Action(
            FIF.VIDEO,
            self.tr("合成视频"),
            triggered=self.on_need_video_action_triggered,
            checkable=True,
        )
        self.need_video_action.setToolTip(self.tr("是否生成新的视频文件"))
        self.command_bar.addAction(self.need_video_action)

        self.command_bar.addSeparator()

        # ffmpeg 核心管理（来源 / 打开目录 / 可用性测试）
        self._setup_ffmpeg_menu()
        self.command_bar.addWidget(self.ffmpeg_button)

        self.command_bar.addSeparator()

        # 添加打开文件夹按钮
        folder_action = Action(FIF.FOLDER, "", triggered=self.open_video_folder)
        folder_action.setToolTip(self.tr("打开输出文件夹"))
        self.command_bar.addAction(folder_action)

    def setup_style(self):
        self.subtitle_input.focusOutEvent = lambda e: super(
            LineEdit, self.subtitle_input
        ).focusOutEvent(e)
        self.subtitle_input.paintEvent = lambda e: super(
            LineEdit, self.subtitle_input
        ).paintEvent(e)
        self.subtitle_input.setStyleSheet(
            self.subtitle_input.styleSheet()
            + """
            QLineEdit {
                border-radius: 15px;
                padding: 0 20px;
                background-color: transparent;
                border: 1px solid rgba(255,255, 255, 0.08);
            }
            QLineEdit:focus[transparent=true] {
                border: 1px solid rgba(47,141, 99, 0.48);
            }
        """
        )

        self.video_input.focusOutEvent = lambda e: super(
            LineEdit, self.video_input
        ).focusOutEvent(e)
        self.video_input.paintEvent = lambda e: super(
            LineEdit, self.video_input
        ).paintEvent(e)
        self.video_input.setStyleSheet(
            self.video_input.styleSheet()
            + """
            QLineEdit {
                border-radius: 15px;
                padding: 0 20px;
                background-color: transparent;
                border: 1px solid rgba(255,255, 255, 0.08);
            }
            QLineEdit:focus[transparent=true] {
                border: 1px solid rgba(47,141, 99, 0.48);
            }
        """
        )

    def setup_signals(self):
        # 文件选择相关信号
        self.subtitle_button.clicked.connect(self.choose_subtitle_file)
        self.video_button.clicked.connect(self.choose_video_file)

        # 合成和文件夹相关信号
        self.synthesize_button.clicked.connect(
            lambda: self.start_video_synthesis(need_create_task=True)
        )

        # 全局 signalBus
        signalBus.soft_subtitle_changed.connect(self.on_soft_subtitle_changed)
        signalBus.need_video_changed.connect(self.on_need_video_changed)
        signalBus.video_quality_changed.connect(self.on_video_quality_changed)
        signalBus.use_subtitle_style_changed.connect(self.on_use_style_changed)
        signalBus.subtitle_render_mode_changed.connect(self.on_render_mode_changed_external)

    def set_value(self):
        """设置初始值"""
        self.soft_subtitle_action.setChecked(cfg.soft_subtitle.value)
        self.need_video_action.setChecked(cfg.need_video.value)
        self.video_quality_button.setText(cfg.video_quality.value.value)

        # 设置样式相关初始值
        self.use_style_action.setChecked(cfg.use_subtitle_style.value)
        self.render_mode_button.setText(cfg.subtitle_render_mode.value.value)
        self._init_encode_controls()
        self._update_synthesis_controls_state()

    def on_soft_subtitle_action_triggered(self, checked: bool):
        """处理软字幕按钮点击（更新配置+显示InfoBar）"""
        cfg.set(cfg.soft_subtitle, checked)
        self._update_synthesis_controls_state()

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
        self._update_synthesis_controls_state()

    def on_need_video_action_triggered(self, checked: bool):
        """处理视频合成按钮点击（更新配置+显示InfoBar）"""
        cfg.set(cfg.need_video, checked)
        self._update_synthesis_controls_state()

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
        self._update_synthesis_controls_state()

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
        need_video = self.need_video_action.isChecked()

        # 合成视频关闭时，禁用所有相关选项
        self.soft_subtitle_action.setEnabled(need_video)
        self.use_style_action.setEnabled(need_video)
        self.video_quality_button.setEnabled(need_video)

        # 渲染模式按钮需要同时满足：合成视频开启 且 使用样式开启
        self._update_style_controls_state()
        # 编码区仅在硬烧录重编码时有意义（软字幕走流复制）
        self._set_encode_section_enabled(
            need_video and not self.soft_subtitle_action.isChecked()
        )

    def _update_style_controls_state(self):
        """更新样式相关控件的启用/禁用状态"""
        need_video = self.need_video_action.isChecked()
        use_style = self.use_style_action.isChecked()
        # 渲染模式按钮：需要合成视频开启 且 使用样式开启
        self.render_mode_button.setEnabled(need_video and use_style)

    # ------------------- 视频编码区 -------------------

    def _setup_encode_section(self):
        """构建【视频编码】区：编码器 / 编码方式 / 质量（见方案 §5、增量 A）。"""
        from videocaptioner.core.synthesis import get_encoder_spec

        self._get_encoder_spec = get_encoder_spec
        self._encoder_labels: dict[str, str] = {}

        header = BodyLabel(self.tr("视频编码"), self)
        header.setStyleSheet("font-weight: bold;")
        self.config_layout.addWidget(header)

        # 编码器下拉（不可用项置灰）
        enc_row = QHBoxLayout()
        enc_row.setSpacing(15)
        enc_row.addWidget(BodyLabel(self.tr("视频编码器"), self))
        self.encoder_button = TransparentDropDownPushButton(
            self.tr("编码器"), self, FIF.VIDEO
        )
        self.encoder_button.setFixedHeight(34)
        self.encoder_button.setMinimumWidth(240)
        self.encoder_menu = RoundMenu(parent=self)
        self._build_encoder_menu()
        self.encoder_button.setMenu(self.encoder_menu)
        enc_row.addWidget(self.encoder_button)
        enc_row.addStretch(1)
        self.config_layout.addLayout(enc_row)

        # 编码方式
        mode_row = QHBoxLayout()
        mode_row.setSpacing(15)
        mode_row.addWidget(BodyLabel(self.tr("编码方式"), self))
        self.encode_mode_combo = ComboBox(self)
        self.encode_mode_combo.addItems([self.tr("固定品质"), self.tr("平均码率")])
        self.encode_mode_combo.setMinimumWidth(140)
        mode_row.addWidget(self.encode_mode_combo)
        mode_row.addStretch(1)
        self.config_layout.addLayout(mode_row)

        # 固定品质
        self.quality_container = QWidget(self)
        q_row = QHBoxLayout(self.quality_container)
        q_row.setContentsMargins(0, 0, 0, 0)
        q_row.setSpacing(15)
        q_row.addWidget(BodyLabel(self.tr("固定品质 (RF/CQ)"), self))
        self.quality_slider = Slider(Qt.Horizontal, self)  # type: ignore
        self.quality_slider.setRange(0, 51)
        self.quality_value_label = BodyLabel("23", self)
        self.quality_value_label.setMinimumWidth(28)
        self.quality_hint = BodyLabel(self.tr("越小画质越好、文件越大"), self)
        q_row.addWidget(self.quality_slider, 1)
        q_row.addWidget(self.quality_value_label)
        q_row.addWidget(self.quality_hint)
        self.config_layout.addWidget(self.quality_container)

        # 平均码率
        self.bitrate_container = QWidget(self)
        b_row = QHBoxLayout(self.bitrate_container)
        b_row.setContentsMargins(0, 0, 0, 0)
        b_row.setSpacing(15)
        b_row.addWidget(BodyLabel(self.tr("平均码率 (kbps)"), self))
        self.bitrate_input = LineEdit(self)
        self.bitrate_input.setMaximumWidth(140)
        b_row.addWidget(self.bitrate_input)
        b_row.addStretch(1)
        self.config_layout.addWidget(self.bitrate_container)

        # 信号
        self.encode_mode_combo.currentIndexChanged.connect(self._on_encode_mode_changed)
        self.quality_slider.valueChanged.connect(self._on_cq_changed)
        self.bitrate_input.textChanged.connect(self._on_bitrate_changed)

    def _build_encoder_menu(self, probe_hardware: bool = False):
        """构建/重建编码器菜单；不可用项置灰并附原因。

        probe_hardware=True 时对硬件编码器做真实探测（"可用性测试"用），
        否则只按当前核心的编译支持置灰。
        """
        from videocaptioner.core.synthesis import available_encoder_keys

        self.encoder_menu.clear()
        self._encoder_labels = {}
        try:
            from videocaptioner.core.synthesis import available_encoders

            avail = available_encoders(
                source=cfg.ffmpeg_source.value, probe_hardware=probe_hardware
            )
        except Exception:
            avail = {}
        for key in available_encoder_keys():
            spec = self._get_encoder_spec(key)
            if spec is None:
                continue
            self._encoder_labels[key] = spec.label
            action = Action(text=spec.label)
            info = avail.get(key)
            if info is not None and not info.available:
                action.setEnabled(False)
                action.setToolTip(info.reason or self.tr("不可用"))
            action.triggered.connect(
                lambda checked, k=key, lb=spec.label: self._on_encoder_selected(k, lb)
            )
            self.encoder_menu.addAction(action)

    def _setup_ffmpeg_menu(self):
        """ffmpeg 核心管理下拉：来源切换 / 打开核心目录 / 可用性测试（见方案 §10.1）。"""
        self.ffmpeg_button = TransparentDropDownPushButton(
            self.tr("ffmpeg 核心"), self, FIF.SETTING
        )
        self.ffmpeg_button.setFixedHeight(34)
        menu = RoundMenu(parent=self)
        src = cfg.ffmpeg_source.value
        self._ffmpeg_default_action = Action(self.tr("内置 (默认)"), checkable=True)
        self._ffmpeg_custom_action = Action(self.tr("自定义 (用户目录)"), checkable=True)
        self._ffmpeg_default_action.setChecked(src == "default")
        self._ffmpeg_custom_action.setChecked(src == "custom")
        self._ffmpeg_default_action.triggered.connect(
            lambda: self._on_ffmpeg_source_changed("default")
        )
        self._ffmpeg_custom_action.triggered.connect(
            lambda: self._on_ffmpeg_source_changed("custom")
        )
        menu.addAction(self._ffmpeg_default_action)
        menu.addAction(self._ffmpeg_custom_action)
        menu.addSeparator()
        menu.addAction(
            Action(FIF.FOLDER, self.tr("打开核心目录"), triggered=self._open_ffmpeg_dir)
        )
        menu.addAction(
            Action(FIF.SYNC, self.tr("可用性测试"), triggered=self._on_availability_test)
        )
        self.ffmpeg_button.setMenu(menu)

    def _on_ffmpeg_source_changed(self, source: str):
        cfg.set(cfg.ffmpeg_source, source)
        self._ffmpeg_default_action.setChecked(source == "default")
        self._ffmpeg_custom_action.setChecked(source == "custom")
        try:
            from videocaptioner.core.synthesis import clear_probe_cache

            clear_probe_cache()
        except Exception:
            pass
        self._build_encoder_menu()
        InfoBar.info(
            self.tr("ffmpeg 核心来源"),
            self.tr("已切换，编码器可用性已刷新"),
            duration=INFOBAR_DURATION_SUCCESS,
            position=InfoBarPosition.BOTTOM,
            parent=self,
        )

    def _open_ffmpeg_dir(self):
        from videocaptioner.config import BIN_PATH

        BIN_PATH.mkdir(parents=True, exist_ok=True)
        open_folder(str(BIN_PATH))

    def _on_availability_test(self):
        from videocaptioner.core.synthesis import clear_probe_cache, run_availability_test

        clear_probe_cache()
        try:
            report = run_availability_test(source=cfg.ffmpeg_source.value)
        except Exception as e:
            InfoBar.error(
                self.tr("可用性测试失败"),
                str(e),
                duration=INFOBAR_DURATION_ERROR,
                position=InfoBarPosition.TOP,
                parent=self,
            )
            return
        self._build_encoder_menu(probe_hardware=True)
        available = sum(1 for a in report.encoders.values() if a.available)
        InfoBar.success(
            self.tr("可用性测试完成"),
            self.tr("可用编码器 {n}/{t}").format(n=available, t=len(report.encoders)),
            duration=INFOBAR_DURATION_SUCCESS,
            position=InfoBarPosition.TOP,
            parent=self,
        )

    def _apply_encoder_range(self, key: str):
        """按所选编码器的原生刻度设置质量拉条范围并夹取当前值（见 Q4-A）。"""
        spec = self._get_encoder_spec(key)
        lo, hi = (spec.quality_min, spec.quality_max) if spec else (0, 63)
        self.quality_slider.setRange(lo, hi)
        clamped = min(max(cfg.encode_cq.value, lo), hi)
        self.quality_slider.setValue(clamped)

    def _on_encoder_selected(self, key: str, label: str):
        cfg.set(cfg.video_encoder, key)
        self.encoder_button.setText(label)
        self._apply_encoder_range(key)
        self._refresh_encoder_options()

    def _on_encode_mode_changed(self, index: int):
        cfg.set(cfg.encode_mode, "cq" if index == 0 else "abr")
        self._update_encode_mode_visibility()

    def _on_cq_changed(self, value: int):
        cfg.set(cfg.encode_cq, value)
        self.quality_value_label.setText(str(value))

    def _on_bitrate_changed(self, text: str):
        try:
            value = int(text)
        except ValueError:
            return
        if 100 <= value <= 200000:
            cfg.set(cfg.encode_bitrate_kbps, value)

    def _update_encode_mode_visibility(self):
        is_cq = cfg.encode_mode.value == "cq"
        self.quality_container.setVisible(is_cq)
        self.bitrate_container.setVisible(not is_cq)

    def _init_encode_controls(self):
        key = cfg.video_encoder.value
        self.encoder_button.setText(self._encoder_labels.get(key, key))
        self._apply_encoder_range(key)
        self.encode_mode_combo.setCurrentIndex(0 if cfg.encode_mode.value == "cq" else 1)
        self.quality_slider.setValue(cfg.encode_cq.value)
        self.quality_value_label.setText(str(cfg.encode_cq.value))
        self.bitrate_input.setText(str(cfg.encode_bitrate_kbps.value))
        self._update_encode_mode_visibility()
        self._refresh_encoder_options()
        self._init_resolution_fps_controls()
        self._init_audio_controls()
        self._init_advanced_controls()
        self.extra_args_input.setText(cfg.extra_args.value)
        self._refresh_command_preview()

    def _set_encode_section_enabled(self, enabled: bool):
        """编码区仅在硬烧录重编码时有意义（软字幕走流复制）。"""
        for w in (
            self.encoder_button,
            self.encode_mode_combo,
            self.quality_container,
            self.bitrate_container,
        ):
            w.setEnabled(enabled)
        for w in (
            self.enc_preset_container,
            self.enc_tune_container,
            self.enc_profile_container,
            self.enc_level_container,
            self.fast_decode_container,
            self.resolution_container,
            self.fps_container,
            self.vfr_container,
            self.audio_container,
            self.advanced_container,
            self.extra_args_input,
        ):
            w.setEnabled(enabled)

    # ------------------- 命令预览区（只读；见 §8、ADR 0007） -------------------

    def _setup_command_preview_section(self):
        """自定义参数输入 + 只读实时命令预览（不做反向编辑，见方案 §8）。"""
        header = BodyLabel(self.tr("命令预览"), self)
        header.setStyleSheet("font-weight: bold;")
        self.config_layout.addWidget(header)

        ea_row = QHBoxLayout()
        ea_row.setSpacing(15)
        ea_row.addWidget(BodyLabel(self.tr("自定义参数"), self))
        self.extra_args_input = LineEdit(self)
        self.extra_args_input.setPlaceholderText(
            self.tr("追加的 ffmpeg 参数，如 -x264-params ref=4:bframes=8")
        )
        ea_row.addWidget(self.extra_args_input)
        self.config_layout.addLayout(ea_row)

        self.command_preview = TextEdit(self)
        self.command_preview.setReadOnly(True)
        self.command_preview.setFixedHeight(96)
        self.config_layout.addWidget(self.command_preview)

        copy_row = QHBoxLayout()
        copy_row.addStretch(1)
        self.copy_command_button = PushButton(self.tr("复制命令"), self)
        copy_row.addWidget(self.copy_command_button)
        self.config_layout.addLayout(copy_row)

        self.extra_args_input.textChanged.connect(self._on_extra_args_changed)
        self.copy_command_button.clicked.connect(self._copy_command)
        # 实时刷新：任一编码相关配置变化都重建只读预览
        for _item in (
            cfg.video_encoder, cfg.encode_mode, cfg.encode_cq, cfg.encode_bitrate_kbps,
            cfg.enc_preset, cfg.enc_tune, cfg.enc_profile, cfg.enc_level, cfg.fast_decode,
            cfg.target_height, cfg.out_fps, cfg.vfr, cfg.audio_encoder, cfg.audio_bitrate_kbps,
            cfg.container, cfg.faststart, cfg.keep_metadata, cfg.start_zero, cfg.extra_args,
            cfg.ffmpeg_source, cfg.soft_subtitle,
        ):
            _item.valueChanged.connect(lambda *_a: self._refresh_command_preview())

    def _on_extra_args_changed(self, text: str):
        cfg.set(cfg.extra_args, text)

    def _copy_command(self):
        QApplication.clipboard().setText(self.command_preview.toPlainText())
        InfoBar.success(
            self.tr("已复制"),
            self.tr("命令已复制到剪贴板"),
            duration=INFOBAR_DURATION_SUCCESS,
            position=InfoBarPosition.BOTTOM,
            parent=self,
        )

    def _refresh_command_preview(self):
        try:
            text = self._build_preview_command_text()
        except Exception as e:  # 预览是尽力展示，失败不应影响页面
            text = f"# 预览生成失败: {e}"
        self.command_preview.setPlainText(text)

    def _build_preview_command_text(self) -> str:
        import subprocess
        from dataclasses import replace

        from videocaptioner.core.synthesis import build_output_name, get_ffmpeg_path
        from videocaptioner.core.synthesis.command_builder import build_ffmpeg_command

        es = TaskFactory.encode_settings_from_cfg()
        ffmpeg = get_ffmpeg_path(es.ffmpeg_source)
        soft = cfg.soft_subtitle.value

        video = self.video_input.text().strip() or "<视频文件>"
        subtitle = self.subtitle_input.text().strip() or "<字幕文件>"
        stem = Path(video).stem if self.video_input.text().strip() else "视频"
        name_es = replace(es, video_encoder="copy") if soft else es
        output = build_output_name(stem, name_es, None, es.container)

        if soft:
            sub_codec = "srt" if es.container == "mkv" else "mov_text"
            cmd = [
                ffmpeg, "-i", video, "-i", subtitle,
                "-c:v", "copy", "-c:a", "copy", "-c:s", sub_codec, "-y", output,
            ]
        else:
            vf = "ass='<字幕(缩放/换行后).ass>'"
            cmd = build_ffmpeg_command(
                ffmpeg=ffmpeg, input_path=video, output_path=output,
                video_filter=vf, settings=es, probe=None,
            )
        return subprocess.list2cmdline(cmd)

    # ------------------- 编码器选项区（预设/微调/配置/级别/快速解码） -------------------

    def _setup_encoder_options_section(self):
        """构建【编码器选项】区：按当前编码器动态披露预设/微调/配置/级别/快速解码（见方案 §4）。"""
        header = BodyLabel(self.tr("编码器选项"), self)
        header.setStyleSheet("font-weight: bold;")
        self.config_layout.addWidget(header)

        self.enc_preset_container = self._make_auto_combo_row(
            "enc_preset_combo", self.tr("预设")
        )
        self.enc_tune_container = self._make_auto_combo_row("enc_tune_combo", self.tr("微调"))
        self.enc_profile_container = self._make_auto_combo_row(
            "enc_profile_combo", self.tr("配置")
        )
        self.enc_level_container = self._make_auto_combo_row("enc_level_combo", self.tr("级别"))

        self.fast_decode_container = QWidget(self)
        fd_row = QHBoxLayout(self.fast_decode_container)
        fd_row.setContentsMargins(0, 0, 0, 0)
        fd_row.setSpacing(15)
        fd_row.addWidget(BodyLabel(self.tr("快速解码"), self))
        self.fast_decode_switch = SwitchButton(self)
        fd_row.addWidget(self.fast_decode_switch)
        fd_row.addStretch(1)
        self.config_layout.addWidget(self.fast_decode_container)

        self.enc_preset_combo.currentIndexChanged.connect(
            lambda i: self._on_auto_combo_changed(cfg.enc_preset, self.enc_preset_combo)
        )
        self.enc_tune_combo.currentIndexChanged.connect(
            lambda i: self._on_auto_combo_changed(cfg.enc_tune, self.enc_tune_combo)
        )
        self.enc_profile_combo.currentIndexChanged.connect(
            lambda i: self._on_auto_combo_changed(cfg.enc_profile, self.enc_profile_combo)
        )
        self.enc_level_combo.currentIndexChanged.connect(
            lambda i: self._on_auto_combo_changed(cfg.enc_level, self.enc_level_combo)
        )
        self.fast_decode_switch.checkedChanged.connect(
            lambda checked: cfg.set(cfg.fast_decode, checked)
        )

    def _make_auto_combo_row(self, attr_name: str, label_text: str) -> QWidget:
        """创建一行"标签 + 下拉框"（首项为"自动/默认"），返回可整体显示/隐藏的容器。"""
        container = QWidget(self)
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(15)
        row.addWidget(BodyLabel(label_text, self))
        combo = ComboBox(self)
        combo.setMinimumWidth(140)
        row.addWidget(combo)
        row.addStretch(1)
        setattr(self, attr_name, combo)
        self.config_layout.addWidget(container)
        return container

    def _on_auto_combo_changed(self, config_item, combo: ComboBox):
        value = combo.currentData()
        cfg.set(config_item, value or "")

    def _populate_auto_combo(self, combo: ComboBox, values: tuple, current: str):
        """填充"自动/默认"下拉框；current 为空字符串表示自动。"""
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(self.tr("自动/默认"), userData="")
        for v in values:
            combo.addItem(v, userData=v)
        idx = combo.findData(current)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)

    def _refresh_encoder_options(self):
        """按当前编码器重建【编码器选项】区可见控件（见方案 §4）。

        自定义（未列出）编码器隐藏全部编码器选项控件。
        """
        key = cfg.video_encoder.value
        spec = self._get_encoder_spec(key)
        if spec is None:
            for w in (
                self.enc_preset_container,
                self.enc_tune_container,
                self.enc_profile_container,
                self.enc_level_container,
                self.fast_decode_container,
            ):
                w.setVisible(False)
            return

        has_preset = bool(spec.presets)
        has_tune = bool(spec.tunes)
        has_profile = bool(spec.profiles)
        has_level = bool(spec.levels)
        self.enc_preset_container.setVisible(has_preset)
        self.enc_tune_container.setVisible(has_tune)
        self.enc_profile_container.setVisible(has_profile)
        self.enc_level_container.setVisible(has_level)
        self.fast_decode_container.setVisible(spec.supports_fastdecode)

        if has_preset:
            self._populate_auto_combo(self.enc_preset_combo, spec.presets, cfg.enc_preset.value)
        if has_tune:
            self._populate_auto_combo(self.enc_tune_combo, spec.tunes, cfg.enc_tune.value)
        if has_profile:
            self._populate_auto_combo(
                self.enc_profile_combo, spec.profiles, cfg.enc_profile.value
            )
        if has_level:
            self._populate_auto_combo(self.enc_level_combo, spec.levels, cfg.enc_level.value)
        self.fast_decode_switch.setChecked(cfg.fast_decode.value)

    # ------------------- 分辨率与帧率区 -------------------

    _RESOLUTION_ITEMS = (
        ("与源相同", 0),
        ("720p", 720),
        ("1080p", 1080),
        ("1440p", 1440),
        ("4K", 2160),
        ("自定义", None),
    )

    def _setup_resolution_fps_section(self):
        """构建【分辨率与帧率】区（见方案 §5）。"""
        header = BodyLabel(self.tr("分辨率与帧率"), self)
        header.setStyleSheet("font-weight: bold;")
        self.config_layout.addWidget(header)

        self.resolution_container = QWidget(self)
        res_row = QHBoxLayout(self.resolution_container)
        res_row.setContentsMargins(0, 0, 0, 0)
        res_row.setSpacing(15)
        res_row.addWidget(BodyLabel(self.tr("分辨率"), self))
        self.resolution_combo = ComboBox(self)
        for text, _ in self._RESOLUTION_ITEMS:
            self.resolution_combo.addItem(self.tr(text))
        self.resolution_combo.setMinimumWidth(140)
        res_row.addWidget(self.resolution_combo)
        self.custom_height_input = LineEdit(self)
        self.custom_height_input.setPlaceholderText(self.tr("高度(px)"))
        self.custom_height_input.setMaximumWidth(100)
        res_row.addWidget(self.custom_height_input)
        res_row.addStretch(1)
        self.config_layout.addWidget(self.resolution_container)

        self.fps_container = QWidget(self)
        fps_row = QHBoxLayout(self.fps_container)
        fps_row.setContentsMargins(0, 0, 0, 0)
        fps_row.setSpacing(15)
        fps_row.addWidget(BodyLabel(self.tr("帧率"), self))
        self.fps_combo = ComboBox(self)
        self.fps_combo.addItems([self.tr("与源相同"), self.tr("自定义")])
        self.fps_combo.setMinimumWidth(140)
        fps_row.addWidget(self.fps_combo)
        self.custom_fps_input = LineEdit(self)
        self.custom_fps_input.setPlaceholderText(self.tr("帧率"))
        self.custom_fps_input.setMaximumWidth(100)
        fps_row.addWidget(self.custom_fps_input)
        fps_row.addStretch(1)
        self.config_layout.addWidget(self.fps_container)

        self.vfr_container = QWidget(self)
        vfr_row = QHBoxLayout(self.vfr_container)
        vfr_row.setContentsMargins(0, 0, 0, 0)
        vfr_row.setSpacing(15)
        vfr_row.addWidget(BodyLabel(self.tr("可变帧率 (VFR)"), self))
        self.vfr_switch = SwitchButton(self)
        vfr_row.addWidget(self.vfr_switch)
        vfr_row.addStretch(1)
        self.config_layout.addWidget(self.vfr_container)

        self.resolution_combo.currentIndexChanged.connect(self._on_resolution_changed)
        self.custom_height_input.textChanged.connect(self._on_custom_height_changed)
        self.fps_combo.currentIndexChanged.connect(self._on_fps_mode_changed)
        self.custom_fps_input.textChanged.connect(self._on_custom_fps_changed)
        self.vfr_switch.checkedChanged.connect(lambda checked: cfg.set(cfg.vfr, checked))

    def _on_resolution_changed(self, index: int):
        _, height = self._RESOLUTION_ITEMS[index]
        is_custom = height is None
        self.custom_height_input.setVisible(is_custom)
        if not is_custom:
            cfg.set(cfg.target_height, height)

    def _on_custom_height_changed(self, text: str):
        if self.resolution_combo.currentIndex() != len(self._RESOLUTION_ITEMS) - 1:
            return
        try:
            value = int(text)
        except ValueError:
            return
        if 0 <= value <= 4320:
            cfg.set(cfg.target_height, value)

    def _on_fps_mode_changed(self, index: int):
        is_custom = index == 1
        self.custom_fps_input.setVisible(is_custom)
        if not is_custom:
            cfg.set(cfg.out_fps, "")

    def _on_custom_fps_changed(self, text: str):
        if self.fps_combo.currentIndex() != 1:
            return
        try:
            float(text)
        except ValueError:
            return
        cfg.set(cfg.out_fps, text)

    def _init_resolution_fps_controls(self):
        target_height = cfg.target_height.value
        preset_heights = {h for _, h in self._RESOLUTION_ITEMS if h is not None}
        if target_height in preset_heights:
            idx = next(
                i for i, (_, h) in enumerate(self._RESOLUTION_ITEMS) if h == target_height
            )
            self.resolution_combo.setCurrentIndex(idx)
            self.custom_height_input.setVisible(False)
        else:
            # 自定义（含 0 未落在预设里的情况按"与源相同"兜底不会发生，因为 0 在预设中）
            custom_idx = len(self._RESOLUTION_ITEMS) - 1
            self.resolution_combo.setCurrentIndex(custom_idx)
            self.custom_height_input.setText(str(target_height))
            self.custom_height_input.setVisible(True)

        out_fps = cfg.out_fps.value
        is_custom_fps = bool(out_fps)
        self.fps_combo.setCurrentIndex(1 if is_custom_fps else 0)
        self.custom_fps_input.setVisible(is_custom_fps)
        if is_custom_fps:
            self.custom_fps_input.setText(out_fps)

        self.vfr_switch.setChecked(cfg.vfr.value)

    # ------------------- 音频区 -------------------

    _AUDIO_ENCODER_ITEMS = (
        ("直通", "copy"),
        ("AAC", "aac"),
        ("Opus", "opus"),
        ("AC3", "ac3"),
        ("MP3", "mp3"),
        ("FLAC", "flac"),
    )
    _AUDIO_BITRATE_ITEMS = ("96", "128", "160", "192", "256", "320")

    def _setup_audio_section(self):
        """构建【音频】区（见方案 §5/§7/§12）。"""
        header = BodyLabel(self.tr("音频"), self)
        header.setStyleSheet("font-weight: bold;")
        self.config_layout.addWidget(header)

        self.audio_container = QWidget(self)
        audio_row = QHBoxLayout(self.audio_container)
        audio_row.setContentsMargins(0, 0, 0, 0)
        audio_row.setSpacing(15)
        audio_row.addWidget(BodyLabel(self.tr("音频编码器"), self))
        self.audio_encoder_combo = ComboBox(self)
        for text, _ in self._AUDIO_ENCODER_ITEMS:
            self.audio_encoder_combo.addItem(self.tr(text))
        self.audio_encoder_combo.setMinimumWidth(140)
        audio_row.addWidget(self.audio_encoder_combo)
        audio_row.addWidget(BodyLabel(self.tr("码率 (kbps)"), self))
        self.audio_bitrate_combo = EditableComboBox(self)
        self.audio_bitrate_combo.addItems(list(self._AUDIO_BITRATE_ITEMS))
        self.audio_bitrate_combo.setMinimumWidth(100)
        audio_row.addWidget(self.audio_bitrate_combo)
        audio_row.addStretch(1)
        self.config_layout.addWidget(self.audio_container)

        self.audio_encoder_combo.currentIndexChanged.connect(self._on_audio_encoder_changed)
        self.audio_bitrate_combo.currentTextChanged.connect(self._on_audio_bitrate_changed)

    def _on_audio_encoder_changed(self, index: int):
        _, key = self._AUDIO_ENCODER_ITEMS[index]
        cfg.set(cfg.audio_encoder, key)
        self.audio_bitrate_combo.setEnabled(key not in ("copy", "flac"))

    def _on_audio_bitrate_changed(self, text: str):
        try:
            value = int(text)
        except ValueError:
            return
        if 32 <= value <= 1024:
            cfg.set(cfg.audio_bitrate_kbps, value)

    def _init_audio_controls(self):
        key = cfg.audio_encoder.value
        idx = next(
            (i for i, (_, k) in enumerate(self._AUDIO_ENCODER_ITEMS) if k == key), 0
        )
        self.audio_encoder_combo.setCurrentIndex(idx)
        self.audio_bitrate_combo.setCurrentText(str(cfg.audio_bitrate_kbps.value))
        self.audio_bitrate_combo.setEnabled(key not in ("copy", "flac"))

    # ------------------- 其他 · 高级区 -------------------

    def _setup_advanced_section(self):
        """构建【其他 · 高级】区（见方案 §11）。"""
        header = BodyLabel(self.tr("其他 · 高级"), self)
        header.setStyleSheet("font-weight: bold;")
        self.config_layout.addWidget(header)

        self.advanced_container = QWidget(self)
        adv_row = QHBoxLayout(self.advanced_container)
        adv_row.setContentsMargins(0, 0, 0, 0)
        adv_row.setSpacing(15)

        adv_row.addWidget(BodyLabel(self.tr("网络优化"), self))
        self.faststart_switch = SwitchButton(self)
        adv_row.addWidget(self.faststart_switch)

        adv_row.addWidget(BodyLabel(self.tr("保留元数据"), self))
        self.keep_metadata_switch = SwitchButton(self)
        adv_row.addWidget(self.keep_metadata_switch)

        adv_row.addWidget(BodyLabel(self.tr("起始归零"), self))
        self.start_zero_switch = SwitchButton(self)
        adv_row.addWidget(self.start_zero_switch)

        adv_row.addWidget(BodyLabel(self.tr("容器"), self))
        self.container_combo = ComboBox(self)
        self.container_combo.addItems(["mp4", "mkv"])
        self.container_combo.setMinimumWidth(80)
        adv_row.addWidget(self.container_combo)

        adv_row.addStretch(1)
        self.config_layout.addWidget(self.advanced_container)

        self.faststart_switch.checkedChanged.connect(
            lambda checked: cfg.set(cfg.faststart, checked)
        )
        self.keep_metadata_switch.checkedChanged.connect(
            lambda checked: cfg.set(cfg.keep_metadata, checked)
        )
        self.start_zero_switch.checkedChanged.connect(
            lambda checked: cfg.set(cfg.start_zero, checked)
        )
        self.container_combo.currentTextChanged.connect(
            lambda text: cfg.set(cfg.container, text)
        )

    def _init_advanced_controls(self):
        self.faststart_switch.setChecked(cfg.faststart.value)
        self.keep_metadata_switch.setChecked(cfg.keep_metadata.value)
        self.start_zero_switch.setChecked(cfg.start_zero.value)
        self.container_combo.setCurrentText(cfg.container.value)

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
        return TaskFactory.create_synthesis_task(video_file, subtitle_file)

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
        self.start_video_synthesis(need_create_task=False)

    def on_video_synthesis_finished(self, task):
        self.synthesize_button.setEnabled(True)
        self.progress_bar.setValue(100)
        self.open_video_folder()
        InfoBar.success(
            self.tr("成功"),
            self.tr("视频合成已完成"),
            duration=INFOBAR_DURATION_SUCCESS,
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
