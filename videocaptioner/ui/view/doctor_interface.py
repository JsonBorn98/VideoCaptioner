from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    InfoBadge,
    InfoBar,
    InfoLevel,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    SimpleCardWidget,
    SubtitleLabel,
    TitleLabel,
    isDarkTheme,
)
from qfluentwidgets import FluentIcon as FIF

from videocaptioner.cli.commands.doctor import Check, run_diagnostics
from videocaptioner.core.constant import INFOBAR_DURATION_ERROR, INFOBAR_DURATION_SUCCESS
from videocaptioner.ui.common.config import cfg


class DoctorThread(QThread):
    finished = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, check_api: bool = False):
        super().__init__()
        self.check_api = check_api

    def run(self):
        try:
            self.finished.emit(run_diagnostics(_build_doctor_config(), check_api=self.check_api))
        except Exception as exc:
            self.error.emit(str(exc))


class StatusPill(InfoBadge):
    def __init__(self, text: str, parent=None):
        super().__init__(parent=parent)
        self.setFixedHeight(24)
        if text in {"ok", "warn", "error", "pending", "checking"}:
            self.setStatus(text)
        else:
            self.setText(text)

    def setStatus(self, status: str):
        self.setText(_status_label(status))
        self.setLevel(
            {
                "ok": InfoLevel.SUCCESS,
                "warn": InfoLevel.WARNING,
                "error": InfoLevel.ERROR,
                "checking": InfoLevel.INFOAMTION,
                "pending": InfoLevel.ATTENTION,
            }.get(status, InfoLevel.ATTENTION)
        )


class HealthCard(SimpleCardWidget):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("healthCard")
        self.setBorderRadius(8)
        self.setMinimumHeight(86)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)
        top = QHBoxLayout()
        self.badge = StatusPill("pending", self)
        self.titleLabel = BodyLabel(title, self)
        top.addWidget(self.titleLabel)
        top.addStretch(1)
        top.addWidget(self.badge)
        self.messageLabel = CaptionLabel("等待自动检查", self)
        self.messageLabel.setWordWrap(True)
        layout.addLayout(top)
        layout.addWidget(self.messageLabel)

    def updateState(self, status: str, message: str):
        self.badge.setStatus(status)
        self.messageLabel.setText(message)
        self.setProperty("status", status)
        self.style().unpolish(self)
        self.style().polish(self)


class CheckRow(QWidget):
    def __init__(self, check: Check, parent=None):
        super().__init__(parent)
        self.setObjectName("checkRow")
        self.setMinimumHeight(48)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 7, 12, 7)
        layout.setSpacing(12)
        self.badge = StatusPill(check.status, self)
        textBox = QVBoxLayout()
        textBox.setSpacing(2)
        primary_text = _primary_check_text(check)
        secondary_text = _secondary_check_text(check)
        self.nameLabel = BodyLabel(primary_text, self)
        self.messageLabel = CaptionLabel(secondary_text, self)
        self.messageLabel.setWordWrap(True)
        textBox.addWidget(self.nameLabel)
        textBox.addWidget(self.messageLabel)
        layout.addWidget(self.badge)
        layout.addLayout(textBox, 1)


class DoctorInterface(ScrollArea):
    """桌面端诊断页。"""

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setWindowTitle(self.tr("诊断"))
        self.thread: DoctorThread | None = None
        self._auto_started = False
        self.scrollWidget = QWidget()
        self.pageLayout = QVBoxLayout(self.scrollWidget)
        self.titleLabel = TitleLabel(self.tr("诊断"), self)
        self.resultContainer = SimpleCardWidget(self.scrollWidget)
        self.resultContainer.setBorderRadius(8)
        self.resultLayout = QVBoxLayout(self.resultContainer)
        self.resultLayout.setContentsMargins(0, 6, 0, 6)
        self.resultLayout.setSpacing(0)
        self._init_ui()

    def _init_ui(self):
        self.resize(1000, 800)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)  # type: ignore
        self.setViewportMargins(0, 80, 0, 20)
        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        self.setObjectName("doctorInterface")
        self.scrollWidget.setObjectName("scrollWidget")
        self.titleLabel.setObjectName("settingLabel")
        self.titleLabel.move(36, 30)
        self.enableTransparentBackground()

        toolbar = QWidget(self.scrollWidget)
        toolbarLayout = QHBoxLayout(toolbar)
        toolbarLayout.setContentsMargins(0, 0, 0, 0)
        toolbarLayout.setSpacing(10)
        heading = QVBoxLayout()
        heading.setSpacing(4)
        heading.addWidget(SubtitleLabel(self.tr("环境健康检查"), toolbar))
        heading.addWidget(CaptionLabel(self.tr("自动检查本机依赖和配置；深度诊断会额外请求当前 API。"), toolbar))
        toolbarLayout.addLayout(heading, 1)
        self.runButton = PushButton(self.tr("重新检查"), toolbar, icon=FIF.SEARCH)
        self.deepRunButton = PrimaryPushButton(self.tr("深度诊断"), toolbar)
        self.deepRunButton.setIcon(FIF.SYNC)
        self.runButton.setFixedHeight(36)
        self.deepRunButton.setFixedHeight(36)
        self.deepRunButton.setToolTip(self.tr("包含少量真实 API 请求，可能产生费用"))
        toolbarLayout.addWidget(self.runButton)
        toolbarLayout.addWidget(self.deepRunButton)

        self.summaryGrid = QGridLayout()
        self.summaryGrid.setHorizontalSpacing(12)
        self.summaryGrid.setVerticalSpacing(12)
        self.healthCards = {
            "env": HealthCard(self.tr("基础环境"), self.scrollWidget),
            "download": HealthCard(self.tr("下载能力"), self.scrollWidget),
            "ai": HealthCard(self.tr("AI 配置"), self.scrollWidget),
            "dubbing": HealthCard(self.tr("配音服务"), self.scrollWidget),
        }
        for index, card in enumerate(self.healthCards.values()):
            self.summaryGrid.addWidget(card, index // 2, index % 2)

        self.pageLayout.setSpacing(16)
        self.pageLayout.setContentsMargins(36, 10, 36, 0)
        self.pageLayout.addWidget(toolbar)
        self.pageLayout.addLayout(self.summaryGrid)
        self.pageLayout.addWidget(BodyLabel(self.tr("检查结果"), self.scrollWidget))
        self.pageLayout.addWidget(self.resultContainer)
        self.pageLayout.addStretch(1)
        self.runButton.clicked.connect(lambda: self._run(False))
        self.deepRunButton.clicked.connect(lambda: self._run(True))
        self._show_pending_rows()

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_page_background()
        if not self._auto_started:
            self._auto_started = True
            QTimer.singleShot(80, lambda: self._run(False))

    def _sync_page_background(self):
        color = "#202020" if isDarkTheme() else "#f5f5f5"
        self.setStyleSheet(f"QScrollArea {{ border: none; background: {color}; }}")
        self.scrollWidget.setStyleSheet(f"QWidget#scrollWidget {{ background: {color}; }}")

    def _show_pending_rows(self):
        self._clear_results()
        pending = [
            Check("python / ffmpeg / ffprobe", "pending", self.tr("准备检查音视频处理工具")),
            Check("yt-dlp", "pending", self.tr("准备检查在线视频下载能力")),
            Check("transcribe / subtitle / LLM", "pending", self.tr("准备检查转录、翻译和大模型配置")),
            Check("dubbing", "pending", self.tr("准备检查配音音色和服务配置")),
        ]
        for check in pending:
            self.resultLayout.addWidget(CheckRow(check, self.resultContainer))

    def _run(self, check_api: bool):
        if self.thread and self.thread.isRunning():
            return
        self._set_running(True)
        self._clear_results()
        running_text = self.tr("正在真实请求当前服务，请稍候") if check_api else self.tr("正在检查本机依赖和基础配置")
        self.resultLayout.addWidget(CheckRow(Check("running", "checking", running_text), self.resultContainer))
        self.thread = DoctorThread(check_api=check_api)
        self.thread.finished.connect(self._on_finished)
        self.thread.error.connect(self._on_error)
        self.thread.start()

    def _on_finished(self, checks: list[Check]):
        self._set_running(False)
        self._clear_results()
        for check in checks:
            self.resultLayout.addWidget(CheckRow(check, self.resultContainer))
        self._update_summary(checks)
        errors = sum(1 for c in checks if c.status == "error")
        warnings = sum(1 for c in checks if c.status == "warn")
        if errors:
            InfoBar.error(
                self.tr("诊断完成"),
                self.tr("发现 {errors} 个错误，{warnings} 个警告").format(errors=errors, warnings=warnings),
                duration=INFOBAR_DURATION_ERROR,
                parent=self,
            )
        else:
            InfoBar.success(
                self.tr("诊断完成"),
                self.tr("发现 {warnings} 个警告").format(warnings=warnings),
                duration=INFOBAR_DURATION_SUCCESS,
                parent=self,
            )

    def _update_summary(self, checks: list[Check]):
        groups = {
            "env": ("python", "ffmpeg", "ffprobe", "config.file"),
            "download": ("yt-dlp",),
            "ai": ("transcribe", "subtitle", "llm", "whisper", "translate"),
            "dubbing": ("dubbing", "api.dubbing"),
        }
        for key, prefixes in groups.items():
            matched = [c for c in checks if c.name.startswith(prefixes)]
            status = _group_status(matched)
            if not matched:
                self.healthCards[key].updateState("warn", self.tr("未发现相关检查项"))
                continue
            errors = sum(c.status == "error" for c in matched)
            warnings = sum(c.status == "warn" for c in matched)
            ok = sum(c.status == "ok" for c in matched)
            message = self.tr("{ok} 正常 / {warnings} 警告 / {errors} 错误").format(
                ok=ok,
                warnings=warnings,
                errors=errors,
            )
            self.healthCards[key].updateState(status, message)

    def _set_running(self, running: bool):
        self.runButton.setEnabled(not running)
        self.deepRunButton.setEnabled(not running)
        for card in self.healthCards.values():
            card.updateState("checking" if running else "pending", self.tr("检查中...") if running else self.tr("等待检查"))

    def _on_error(self, message: str):
        self._set_running(False)
        InfoBar.error(self.tr("诊断失败"), message, duration=INFOBAR_DURATION_ERROR, parent=self)

    def _clear_results(self):
        while self.resultLayout.count():
            item = self.resultLayout.takeAt(0)
            if widget := item.widget():
                widget.deleteLater()


def _group_status(checks: list[Check]) -> str:
    if any(c.status == "error" for c in checks):
        return "error"
    if any(c.status == "warn" for c in checks):
        return "warn"
    return "ok"


def _primary_check_text(check: Check) -> str:
    if check.status in {"error", "warn"} and check.fix:
        return _friendly_fix_text(check.fix)
    if check.status == "checking":
        return "正在检查"
    if check.status == "pending":
        return check.message
    return _friendly_check_name(check.name)


def _secondary_check_text(check: Check) -> str:
    name = _friendly_check_name(check.name)
    if check.status in {"error", "warn"} and check.fix:
        return f"{name}：{_brief_check_message(check)}。影响：{_impact_text(check.name)}"
    if check.status in {"checking", "pending"}:
        return name
    return _brief_check_message(check)


def _brief_check_message(check: Check) -> str:
    message = check.message
    if check.status == "ok":
        if check.name in {"ffmpeg", "ffprobe", "yt-dlp"}:
            executable = message.split(" (", 1)[0].strip()
            executable_name = executable.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            return f"已检测到 {executable_name}"
        if check.name == "config.file":
            return "配置文件已保存"
        if check.name == "api.dubbing":
            return "真实配音请求成功"
    if check.name == "config.file" and "Config file does not exist" in message:
        return "还没有保存过配置文件"
    if check.name == "yt-dlp" and "may be old" in message:
        return "在线视频下载组件可能偏旧"
    if check.name == "api.dubbing" and "Skipped real TTS request" in message:
        return "未填写配音 API Key，已跳过真实请求"
    return message


def _impact_text(name: str) -> str:
    if name in {"ffmpeg", "ffprobe"}:
        return "视频合成、配音合入视频和媒体时长读取可能失败"
    if name == "yt-dlp":
        return "在线视频链接可能无法下载"
    if name == "config.file":
        return "部分设置可能无法持久保存"
    if name.startswith("llm"):
        return "字幕优化、断句或大模型翻译可能失败"
    if name.startswith("whisper") or name == "transcribe.asr":
        return "语音转字幕可能失败"
    if name.startswith("dubbing") or name == "api.dubbing":
        return "配音试听或正式生成配音可能失败"
    return "相关功能可能无法正常使用"


def _friendly_check_name(name: str) -> str:
    return {
        "python": "Python 运行环境",
        "ffmpeg": "FFmpeg 音视频处理",
        "ffprobe": "FFprobe 媒体信息读取",
        "yt-dlp": "在线视频下载",
        "config.file": "配置文件",
        "transcribe.asr": "转录方式",
        "subtitle.processing": "字幕处理",
        "llm.api_key": "大模型 API Key",
        "llm.model": "大模型名称",
        "whisper_api.api_key": "Whisper API Key",
        "whisper-cpp": "本地 Whisper",
        "dubbing.preset": "配音音色",
        "dubbing.api_key": "配音 API Key",
        "dubbing.provider": "配音服务",
        "dubbing.voice": "配音声音",
        "api.dubbing": "配音真实请求",
        "running": "诊断任务",
    }.get(name, name)


def _friendly_fix_text(fix: str) -> str:
    replacements = {
        "Install ffmpeg and make sure it is on PATH": "安装 FFmpeg，并确保命令行可以直接运行 ffmpeg",
        "Install ffprobe and make sure it is on PATH": "安装 FFprobe，并确保命令行可以直接运行 ffprobe",
        "Install yt-dlp and make sure it is on PATH": "安装或更新 yt-dlp，用于下载在线视频",
        "Update yt-dlp if online downloads fail": "如果在线视频下载失败，请先更新 yt-dlp",
        "Run 'videocaptioner config init' or set values with environment variables": "打开设置页保存一次配置，或运行 videocaptioner config init",
        "Run 'videocaptioner config set llm.api_key <key>' or disable AI polish/split": "填写大模型 API Key，或关闭需要大模型的字幕优化功能",
        "Run 'videocaptioner config set llm.model <model>'": "选择一个可用的大模型名称",
        "Run 'videocaptioner config set dubbing.api_key <key>'": "在配音页或设置页填写当前配音服务的 API Key",
        "Run 'videocaptioner config set dubbing.preset edge-cn-female'": "在配音页选择一个默认音色",
        "Use siliconflow, gemini, or edge": "配音服务请选择 Edge、Gemini 或 SiliconFlow",
        "Use a preset or a provider-supported voice": "选择当前服务支持的音色",
        "Run a short 'videocaptioner dub sample.srt' to verify billing/provider access": "用一小段字幕测试配音服务是否能真实返回音频",
        "Open Settings > Dubbing and verify provider, API Key, Base URL, model, and voice": "打开设置页的配音配置，检查服务、API Key、Base URL、模型和音色",
    }
    return replacements.get(fix, fix)


def _status_label(status: str) -> str:
    return {
        "ok": "正常",
        "warn": "注意",
        "error": "错误",
        "checking": "检查中",
        "pending": "待检查",
    }.get(status, status.upper())


def _build_doctor_config() -> dict:
    provider = cfg.dubbing_provider.value
    return {
        "llm": {
            "api_key": _current_llm_api_key(),
            "api_base": _current_llm_api_base(),
            "model": _current_llm_model(),
        },
        "whisper_api": {
            "api_key": cfg.whisper_api_key.value,
            "api_base": cfg.whisper_api_base.value,
            "model": cfg.whisper_api_model.value or "whisper-1",
        },
        "transcribe": {
            "asr": cfg.transcribe_model.value.name.lower().replace("_", "-"),
        },
        "subtitle": {
            "optimize": cfg.need_optimize.value,
            "split": cfg.need_split.value,
        },
        "translate": {
            "service": cfg.translator_service.value.name.lower(),
        },
        "dubbing": {
            "provider": provider,
            "preset": cfg.dubbing_preset.value,
            "api_key": cfg.dubbing_api_key.value,
            "api_base": cfg.dubbing_api_base.value,
            "model": cfg.dubbing_model.value,
            "voice": cfg.dubbing_voice.value,
            "timing": "balanced",
            "audio_mode": "replace",
        },
    }


def _current_llm_api_key() -> str:
    service = cfg.llm_service.value
    return {
        "OPENAI": cfg.openai_api_key.value,
        "SILICON_CLOUD": cfg.silicon_cloud_api_key.value,
        "DEEPSEEK": cfg.deepseek_api_key.value,
        "OLLAMA": cfg.ollama_api_key.value,
        "LM_STUDIO": cfg.lm_studio_api_key.value,
        "GEMINI": cfg.gemini_api_key.value,
        "CHATGLM": cfg.chatglm_api_key.value,
    }.get(service.name, "")


def _current_llm_api_base() -> str:
    service = cfg.llm_service.value
    return {
        "OPENAI": cfg.openai_api_base.value,
        "SILICON_CLOUD": cfg.silicon_cloud_api_base.value,
        "DEEPSEEK": cfg.deepseek_api_base.value,
        "OLLAMA": cfg.ollama_api_base.value,
        "LM_STUDIO": cfg.lm_studio_api_base.value,
        "GEMINI": cfg.gemini_api_base.value,
        "CHATGLM": cfg.chatglm_api_base.value,
    }.get(service.name, "")


def _current_llm_model() -> str:
    service = cfg.llm_service.value
    return {
        "OPENAI": cfg.openai_model.value,
        "SILICON_CLOUD": cfg.silicon_cloud_model.value,
        "DEEPSEEK": cfg.deepseek_model.value,
        "OLLAMA": cfg.ollama_model.value,
        "LM_STUDIO": cfg.lm_studio_model.value,
        "GEMINI": cfg.gemini_model.value,
        "CHATGLM": cfg.chatglm_model.value,
    }.get(service.name, "")
