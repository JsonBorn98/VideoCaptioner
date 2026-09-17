"""恢复提示对话框组件（票 08）：模块无关渲染，供后处理票（09）复用。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _run_qt_script(script: str, tmp_dir: Path | None = None) -> None:
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONIOENCODING"] = "utf-8"
    if tmp_dir is not None:
        # 隔离 qconfig settings.json：页面构造会写配置，不能落进真实 AppData。
        env["VIDEOCAPTIONER_APPDATA_PATH"] = str(tmp_dir.resolve())
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_dialog_renders_completed_counts_time_and_drift():
    _run_qt_script(
        """
from PyQt5.QtWidgets import QApplication, QWidget
from videocaptioner.core.recovery import RecoverySummary
from videocaptioner.ui.components.RecoveryPromptDialog import RecoveryPromptDialog

app = QApplication([])
parent = QWidget()
summary = RecoverySummary(
    module='translation',
    identity={'source_fingerprint': 'sha256:test'},
    completed={'analysis': 1, 'glossary': 1, 'translation_segments': 3, 'audit_batches': 2},
    checkpoint_time='2026-09-17T00:00:00Z',
    configuration_drift=('主翻译提示词：检查点 sha256:old，当前 sha256:new',),
)
dialog = RecoveryPromptDialog(
    summary,
    completed_labels={
        'analysis': ('全文分析', None),
        'glossary': ('术语确认', None),
        'translation_segments': ('字幕段', '批'),
        'audit_batches': ('审计批', '批'),
    },
    parent=parent,
)
assert dialog._title_label.text() == '发现恢复检查点'
assert dialog._time_label.text() == '检查点时间：2026-09-17T00:00:00Z'
assert dialog._completed_label.text() == '已完成：全文分析已完成、术语确认已完成、字幕段 3 批、审计批 2 批'
assert [label.text() for label in dialog._drift_labels] == [
    '主翻译提示词：检查点 sha256:old，当前 sha256:new',
]
assert dialog.yesButton.text() == '从检查点继续'
assert dialog.cancelButton.text() == '从头开始'
dialog.close()
"""
    )


def test_dialog_renders_empty_drift_as_none_and_postprocess_labels():
    """无漂移显示「无」；后处理键集合（phase/rounds）用同一组件渲染。"""

    _run_qt_script(
        """
from PyQt5.QtWidgets import QApplication, QWidget
from videocaptioner.core.recovery import RecoverySummary
from videocaptioner.ui.components.RecoveryPromptDialog import RecoveryPromptDialog

app = QApplication([])
parent = QWidget()
summary = RecoverySummary(
    module='postprocess',
    identity={'input_fingerprint': 'sha256:test'},
    completed={'phase': 1, 'rounds': 2},
    checkpoint_time='',
    configuration_drift=(),
)
dialog = RecoveryPromptDialog(
    summary,
    completed_labels={'phase': ('阶段末', None), 'rounds': ('修复轮次', '轮')},
    title='发现后处理恢复检查点',
    parent=parent,
)
assert dialog._time_label.text() == '检查点时间：未知'
assert dialog._completed_label.text() == '已完成：阶段末已完成、修复轮次 2 轮'
assert [label.text() for label in dialog._drift_labels] == ['无']
dialog.close()
"""
    )


def test_dialog_does_not_import_module_specific_types():
    """组件不依赖翻译/后处理专有类型：只从 core.recovery 取类型。"""

    _run_qt_script(
        """
import inspect
import videocaptioner.ui.components.RecoveryPromptDialog as dialog_module

source = inspect.getsource(dialog_module)
assert 'translate.enhanced' not in source
assert 'postprocess' not in source
assert 'from videocaptioner.core.recovery import' in source
"""
    )


def test_subtitle_interface_submits_both_decisions_from_dialog(tmp_path):
    """独立任务页对话框接线：两种决定都提交回线程（页面级接线覆盖）。"""

    _run_qt_script(
        """
from PyQt5.QtWidgets import QApplication, QWidget
from videocaptioner.core.recovery import RecoverySummary
from videocaptioner.ui.view.subtitle_interface import SubtitleInterface

app = QApplication([])
parent = QWidget()
page = SubtitleInterface(parent)

class FakeThread:
    def __init__(self):
        self.decisions = []
    def submit_recovery_decision(self, decision):
        self.decisions.append(decision)
    def stop(self):
        return None  # closeEvent 会停线程；FakeThread 无可停。

submitted = {}
page.subtitle_optimization_thread = FakeThread()

dialogs = []
class PatchedDialog:
    # 类级 accept 开关：exec() 读当前值，两次调用可分别 accept/reject。
    accept = True
    def __init__(self, summary, **kwargs):
        self.summary = summary
        self.kwargs = kwargs
        dialogs.append(self)
    def exec(self):
        return PatchedDialog.accept

import videocaptioner.ui.view.subtitle_interface as interface_module
interface_module.RecoveryPromptDialog = PatchedDialog

# 「从检查点继续」：对话框 accept → 提交 continue。
dialogs.clear()
page.subtitle_optimization_thread.decisions.clear()
page._show_recovery_decision(RecoverySummary(
    module='translation',
    identity={'source_fingerprint': 'sha256:test'},
    completed={'analysis': 1, 'glossary': 1, 'translation_segments': 3, 'audit_batches': 2},
    checkpoint_time='2026-09-17T00:00:00Z',
    configuration_drift=('主翻译提示词：检查点 sha256:old，当前 sha256:new',),
))
assert page.subtitle_optimization_thread.decisions == ['continue']
# 标签表覆盖四个级别；translation_segments 计数单位是「个」（非批数）。
labels = dialogs[0].kwargs['completed_labels']
assert set(labels) == {'analysis', 'glossary', 'translation_segments', 'audit_batches'}
assert labels['translation_segments'][0] == page.tr('字幕段')
assert labels['translation_segments'][1] == page.tr('个')
assert labels['audit_batches'][1] == page.tr('批')

# 「从头开始」：对话框 reject → 提交 start_fresh。
PatchedDialog.accept = False
page.subtitle_optimization_thread.decisions.clear()
page._show_recovery_decision(RecoverySummary(
    module='translation',
    identity={},
    completed={},
    checkpoint_time='',
    configuration_drift=(),
))
assert page.subtitle_optimization_thread.decisions == ['start_fresh']
page.close()
""",
        tmp_dir=tmp_path,
    )
