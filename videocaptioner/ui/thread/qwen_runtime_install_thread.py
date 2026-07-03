from PyQt5.QtCore import QThread, pyqtSignal

from videocaptioner.core.asr.qwen_runtime_manager import QwenRuntimeProfile, install_qwen_runtime


class QwenRuntimeInstallThread(QThread):
    progress = pyqtSignal(str)
    error = pyqtSignal(str)
    installed = pyqtSignal(str, str)

    def __init__(self, profile: QwenRuntimeProfile = "cpu"):
        super().__init__()
        self.profile: QwenRuntimeProfile = profile

    def run(self):
        try:
            status = install_qwen_runtime(profile=self.profile, progress=self.progress.emit)
            self.installed.emit(str(status.runtime_dir), status.torch_message)
        except Exception as exc:
            self.error.emit(str(exc))
