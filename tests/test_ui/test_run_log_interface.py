"""Focused checks for the runtime log page and its queued Qt bridge."""

import os
import subprocess
import sys
from types import SimpleNamespace


def _run_qt_script(script: str) -> None:
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_runtime_logs_are_queued_to_the_gui_thread_and_can_be_filtered():
    _run_qt_script(
        r'''
import logging
import threading
import time

from PyQt5.QtCore import QThread, pyqtSlot
from PyQt5.QtWidgets import QApplication

from videocaptioner.config import LOG_PATH
from videocaptioner.core.utils.logger import setup_logger
from videocaptioner.core.utils.stage_summary import StageSummary
from videocaptioner.ui.common.log_bridge import publish_stage_summary
from videocaptioner.ui.view import run_log_interface as run_log_module
from videocaptioner.ui.view.run_log_interface import RunLogInterface


class TrackingRunLogInterface(RunLogInterface):
    delivery_thread = None

    @pyqtSlot(str, int, str, str)
    def _append_log_event(self, timestamp, level, logger_name, message):
        self.delivery_thread = QThread.currentThread()
        super()._append_log_event(timestamp, level, logger_name, message)


def wait_until(predicate, app, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('condition was not reached')


app = QApplication([])
page = TrackingRunLogInterface()
logger = setup_logger('test.gui.runtime.bridge', console_output=False)


def write_logs():
    logger.debug('hidden debug')
    logger.info('worker narrative')
    logger.warning('worker warning')
    logger.error('worker error')
    publish_stage_summary(StageSummary('translate', [('段', 3)], status='ok'))


worker = threading.Thread(target=write_logs)
worker.start()
worker.join()
wait_until(lambda: 'worker error' in page.log_view.toPlainText(), app)

text = page.log_view.toPlainText()
assert 'worker narrative' in text
assert 'worker warning' in text
assert 'worker error' in text
assert 'hidden debug' not in text
assert 'translate · 3 段 [ok]' in text
assert page.delivery_thread is app.thread()

page.level_combo.setCurrentIndex(2)
app.processEvents()
text = page.log_view.toPlainText()
assert 'worker error' in text
assert 'worker warning' not in text
assert 'worker narrative' not in text

page.level_combo.setCurrentIndex(0)
page.search_edit.setText('warning')
wait_until(lambda: 'worker error' not in page.log_view.toPlainText(), app)
text = page.log_view.toPlainText()
assert 'worker warning' in text
assert 'worker error' not in text

page.clear_display()
assert page.log_view.toPlainText() == ''
assert len(page._entries) == 0

page.search_edit.clear()
wait_until(lambda: page._search_query == '', app)

# Per-record work stays O(1): visibility is evaluated once per new entry,
# rather than rescanning the complete deque for the status label.
visibility_checks = 0
original_is_visible = page._entry_is_visible
def counted_is_visible(entry):
    global visibility_checks
    visibility_checks += 1
    return original_is_visible(entry)
page._entry_is_visible = counted_is_visible
for index in range(50):
    page._append_log_event('12:00:00', logging.INFO, 'stress', f'line {index}')
assert visibility_checks == 50, visibility_checks
page._entry_is_visible = original_is_visible

# A traceback remains one retained QTextBlock and continuation lines are
# visually marked instead of appearing as unrelated log entries.
page.clear_display()
page._append_log_event('12:00:01', logging.ERROR, 'worker', 'failed\ntraceback line')
assert page.log_view.document().blockCount() == 1
assert '│ traceback line' in page.log_view.toPlainText()

opened = []
run_log_module.open_folder = opened.append
page.open_log_folder()
assert opened == [str(LOG_PATH)]

page.shutdown()
logger.error('after shutdown')
app.processEvents()
assert 'after shutdown' not in page.log_view.toPlainText()
assert publish_stage_summary(StageSummary('late')) is False
page.close()
'''
    )


def test_home_log_link_routes_to_the_main_run_log_page():
    from videocaptioner.ui.view.task_creation_interface import TaskCreationInterface

    target = object()
    routed = []
    host = SimpleNamespace(runLogInterface=target, switchTo=routed.append)
    interface = SimpleNamespace(window=lambda: host)

    TaskCreationInterface.show_run_log(interface)

    assert routed == [target]
