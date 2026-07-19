import logging
from unittest.mock import patch

from videocaptioner.core.utils.logger import (
    _WindowsSafeRotatingFileHandler,
    register_log_handler,
    setup_logger,
    unregister_log_handler,
)


class _RecordingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def test_named_loggers_share_one_file_handler(tmp_path):
    log_path = tmp_path / "shared.log"
    first = setup_logger("test.shared.first", log_file=str(log_path), console_output=False)
    second = setup_logger("test.shared.second", log_file=str(log_path), console_output=False)

    assert len(first.handlers) == 1
    assert len(second.handlers) == 1
    assert first.handlers[0] is second.handlers[0]

    first.info("from first")
    second.info("from second")
    first.handlers[0].flush()
    content = log_path.read_text(encoding="utf-8")
    assert "from first" in content
    assert "from second" in content


def test_rollover_file_lock_is_delayed_without_losing_log_record(tmp_path):
    log_path = tmp_path / "locked.log"
    log_path.write_text("existing record\n", encoding="utf-8")
    handler = _WindowsSafeRotatingFileHandler(
        log_path,
        maxBytes=10,
        backupCount=1,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("test.locked.rollover")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    try:
        with patch.object(handler, "rotate", side_effect=PermissionError(13, "locked")):
            handler.doRollover()
        logger.info("record survives")
        handler.flush()
        assert "record survives" in log_path.read_text(encoding="utf-8")
        assert handler._rollover_retry_after > 0
    finally:
        handler.close()
        logger.handlers.clear()


def test_registered_handler_observes_existing_and_future_loggers(tmp_path):
    existing = setup_logger(
        "test.observer.existing",
        log_file=str(tmp_path / "existing.log"),
        console_output=False,
    )
    observer = _RecordingHandler()

    register_log_handler(observer)
    register_log_handler(observer)
    try:
        future = setup_logger(
            "test.observer.future",
            log_file=str(tmp_path / "future.log"),
            console_output=False,
        )
        assert sum(handler is observer for handler in existing.handlers) == 1
        assert sum(handler is observer for handler in future.handlers) == 1

        existing.info("from existing")
        future.warning("from future")
        assert observer.messages == ["from existing", "from future"]
    finally:
        unregister_log_handler(observer)

    existing.info("after unregister")
    assert observer.messages == ["from existing", "from future"]


def test_suppress_console_keeps_exception_in_file_without_duplicate_output(tmp_path, capsys):
    log_path = tmp_path / "exception.log"
    logger = setup_logger("test.suppress.console", log_file=str(log_path))

    for _ in range(10):
        logger.error("native event failed", extra={"suppress_console": True})

    for handler in logger.handlers:
        handler.flush()

    captured = capsys.readouterr()
    assert "native event failed" not in captured.out
    assert "native event failed" not in captured.err
    assert log_path.read_text(encoding="utf-8").count("native event failed") == 10
