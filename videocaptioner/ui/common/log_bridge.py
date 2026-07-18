"""Thread-safe bridge from Python logging records to the Qt event loop."""

import logging
import threading
from datetime import datetime
from typing import TYPE_CHECKING

from PyQt5.QtCore import QObject, pyqtSignal

if TYPE_CHECKING:
    from videocaptioner.core.utils.stage_summary import StageSummary


class LogRecordEmitter(QObject):
    """Own the Qt signal used by :class:`QtLogHandler`."""

    record_emitted = pyqtSignal(str, int, str, str)
    stage_summary_emitted = pyqtSignal(object)


_summary_emitter: LogRecordEmitter | None = None
_summary_emitter_lock = threading.RLock()


def install_stage_summary_emitter(emitter: LogRecordEmitter) -> None:
    """Install the main-window emitter used by GUI worker stage boundaries."""

    global _summary_emitter
    with _summary_emitter_lock:
        _summary_emitter = emitter


def uninstall_stage_summary_emitter(emitter: LogRecordEmitter) -> None:
    """Remove *emitter* if it is still the active GUI summary destination."""

    global _summary_emitter
    with _summary_emitter_lock:
        if _summary_emitter is emitter:
            _summary_emitter = None


def publish_stage_summary(summary: "StageSummary") -> bool:
    """Emit a structured summary from any GUI worker thread.

    Returns ``False`` when no runtime-log page is active.  Producers do not
    retain or call the page itself; Qt owns the cross-thread delivery.
    """

    with _summary_emitter_lock:
        emitter = _summary_emitter
    if emitter is None:
        return False
    emitter.stage_summary_emitted.emit(summary)
    return True


class QtLogHandler(logging.Handler):
    """Format log records in their worker thread and emit immutable values.

    The receiver must connect ``record_emitted`` to a GUI slot.  Qt queues the
    delivery to the receiver's thread, so this handler never touches widgets.
    """

    def __init__(self, level: int = logging.INFO):
        super().__init__(level)
        self.emitter = LogRecordEmitter()
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            timestamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
            self.emitter.record_emitted.emit(
                timestamp,
                record.levelno,
                record.name,
                self.format(record),
            )
        except Exception:
            self.handleError(record)


__all__ = [
    "LogRecordEmitter",
    "QtLogHandler",
    "install_stage_summary_emitter",
    "publish_stage_summary",
    "uninstall_stage_summary_emitter",
]
