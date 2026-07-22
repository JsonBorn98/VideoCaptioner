import atexit
import os
import shutil

import psutil
from PyQt5.QtCore import QSize, QUrl
from PyQt5.QtGui import QDesktopServices, QIcon
from PyQt5.QtWidgets import QApplication
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import (
    FluentWindow,
    InfoBar,
    InfoBarPosition,
    MessageBox,
    NavigationItemPosition,
    SplashScreen,
)

from videocaptioner.config import ASSETS_PATH, GITHUB_REPO_URL
from videocaptioner.core.constant import INFOBAR_DURATION_FOREVER
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.view.batch_process_interface import BatchProcessInterface
from videocaptioner.ui.view.home_interface import HomeInterface
from videocaptioner.ui.view.llm_logs_interface import LLMLogsInterface
from videocaptioner.ui.view.postprocess_setting_interface import PostprocessSettingInterface
from videocaptioner.ui.view.run_log_interface import RunLogInterface
from videocaptioner.ui.view.setting_interface import SettingInterface
from videocaptioner.ui.view.subtitle_style_interface import SubtitleStyleInterface

LOGO_PATH = ASSETS_PATH / "logo.png"


class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.initWindow()

        # 创建子界面
        self.homeInterface = HomeInterface(self)
        self.settingInterface = SettingInterface(self)
        self.postprocessSettingInterface = PostprocessSettingInterface(self)
        self.subtitleStyleInterface = SubtitleStyleInterface(self)
        self.batchProcessInterface = BatchProcessInterface(self)
        self.llmLogsInterface = LLMLogsInterface(self)
        self.runLogInterface = RunLogInterface(self)

        # Dedicated postprocess settings are reached from Settings/Postprocess.
        # They intentionally have no separate main-navigation item.
        self.stackedWidget.addWidget(self.postprocessSettingInterface)

        # 初始化导航界面
        self.initNavigation()
        self.splashScreen.finish()

        # 检查系统依赖
        self._check_ffmpeg()

        # 注册退出处理， 清理进程
        atexit.register(self.stop)

    def initNavigation(self):
        """初始化导航栏"""
        # 添加导航项
        self.addSubInterface(self.homeInterface, FIF.HOME, self.tr("主页"))
        self.addSubInterface(self.batchProcessInterface, FIF.VIDEO, self.tr("批量处理"))
        self.addSubInterface(self.subtitleStyleInterface, FIF.FONT, self.tr("字幕样式"))
        self.addSubInterface(self.llmLogsInterface, FIF.HISTORY, self.tr("请求日志"))
        self.addSubInterface(self.runLogInterface, FIF.DEVELOPER_TOOLS, self.tr("运行日志"))

        self.navigationInterface.addSeparator()

        # 在底部添加自定义小部件
        self.navigationInterface.addItem(
            routeKey="avatar",
            text="GitHub",
            icon=FIF.GITHUB,
            onClick=self.onGithubDialog,
            position=NavigationItemPosition.BOTTOM,
        )
        self.addSubInterface(
            self.settingInterface,
            FIF.SETTING,
            self.tr("Settings"),
            NavigationItemPosition.BOTTOM,
        )

        # 设置默认界面
        self.switchTo(self.homeInterface)

    def switchTo(self, interface):
        if interface.windowTitle():
            self.setWindowTitle(interface.windowTitle())
        else:
            self.setWindowTitle("VideoCaptioner")
        self.stackedWidget.setCurrentWidget(interface, popOut=False)

    def initWindow(self):
        """初始化窗口"""
        self.resize(1050, 800)
        self.setMinimumWidth(700)
        self.setWindowIcon(QIcon(str(LOGO_PATH)))
        self.setWindowTitle("VideoCaptioner")

        self.setMicaEffectEnabled(cfg.get(cfg.micaEnabled))

        # 创建启动画面
        self.splashScreen = SplashScreen(self.windowIcon(), self)
        self.splashScreen.setIconSize(QSize(106, 106))
        self.splashScreen.raise_()

        # 设置窗口位置, 居中
        desktop = QApplication.desktop().availableGeometry()
        w, h = desktop.width(), desktop.height()
        self.move(w // 2 - self.width() // 2, h // 2 - self.height() // 2)

        self.show()
        QApplication.processEvents()

    def onGithubDialog(self):
        """打开GitHub"""
        w = MessageBox(
            self.tr("GitHub信息"),
            self.tr(
                "VideoCaptioner 目前托管在 GitHub 上，欢迎 Star、Fork 或提交 Issue。\n\n https://github.com/JsonBorn98/VideoCaptioner"
            ),
            self,
        )
        w.yesButton.setText(self.tr("打开 GitHub"))
        w.cancelButton.setText(self.tr("关闭"))
        if w.exec():
            QDesktopServices.openUrl(QUrl(GITHUB_REPO_URL))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "splashScreen"):
            self.splashScreen.resize(self.size())

    def closeEvent(self, event):
        self.runLogInterface.shutdown()
        # 关闭所有子界面
        # self.homeInterface.close()
        # self.batchProcessInterface.close()
        # self.subtitleStyleInterface.close()
        # self.settingInterface.close()
        super().closeEvent(event)

        # 强制退出应用程序
        QApplication.quit()

        # 确保所有线程和进程都被终止 要是一些错误退出就不会处理了。
        # import os
        # os._exit(0)

    def stop(self):
        # 找到 FFmpeg 进程并关闭
        process = psutil.Process(os.getpid())
        for child in process.children(recursive=True):
            child.kill()

    def _check_ffmpeg(self):
        """检查 FFmpeg 是否已安装"""
        if shutil.which("ffmpeg") is None:
            InfoBar.warning(
                self.tr("FFmpeg 未安装"),
                self.tr("软件处理音视频文件时需要 FFmpeg，请先安装"),
                duration=INFOBAR_DURATION_FOREVER,
                position=InfoBarPosition.BOTTOM,
                parent=self,
            )
