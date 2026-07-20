# -*- coding: utf-8 -*-
import json
import os
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from PyQt5.QtCore import QAbstractTableModel, QModelIndex, Qt, QTime, pyqtSignal
from PyQt5.QtGui import QCloseEvent, QColor, QDragEnterEvent, QDropEvent, QKeyEvent
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    BodyLabel,
    CommandBar,
    InfoBar,
    InfoBarPosition,
    MessageBoxBase,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    RoundMenu,
    TableView,
    TextEdit,
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
    SubtitleTask,
    SupportedSubtitleFormats,
)
from videocaptioner.core.subtitle import get_subtitle_style
from videocaptioner.core.subtitle.io import (
    canonical_stage_path,
    clone_subtitle_data,
    export_subtitle_atomic,
    import_subtitle,
    save_canonical_srt,
)
from videocaptioner.core.translate.types import TargetLanguage
from videocaptioner.core.utils.platform_utils import open_folder, reveal_in_explorer
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.common.signal_bus import signalBus
from videocaptioner.ui.components.GlossaryReviewPage import GlossaryReviewPage
from videocaptioner.ui.components.SubtitleSettingDialog import SubtitleSettingDialog
from videocaptioner.ui.components.TranslationAuditPage import TranslationAuditPage
from videocaptioner.ui.components.TranslationModeSelector import TranslationModeSelector
from videocaptioner.ui.task_factory import TaskFactory
from videocaptioner.ui.thread.subtitle_thread import RetranslateThread, SubtitleThread


def save_editor_asr_data(
    asr_data: ASRData,
    save_path: str,
    layout: SubtitleLayoutEnum,
    style_name: str,
) -> None:
    """Save edited subtitle data while preserving ASS style settings."""
    style_str = get_subtitle_style(style_name) or ""
    reference = TaskFactory.get_style_reference(style_name, SubtitleRenderModeEnum.ASS_STYLE)
    export_subtitle_atomic(
        asr_data,
        save_path,
        layout=layout,
        ass_style=style_str,
        reference_resolution=reference,
    )


def load_editor_asr_data(file_path: str, layout: SubtitleLayoutEnum) -> ASRData:
    """Load subtitle data using the editor's current bilingual layout."""
    return import_subtitle(file_path, layout_hint=layout).data


class SubtitleTableModel(QAbstractTableModel):
    edited = pyqtSignal()

    def __init__(self, data: Union[str, Dict[str, Any]] = ""):
        super().__init__()
        self._data: Dict[str, Any] = {}
        if isinstance(data, str):
            self.load_data(data)
        else:
            self._data = data

    def load_data(self, data: str):
        """加载字幕数据"""
        try:
            self._data = json.loads(data)
            self.layoutChanged.emit()
        except json.JSONDecodeError:
            pass

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:  # type: ignore
        if not index.isValid() or not self._data:
            return None

        row = index.row()
        col = index.column()
        segment = self._data.get(str(row + 1))

        if not segment:
            return None

        if role == Qt.DisplayRole or role == Qt.EditRole:  # type: ignore
            if col == 0:
                return QTime(0, 0).addMSecs(segment["start_time"]).toString("hh:mm:ss.zzz")[:-2]
            elif col == 1:
                return QTime(0, 0).addMSecs(segment["end_time"]).toString("hh:mm:ss.zzz")[:-2]
            elif col == 2:
                return segment["original_subtitle"]
            elif col == 3:
                return segment["translated_subtitle"]
        elif role == Qt.TextAlignmentRole:  # type: ignore
            if col in [0, 1]:
                return Qt.AlignCenter  # type: ignore
        return None

    def setData(self, index: QModelIndex, value: Any, role: int = Qt.EditRole) -> bool:  # type: ignore
        if not index.isValid() or not self._data:
            return False

        if role == Qt.EditRole:  # type: ignore
            row = index.row()
            col = index.column()
            segment = self._data.get(str(row + 1))

            if not segment:
                return False

            if col == 2:
                segment["original_subtitle"] = value
            elif col == 3:
                segment["translated_subtitle"] = value
            else:
                return False

            self.dataChanged.emit(index, index, [Qt.DisplayRole, Qt.EditRole])  # type: ignore
            self.edited.emit()
            return True
        return False

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.DisplayRole,  # type: ignore
    ) -> Any:  # type: ignore
        if role == Qt.DisplayRole:  # type: ignore
            if orientation == Qt.Horizontal:  # type: ignore
                return [
                    self.tr("开始时间"),
                    self.tr("结束时间"),
                    self.tr("字幕内容"),
                    (self.tr("翻译字幕") if cfg.need_translate.value else self.tr("优化字幕")),
                ][section]
            elif orientation == Qt.Vertical:  # type: ignore
                return str(section + 1)  # 显示行号
        elif role == Qt.TextAlignmentRole:  # type: ignore
            return Qt.AlignCenter  # type: ignore  # 居中对齐
        return None

    def rowCount(self, parent: Optional[QModelIndex] = None) -> int:
        return len(self._data)

    def columnCount(self, parent: Optional[QModelIndex] = None) -> int:
        return 4

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        if not index.isValid():
            return Qt.NoItemFlags  # type: ignore
        if index.column() in [2, 3]:
            return Qt.ItemIsEditable | Qt.ItemIsEnabled | Qt.ItemIsSelectable  # type: ignore
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable  # type: ignore

    def update_data(self, new_data: Dict[str, str], *, mark_dirty: bool = False) -> None:
        """更新字幕数据"""
        updated_rows = set()

        # 更新内部数据
        for key, value in new_data.items():
            if key in self._data:
                self._data[key]["translated_subtitle"] = value
                row = list(self._data.keys()).index(key)
                updated_rows.add(row)

        # 如果有更新，发出dataChanged信号
        if updated_rows:
            min_row = min(updated_rows)
            max_row = max(updated_rows)
            top_left = self.index(min_row, 2)
            bottom_right = self.index(max_row, 3)
            self.dataChanged.emit(top_left, bottom_right, [Qt.DisplayRole, Qt.EditRole])  # type: ignore
            if mark_dirty:
                self.edited.emit()

    def update_all(self, data: Dict[str, Any], *, mark_dirty: bool = False) -> None:
        """更新所有数据"""
        self._data = data
        self.layoutChanged.emit()
        if mark_dirty:
            self.edited.emit()


class SubtitleInterface(QWidget):
    finished = pyqtSignal(str, str)

    def __init__(self, parent: Optional[QWidget] = None, *, profile_store=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.task: Optional[SubtitleTask] = None
        self.subtitle_path: Optional[str] = None
        self.primary_srt_path: Optional[str] = None
        self._dirty = False
        self.custom_prompt_text: str = cfg.main_translation_prompt.value
        self._profile_store = profile_store
        self.setAttribute(Qt.WA_DeleteOnClose)  # type: ignore
        self._init_ui()
        self._setup_signals()
        self._update_prompt_button_style()
        self.set_values()

    def _init_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setObjectName("main_layout")
        self.main_layout.setSpacing(20)

        self._setup_top_layout()
        self._setup_subtitle_table()
        self._setup_translation_workspace()
        self._setup_bottom_layout()

    def set_values(self):
        self.layout_button.setText(cfg.subtitle_layout.value.value)  # Get enum's string value
        self.translate_button.setChecked(cfg.need_translate.value)
        self.optimize_button.setChecked(cfg.need_optimize.value)
        self.target_language_button.setText(cfg.target_language.value.value)
        self.target_language_button.setEnabled(cfg.need_translate.value)
        self.translation_mode_selector.set_mode(cfg.translation_mode.value, persist=False)
        self._refresh_start_availability(ignore_processing=True)

    def _setup_top_layout(self):
        # 创建水平布局
        top_layout = QHBoxLayout()

        # 创建命令栏
        self.command_bar = CommandBar(self)
        self.command_bar.setToolButtonStyle(
            Qt.ToolButtonTextBesideIcon  # type: ignore
        )  # 设置图标和文字并排显示
        top_layout.addWidget(self.command_bar, 1)  # 设置stretch为1，使其尽可能占用空间

        self.save_action = Action(FIF.SAVE, self.tr("保存"), triggered=self.save_primary_srt)
        self.command_bar.addAction(self.save_action)

        export_menu = RoundMenu(parent=self)
        export_menu.view.setMaxVisibleItems(8)
        for format in OutputSubtitleFormatEnum:
            action = Action(text=format.value)
            action.triggered.connect(lambda checked, f=format.value: self.on_export_format_clicked(f))
            export_menu.addAction(action)

        export_button = TransparentDropDownPushButton(self.tr("导出"), self, FIF.SHARE)
        export_button.setMenu(export_menu)
        export_button.setFixedHeight(34)
        self.command_bar.addWidget(export_button)

        # 添加字幕排布下拉按钮
        self.layout_button = TransparentDropDownPushButton(self.tr("字幕排布"), self, FIF.LAYOUT)
        self.layout_button.setFixedHeight(34)
        self.layout_button.setMinimumWidth(125)
        self.layout_menu = RoundMenu(parent=self)
        for layout in ["译文在上", "原文在上", "仅译文", "仅原文"]:
            action = Action(text=layout)
            action.triggered.connect(
                lambda checked, layout_value=layout: signalBus.subtitle_layout_changed.emit(
                    layout_value
                )
            )
            self.layout_menu.addAction(action)
        self.layout_button.setMenu(self.layout_menu)
        self.command_bar.addWidget(self.layout_button)

        self.command_bar.addSeparator()

        # 添加字幕优化按钮
        self.optimize_button = Action(
            FIF.EDIT,
            self.tr("字幕校正"),
            triggered=self.on_subtitle_optimization_changed,
            checkable=True,
        )
        self.command_bar.addAction(self.optimize_button)

        # 添加字幕翻译按钮
        self.translate_button = Action(
            FIF.LANGUAGE,
            self.tr("字幕翻译"),
            triggered=self.on_subtitle_translation_changed,
            checkable=True,
        )
        self.command_bar.addAction(self.translate_button)

        # 添加翻译语言选择
        self.target_language_button = TransparentDropDownPushButton(
            self.tr("翻译语言"), self, FIF.LANGUAGE
        )
        self.target_language_button.setFixedHeight(34)
        self.target_language_button.setMinimumWidth(125)
        self.target_language_menu = RoundMenu(parent=self)
        self.target_language_menu.setMaxVisibleItems(10)
        for lang in TargetLanguage:
            action = Action(text=lang.value)
            action.triggered.connect(
                lambda checked, lang_value=lang.value: signalBus.target_language_changed.emit(
                    lang_value
                )
            )
            self.target_language_menu.addAction(action)
        self.target_language_button.setMenu(self.target_language_menu)

        self.command_bar.addWidget(self.target_language_button)

        # 添加文稿提示按钮
        self.prompt_button = Action(
            FIF.DOCUMENT, self.tr("Prompt"), triggered=self.show_prompt_dialog
        )
        self.command_bar.addAction(self.prompt_button)

        # 这里只保留上游断句/单句上限；速度、标点和时间轴属于下一阶段。
        self.command_bar.addAction(
            Action(FIF.SETTING, "", triggered=self.show_subtitle_settings)
        )

        # 添加视频播放按钮
        # self.command_bar.addAction(Action(FIF.VIDEO, "", triggered=self.show_video_player))

        # 添加打开文件夹按钮
        self.command_bar.addAction(Action(FIF.FOLDER, "", triggered=self.on_open_folder_clicked))

        self.command_bar.addSeparator()

        # 添加文件选择按钮
        self.command_bar.addAction(Action(FIF.FOLDER_ADD, "", triggered=self.on_file_select))

        # 添加开始按钮到水平布局
        self.start_button = PrimaryPushButton(self.tr("开始"), self, icon=FIF.PLAY)
        self.start_button.clicked.connect(
            lambda: self.start_subtitle_optimization(need_create_task=True)
        )
        self.start_button.setFixedHeight(34)
        top_layout.addWidget(self.start_button)

        self.main_layout.addLayout(top_layout)

    def _setup_subtitle_table(self):
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

        # 配置垂直表头
        self.subtitle_table.verticalHeader().setVisible(True)  # 显示垂直表头
        self.subtitle_table.verticalHeader().setDefaultAlignment(
            Qt.AlignCenter  # type: ignore
        )  # 居中对齐
        self.subtitle_table.verticalHeader().setDefaultSectionSize(50)  # 行高
        self.subtitle_table.verticalHeader().setMinimumWidth(20)  # 设置最小宽度

        self.subtitle_table.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed  # type: ignore
        )
        self.subtitle_table.clicked.connect(self.on_subtitle_clicked)
        # 添加右键菜单支持
        self.subtitle_table.setContextMenuPolicy(Qt.CustomContextMenu)  # type: ignore
        self.subtitle_table.customContextMenuRequested.connect(self.show_context_menu)

    def _setup_translation_workspace(self) -> None:
        """Create the shared editable workspace and the two full-size result pages."""

        self.workspace_stack = QStackedWidget(self)
        self.translation_workspace = QWidget(self.workspace_stack)
        workspace_layout = QVBoxLayout(self.translation_workspace)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        self.translation_mode_selector = TranslationModeSelector(
            self.translation_workspace, profile_store=self._profile_store
        )
        self.translation_mode_selector.availability_changed.connect(
            self._on_translation_availability_changed
        )
        workspace_layout.addWidget(self.translation_mode_selector)
        workspace_layout.addWidget(self.subtitle_table, 1)

        self.glossary_review_page = GlossaryReviewPage(self.workspace_stack)
        self.glossary_review_page.confirmed.connect(self._submit_term_confirmation)
        self.translation_audit_page = TranslationAuditPage(self.workspace_stack)
        self.translation_audit_page.closed.connect(self._show_translation_workspace)
        self.workspace_stack.addWidget(self.translation_workspace)
        self.workspace_stack.addWidget(self.glossary_review_page)
        self.workspace_stack.addWidget(self.translation_audit_page)
        self.main_layout.addWidget(self.workspace_stack, 1)

    def _setup_bottom_layout(self):
        self.bottom_layout = QHBoxLayout()
        self.progress_bar = ProgressBar(self)
        self.status_label = BodyLabel(self.tr("请拖入字幕文件"), self)
        self.status_label.setMinimumWidth(100)
        self.status_label.setAlignment(Qt.AlignCenter)  # type: ignore

        # 添加取消按钮
        self.cancel_button = PushButton(self.tr("取消"), self, icon=FIF.CANCEL)
        self.cancel_button.hide()  # 初始隐藏
        self.cancel_button.clicked.connect(self.cancel_optimization)

        self.bottom_layout.addWidget(self.progress_bar, 1)
        self.bottom_layout.addWidget(self.status_label)
        self.bottom_layout.addWidget(self.cancel_button)
        self.main_layout.addLayout(self.bottom_layout)

    def _setup_signals(self) -> None:
        signalBus.subtitle_layout_changed.connect(self.on_subtitle_layout_changed)
        signalBus.target_language_changed.connect(self.on_target_language_changed)
        signalBus.subtitle_optimization_changed.connect(self.on_subtitle_optimization_changed)
        signalBus.subtitle_translation_changed.connect(self.on_subtitle_translation_changed)
        # self.subtitle_setting_button.clicked.connect(self.show_subtitle_settings)
        # self.video_player_button.clicked.connect(self.show_video_player)

    def show_prompt_dialog(self) -> None:
        dialog = PromptDialog(self)
        if dialog.exec_():
            self.custom_prompt_text = cfg.main_translation_prompt.value
            self._update_prompt_button_style()

    def _update_prompt_button_style(self) -> None:
        if self.custom_prompt_text.strip():
            green_icon = FIF.DOCUMENT.colored(QColor(76, 255, 165), QColor(76, 255, 165))
            self.prompt_button.setIcon(green_icon)
        else:
            self.prompt_button.setIcon(FIF.DOCUMENT)

    def set_task(self, task: SubtitleTask) -> None:
        """设置任务并更新UI"""
        if hasattr(self, "subtitle_optimization_thread"):
            self.subtitle_optimization_thread.stop()  # type: ignore
        self.start_button.setEnabled(True)
        self.task = task
        self.subtitle_path = task.subtitle_path
        self.update_info(task)
        self.set_values()

    def update_info(self, task: SubtitleTask) -> None:
        """更新页面信息"""
        if not self.task:
            return
        layout = (
            task.subtitle_config.subtitle_layout
            if task.subtitle_config
            else cfg.subtitle_layout.value
        )
        input_data = getattr(task, "input_data", None)
        if input_data is not None:
            asr_data = clone_subtitle_data(input_data)
        else:
            original_subtitle_save_path = Path(str(self.task.subtitle_path))
            asr_data = load_editor_asr_data(str(original_subtitle_save_path), layout)
        self.model._data = asr_data.to_json()
        self.model.layoutChanged.emit()
        self.primary_srt_path = str(getattr(task, "output_path", "") or "") or None
        self._set_dirty(False)
        self.status_label.setText(self.tr("已加载文件"))

    def start_subtitle_optimization(self, need_create_task: bool = True) -> None:
        if self._is_processing():
            return
        if (
            need_create_task
            and cfg.need_translate.value
            and not self.translation_mode_selector.is_selected_mode_available
        ):
            missing = self.translation_mode_selector.missing_configuration(
                self.translation_mode_selector.selected_mode
            )
            InfoBar.warning(
                self.tr("翻译配置不完整"),
                self.tr("缺少：") + "、".join(missing),
                duration=INFOBAR_DURATION_WARNING,
                parent=self,
            )
            self._refresh_start_availability()
            return
        if not self.subtitle_path:
            InfoBar.warning(
                self.tr("警告"),
                self.tr("请先加载字幕文件"),
                duration=INFOBAR_DURATION_WARNING,
                parent=self,
            )
            return
        self.start_button.setEnabled(False)
        self.progress_bar.resume()
        self.progress_bar.reset()
        self.cancel_button.show()

        if need_create_task:
            self.task = TaskFactory.create_subtitle_task(
                file_path=self.subtitle_path,
                video_path=None,
                imported_glossary_path=(
                    self.translation_mode_selector.imported_glossary_path or None
                ),
            )
            self.task.editor_data_json = deepcopy(self.model._data)
        if not self.task:
            self.start_button.setEnabled(True)
            self.cancel_button.hide()
            return
        self.task.editor_data_json = deepcopy(self.model._data)
        self.subtitle_optimization_thread = SubtitleThread(self.task)
        self.subtitle_optimization_thread.finished.connect(self.on_subtitle_optimization_finished)
        self.subtitle_optimization_thread.progress.connect(self.on_subtitle_optimization_progress)
        self.subtitle_optimization_thread.update.connect(self.update_data)
        self.subtitle_optimization_thread.update_all.connect(self.update_all)
        self.subtitle_optimization_thread.error.connect(self.on_subtitle_optimization_error)
        if hasattr(self.subtitle_optimization_thread, "term_confirmation_required"):
            self.subtitle_optimization_thread.term_confirmation_required.connect(
                self._show_term_confirmation
            )
        if hasattr(self.subtitle_optimization_thread, "audit_ready"):
            self.subtitle_optimization_thread.audit_ready.connect(self._show_translation_audit)
        self.subtitle_optimization_thread.set_custom_prompt_text(self.custom_prompt_text)
        self.subtitle_optimization_thread.start()
        InfoBar.info(
            self.tr("开始优化"),
            self.tr("开始优化字幕"),
            duration=INFOBAR_DURATION_INFO,
            parent=self,
        )

    def process(self) -> None:
        """主处理函数"""
        # 检查是否有任务
        self.start_subtitle_optimization(need_create_task=False)

    def on_subtitle_optimization_finished(self, video_path: str, output_path: str) -> None:
        self._refresh_start_availability(ignore_processing=True)
        self.cancel_button.hide()
        self.progress_bar.setValue(100)
        self.primary_srt_path = output_path
        self.subtitle_path = output_path
        if Path(output_path).is_file():
            self.load_subtitle_file(output_path, mark_clean=True)
        else:
            self.save_primary_srt(show_feedback=False)
        if self.task and self.task.need_next_task:
            self.finished.emit(video_path, output_path)
        title = self.tr("优化完成")
        InfoBar.success(
            title,
            title,
            duration=INFOBAR_DURATION_SUCCESS,
            position=InfoBarPosition.BOTTOM,
            parent=self.parent(),
        )

    def on_subtitle_optimization_error(self, error: str) -> None:
        self._show_translation_workspace()
        self._refresh_start_availability(ignore_processing=True)
        self.cancel_button.hide()  # 隐藏取消按钮
        self.progress_bar.error()
        InfoBar.error(
            self.tr("优化失败"),
            self.tr(error),
            duration=INFOBAR_DURATION_ERROR,
            parent=self,
        )

    def on_subtitle_optimization_progress(self, value: int, status: str) -> None:
        self.progress_bar.setValue(value)
        self.status_label.setText(status)

    def _on_translation_availability_changed(self, available: bool, reason: str) -> None:
        self.start_button.setToolTip("" if available else reason)
        self._refresh_start_availability()

    def _refresh_start_availability(self, *, ignore_processing: bool = False) -> None:
        if not hasattr(self, "start_button") or (
            self._is_processing() and not ignore_processing
        ):
            return
        available = (
            not cfg.need_translate.value
            or self.translation_mode_selector.is_selected_mode_available
        )
        self.start_button.setEnabled(available)
        missing = self.translation_mode_selector.missing_configuration(
            self.translation_mode_selector.selected_mode
        )
        self.start_button.setToolTip(
            self.tr("缺少：") + "、".join(missing)
            if cfg.need_translate.value and missing
            else ""
        )

    def _show_term_confirmation(self, candidates) -> None:
        source_by_id = {
            int(key): str(value.get("original_subtitle", ""))
            for key, value in self.model._data.items()
        }
        ordered_ids = sorted(source_by_id)
        positions = {cue_id: position for position, cue_id in enumerate(ordered_ids)}
        radius = (
            self.task.subtitle_config.term_context_radius
            if self.task is not None and self.task.subtitle_config is not None
            else int(cfg.term_context_radius.value)
        )
        context = {}
        for candidate in candidates:
            representative_ids = candidate.representative_context_ids or tuple(
                candidate.occurrence_ids[:5]
            )
            for cue_id in representative_ids:
                position = positions.get(cue_id)
                if position is None:
                    continue
                window = ordered_ids[
                    max(0, position - radius) : min(len(ordered_ids), position + radius + 1)
                ]
                context[cue_id] = "\n".join(
                    f"{'→' if value == cue_id else ' '} #{value} {source_by_id[value]}"
                    for value in window
                )
        self.glossary_review_page.set_candidates(candidates, context)
        self.workspace_stack.setCurrentWidget(self.glossary_review_page)
        self.status_label.setText(self.tr("等待人工确认术语"))

    def _submit_term_confirmation(self, candidates) -> None:
        thread = getattr(self, "subtitle_optimization_thread", None)
        if thread is not None:
            thread.submit_term_confirmation(candidates)
        self._show_translation_workspace()
        self.status_label.setText(self.tr("继续翻译"))

    def _show_translation_audit(self, report) -> None:
        self.translation_audit_page.set_report(report)
        self.workspace_stack.setCurrentWidget(self.translation_audit_page)

    def _show_translation_workspace(self) -> None:
        self.workspace_stack.setCurrentWidget(self.translation_workspace)

    def update_data(self, data):
        self.model.update_data(data)

    def update_all(self, data):
        self.model.update_all(data)

    def remove_widget(self) -> None:
        """隐藏顶部开始按钮和底部进度条"""
        self.start_button.hide()
        for i in range(self.bottom_layout.count()):
            item = self.bottom_layout.itemAt(i)
            if item:
                widget = item.widget()
                if widget:
                    widget.hide()

    def on_file_select(self) -> None:
        # 构建文件过滤器
        subtitle_formats = " ".join(f"*.{fmt.value}" for fmt in SupportedSubtitleFormats)
        filter_str = f"{self.tr('字幕文件')} ({subtitle_formats})"

        file_path, _ = QFileDialog.getOpenFileName(self, self.tr("选择字幕文件"), "", filter_str)
        if file_path:
            self.subtitle_path = file_path
            self.load_subtitle_file(file_path)

    def _default_primary_path(self) -> Optional[str]:
        if self.primary_srt_path:
            return str(Path(self.primary_srt_path).with_suffix(".srt"))
        if not self.subtitle_path:
            return None
        return str(canonical_stage_path(self.subtitle_path, "初版字幕"))

    def save_primary_srt(self, _checked: bool = False, *, show_feedback: bool = True) -> bool:
        """Persist the editable working draft to its canonical SRT artifact."""
        save_path = self._default_primary_path()
        if not save_path:
            if show_feedback:
                InfoBar.warning(
                    self.tr("警告"), self.tr("请先加载字幕文件"),
                    duration=INFOBAR_DURATION_WARNING, parent=self,
                )
            return False
        try:
            save_canonical_srt(
                ASRData.from_json(self.model._data), save_path, layout=cfg.subtitle_layout.value
            )
            self.primary_srt_path = save_path
            self._set_dirty(False)
            if show_feedback:
                InfoBar.success(
                    self.tr("保存成功"), self.tr("工作稿已保存至:") + save_path,
                    duration=INFOBAR_DURATION_SUCCESS, parent=self,
                )
            return True
        except Exception as exc:
            if show_feedback:
                InfoBar.error(
                    self.tr("保存失败"), self.tr("保存工作稿失败: ") + str(exc),
                    duration=INFOBAR_DURATION_ERROR, parent=self,
                )
            return False

    def on_export_format_clicked(self, format: str) -> None:
        """Export a derived copy without changing the canonical SRT artifact."""
        if not self.subtitle_path:
            InfoBar.warning(
                self.tr("警告"),
                self.tr("请先加载字幕文件"),
                duration=INFOBAR_DURATION_WARNING,
                parent=self,
            )
            return

        # 获取保存路径
        default_name = Path(self._default_primary_path() or self.subtitle_path).stem
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            self.tr("保存字幕文件"),
            default_name,  # 使用原文件名作为默认名
            f"{self.tr('字幕文件')} (*.{format})",
        )
        if not file_path:
            return
        if Path(file_path).suffix.lower() != f".{format.lower()}":
            file_path = str(Path(file_path).with_suffix(f".{format.lower()}"))

        try:
            # 转换并保存字幕
            asr_data = ASRData.from_json(self.model._data)
            layout = cfg.subtitle_layout.value

            save_editor_asr_data(
                asr_data,
                file_path,
                layout,
                cfg.subtitle_style_name.value,
            )
            InfoBar.success(
                self.tr("保存成功"),
                self.tr("字幕已保存至:") + file_path,
                duration=INFOBAR_DURATION_SUCCESS,
                parent=self,
            )
            reveal_in_explorer(file_path)
        except Exception as e:
            InfoBar.error(
                self.tr("保存失败"),
                self.tr("保存字幕文件失败: ") + str(e),
                duration=INFOBAR_DURATION_ERROR,
                parent=self,
            )

    def on_open_folder_clicked(self) -> None:
        """打开文件夹按钮点击事件"""
        if not self.task:
            InfoBar.warning(
                self.tr("警告"),
                self.tr("请先加载字幕文件"),
                duration=INFOBAR_DURATION_WARNING,
                parent=self,
            )
            return
        if not self.task:
            return
        if self.task.output_path:
            output_path = Path(self.task.output_path)
            target_dir = str(
                output_path.parent if output_path.exists() else Path(self.task.subtitle_path).parent
            )
        else:
            target_dir = str(Path(self.task.subtitle_path).parent)
        open_folder(target_dir)

    # Compatibility for extensions and older tests that still call the former slot.
    on_save_format_clicked = on_export_format_clicked

    def load_subtitle_file(self, file_path: str, *, mark_clean: bool = True) -> None:
        self.subtitle_path = file_path
        asr_data = load_editor_asr_data(file_path, cfg.subtitle_layout.value)
        self.model._data = asr_data.to_json()
        self.model.layoutChanged.emit()
        if mark_clean:
            self._set_dirty(False)
        self.status_label.setText(self.tr("已加载文件"))

    def _mark_dirty(self) -> None:
        self._set_dirty(True)

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = dirty
        if dirty and not self._is_processing():
            self.status_label.setText(self.tr("有未保存的修改"))
        elif self.model._data:
            self.status_label.setText(self.tr("已保存"))

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        event.accept() if event.mimeData().hasUrls() else event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        files = [u.toLocalFile() for u in event.mimeData().urls()]
        for file_path in files:
            if not os.path.isfile(file_path):
                continue

            file_ext = os.path.splitext(file_path)[1][1:].lower()

            # 检查文件格式是否支持
            supported_formats = {fmt.value for fmt in SupportedSubtitleFormats}
            is_supported = file_ext in supported_formats

            if is_supported:
                self.load_subtitle_file(file_path)
                InfoBar.success(
                    self.tr("导入成功"),
                    self.tr("成功导入") + os.path.basename(file_path),
                    duration=INFOBAR_DURATION_SUCCESS,
                    position=InfoBarPosition.BOTTOM,
                    parent=self,
                )
                break
            else:
                InfoBar.error(
                    self.tr("格式错误") + file_ext,
                    self.tr("支持的字幕格式:") + str(supported_formats),
                    duration=INFOBAR_DURATION_ERROR,
                    parent=self,
                )
        event.accept()

    def closeEvent(self, event: QCloseEvent) -> None:
        if hasattr(self, "subtitle_optimization_thread"):
            self.subtitle_optimization_thread.stop()  # type: ignore
        super().closeEvent(event)

    def show_subtitle_settings(self) -> None:
        """Show settings owned by the upstream optimization stage."""
        SubtitleSettingDialog(self.window()).exec_()

    def show_video_player(self) -> None:
        """显示视频播放器窗口"""
        # 创建视频播放器窗口（延迟导入，因为vlc是可选依赖）
        from videocaptioner.ui.components.MyVideoWidget import MyVideoWidget

        self.video_player = MyVideoWidget()
        self.video_player.resize(800, 600)

        def signal_update() -> None:
            if not self.model._data:
                return
            temp_srt_path = os.path.join(tempfile.gettempdir(), "temp_subtitle.ass")
            save_editor_asr_data(
                ASRData.from_json(self.model._data),
                temp_srt_path,
                cfg.subtitle_layout.value,
                cfg.subtitle_style_name.value,
            )
            signalBus.add_subtitle(temp_srt_path)

        # 如果有字幕文件,则添加字幕
        signal_update()

        signalBus.subtitle_layout_changed.connect(signal_update)
        self.model.dataChanged.connect(signal_update)
        self.model.layoutChanged.connect(signal_update)

        # 如果有关联的视频文件,则自动加载
        # Note: SubtitleTask doesn't have file_path attribute
        # if self.task and hasattr(self.task, "file_path") and self.task.file_path:
        #     self.video_player.setVideo(QUrl.fromLocalFile(self.task.file_path))

        self.video_player.show()
        self.video_player.play()

    def on_subtitle_clicked(self, index: QModelIndex) -> None:
        row = index.row()
        item = list(self.model._data.values())[row]
        start_time = item["start_time"]  # 毫秒
        end_time = item["end_time"] - 50 if item["end_time"] - 50 > start_time else item["end_time"]
        signalBus.play_video_segment(start_time, end_time)

    def show_context_menu(self, pos) -> None:
        """显示右键菜单"""
        menu = RoundMenu(parent=self)

        # 获取选中的行
        indexes = self.subtitle_table.selectedIndexes()
        if not indexes:
            return

        # 获取唯一的行号
        rows = sorted(set(index.row() for index in indexes))
        if not rows:
            return

        # 添加菜单项
        merge_action = Action(FIF.LINK, self.tr("合并"))
        delete_action = Action(FIF.DELETE, self.tr("删除"))
        retranslate_action = Action(FIF.SYNC, self.tr("重新翻译"))
        menu.addAction(merge_action)
        menu.addAction(delete_action)
        menu.addAction(retranslate_action)
        merge_action.setShortcut("Ctrl+M")
        delete_action.setShortcut("Delete")
        retranslate_action.setShortcut("Ctrl+T")

        merge_action.setEnabled(len(rows) > 1)
        retranslate_action.setEnabled(cfg.need_translate.value and not self._is_processing())

        merge_action.triggered.connect(lambda: self.merge_selected_rows(rows))
        delete_action.triggered.connect(lambda: self.delete_selected_rows(rows))
        retranslate_action.triggered.connect(lambda: self.retranslate_selected_rows(rows))

        # 显示菜单
        menu.exec(self.subtitle_table.viewport().mapToGlobal(pos))

    def merge_selected_rows(self, rows: List[int]) -> None:
        """合并选中的字幕行"""
        if not rows or len(rows) < 2:
            return

        # 获取选中行的数据
        data = self.model._data
        data_list = list(data.values())

        # 获取第一行和最后一行的时间戳
        first_row = data_list[rows[0]]
        last_row = data_list[rows[-1]]
        start_time = first_row["start_time"]
        end_time = last_row["end_time"]

        # 合并字幕内容
        original_subtitles = []
        translated_subtitles = []
        for row in rows:
            item = data_list[row]
            original_subtitles.append(item["original_subtitle"])
            translated_subtitles.append(item["translated_subtitle"])

        merged_original = " ".join(original_subtitles)
        merged_translated = " ".join(translated_subtitles)

        # 创建新的合并后的字幕项
        merged_item = {
            "start_time": start_time,
            "end_time": end_time,
            "original_subtitle": merged_original,
            "translated_subtitle": merged_translated,
        }

        # 获取所有需要保留的键
        keys = list(data.keys())
        preserved_keys = keys[: rows[0]] + keys[rows[-1] + 1 :]

        # 创建新的数据字典
        new_data = {}
        for i, key in enumerate(preserved_keys):
            if i == rows[0]:
                new_key = f"{len(new_data) + 1}"
                new_data[new_key] = merged_item
            new_key = f"{len(new_data) + 1}"
            new_data[new_key] = data[key]

        # 如果合并的是最后几行，需要确保合并项被添加
        if rows[0] >= len(preserved_keys):
            new_key = f"{len(new_data) + 1}"
            new_data[new_key] = merged_item

        # 更新模型数据
        self.subtitle_table.clearSelection()
        self.model.update_all(new_data, mark_dirty=True)

        # 显示成功提示
        InfoBar.success(
            self.tr("合并成功"),
            self.tr("已成功合并选中的字幕行"),
            duration=INFOBAR_DURATION_SUCCESS,
            parent=self,
        )

    def delete_selected_rows(self, rows: List[int]) -> None:
        """删除选中的字幕行"""
        if not rows:
            return

        data = self.model._data
        keys = list(data.keys())
        rows_set = set(rows)

        new_data = {}
        for i, key in enumerate(keys):
            if i not in rows_set:
                new_key = f"{len(new_data) + 1}"
                new_data[new_key] = data[key]

        self.subtitle_table.clearSelection()
        self.model.update_all(new_data, mark_dirty=True)

    def _is_processing(self) -> bool:
        """是否有任何处理任务正在运行"""
        if (
            hasattr(self, "subtitle_optimization_thread")
            and self.subtitle_optimization_thread.isRunning()
        ):  # type: ignore
            return True
        if hasattr(self, "_retranslate_thread") and self._retranslate_thread.isRunning():
            return True
        return False

    def retranslate_selected_rows(self, rows: List[int]) -> None:
        """重新翻译选中的字幕行"""
        if not rows or not self.model._data:
            return
        if self._is_processing():
            return

        # 提取选中行数据，保留原始键名（行号字符串）
        all_keys = list(self.model._data.keys())
        selected_data = {all_keys[row]: self.model._data[all_keys[row]] for row in rows}

        # 获取当前翻译配置
        subtitle_task = TaskFactory.create_subtitle_task(file_path=self.subtitle_path or "")
        config = subtitle_task.subtitle_config
        if not config:
            return

        self.start_button.setEnabled(False)
        self.status_label.setText(self.tr("正在重新翻译..."))
        self.progress_bar.resume()
        self.progress_bar.reset()

        file_name = Path(self.subtitle_path).name if self.subtitle_path else ""
        self._retranslate_thread = RetranslateThread(selected_data, config, file_name)
        self._retranslate_thread.finished.connect(self._on_retranslate_finished)
        self._retranslate_thread.progress.connect(self.on_subtitle_optimization_progress)
        self._retranslate_thread.error.connect(self._on_retranslate_error)
        self._retranslate_thread.start()

    def _on_retranslate_finished(self, result: dict) -> None:
        self._refresh_start_availability(ignore_processing=True)
        self.model.update_data(result, mark_dirty=True)
        self.progress_bar.setValue(100)
        self.status_label.setText(self.tr("重新翻译完成"))
        InfoBar.success(
            self.tr("翻译完成"),
            self.tr("已更新选中行的翻译"),
            duration=INFOBAR_DURATION_SUCCESS,
            parent=self,
        )

    def _on_retranslate_error(self, error: str) -> None:
        self._refresh_start_availability(ignore_processing=True)
        self.progress_bar.error()
        self.status_label.setText(self.tr("重新翻译失败"))
        InfoBar.error(
            self.tr("翻译失败"),
            error,
            duration=INFOBAR_DURATION_ERROR,
            parent=self,
        )

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """处理键盘事件"""
        if event.modifiers() == Qt.ControlModifier and event.key() == Qt.Key_M:  # type: ignore
            indexes = self.subtitle_table.selectedIndexes()
            if indexes:
                rows = sorted(set(index.row() for index in indexes))
                if len(rows) > 1:
                    self.merge_selected_rows(rows)
            event.accept()
        elif event.key() == Qt.Key_Delete:  # type: ignore
            indexes = self.subtitle_table.selectedIndexes()
            if indexes:
                rows = sorted(set(index.row() for index in indexes))
                self.delete_selected_rows(rows)
            event.accept()
        elif event.modifiers() == Qt.ControlModifier and event.key() == Qt.Key_T:  # type: ignore
            if cfg.need_translate.value and not self._is_processing():
                indexes = self.subtitle_table.selectedIndexes()
                if indexes:
                    rows = sorted(set(index.row() for index in indexes))
                    self.retranslate_selected_rows(rows)
            event.accept()
        else:
            super().keyPressEvent(event)

    def cancel_optimization(self) -> None:
        """取消字幕校正"""
        if hasattr(self, "subtitle_optimization_thread"):
            self.subtitle_optimization_thread.stop()  # type: ignore
            self._show_translation_workspace()
            self._refresh_start_availability()
            self.cancel_button.hide()
            self.progress_bar.resume()  # 恢复正常状态
            self.progress_bar.setValue(0)
            self.status_label.setText(self.tr("已取消校正"))
            InfoBar.warning(
                self.tr("已取消"),
                self.tr("字幕校正已取消"),
                duration=INFOBAR_DURATION_WARNING,
                parent=self,
            )

    def on_target_language_changed(self, language: str) -> None:
        """处理翻译语言变更"""
        for lang in TargetLanguage:
            if lang.value == language:
                self.target_language_button.setText(lang.value)
                cfg.set(cfg.target_language, lang)
                break

    def on_subtitle_optimization_changed(self, checked: bool) -> None:
        """处理字幕优化开关变更"""
        cfg.set(cfg.need_optimize, checked)
        self.optimize_button.setChecked(checked)

    def on_subtitle_translation_changed(self, checked: bool) -> None:
        """处理字幕翻译开关变更"""
        cfg.set(cfg.need_translate, checked)
        self.translate_button.setChecked(checked)
        # 控制翻译语言选择按钮的启用状态
        self.target_language_button.setEnabled(checked)
        self._refresh_start_availability()

    def on_subtitle_layout_changed(self, layout: str) -> None:
        """处理字幕排布变更"""
        layout_enum = SubtitleLayoutEnum(layout)  # Convert string to enum
        cfg.set(cfg.subtitle_layout, layout_enum)
        self.layout_button.setText(layout)


class PromptDialog(MessageBoxBase):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setup_ui()
        self.setWindowTitle(self.tr("主翻译 Prompt"))
        # 连接按钮点击事件
        self.yesButton.clicked.connect(self.save_prompt)

    def setup_ui(self) -> None:
        self.titleLabel = BodyLabel(self.tr("主翻译 Prompt"), self)

        # 添加文本编辑框
        self.text_edit = TextEdit(self)
        self.text_edit.setPlaceholderText(
            self.tr(
                "请输入主翻译角色的长期翻译要求。\n\n"
                "例如：目标语语域、人名与专名偏好、必须保留的格式，以及领域术语规则。"
            )
        )
        self.text_edit.setText(cfg.main_translation_prompt.value)

        self.text_edit.setMinimumWidth(420)
        self.text_edit.setMinimumHeight(380)

        # 添加到布局
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.text_edit)
        self.viewLayout.setSpacing(10)

        # 设置按钮文本
        self.yesButton.setText(self.tr("确定"))
        self.cancelButton.setText(self.tr("取消"))

    def get_prompt(self) -> str:
        return self.text_edit.toPlainText()

    def save_prompt(self) -> None:
        # 在点击确定按钮时保存提示文本到配置
        prompt_text = self.text_edit.toPlainText()
        cfg.set(cfg.main_translation_prompt, prompt_text)


if __name__ == "__main__":
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough  # type: ignore
    )
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)  # type: ignore
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)  # type: ignore

    app = QApplication(sys.argv)
    window = SubtitleInterface()
    window.show()
    sys.exit(app.exec_())
