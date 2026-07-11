import logging
from unittest.mock import patch

from videocaptioner.core.utils.logger import (
    _WindowsSafeRotatingFileHandler,
    setup_logger,
)


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
