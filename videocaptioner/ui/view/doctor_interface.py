from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    IconWidget,
    InfoBadge,
    InfoBar,
    InfoLevel,
    LargeTitleLabel,
    PrimaryPushButton,
    ScrollArea,
    SubtitleLabel,
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


class HealthCard(CardWidget):
    def __init__(self, title: str, icon, parent=None):
        super().__init__(parent)
        self.setObjectName("healthCard")
        self.setMinimumHeight(112)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        top = QHBoxLayout()
        self.iconLabel = IconWidget(self)
        self.iconLabel.setIcon(icon)
        self.iconLabel.setFixedSize(34, 34)
        self.badge = StatusPill("待检查", self)
        top.addWidget(self.iconLabel)
        top.addStretch(1)
        top.addWidget(self.badge)
        self.titleLabel = BodyLabel(title, self)
        self.messageLabel = CaptionLabel("等待自动检查", self)
        self.messageLabel.setWordWrap(True)
        layout.addLayout(top)
        layout.addWidget(self.titleLabel)
        layout.addWidget(self.messageLabel)

    def updateState(self, status: str, message: str):
        self.badge.setStatus(status)
        self.messageLabel.setText(message)
        self.setProperty("status", status)
        self.style().unpolish(self)
        self.style().polish(self)


class CheckRow(CardWidget):
    def __init__(self, check: Check, parent=None):
        super().__init__(parent)
        self.setObjectName("checkRow")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(12)
        self.badge = StatusPill(check.status, self)
        textBox = QVBoxLayout()
        textBox.setSpacing(3)
        self.nameLabel = BodyLabel(check.name, self)
        self.messageLabel = CaptionLabel(check.message, self)
        self.messageLabel.setWordWrap(True)
        textBox.addWidget(self.nameLabel)
        textBox.addWidget(self.messageLabel)
        layout.addWidget(self.badge)
        layout.addLayout(textBox, 1)
        if check.fix:
            fix = CaptionLabel(check.fix, self)
            fix.setWordWrap(True)
            layout.addWidget(fix, 1)
        self.setProperty("status", check.status)


class DoctorInterface(ScrollArea):
    """桌面端诊断页。"""

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setWindowTitle(self.tr("诊断"))
        self.thread: DoctorThread | None = None
        self._auto_started = False
        self.scrollWidget = QWidget()
        self.pageLayout = QVBoxLayout(self.scrollWidget)
        self.titleLabel = LargeTitleLabel(self.tr("诊断"), self)
        self.resultContainer = QWidget(self.scrollWidget)
        self.resultLayout = QVBoxLayout(self.resultContainer)
        self.resultLayout.setContentsMargins(0, 0, 0, 0)
        self.resultLayout.setSpacing(10)
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
        self.setStyleSheet(
            """
            DoctorInterface, #scrollWidget { background-color: transparent; }
            QScrollArea { border: none; background-color: transparent; }
            CardWidget#healthCard, CardWidget#checkRow { border-radius: 10px; background: rgba(38, 38, 38, 0.92); border: 1px solid rgba(255, 255, 255, 0.08); }
            CardWidget#healthCard[status="ok"], CardWidget#checkRow[status="ok"] { border: 1px solid rgba(67, 217, 154, 0.55); }
            CardWidget#healthCard[status="warn"], CardWidget#checkRow[status="warn"] { border: 1px solid rgba(255, 191, 71, 0.55); }
            CardWidget#healthCard[status="error"], CardWidget#checkRow[status="error"] { border: 1px solid rgba(255, 84, 84, 0.55); }
            CardWidget#healthCard[status="checking"], CardWidget#checkRow[status="checking"] { border: 1px solid rgba(120, 170, 255, 0.45); }
            CardWidget#healthCard[status="pending"], CardWidget#checkRow[status="pending"] { border: 1px solid rgba(255, 255, 255, 0.10); }
            """
        )

        toolbar = QWidget(self.scrollWidget)
        toolbarLayout = QHBoxLayout(toolbar)
        toolbarLayout.setContentsMargins(0, 0, 0, 0)
        toolbarLayout.setSpacing(10)
        heading = QVBoxLayout()
        heading.setSpacing(4)
        heading.addWidget(SubtitleLabel(self.tr("环境健康检查"), toolbar))
        heading.addWidget(CaptionLabel(self.tr("快速项会自动检查；深度诊断会尝试真实服务请求。"), toolbar))
        toolbarLayout.addLayout(heading, 1)
        self.runButton = PrimaryPushButton(self.tr("重新检查"), toolbar, icon=FIF.SEARCH)
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
            "env": HealthCard(self.tr("基础环境"), FIF.COMMAND_PROMPT, self.scrollWidget),
            "download": HealthCard(self.tr("下载能力"), FIF.DOWNLOAD, self.scrollWidget),
            "ai": HealthCard(self.tr("AI 配置"), FIF.ROBOT, self.scrollWidget),  # type: ignore
            "dubbing": HealthCard(self.tr("配音服务"), FIF.VOLUME, self.scrollWidget),
        }
        for index, card in enumerate(self.healthCards.values()):
            self.summaryGrid.addWidget(card, index // 2, index % 2)

        self.pageLayout.setSpacing(18)
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
        if not self._auto_started:
            self._auto_started = True
            QTimer.singleShot(80, lambda: self._run(False))

    def _show_pending_rows(self):
        self._clear_results()
        pending = [
            Check("python / ffmpeg / ffprobe", "pending", self.tr("等待检查本机音视频依赖")),
            Check("yt-dlp", "pending", self.tr("等待检查在线视频下载能力")),
            Check("transcribe / subtitle / LLM", "pending", self.tr("等待检查转录、字幕处理和模型配置")),
            Check("dubbing", "pending", self.tr("等待检查当前配音 provider、音色和 Key")),
        ]
        for check in pending:
            self.resultLayout.addWidget(CheckRow(check, self.resultContainer))

    def _run(self, check_api: bool):
        if self.thread and self.thread.isRunning():
            return
        self._set_running(True)
        self._clear_results()
        self.resultLayout.addWidget(CheckRow(Check("running", "checking", self.tr("正在检查当前环境和配置")), self.resultContainer))
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
