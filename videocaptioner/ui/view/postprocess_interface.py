"""Standalone subtitle postprocess page used by workflows and direct jobs."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    BodyLabel,
    ComboBox,
    CommandBar,
    InfoBar,
    InfoBarPosition,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    RoundMenu,
    SubtitleLabel,
    TableView,
    TransparentDropDownPushButton,
)
from qfluentwidgets import FluentIcon as FIF

from videocaptioner.core.asr.asr_data import ASRData
from videocaptioner.core.constant import (
    INFOBAR_DURATION_ERROR,
    INFOBAR_DURATION_INFO,
    INFOBAR_DURATION_SUCCESS,
    INFOBAR_DURATION_WARNING,
)
from videocaptioner.core.entities import (
    OutputSubtitleFormatEnum,
    SubtitleLayoutEnum,
    SubtitleRenderModeEnum,
)
from videocaptioner.core.postprocess.models import PostprocessLayoutMode, PostprocessTask
from videocaptioner.core.postprocess.profiles import PostprocessProfileStore
from videocaptioner.core.subtitle import get_subtitle_style
from videocaptioner.core.subtitle.io import (
    canonical_stage_path,
    clone_subtitle_data,
    export_subtitle_atomic,
    import_subtitle,
    read_videocaptioner_layout,
    save_canonical_srt,
)
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.task_factory import TaskFactory
from videocaptioner.ui.view.subtitle_interface import SubtitleTableModel


class PostprocessInterface(QWidget):
    """Run the postprocess stage without exposing upstream translation controls."""

    finished = pyqtSignal(str, str)

    _LAYOUTS = (
        (PostprocessLayoutMode.AUTO, "自动识别"),
        (PostprocessLayoutMode.SINGLE, "单语字幕"),
        (PostprocessLayoutMode.ORIGINAL_ON_TOP, "第一行是原文"),
        (PostprocessLayoutMode.TRANSLATE_ON_TOP, "第一行是译文"),
    )
    _OUTPUT_LAYOUTS = (
        (None, "保持输入顺序"),
        (SubtitleLayoutEnum.ORIGINAL_ON_TOP, "输出原文在上"),
        (SubtitleLayoutEnum.TRANSLATE_ON_TOP, "输出译文在上"),
    )

    def __init__(self, parent: Optional[QWidget] = None, *, profile_store=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setObjectName("PostprocessInterface")
        self.task: Any | None = None
        self.subtitle_path: str | None = None
        self.media_path: str | None = None
        self._thread: Any | None = None
        self._workflow_mode = False
        self.primary_srt_path: str | None = None
        self._dirty = False
        self._structure_confirmation_required = False
        self._effective_layout = SubtitleLayoutEnum.ONLY_ORIGINAL
        self._loading_layout = False
        self._profile_store = profile_store or PostprocessProfileStore()
        self._setup_ui()
        self.refresh_profiles()
        self._set_processing(False)

    def _setup_ui(self) -> None:
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(18, 14, 18, 12)
        self.main_layout.setSpacing(14)

        self.command_bar = CommandBar(self)
        self.command_bar.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)  # type: ignore[arg-type]
        self.open_action = Action(FIF.FOLDER_ADD, self.tr("选择字幕"), triggered=self.select_subtitle)
        self.media_action = Action(FIF.VIDEO, self.tr("关联媒体"), triggered=self.select_media)
        self.settings_action = Action(FIF.SETTING, self.tr("功能设置"), triggered=self.open_settings)
        self.command_bar.addAction(self.open_action)
        self.command_bar.addAction(self.media_action)
        self.command_bar.addSeparator()
        self.command_bar.addAction(self.settings_action)

        self.save_action = Action(FIF.SAVE, self.tr("保存"), triggered=self.save_primary_srt)
        self.command_bar.addAction(self.save_action)
        export_menu = RoundMenu(parent=self)
        for output_format in OutputSubtitleFormatEnum:
            action = Action(text=output_format.value)
            action.triggered.connect(
                lambda _checked=False, value=output_format.value: self.export_copy(value)
            )
            export_menu.addAction(action)
        self.export_button = TransparentDropDownPushButton(self.tr("导出"), self, FIF.SHARE)
        self.export_button.setMenu(export_menu)
        self.export_button.setFixedHeight(34)
        self.command_bar.addWidget(self.export_button)

        self.profile_button = TransparentDropDownPushButton(self.tr("均衡"), self, FIF.TILES)
        self.profile_button.setFixedHeight(34)
        self.profile_menu = RoundMenu(parent=self)
        self.profile_button.setMenu(self.profile_menu)
        self.command_bar.addWidget(self.profile_button)

        self.main_layout.addWidget(self.command_bar)

        options = QWidget(self)
        options_layout = QHBoxLayout(options)
        options_layout.setContentsMargins(10, 6, 10, 6)
        options_layout.addWidget(SubtitleLabel(self.tr("输入结构"), options))
        self.layout_combo = ComboBox(options)
        for mode, label in self._LAYOUTS:
            self.layout_combo.addItem(self.tr(label), userData=mode.value)
        self.layout_combo.currentIndexChanged.connect(self._layout_changed)
        options_layout.addWidget(self.layout_combo)
        options_layout.addSpacing(18)
        options_layout.addWidget(SubtitleLabel(self.tr("输出布局"), options))
        self.output_layout_combo = ComboBox(options)
        for layout, label in self._OUTPUT_LAYOUTS:
            self.output_layout_combo.addItem(
                self.tr(label), userData=layout.value if layout is not None else "preserve"
            )
        self.output_layout_combo.currentIndexChanged.connect(self._output_layout_changed)
        options_layout.addWidget(self.output_layout_combo)
        options_layout.addSpacing(18)
        self.mode_button = TransparentDropDownPushButton(self.tr("应用修改"), options, FIF.EDIT)
        self.mode_menu = RoundMenu(parent=self.mode_button)
        for value, text in (("apply", self.tr("应用修改")), ("analyze", self.tr("仅分析"))):
            action = Action(text=text)
            action.triggered.connect(lambda _checked=False, value=value: self.set_mode(value))
            self.mode_menu.addAction(action)
        self.mode_button.setMenu(self.mode_menu)
        options_layout.addWidget(self.mode_button)
        options_layout.addStretch(1)
        self.main_layout.addWidget(options)

        self.input_label = BodyLabel(self.tr("请拖入完整的 SRT、VTT 或 ASS 字幕"), self)
        self.input_label.setAlignment(Qt.AlignCenter)  # type: ignore[arg-type]
        self.input_label.setMinimumHeight(54)
        self.input_label.setObjectName("postprocessDropHint")
        # 边框放在父级样式表按 objectName 命中，避免直接 setStyleSheet 覆盖 Fluent
        # 标签自带的主题色 QSS（否则暗色模式下文字会退回黑色而看不清）。
        self.setStyleSheet(
            "#postprocessDropHint {"
            " border: 1px dashed rgba(128,128,128,120);"
            " border-radius: 8px; padding: 18px; }"
        )
        self.main_layout.addWidget(self.input_label)

        self.layout_warning = QLabel(self)
        self.layout_warning.setObjectName("postprocessLayoutWarning")
        self.layout_warning.setWordWrap(True)
        self.layout_warning.setStyleSheet("color: #d89614; padding: 4px 10px;")
        self.layout_warning.hide()
        self.main_layout.addWidget(self.layout_warning)

        self.result_title = SubtitleLabel(self.tr("后处理工作稿"), self)
        self.main_layout.addWidget(self.result_title)
        self.subtitle_table = TableView(self)
        self.model = SubtitleTableModel("")
        self.model.edited.connect(self._mark_dirty)
        self.subtitle_table.setModel(self.model)
        self.subtitle_table.setBorderVisible(True)
        self.subtitle_table.setBorderRadius(8)
        self.subtitle_table.setWordWrap(True)
        self.subtitle_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.subtitle_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.subtitle_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self.subtitle_table.setColumnWidth(0, 120)
        self.subtitle_table.setColumnWidth(1, 120)
        self.subtitle_table.verticalHeader().setDefaultSectionSize(50)
        self.subtitle_table.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed  # type: ignore[arg-type]
        )
        self.main_layout.addWidget(self.subtitle_table, 1)

        footer = QHBoxLayout()
        self.progress_bar = ProgressBar(self)
        self.status_label = BodyLabel(self.tr("准备就绪"), self)
        self.cancel_button = PushButton(self.tr("取消"), self, icon=FIF.CANCEL)
        self.cancel_button.clicked.connect(self.cancel)
        self.start_button = PrimaryPushButton(self.tr("开始后处理"), self, icon=FIF.PLAY)
        self.start_button.clicked.connect(self.start)
        footer.addWidget(self.progress_bar, 1)
        footer.addWidget(self.status_label)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.start_button)
        self.main_layout.addLayout(footer)

    def refresh_profiles(self, selected_id: str | None = None) -> None:
        profiles = self._profile_store.list()
        current = selected_id or getattr(cfg, "postprocess_profile", cfg.speed_profile).value
        if current not in {item.profile_id for item in profiles}:
            current = "balanced"
        self.profile_menu.clear()
        for profile in profiles:
            action = Action(text=profile.name)
            action.triggered.connect(
                lambda _checked=False, profile_id=profile.profile_id: self.select_profile(profile_id)
            )
            self.profile_menu.addAction(action)
        profile = self._profile_store.get(current)
        self.profile_button.setText(profile.name)
        profile_item = getattr(cfg, "postprocess_profile", cfg.speed_profile)
        if cfg.get(profile_item) != current:
            cfg.set(profile_item, current)
        self.set_mode(profile.config.speed_mode, persist=False)

    def select_profile(self, profile_id: str) -> None:
        profile_item = getattr(cfg, "postprocess_profile", cfg.speed_profile)
        cfg.set(profile_item, profile_id)
        self.refresh_profiles(profile_id)

    def set_mode(self, mode: str, *, persist: bool = True) -> None:
        mode = "analyze" if mode == "analyze" else "apply"
        self.mode_button.setText(self.tr("仅分析") if mode == "analyze" else self.tr("应用修改"))
        if persist:
            profile_item = getattr(cfg, "postprocess_profile", cfg.speed_profile)
            self._profile_store.set_field(cfg.get(profile_item), "speed_mode", mode)

    def _layout_changed(self) -> None:
        if self._loading_layout:
            return
        if self.layout_mode is not PostprocessLayoutMode.AUTO:
            self._structure_confirmation_required = False
            self.layout_warning.hide()
            self._effective_layout = self._layout_enum(self.layout_mode)
            if self.subtitle_path:
                self._load_working_data(self.subtitle_path, self._effective_layout)
        elif self.subtitle_path:
            self.load_subtitle(self.subtitle_path)
        self._update_start_enabled()

    def _output_layout_changed(self) -> None:
        if self.primary_srt_path and self.model.rowCount() > 0:
            self._mark_dirty()

    @property
    def layout_mode(self) -> PostprocessLayoutMode:
        return PostprocessLayoutMode(self.layout_combo.currentData())

    def select_subtitle(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("选择成型字幕"),
            "",
            self.tr("字幕文件 (*.srt *.vtt *.ass)"),
        )
        if path:
            self.load_subtitle(path)

    def load_subtitle(self, path: str) -> None:
        self.subtitle_path = os.path.abspath(path)
        self.input_label.setText(self.subtitle_path)
        self.status_label.setText(self.tr("已加载字幕"))
        self.primary_srt_path = None
        mode = self.layout_mode
        if mode is not PostprocessLayoutMode.AUTO:
            self._effective_layout = self._layout_enum(mode)
            self._structure_confirmation_required = False
            self._load_working_data(self.subtitle_path, self._effective_layout)
            self.layout_warning.hide()
            self._update_start_enabled()
            return

        metadata_layout = read_videocaptioner_layout(self.subtitle_path)
        if metadata_layout is not None:
            trusted_mode = self._mode_for_layout(metadata_layout)
            self._loading_layout = True
            self.layout_combo.setCurrentIndex(self.layout_combo.findData(trusted_mode.value))
            self._loading_layout = False
            self._effective_layout = (
                metadata_layout
                if trusted_mode is not PostprocessLayoutMode.SINGLE
                else SubtitleLayoutEnum.ONLY_ORIGINAL
            )
            self._structure_confirmation_required = False
            self._load_working_data(self.subtitle_path, metadata_layout)
            self.layout_warning.hide()
        else:
            imported = import_subtitle(self.subtitle_path)
            self.model.update_all(imported.data.to_json())
            self._effective_layout = imported.layout
            self._structure_confirmation_required = imported.confidence < 0.7
            if self._structure_confirmation_required:
                self.layout_warning.setText(
                    self.tr(
                        "无法确定输入字幕的上下行角色。请选择第一行是原文还是译文；"
                        "该选择只解释输入，输出顺序由右侧“输出布局”控制。"
                    )
                )
                self.layout_warning.show()
            else:
                self.layout_warning.setText(
                    self.tr("已自动识别为双语字幕；如原文与译文角色不符，请在开始前手动调整。")
                )
                self.layout_warning.show()
        self._set_dirty(False)
        self._update_start_enabled()

    @staticmethod
    def _layout_enum(mode: PostprocessLayoutMode) -> SubtitleLayoutEnum:
        if mode is PostprocessLayoutMode.TRANSLATE_ON_TOP:
            return SubtitleLayoutEnum.TRANSLATE_ON_TOP
        if mode is PostprocessLayoutMode.ORIGINAL_ON_TOP:
            return SubtitleLayoutEnum.ORIGINAL_ON_TOP
        if mode is PostprocessLayoutMode.TRANSLATE_ONLY:
            return SubtitleLayoutEnum.ONLY_TRANSLATE
        return SubtitleLayoutEnum.ONLY_ORIGINAL

    def _delivery_layout(self) -> SubtitleLayoutEnum:
        value = self.output_layout_combo.currentData()
        if value == SubtitleLayoutEnum.ORIGINAL_ON_TOP.value:
            return SubtitleLayoutEnum.ORIGINAL_ON_TOP
        if value == SubtitleLayoutEnum.TRANSLATE_ON_TOP.value:
            return SubtitleLayoutEnum.TRANSLATE_ON_TOP
        return self._effective_layout

    @staticmethod
    def _mode_for_layout(layout: SubtitleLayoutEnum) -> PostprocessLayoutMode:
        if layout is SubtitleLayoutEnum.TRANSLATE_ON_TOP:
            return PostprocessLayoutMode.TRANSLATE_ON_TOP
        if layout is SubtitleLayoutEnum.ORIGINAL_ON_TOP:
            return PostprocessLayoutMode.ORIGINAL_ON_TOP
        if layout is SubtitleLayoutEnum.ONLY_TRANSLATE:
            return PostprocessLayoutMode.TRANSLATE_ONLY
        if layout is SubtitleLayoutEnum.ONLY_ORIGINAL:
            return PostprocessLayoutMode.ORIGINAL_ONLY
        return PostprocessLayoutMode.SINGLE

    def _load_working_data(self, path: str, layout: SubtitleLayoutEnum) -> None:
        self.model.update_all(import_subtitle(path, layout_hint=layout).data.to_json())
        self._set_dirty(False)

    def select_media(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("关联视频或音频"),
            "",
            self.tr("媒体文件 (*.mp4 *.mkv *.mov *.avi *.webm *.mp3 *.wav *.m4a *.flac)"),
        )
        if path:
            self.media_path = os.path.abspath(path)
            self.media_action.setText(self.tr("已关联媒体"))

    def open_settings(self) -> None:
        window = self.window()
        interface = getattr(window, "postprocessSettingInterface", None)
        if interface is not None and hasattr(window, "switchTo"):
            window.switchTo(interface)

    def set_task(self, task: Any) -> None:
        self.cancel()
        self.task = task
        self._workflow_mode = bool(getattr(task, "need_next_task", False))
        subtitle_path = (
            getattr(task, "subtitle_path", None)
            or getattr(task, "source_subtitle_path", None)
            or getattr(task, "input_path", None)
        )
        self.media_path = getattr(task, "video_path", None) or getattr(task, "media_path", None)
        layout = getattr(task, "layout_mode", None)
        value = getattr(layout, "value", layout) if layout else None
        if layout:
            index = self.layout_combo.findData(value)
            if index >= 0:
                self._loading_layout = True
                self.layout_combo.setCurrentIndex(index)
                self._loading_layout = False
        input_data = getattr(task, "input_data", None)
        if input_data is not None:
            if subtitle_path:
                self.subtitle_path = os.path.abspath(subtitle_path)
                self.input_label.setText(self.subtitle_path)
            self.model.update_all(clone_subtitle_data(input_data).to_json())
            self._structure_confirmation_required = False
            if layout:
                self._effective_layout = self._layout_enum(PostprocessLayoutMode(value))
            self._update_start_enabled()
            self._set_dirty(False)
        elif subtitle_path:
            self.load_subtitle(subtitle_path)

    def _create_task(self) -> Any:
        if self.subtitle_path is None:
            raise ValueError("subtitle path is required")
        profile_item = getattr(cfg, "postprocess_profile", cfg.speed_profile)
        profile_id = cfg.get(profile_item)
        # 设置页用另一个 store 实例写盘；运行前重新读取，确保后处理设置的最新改动生效
        # （否则本页构造时载入的内存快照会静默忽略之后的改动，直到重启）。
        self._profile_store.reload()
        config = self._profile_store.resolve_config(profile_id)
        config.speed_mode = "analyze" if self.mode_button.text() == self.tr("仅分析") else "apply"
        try:
            task = TaskFactory.create_postprocess_task(
                self.subtitle_path,
                self.media_path,
                need_next_task=False,
            )
        except (AttributeError, TypeError):
            task = PostprocessTask(
                source_subtitle_path=str(self.subtitle_path),
                media_path=self.media_path,
                profile_id=profile_id,
                layout_mode=self.layout_mode,
                config_snapshot=config,
            )
        if hasattr(task, "profile_id"):
            task.profile_id = profile_id
        if hasattr(task, "layout_mode"):
            task.layout_mode = self.layout_mode
        if hasattr(task, "config_snapshot"):
            task.config_snapshot = config
        return task

    def _snapshot_task_input(self, task: PostprocessTask) -> None:
        task.input_data = clone_subtitle_data(ASRData.from_json(self.model._data))
        task.layout_mode = (
            PostprocessLayoutMode.TRANSLATE_ON_TOP
            if self._effective_layout is SubtitleLayoutEnum.TRANSLATE_ON_TOP
            else PostprocessLayoutMode.ORIGINAL_ON_TOP
            if self._effective_layout is SubtitleLayoutEnum.ORIGINAL_ON_TOP
            else PostprocessLayoutMode.TRANSLATE_ONLY
            if self._effective_layout is SubtitleLayoutEnum.ONLY_TRANSLATE
            else PostprocessLayoutMode.ORIGINAL_ONLY
            if self._effective_layout is SubtitleLayoutEnum.ONLY_ORIGINAL
            else PostprocessLayoutMode.SINGLE
        )

    def start(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            return
        has_memory_input = isinstance(self.task, PostprocessTask) and self.task.input_data is not None
        if (not self.subtitle_path or not Path(self.subtitle_path).is_file()) and not has_memory_input:
            InfoBar.warning(
                self.tr("请选择字幕"),
                self.tr("字幕后处理只接受已经成型的字幕文件"),
                duration=INFOBAR_DURATION_WARNING,
                parent=self,
            )
            return
        if self._structure_confirmation_required:
            InfoBar.warning(
                self.tr("请确认字幕结构"),
                self.tr("自动识别置信度不足，请先明确选择字幕结构。"),
                duration=INFOBAR_DURATION_WARNING,
                parent=self,
            )
            return
        if self.task is None or not self._workflow_mode:
            self.task = self._create_task()
        if not isinstance(self.task, PostprocessTask):
            raise TypeError("postprocess page requires a PostprocessTask")
        self._snapshot_task_input(self.task)
        try:
            from videocaptioner.ui.thread.postprocess_thread import PostprocessThread
        except ImportError as exc:
            InfoBar.error(
                self.tr("后处理不可用"),
                str(exc),
                duration=INFOBAR_DURATION_ERROR,
                parent=self,
            )
            return
        self._thread = PostprocessThread(self.task)
        self._thread.progress.connect(self._on_progress)
        self._thread.finished.connect(self._on_finished)
        self._thread.warning.connect(self._on_warning)
        self._thread.error.connect(self._on_error)
        self._thread.cancelled.connect(self._on_cancelled)
        self._set_processing(True)
        self.progress_bar.reset()
        self.status_label.setText(self.tr("正在分析字幕结构与阅读速度…"))
        self._thread.start()
        InfoBar.info(
            self.tr("开始后处理"),
            self.tr("输入字幕会被保留，结果将写入新文件"),
            duration=INFOBAR_DURATION_INFO,
            parent=self,
        )

    def process(self) -> None:
        self.start()

    def cancel(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            if hasattr(self._thread, "stop"):
                self._thread.stop()
            else:
                self._thread.requestInterruption()
            self.status_label.setText(self.tr("正在取消，等待当前步骤安全结束…"))
            return
        self._set_processing(False)

    def _set_processing(self, running: bool) -> None:
        self.start_button.setEnabled(not running and not self._structure_confirmation_required)
        self.cancel_button.setVisible(running)
        self.open_action.setEnabled(not running)
        self.layout_combo.setEnabled(not running)
        self.output_layout_combo.setEnabled(not running)
        self.subtitle_table.setEnabled(not running)

    def _update_start_enabled(self) -> None:
        running = self._thread is not None and self._thread.isRunning()
        self.start_button.setEnabled(not running and not self._structure_confirmation_required)

    def _on_progress(self, value: int, status: str) -> None:
        self.progress_bar.setValue(value)
        self.status_label.setText(status)

    def _on_warning(self, message: str) -> None:
        InfoBar.warning(
            self.tr("字幕后处理警告"),
            message,
            duration=INFOBAR_DURATION_WARNING,
            parent=self,
        )

    def _on_cancelled(self) -> None:
        self._set_processing(False)
        self.progress_bar.pause()
        self.status_label.setText(self.tr("已取消"))

    def _on_finished(self, video_path: str, output_path: str) -> None:
        self._set_processing(False)
        self.progress_bar.setValue(100)
        try:
            result = getattr(self._thread, "result", None)
            if result is not None:
                result_data = clone_subtitle_data(result.output_data)
                self._effective_layout = result.layout
            else:
                result_data = import_subtitle(output_path, layout_hint=self._effective_layout).data
            self.model.update_all(result_data.to_json())
            non_writing_result = (
                bool(result is not None and result.used_fallback)
                or bool(isinstance(self.task, PostprocessTask) and self.task.status in {"fallback", "skipped"})
                or bool(
                    isinstance(self.task, PostprocessTask)
                    and self.task.config_snapshot is not None
                    and self.task.config_snapshot.speed_mode == "analyze"
                )
            )
            if non_writing_result:
                self.primary_srt_path = None
                self._set_dirty(False)
                self.status_label.setText(self.tr("未生成后处理字幕"))
                InfoBar.warning(
                    self.tr("字幕后处理未写入结果"),
                    self.tr("任务已回退、跳过或处于仅分析模式，初版字幕保持不变。"),
                    duration=INFOBAR_DURATION_WARNING,
                    parent=self,
                )
                if self.task is not None and getattr(self.task, "need_next_task", False):
                    self.finished.emit(video_path, output_path)
                return
            self.primary_srt_path = self._canonical_output_path(output_path)
            if not self.save_primary_srt(show_feedback=False):
                raise OSError(self.tr("无法保存规范 SRT 工作稿"))
            if isinstance(self.task, PostprocessTask):
                self.task.postprocessed_subtitle_path = self.primary_srt_path
                self.task.active_subtitle_path = self.primary_srt_path
        except Exception as exc:
            self._on_error(str(exc))
            return
        self.status_label.setText(self.tr("处理完成"))
        InfoBar.success(
            self.tr("字幕后处理完成"),
            self.primary_srt_path,
            duration=INFOBAR_DURATION_SUCCESS,
            position=InfoBarPosition.BOTTOM,
            parent=self.parent(),
        )
        if self.task is not None and getattr(self.task, "need_next_task", False):
            self.finished.emit(video_path, self.primary_srt_path or output_path)

    def _on_error(self, error: str) -> None:
        self._set_processing(False)
        self.progress_bar.error()
        self.status_label.setText(self.tr("处理失败"))
        InfoBar.error(
            self.tr("字幕后处理失败"),
            error,
            duration=INFOBAR_DURATION_ERROR,
            parent=self,
        )

    def _canonical_output_path(self, output_path: str) -> str:
        return str(canonical_stage_path(self.subtitle_path or output_path, "后处理字幕"))

    def save_primary_srt(self, _checked: bool = False, *, show_feedback: bool = True) -> bool:
        if not self.primary_srt_path:
            if show_feedback:
                InfoBar.warning(
                    self.tr("尚无后处理结果"), self.tr("请先完成一次后处理。"),
                    duration=INFOBAR_DURATION_WARNING, parent=self,
                )
            return False
        try:
            save_canonical_srt(
                ASRData.from_json(self.model._data),
                self.primary_srt_path,
                layout=self._delivery_layout(),
            )
            self._set_dirty(False)
            if show_feedback:
                InfoBar.success(
                    self.tr("保存成功"), self.primary_srt_path,
                    duration=INFOBAR_DURATION_SUCCESS, parent=self,
                )
            return True
        except Exception as exc:
            if show_feedback:
                InfoBar.error(
                    self.tr("保存失败"), str(exc),
                    duration=INFOBAR_DURATION_ERROR, parent=self,
                )
            return False

    def export_copy(self, output_format: str) -> None:
        if not self.model._data:
            return
        default = Path(self.primary_srt_path or self.subtitle_path or "subtitle.srt").with_suffix(
            f".{output_format}"
        )
        path, _ = QFileDialog.getSaveFileName(
            self, self.tr("导出字幕"), str(default),
            f"{self.tr('字幕文件')} (*.{output_format})",
        )
        if not path:
            return
        path = str(Path(path).with_suffix(f".{output_format.lower()}"))
        try:
            reference = TaskFactory.get_style_reference(
                cfg.subtitle_style_name.value, SubtitleRenderModeEnum.ASS_STYLE
            )
            export_subtitle_atomic(
                ASRData.from_json(self.model._data),
                path,
                export_format=output_format,
                layout=self._delivery_layout(),
                ass_style=get_subtitle_style(cfg.subtitle_style_name.value) or "",
                reference_resolution=reference,
            )
            InfoBar.success(
                self.tr("导出成功"), path,
                duration=INFOBAR_DURATION_SUCCESS, parent=self,
            )
        except Exception as exc:
            InfoBar.error(
                self.tr("导出失败"), str(exc),
                duration=INFOBAR_DURATION_ERROR, parent=self,
            )

    def _mark_dirty(self) -> None:
        self._set_dirty(True)

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = dirty
        running = self._thread is not None and self._thread.isRunning()
        if dirty and not running:
            self.status_label.setText(self.tr("有未保存的修改"))
        elif self.model._data:
            self.status_label.setText(self.tr("已保存"))

    def dragEnterEvent(self, event) -> None:
        event.acceptProposedAction() if event.mimeData().hasUrls() else event.ignore()

    def dropEvent(self, event) -> None:
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if Path(path).suffix.lower() in {".srt", ".vtt", ".ass"}:
                self.load_subtitle(path)
                event.acceptProposedAction()
                return
        event.ignore()

    def closeEvent(self, event) -> None:
        self.cancel()
        super().closeEvent(event)
