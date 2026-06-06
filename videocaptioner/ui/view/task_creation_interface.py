# -*- coding: utf-8 -*-
import os
import sys
from urllib.parse import urlparse

from PyQt5.QtCore import QStandardPaths, Qt, pyqtSignal
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    HyperlinkButton,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    PrimaryPushButton,
    ProgressBar,
    TitleLabel,
)

from videocaptioner.config import APPDATA_PATH, ASSETS_PATH, VERSION
from videocaptioner.core.constant import (
    INFOBAR_DURATION_ERROR,
    INFOBAR_DURATION_INFO,
    INFOBAR_DURATION_SUCCESS,
    INFOBAR_DURATION_WARNING,
)
from videocaptioner.core.entities import (
    SupportedAudioFormats,
    SupportedVideoFormats,
)
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.common.signal_bus import signalBus
from videocaptioner.ui.components.DonateDialog import DonateDialog
from videocaptioner.ui.thread.video_download_thread import VideoDownloadThread
from videocaptioner.ui.view.log_window import LogWindow

LOGO_PATH = ASSETS_PATH / "logo.png"


class TaskCreationInterface(QWidget):
    """
    任务创建界面类，用于创建和配置任务。
    """

    finished = pyqtSignal(str, object)  # 该信号用于在任务创建完成后通知主窗口

    def __init__(self, parent=None):
        super().__init__(parent)
        self.task = None
        self.log_window = None
        self._start_mode = "browse"

        self.setObjectName("TaskCreationInterface")
        self.setAttribute(Qt.WA_StyledBackground, True)  # type: ignore
        self.setAcceptDrops(True)

        self.setup_ui()
        self.setup_values()
        self.setup_signals()

    def setup_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setObjectName("main_layout")
        self.main_layout.setSpacing(20)
        self.main_layout.addSpacing(90)
        self.setup_logo()
        self.setup_heading()
        self.setup_search_layout()
        self.setup_status_layout()
        self.setup_info_label()

    def setup_logo(self):
        self.logo_label = QLabel(self)
        self.logo_pixmap = QPixmap(str(LOGO_PATH))
        self.logo_pixmap = self.logo_pixmap.scaled(
            108,
            108,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.SmoothTransformation,  # type: ignore
        )

        self.logo_label.setPixmap(self.logo_pixmap)
        self.logo_label.setAlignment(Qt.AlignCenter)  # type: ignore
        self.main_layout.addWidget(self.logo_label)

    def setup_heading(self):
        self.heading_title = TitleLabel(self.tr("导入视频，生成字幕与配音"), self)
        self.heading_title.setAlignment(Qt.AlignCenter)  # type: ignore
        self.heading_desc = CaptionLabel(self.tr("拖入本地媒体，或粘贴在线视频链接开始处理"), self)
        self.heading_desc.setAlignment(Qt.AlignCenter)  # type: ignore
        self.heading_hint = CaptionLabel(self.tr("选择本地视频/音频，或粘贴 B 站、YouTube 等视频链接后点击开始处理"), self)
        self.heading_hint.setAlignment(Qt.AlignCenter)  # type: ignore
        self.main_layout.addWidget(self.heading_title)
        self.main_layout.addWidget(self.heading_desc)
        self.main_layout.addWidget(self.heading_hint)
        self.main_layout.addSpacing(18)

    def setup_search_layout(self):
        self.search_layout = QHBoxLayout()
        self.search_layout.setContentsMargins(120, 0, 120, 0)
        self.search_input = LineEdit(self)
        self.search_input.setPlaceholderText(self.tr("粘贴视频链接，或拖拽文件到这里"))
        self.search_input.setFixedHeight(40)
        self.search_input.setClearButtonEnabled(True)
        self.search_input.focusOutEvent = lambda e: super(
            LineEdit, self.search_input
        ).focusOutEvent(e)
        self.search_input.paintEvent = lambda e: super(
            LineEdit, self.search_input
        ).paintEvent(e)
        self.start_button = PrimaryPushButton(FluentIcon.FOLDER, self.tr("选择文件"), self)
        self.start_button.setFixedHeight(40)
        self.start_button.setMinimumWidth(108)
        self.search_layout.addWidget(self.search_input)
        self.search_layout.addWidget(self.start_button)
        self.search_layout.setSpacing(10)
        self.main_layout.addLayout(self.search_layout)
        self.main_layout.addSpacing(70)

    def setup_status_layout(self):
        self.status_layout = QVBoxLayout()
        self.status_layout.setContentsMargins(50, 0, 30, 5)
        self.status_layout.setAlignment(Qt.AlignBottom | Qt.AlignHCenter)  # type: ignore
        self.status_label = BodyLabel(self.tr("准备就绪"), self)
        self.status_label.setStyleSheet("font-size: 14px; color: #888888;")
        self.status_layout.addWidget(self.status_label, 0, Qt.AlignCenter)  # type: ignore
        self.progress_bar = ProgressBar(self)
        self.status_label.hide()
        self.progress_bar.hide()
        self.progress_bar.setFixedWidth(300)
        self.status_layout.addWidget(self.progress_bar, 0, Qt.AlignCenter)  # type: ignore

        self.main_layout.addStretch(1)
        self.main_layout.addLayout(self.status_layout)

    def setup_info_label(self):
        # 创建底部容器
        bottom_container = QWidget()
        bottom_layout = QHBoxLayout(bottom_container)
        bottom_layout.setContentsMargins(0, 0, 0, 0)

        # 创建日志按钮
        self.log_button = HyperlinkButton(url="", text=self.tr("查看日志"), parent=self)

        # 创建捐助按钮
        self.donate_button = HyperlinkButton(url="", text=self.tr("捐助"), parent=self)

        # 添加版权信息标签
        self.info_label = BodyLabel(
            self.tr(f"©VideoCaptioner {VERSION} • By Weifeng"), self
        )
        self.info_label.setAlignment(Qt.AlignCenter)  # type: ignore
        self.info_label.setStyleSheet("font-size: 12px; color: #888888;")

        # 将组件添加到底部布局
        bottom_layout.addStretch()
        bottom_layout.addWidget(self.info_label)
        bottom_layout.addWidget(self.log_button)
        bottom_layout.addWidget(self.donate_button)
        bottom_layout.addStretch()

        self.main_layout.addStretch()
        self.main_layout.addWidget(bottom_container)

    def setup_signals(self):
        self.start_button.clicked.connect(self.on_start_clicked)
        self.search_input.textChanged.connect(self.on_search_input_changed)
        self.log_button.clicked.connect(self.show_log_window)
        self.donate_button.clicked.connect(self.show_donate_dialog)

    def setup_values(self):
        self.search_input.setText("")
        cfg.set(cfg.need_translate, True)
        signalBus.subtitle_translation_changed.emit(True)

    def on_start_clicked(self):
        if self._start_mode == "browse":
            desktop_path = QStandardPaths.writableLocation(
                QStandardPaths.DesktopLocation
            )
            file_dialog = QFileDialog()

            # 构建文件过滤器
            video_formats = " ".join(f"*.{fmt.value}" for fmt in SupportedVideoFormats)
            audio_formats = " ".join(f"*.{fmt.value}" for fmt in SupportedAudioFormats)
            filter_str = f"{self.tr('媒体文件')} ({video_formats} {audio_formats});;{self.tr('视频文件')} ({video_formats});;{self.tr('音频文件')} ({audio_formats})"

            file_path, _ = file_dialog.getOpenFileName(
                self, self.tr("选择媒体文件"), desktop_path, filter_str
            )
            if file_path:
                self.search_input.setText(file_path)
            return

        self.process()

    def on_search_input_changed(self):
        if self.search_input.text():
            self._start_mode = "process"
            self.start_button.setIcon(FluentIcon.PLAY)
            self.start_button.setText(self.tr("开始处理"))
        else:
            self._start_mode = "browse"
            self.start_button.setIcon(FluentIcon.FOLDER)
            self.start_button.setText(self.tr("选择文件"))

    def dragEnterEvent(self, event):
        event.accept() if event.mimeData().hasUrls() else event.ignore()

    def dropEvent(self, event):
        files = [u.toLocalFile() for u in event.mimeData().urls()]
        for file_path in files:
            if not os.path.isfile(file_path):
                continue

            file_ext = os.path.splitext(file_path)[1][1:].lower()

            # 检查文件格式是否支持
            supported_formats = {fmt.value for fmt in SupportedVideoFormats} | {
                fmt.value for fmt in SupportedAudioFormats
            }
            is_supported = file_ext in supported_formats

            if is_supported:
                self.search_input.setText(file_path)
                self.status_label.setText(self.tr("导入成功"))
                InfoBar.success(
                    self.tr("导入成功"),
                    self.tr("导入媒体文件成功"),
                    duration=INFOBAR_DURATION_SUCCESS,
                    parent=self,
                )
                break
            else:
                InfoBar.error(
                    self.tr("格式错误") + file_ext,
                    self.tr("不支持该文件格式"),
                    duration=INFOBAR_DURATION_ERROR,
                    parent=self,
                )

    def create_task(self):
        search_input = self.search_input.text()
        if os.path.isfile(search_input):
            self._process_file(search_input)
        elif self._is_valid_url(search_input):
            self._process_url(search_input)
        else:
            InfoBar.error(
                self.tr("错误"),
                self.tr("请输入有效的文件路径或视频URL"),
                duration=INFOBAR_DURATION_ERROR,
                parent=self,
            )

    def _is_valid_url(self, url):
        try:
            result = urlparse(url)
            return result.scheme in ("http", "https") and bool(result.netloc)
        except ValueError:
            return False

    def _process_file(self, file_path):
        self.finished.emit(file_path, None)

    def _process_url(self, url):
        # 检测 cookies.txt 文件
        cookiefile_path = APPDATA_PATH / "cookies.txt"
        if not cookiefile_path.exists():
            InfoBar.warning(
                self.tr("警告"),
                self.tr("未检测到 cookies.txt。YouTube 可能要求登录验证，失败时请先配置 cookies。"),
                duration=INFOBAR_DURATION_WARNING,
                parent=self,
            )

        # 创建视频下载线程
        self.video_download_thread = VideoDownloadThread(url, str(cfg.work_dir.value))
        self.video_download_thread.finished.connect(self.on_video_download_finished)
        self.video_download_thread.progress.connect(self.on_create_task_progress)
        self.video_download_thread.error.connect(self.on_create_task_error)
        self.video_download_thread.start()

        InfoBar.info(
            self.tr("开始下载"),
            self.tr("开始下载视频..."),
            duration=INFOBAR_DURATION_INFO,
            parent=self,
        )

    def on_video_download_finished(self, video_file_path, subtitle_file_path=None):
        """视频下载完成的回调函数"""
        if video_file_path:
            self.finished.emit(video_file_path, subtitle_file_path)
            InfoBar.success(
                self.tr("下载成功"),
                self.tr("视频下载完成，开始自动处理..."),
                duration=INFOBAR_DURATION_SUCCESS,
                position=InfoBarPosition.BOTTOM,
                parent=self.parent(),
            )
        else:
            InfoBar.error(
                self.tr("错误"),
                self.tr("视频下载失败"),
                duration=INFOBAR_DURATION_ERROR,
                parent=self,
            )

    def on_create_task_progress(self, value, status):
        self.progress_bar.show()
        self.status_label.show()
        self.progress_bar.setValue(value)
        self.status_label.setText(status)

    def on_create_task_error(self, error):
        InfoBar.error(
            self.tr("错误"),
            self.tr(error),
            duration=INFOBAR_DURATION_ERROR,
            parent=self,
        )

    def set_task(self, task):
        self.task = task
        self.update_info()

    def update_info(self):
        if self.task:
            self.search_input.setText(self.task.file_path)

    def process(self):
        search_input = self.search_input.text()

        if os.path.isfile(search_input):
            self._process_file(search_input)
        elif self._is_valid_url(search_input):
            self._process_url(search_input)
        else:
            InfoBar.error(
                self.tr("错误"),
                self.tr("请输入音视频文件路径或URL"),
                duration=INFOBAR_DURATION_ERROR,
                parent=self,
            )

    def show_log_window(self):
        """显示日志窗口"""
        if self.log_window is None:
            self.log_window = LogWindow()
        if self.log_window.isHidden():
            self.log_window.show()
        else:
            self.log_window.activateWindow()

    def show_donate_dialog(self):
        """显示捐助窗口"""
        donate_dialog = DonateDialog(self)
        donate_dialog.exec_()


if __name__ == "__main__":
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)  # type: ignore
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)  # type: ignore

    app = QApplication(sys.argv)
    window = TaskCreationInterface()
    window.show()
    sys.exit(app.exec_())
