from PyQt5.QtCore import QThread, pyqtSignal

from videocaptioner.core.asr.qwen_runtime_manager import install_qwen_runtime


class QwenRuntimeInstallThread(QThread):
    progress = pyqtSignal(str)
    error = pyqtSignal(str)
    installed = pyqtSignal(str)

    def run(self):
        try:
            status = install_qwen_runtime(progress=self.progress.emit)
            self.installed.emit(str(status.runtime_dir))
        except Exception as exc:
            self.error.emit(str(exc))
