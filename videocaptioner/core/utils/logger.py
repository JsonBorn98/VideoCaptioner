import logging
import logging.handlers
import os
import threading
import time
from pathlib import Path
from typing import Union

from ...config import LOG_LEVEL, LOG_PATH

LogLevel = Union[int, str]


class _WindowsSafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Rotate without surfacing transient Windows file-sharing errors.

    A second VideoCaptioner process may still have ``app.log`` open when this
    process reaches the rollover threshold.  Windows then rejects the rename.
    Keep appending to the active log and retry later instead of printing a
    logging-internal traceback to the user.
    """

    _ROLLOVER_RETRY_SECONDS = 60.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._rollover_retry_after = 0.0

    def shouldRollover(self, record: logging.LogRecord) -> bool:  # noqa: N802
        if time.monotonic() < self._rollover_retry_after:
            return False
        return bool(super().shouldRollover(record))

    def doRollover(self) -> None:  # noqa: N802
        try:
            super().doRollover()
        except (PermissionError, FileNotFoundError):
            self._rollover_retry_after = time.monotonic() + self._ROLLOVER_RETRY_SECONDS
        except OSError as exc:
            if os.name != "nt" or getattr(exc, "winerror", None) not in {13, 32}:
                raise
            self._rollover_retry_after = time.monotonic() + self._ROLLOVER_RETRY_SECONDS


_FILE_HANDLERS: dict[str, _WindowsSafeRotatingFileHandler] = {}
_FILE_HANDLERS_LOCK = threading.RLock()


def _shared_file_handler(
    log_file: str,
    formatter: logging.Formatter,
) -> _WindowsSafeRotatingFileHandler:
    """Return the sole file handler for a path within this process."""

    resolved_path = str(Path(log_file).resolve())
    with _FILE_HANDLERS_LOCK:
        handler = _FILE_HANDLERS.get(resolved_path)
        if handler is None:
            Path(resolved_path).parent.mkdir(parents=True, exist_ok=True)
            handler = _WindowsSafeRotatingFileHandler(
                resolved_path,
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
                delay=True,
            )
            handler.setLevel(logging.NOTSET)
            handler.setFormatter(formatter)
            _FILE_HANDLERS[resolved_path] = handler
        return handler


def _coerce_log_level(value: LogLevel | None, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    normalized = value.strip().upper()
    if not normalized:
        return default
    if normalized.isdigit():
        return int(normalized)
    return int(logging._nameToLevel.get(normalized, default))


def setup_logger(
    name: str,
    level: int = LOG_LEVEL,
    info_fmt: str = "%(message)s",  # INFO级别使用简化格式
    default_fmt: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s",  # 其他级别使用详细格式
    datefmt: str = "%Y-%m-%d %H:%M:%S",
    log_file: str = str(LOG_PATH / "app.log"),
    console_output: bool = True,
    console_level: LogLevel | None = None,
) -> logging.Logger:
    """
    创建并配置一个日志记录器，INFO级别使用简化格式。

    参数:
    - name: 日志记录器的名称
    - level: 日志级别
    - info_fmt: INFO级别的日志格式字符串
    - default_fmt: 其他级别的日志格式字符串
    - datefmt: 时间格式字符串
    - log_file: 日志文件路径
    """

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    if not logger.handlers:
        class LevelSpecificFormatter(logging.Formatter):
            """Thread-safe formatter that uses different formats per log level."""

            def __init__(self, *args, include_exception: bool = True, **kwargs):
                super().__init__(*args, **kwargs)
                self.include_exception = include_exception

            def format(self, record):
                # Use local variable instead of mutating shared _style._fmt
                fmt = info_fmt if record.levelno == logging.INFO else default_fmt
                formatter = logging.Formatter(fmt, datefmt=datefmt)
                if self.include_exception:
                    return formatter.format(record)

                original_exc_info = record.exc_info
                original_exc_text = record.exc_text
                record.exc_info = None
                record.exc_text = None
                try:
                    return formatter.format(record)
                finally:
                    record.exc_info = original_exc_info
                    record.exc_text = original_exc_text

        level_formatter = LevelSpecificFormatter(default_fmt, datefmt=datefmt)
        console_formatter = LevelSpecificFormatter(
            default_fmt,
            datefmt=datefmt,
            include_exception=False,
        )

        # 只在console_output为True时添加控制台处理器
        if console_output:
            resolved_console_level = _coerce_log_level(
                console_level or os.environ.get("VIDEOCAPTIONER_CONSOLE_LOG_LEVEL"),
                logging.WARNING,
            )

            class ConsoleFilter(logging.Filter):
                def filter(self, record: logging.LogRecord) -> bool:
                    if getattr(record, "suppress_console", False):
                        return False
                    return record.levelno >= resolved_console_level or bool(
                        getattr(record, "console", False)
                    )

            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.NOTSET)
            console_handler.addFilter(ConsoleFilter())
            console_handler.setFormatter(console_formatter)
            logger.addHandler(console_handler)

        # 文件处理器
        if log_file:
            logger.addHandler(_shared_file_handler(log_file, level_formatter))

    # 设置特定库的日志级别为ERROR以减少日志噪音
    error_loggers = [
        "urllib3",
        "requests",
        "openai",
        "httpx",
        "httpcore",
        "ssl",
        "certifi",
    ]
    for lib in error_loggers:
        logging.getLogger(lib).setLevel(logging.ERROR)

    return logger
