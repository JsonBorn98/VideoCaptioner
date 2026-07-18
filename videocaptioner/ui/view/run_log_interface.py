"""Live, in-application view of VideoCaptioner's runtime log stream."""

import logging
from collections import deque
from dataclasses import dataclass

from PyQt5.QtCore import Qt, QTimer, pyqtSlot
from PyQt5.QtGui import QColor, QFont, QFontDatabase, QPalette, QTextCharFormat, QTextCursor
from PyQt5.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    PlainTextEdit,
    PushButton,
    SearchLineEdit,
    SubtitleLabel,
    isDarkTheme,
)
from qfluentwidgets import (
    FluentIcon as FIF,
)

from videocaptioner.config import LOG_PATH
from videocaptioner.core.utils.logger import register_log_handler, unregister_log_handler
from videocaptioner.core.utils.platform_utils import open_folder
from videocaptioner.core.utils.stage_summary import StageSummary, format_stage_summary
from videocaptioner.ui.common.log_bridge import (
    QtLogHandler,
    install_stage_summary_emitter,
    uninstall_stage_summary_emitter,
)

MAX_LOG_ENTRIES = 5000
APP_LOG_PATH = LOG_PATH / "app.log"


@dataclass(frozen=True)
class RunLogEntry:
    timestamp: str
    level: int
    logger_name: str
    message: str


class RunLogInterface(QWidget):
    """Render INFO-and-above records without allowing workers to touch Qt widgets."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("runLogInterface")
        self.setWindowTitle(self.tr("运行日志"))

        self._entries: deque[RunLogEntry] = deque(maxlen=MAX_LOG_ENTRIES)
        self._follow_paused = False
        self._is_shutdown = False
        self._minimum_level = logging.INFO
        self._search_query = ""
        self._visible_count = 0
        self._format_cache = None
        self._format_cache_key = None

        self._setup_ui()
        self._connect_controls()

        self._handler = QtLogHandler(logging.INFO)
        self._handler.emitter.record_emitted.connect(
            self._append_log_event,
            Qt.QueuedConnection,  # type: ignore
        )
        self._handler.emitter.stage_summary_emitted.connect(
            self._append_stage_summary,
            Qt.QueuedConnection,  # type: ignore
        )
        register_log_handler(self._handler)
        install_stage_summary_emitter(self._handler.emitter)
        self._update_status()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)

        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        title = SubtitleLabel(self.tr("运行日志"), self)
        title_row.addWidget(title)
        title_row.addStretch()
        self.connection_label = CaptionLabel(self.tr("● 实时连接"), self)
        self.connection_label.setStyleSheet("color: #27a269; font-weight: 600;")
        title_row.addWidget(self.connection_label)
        layout.addLayout(title_row)

        description = BodyLabel(
            self.tr("查看当前会话的运行叙述、警告与错误；完整诊断记录仍保存在 app.log。"),
            self,
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        self.search_edit = SearchLineEdit(self)
        self.search_edit.setPlaceholderText(self.tr("搜索日志内容或模块"))
        self.search_edit.setFixedWidth(260)
        toolbar.addWidget(self.search_edit)

        self.level_combo = ComboBox(self)
        self.level_combo.addItem(self.tr("全部级别"), userData=logging.INFO)
        self.level_combo.addItem(self.tr("警告及以上"), userData=logging.WARNING)
        self.level_combo.addItem(self.tr("仅错误"), userData=logging.ERROR)
        self.level_combo.setFixedWidth(130)
        toolbar.addWidget(self.level_combo)

        toolbar.addStretch()

        self.pause_button = PushButton(FIF.PAUSE, self.tr("暂停跟随"), self)
        self.pause_button.setCheckable(True)
        toolbar.addWidget(self.pause_button)

        self.clear_button = PushButton(FIF.DELETE, self.tr("清空显示"), self)
        toolbar.addWidget(self.clear_button)

        self.open_folder_button = PushButton(FIF.FOLDER, self.tr("打开日志文件夹"), self)
        toolbar.addWidget(self.open_folder_button)
        layout.addLayout(toolbar)

        self.log_view = PlainTextEdit(self)
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText(self.tr("任务开始后，运行日志会实时显示在这里。"))
        self.log_view.setLineWrapMode(PlainTextEdit.NoWrap)
        # Every entry remains one QTextBlock; multiline messages use visual
        # line separators so retention and deque eviction stay entry-aligned.
        self.log_view.document().setMaximumBlockCount(MAX_LOG_ENTRIES)
        fixed_font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        fixed_font.setStyleHint(QFont.Monospace)
        self.log_view.setFont(fixed_font)
        layout.addWidget(self.log_view, 1)

        footer = QHBoxLayout()
        self.status_label = CaptionLabel(self)
        footer.addWidget(self.status_label)
        footer.addStretch()
        file_label = CaptionLabel(str(APP_LOG_PATH), self)
        file_label.setTextInteractionFlags(Qt.TextSelectableByMouse)  # type: ignore
        footer.addWidget(file_label)
        layout.addLayout(footer)

    def _connect_controls(self) -> None:
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(180)
        self._search_timer.timeout.connect(self._apply_filters)
        self.search_edit.textChanged.connect(self._schedule_search)
        self.level_combo.currentIndexChanged.connect(self._apply_filters)
        self.pause_button.toggled.connect(self._set_follow_paused)
        self.clear_button.clicked.connect(self.clear_display)
        self.open_folder_button.clicked.connect(self.open_log_folder)

    @pyqtSlot(str, int, str, str)
    def _append_log_event(
        self,
        timestamp: str,
        level: int,
        logger_name: str,
        message: str,
    ) -> None:
        evicted = self._entries[0] if len(self._entries) == MAX_LOG_ENTRIES else None
        evicted_was_visible = evicted is not None and self._entry_is_visible(evicted)
        entry = RunLogEntry(timestamp, level, logger_name, message)
        self._entries.append(entry)
        if evicted_was_visible:
            self._visible_count -= 1

        is_visible = self._entry_is_visible(entry)
        if is_visible:
            self._visible_count += 1

        if not self._follow_paused and evicted_was_visible:
            self._remove_first_entry_block()
        if not self._follow_paused and is_visible:
            self._append_entry(entry)
        self._update_status()

    @pyqtSlot(object)
    def _append_stage_summary(self, summary: StageSummary) -> None:
        self._append_log_event(
            "SUMMARY",
            logging.INFO,
            "stage",
            format_stage_summary(summary),
        )

    def _entry_is_visible(self, entry: RunLogEntry) -> bool:
        if entry.level < self._minimum_level:
            return False
        if not self._search_query:
            return True
        haystack = f"{entry.logger_name}\n{entry.message}".casefold()
        return self._search_query in haystack

    @pyqtSlot(str)
    def _schedule_search(self, _text: str) -> None:
        self._search_timer.start()

    @pyqtSlot()
    def _apply_filters(self, *_args) -> None:
        minimum_level = self.level_combo.currentData()
        self._minimum_level = int(minimum_level or logging.INFO)
        self._search_query = self.search_edit.text().strip().casefold()
        self._visible_count = sum(self._entry_is_visible(entry) for entry in self._entries)
        self._render_entries()

    @pyqtSlot()
    def _render_entries(self, *_args) -> None:
        self.log_view.clear()
        for entry in self._entries:
            if self._entry_is_visible(entry):
                self._append_entry(entry)
        self._update_status()

    def _append_entry(self, entry: RunLogEntry) -> None:
        scrollbar = self.log_view.verticalScrollBar()
        should_follow = scrollbar.value() >= scrollbar.maximum() - 2

        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.End)
        if not self.log_view.document().isEmpty():
            cursor.insertBlock()

        formats = self._text_formats()
        timestamp_text = f"{entry.timestamp:<8} "
        level_text = f"{self._level_label(entry.level):<7} "
        module_text = f"{entry.logger_name} › "
        cursor.insertText(timestamp_text, formats["timestamp"])
        cursor.insertText(level_text, formats[self._level_format_key(entry.level)])
        cursor.insertText(module_text, formats["module"])

        lines = entry.message.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        cursor.insertText(lines[0], formats["message"])
        continuation_indent = " " * (len(timestamp_text) + len(level_text) + len(module_text))
        for line in lines[1:]:
            cursor.insertText(f"\u2028{continuation_indent}│ ", formats["module"])
            cursor.insertText(line, formats["message"])

        if should_follow:
            scrollbar.setValue(scrollbar.maximum())

    def _text_formats(self) -> dict[str, QTextCharFormat]:
        dark = isDarkTheme()
        message_color = self.log_view.palette().color(QPalette.Text)
        cache_key = (dark, message_color.rgba())
        if self._format_cache_key == cache_key and self._format_cache is not None:
            return self._format_cache

        def text_format(color: str, *, bold: bool = False) -> QTextCharFormat:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            if bold:
                fmt.setFontWeight(QFont.DemiBold)
            return fmt

        message_format = QTextCharFormat()
        message_format.setForeground(message_color)
        self._format_cache = {
            "timestamp": text_format("#8b949e" if dark else "#6b7280"),
            "module": text_format("#aeb6c2" if dark else "#667085"),
            "info": text_format("#66a3ff" if dark else "#2563eb", bold=True),
            "warning": text_format("#f5b642" if dark else "#a16207", bold=True),
            "error": text_format("#ff6b6b" if dark else "#c53030", bold=True),
            "message": message_format,
        }
        self._format_cache_key = cache_key
        return self._format_cache

    @staticmethod
    def _level_format_key(level: int) -> str:
        if level >= logging.ERROR:
            return "error"
        if level >= logging.WARNING:
            return "warning"
        return "info"

    def _remove_first_entry_block(self) -> None:
        if self.log_view.document().isEmpty():
            return
        cursor = QTextCursor(self.log_view.document())
        cursor.movePosition(QTextCursor.Start)
        cursor.select(QTextCursor.BlockUnderCursor)
        cursor.removeSelectedText()
        cursor.deleteChar()

    @staticmethod
    def _level_label(level: int) -> str:
        if level >= logging.CRITICAL:
            return "FATAL"
        if level >= logging.ERROR:
            return "ERROR"
        if level >= logging.WARNING:
            return "WARN"
        return "INFO"

    @pyqtSlot(bool)
    def _set_follow_paused(self, paused: bool) -> None:
        self._follow_paused = paused
        if paused:
            self.pause_button.setIcon(FIF.PLAY)
            self.pause_button.setText(self.tr("继续跟随"))
        else:
            self.pause_button.setIcon(FIF.PAUSE)
            self.pause_button.setText(self.tr("暂停跟随"))
            self._render_entries()
        self._update_status()

    @pyqtSlot()
    def clear_display(self) -> None:
        """Clear the session view without deleting or truncating app.log."""

        self._entries.clear()
        self._visible_count = 0
        self.log_view.clear()
        self._update_status()

    @pyqtSlot()
    def open_log_folder(self) -> None:
        open_folder(str(LOG_PATH))

    def _update_status(self) -> None:
        if self._follow_paused:
            self.status_label.setText(
                self.tr("已暂停 · 已缓存 {total} 条").format(total=len(self._entries))
            )
            return
        self.status_label.setText(
            self.tr("正在接收 · 显示 {visible} / {total} 条").format(
                visible=self._visible_count,
                total=len(self._entries),
            )
        )

    def shutdown(self) -> None:
        """Detach the process-wide observer before Qt destroys this page."""

        if self._is_shutdown:
            return
        self._is_shutdown = True
        uninstall_stage_summary_emitter(self._handler.emitter)
        unregister_log_handler(self._handler)
        try:
            self._handler.emitter.record_emitted.disconnect(self._append_log_event)
        except (TypeError, RuntimeError):
            pass
        try:
            self._handler.emitter.stage_summary_emitted.disconnect(
                self._append_stage_summary
            )
        except (TypeError, RuntimeError):
            pass
        self._handler.close()
        self.connection_label.setText(self.tr("○ 已断开"))
        self.connection_label.setStyleSheet("color: #8b949e;")


__all__ = ["APP_LOG_PATH", "MAX_LOG_ENTRIES", "RunLogEntry", "RunLogInterface"]
