import io
import logging
import sys
import threading
from typing import Callable

from modelscope.hub.callback import ProgressCallback
from modelscope.hub.snapshot_download import snapshot_download
from PyQt5.QtCore import QThread, pyqtSignal


class DownloadCancelled(RuntimeError):
    """Raised inside ModelScope callbacks when the user cancels a download."""


class SuppressOutput:
    """上下文管理器：抑制 stdout/stderr 和 modelscope 日志"""

    def __enter__(self):
        self._stdout = sys.stdout
        self._stderr = sys.stderr
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        self._loggers: dict[str, int] = {}
        for name in ["modelscope", "tqdm"]:
            logger = logging.getLogger(name)
            self._loggers[name] = logger.level
            logger.setLevel(logging.CRITICAL)
        return self

    def __exit__(self, *args):
        sys.stdout = self._stdout
        sys.stderr = self._stderr
        for name, level in self._loggers.items():
            logging.getLogger(name).setLevel(level)


def create_progress_callback_class(
    progress_callback: Callable[[int, str], None],
    cancel_event: threading.Event | None = None,
) -> type[ProgressCallback]:
    """创建一个自定义的 ProgressCallback 类，用于接收下载进度.

    ModelScope 会为 snapshot 中的每个文件创建独立 callback，并且大文件还会
    分片并发下载。这里把多个文件的进度聚合成一个稳定的总进度，避免 UI 在
    多个 shard 的单文件百分比之间来回跳动。
    """

    lock = threading.Lock()
    file_sizes: dict[str, int] = {}
    file_downloaded: dict[str, int] = {}
    last_emitted_percentage = -1

    def raise_if_cancelled() -> None:
        if cancel_event and cancel_event.is_set():
            raise DownloadCancelled("Download cancelled")

    class CustomProgressCallback(ProgressCallback):
        def __init__(self, filename: str, file_size: int):
            super().__init__(filename, file_size)
            raise_if_cancelled()
            self.downloaded = 0
            with lock:
                file_sizes[self.filename] = max(int(self.file_size or 0), 0)
                file_downloaded.setdefault(self.filename, 0)

        def update(self, size: int):
            nonlocal last_emitted_percentage

            raise_if_cancelled()
            message = ""
            percentage = 0
            should_emit = False
            with lock:
                file_size = max(int(self.file_size or 0), 0)
                delta = max(int(size or 0), 0)
                if file_size:
                    self.downloaded = min(self.downloaded + delta, file_size)
                else:
                    self.downloaded += delta
                file_downloaded[self.filename] = self.downloaded

                if file_size > 0:
                    file_percentage = min(int(self.downloaded * 100 / file_size), 99)
                else:
                    file_percentage = 0

                known_total = sum(value for value in file_sizes.values() if value > 0)
                known_downloaded = sum(
                    min(file_downloaded.get(name, 0), total)
                    for name, total in file_sizes.items()
                    if total > 0
                )
                if known_total > 0:
                    percentage = min(int(known_downloaded * 100 / known_total), 99)
                else:
                    percentage = 0

                # File callbacks can appear as each file starts. Keep the progress
                # bar monotonic so newly discovered large files cannot make it jump back.
                percentage = max(last_emitted_percentage, percentage, 0)

                # Avoid noisy "file A: 0%" / "file B: 0%" ping-pong at startup.
                should_emit = percentage > last_emitted_percentage or file_percentage > 0
                if should_emit:
                    last_emitted_percentage = percentage
                    message = f"正在下载 {self.filename}: {file_percentage}%（总进度 {percentage}%）"

            if should_emit:
                progress_callback(percentage, message)
            raise_if_cancelled()

        def end(self):
            raise_if_cancelled()
            with lock:
                file_size = max(int(self.file_size or 0), 0)
                if file_size > 0:
                    self.downloaded = file_size
                    file_downloaded[self.filename] = file_size

    return CustomProgressCallback


class ModelscopeDownloadThread(QThread):
    progress = pyqtSignal(int, str)
    error = pyqtSignal(str)
    canceled = pyqtSignal(str)

    def __init__(self, model_id: str, save_path: str):
        super().__init__()
        self.model_id = model_id
        self.save_path = save_path
        self._cancel_event = threading.Event()

    def cancel(self):
        self._cancel_event.set()
        self.requestInterruption()

    def run(self):
        try:
            self.progress.emit(0, self.tr("开始下载..."))

            callback_class = create_progress_callback_class(
                self.progress.emit,
                cancel_event=self._cancel_event,
            )

            with SuppressOutput():
                snapshot_download(
                    self.model_id,
                    local_dir=self.save_path,
                    progress_callbacks=[callback_class],
                )

            self.progress.emit(100, self.tr("下载完成"))

        except DownloadCancelled:
            self.canceled.emit(self.tr("下载已取消"))
        except Exception as e:
            self.error.emit(str(e))


if __name__ == "__main__":
    import sys

    from PyQt5.QtCore import QCoreApplication

    app = QCoreApplication(sys.argv)
    model_id = "pengzhendong/faster-whisper-tiny"
    save_path = r"models/faster-whisper-tiny"
    downloader = ModelscopeDownloadThread(model_id, save_path)

    def on_progress(percentage, message):
        print(f"进度: {message}")

    def on_error(error_msg):
        print(f"错误: {error_msg}")
        app.quit()

    def on_finished():
        print("下载完成！")
        app.quit()

    downloader.progress.connect(on_progress)
    downloader.error.connect(on_error)
    downloader.finished.connect(on_finished)

    print(f"开始下载模型 {model_id}")
    downloader.start()

    sys.exit(app.exec_())
