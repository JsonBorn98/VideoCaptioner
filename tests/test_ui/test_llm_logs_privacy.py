import os
from importlib import import_module

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication

from videocaptioner.core.llm.request_logger import (
    is_llm_content_logging_enabled,
)
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.view.llm_logs_interface import LLMLogsInterface

app = QApplication.instance() or QApplication([])


def test_gui_content_logging_config_updates_core_logger():
    previous = cfg.llm_content_logging.value
    try:
        cfg.set(cfg.llm_content_logging, False)
        assert is_llm_content_logging_enabled() is False
        cfg.set(cfg.llm_content_logging, True)
        assert is_llm_content_logging_enabled() is True
    finally:
        cfg.set(cfg.llm_content_logging, previous)


def test_clear_logs_removes_current_and_rotated_legacy_content(tmp_path, monkeypatch):
    module = import_module("videocaptioner.ui.view.llm_logs_interface")
    log_path = tmp_path / "llm_requests.jsonl"
    old_path = log_path.with_suffix(".jsonl.old")
    log_path.write_text('{"request":{"messages":["private"]}}\n', encoding="utf-8")
    old_path.write_text('{"response":{"raw":"legacy"}}\n', encoding="utf-8")
    monkeypatch.setattr(module, "LLM_LOG_FILE", log_path)
    monkeypatch.setattr(module, "LOG_PATH", tmp_path)

    class Confirm:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def exec(self):
            return True

    monkeypatch.setattr(module, "MessageBox", Confirm)
    widget = LLMLogsInterface()

    widget._clear_logs()
    app.processEvents()

    assert not log_path.exists()
    assert not old_path.exists()
    assert "旧日志可能仍含字幕" in widget.privacy_hint.text()
    widget.close()
