"""GUI/CLI 前端消费结构化事件（票 07 验收 7）：offscreen Qt + CLI 模式。

GUI 沿 ``PostprocessThread.progress_event`` 信号（queued 送达 GUI 线程），
worker 不触碰控件；CLI 普通 / 详细 / 安静三模式来自同一事件事实
（ADR-0009：控制台由前端拥有）。测试穿过真实 ``run_postprocess_task``
+ 受控网关，不 mock 内部函数。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _probe_env(tmp_path: Path) -> dict:
    """子进程环境：隔离 AppData + offscreen Qt + UTF-8 stdio（票 01 先例）。"""
    env = os.environ.copy()
    env["VIDEOCAPTIONER_APPDATA_PATH"] = str((tmp_path / "appdata").resolve())
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run_python(code: str, tmp_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=_probe_env(tmp_path),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120.0,
        check=False,
    )


def _task_code(tmp_path: Path) -> str:
    """构造完整任务的脚本前奏：合成输入 + 受控网关 + 增强快照。"""
    return f"""
from pathlib import Path
from videocaptioner.core.entities import SubtitleLayoutEnum
from videocaptioner.core.llm import LLMResult
from videocaptioner.core.llm.models import LLMModelProfile, LLMTransport, ProviderDialect
from videocaptioner.core.postprocess.config import PostprocessConfig
from videocaptioner.core.postprocess.models import PostprocessLayoutMode, PostprocessTask
from videocaptioner.core.postprocess.translation import TranslationExecutionSnapshot
from videocaptioner.core.subtitle.io import save_canonical_srt
from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
import json, threading

ROOT = Path({str(tmp_path)!r})
segments = []
for index in range(12):
    original = f"Item {{index}}: hello."
    translated = "这是一个用于离线测试的非常冗长且需要修复的字幕翻译内容" if index % 2 == 0 else "你好"
    segments.append(ASRDataSeg(original, index * 8000, index * 8000 + 6000, translated))
source = ROOT / "input.srt"
save_canonical_srt(ASRData(segments), source, layout=SubtitleLayoutEnum.ORIGINAL_ON_TOP)

profiles = {{
    role: LLMModelProfile(
        profile_id=f"frontend-{{role}}",
        name=f"frontend-{{role}}",
        transport=LLMTransport.OPENAI_COMPATIBLE,
        dialect=ProviderDialect.GENERIC,
        base_url="http://127.0.0.1:1/v1",
        api_key="offline-placeholder",
        model=f"controlled-{{role}}",
        max_output_tokens=8192,
    )
    for role in ("main", "review")
}}

class ControlledGateway:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()
    def complete(self, profile, request, **kwargs):
        user = request.messages[-1].content
        payload = json.loads(user.split("<input>", 1)[1].split("</input>", 1)[0])
        role = "review" if "review_subjects" in payload else "main"
        with self.lock:
            self.calls.append(role)
        if role == "review":
            reviews = [
                {{"problem_id": segment["problem_ids"][0], "output_index": 0, "translated": "您好"}}
                for subject in payload["review_subjects"]
                for segment in subject["segments"]
                if segment["problem_ids"]
            ]
            text = json.dumps({{"reviews": reviews}}, ensure_ascii=False)
        else:
            repairs = [
                {{"problem_id": segment["problem_ids"][0], "output_index": 0,
                  "original": segment["text"], "translated": "你好"}}
                for subject in payload["repair_subjects"]
                for segment in subject["segments"]
                if segment["problem_ids"]
            ]
            text = json.dumps({{"repairs": repairs}}, ensure_ascii=False)
        return LLMResult(text=text)

def make_task():
    return PostprocessTask(
        str(source),
        postprocessed_subtitle_path=str(ROOT / "result.srt"),
        layout_mode=PostprocessLayoutMode.ORIGINAL_ON_TOP,
        config_snapshot=PostprocessConfig(
            trim_trailing_punct=False,
            qa_report=True,
            speed_optimize=False,
            speed_semantic_repair=False,
        ),
        thread_num=4,
        translation_snapshot=TranslationExecutionSnapshot(
            method="enhanced_llm",
            main_profile=profiles["main"],
            review_profile=profiles["review"],
            boundary_context_radius=2,
        ),
    )
"""


def test_gui_worker_emits_events_through_qt_signal(tmp_path):
    """GUI 线程：progress_event 信号送达事件循环，摘要 / 详情共享同一事实。"""
    code = (
        _task_code(tmp_path)
        + """
import json
import time
from PyQt5.QtCore import QCoreApplication
from PyQt5.QtWidgets import QApplication
from videocaptioner.ui.thread.postprocess_thread import PostprocessThread

app = QApplication.instance() or QApplication([])
task = make_task()
gateway = ControlledGateway()
# 首个主修复请求受控延迟 0.8s：等待刷新（0.2s 节流）在窗口内可见；
# 不缩短受控延迟伪造快速返回（票 07 刷新门槛同型探针）。
_real_complete = gateway.complete
_main_calls = {"n": 0}
def slow_complete(profile, request, **kwargs):
    user = request.messages[-1].content
    payload = json.loads(user.split("<input>", 1)[1].split("</input>", 1)[0])
    is_main = "review_subjects" not in payload
    if is_main:
        _main_calls["n"] += 1
        if _main_calls["n"] == 1:
            deadline = time.perf_counter() + 0.8
            while time.perf_counter() < deadline:
                if kwargs.get("cancelled") is not None and kwargs["cancelled"]():
                    raise RuntimeError("probe request cancelled in flight")
                time.sleep(0.01)
    return _real_complete(profile, request, **kwargs)
gateway.complete = slow_complete
thread = PostprocessThread(task, gateway=gateway)
events = []
thread.progress_event.connect(lambda payload: events.append(json.loads(payload)))
terminal = []
thread.finished.connect(lambda video, path: terminal.append((video, path)))
thread.start()
deadline = __import__("time").perf_counter() + 30
while not terminal and __import__("time").perf_counter() < deadline:
    app.processEvents()
assert terminal, "thread did not finish: " + repr(events[-5:])
kinds = [event.get("kind") for event in events]
assert "round" in kinds and "batch" in kinds, kinds
assert "terminal" in kinds
waiting = [event for event in events if event.get("kind") == "waiting"]
assert waiting, "no waiting events reached the GUI loop"
first_waiting = waiting[0]
assert first_waiting["counts"]["window"] >= 1
assert "message" in first_waiting
print(json.dumps({"kinds": kinds, "waiting": len(waiting)}))
"""
    )
    completed = _run_python(code, tmp_path)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_runner_owns_single_cancelled_terminal_event(tmp_path):
    """完整任务入口：取消终态恰发一次（runner 持有，修复层不重复）。"""
    code = (
        _task_code(tmp_path)
        + """
import json, time
from PyQt5.QtWidgets import QApplication
from videocaptioner.ui.thread.postprocess_thread import PostprocessThread

app = QApplication.instance() or QApplication([])
task = make_task()
gateway = ControlledGateway()
# 首个主修复请求挂起等待停止：barrier 用网关首次调用时刻起 0.05s 置停止。
_stop = {"armed": False}
_real_complete = gateway.complete
def hanging_complete(profile, request, **kwargs):
    user = request.messages[-1].content
    payload = json.loads(user.split("<input>", 1)[1].split("</input>", 1)[0])
    is_main = "review_subjects" not in payload
    if is_main and not _stop["armed"]:
        _stop["armed"] = True
        deadline = time.perf_counter() + 5.0
        while time.perf_counter() < deadline:
            if kwargs.get("cancelled") is not None and kwargs["cancelled"]():
                raise RuntimeError("hanging request cancelled in flight")
            time.sleep(0.01)
    return _real_complete(profile, request, **kwargs)
gateway.complete = hanging_complete

thread = PostprocessThread(task, gateway=gateway)
events = []
thread.progress_event.connect(lambda payload: events.append(json.loads(payload)))
terminal = []
thread.cancelled.connect(
    lambda: terminal.append(True),
    __import__("PyQt5.QtCore", fromlist=["Qt"]).Qt.DirectConnection,
)
thread.start()
# 等首个 waiting 事件（刷新已可见）再请求停止。
deadline = time.perf_counter() + 10
while time.perf_counter() < deadline and not any(
    event.get("kind") == "waiting" for event in events
):
    app.processEvents()
thread.stop()
deadline = time.perf_counter() + 15
while time.perf_counter() < deadline and not terminal:
    app.processEvents()
assert terminal, "thread did not reach cancelled: " + repr([
    event.get("kind") for event in events[-8:]])
cancelled_events = [event for event in events
                    if event.get("kind") == "terminal" and event.get("status") == "cancelled"]
assert len(cancelled_events) == 1, cancelled_events  # 恰发一次（无重复完成）
assert cancelled_events[0]["counts"]["requests"] >= 1
print(json.dumps({"cancelled_terminal": 1}))
"""
    )
    completed = _run_python(code, tmp_path)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_gui_page_summary_and_expandable_detail_render(tmp_path):
    """GUI 页面：摘要行 + 展开/收起详情渲染最新事件字段。"""
    code = (
        _task_code(tmp_path)
        + """
import json
from PyQt5.QtWidgets import QApplication
from videocaptioner.core.postprocess import diagnostics
from videocaptioner.ui.view.postprocess_interface import PostprocessInterface
from videocaptioner.core.postprocess.profiles import PostprocessProfileStore
import videocaptioner.ui.view.postprocess_interface as view_module
view_module.InfoBar.info = staticmethod(lambda *a, **k: None)
view_module.InfoBar.success = staticmethod(lambda *a, **k: None)
view_module.InfoBar.warning = staticmethod(lambda *a, **k: None)
view_module.InfoBar.error = staticmethod(lambda *a, **k: None)

app = QApplication.instance() or QApplication([])
import tempfile, pathlib
widget = PostprocessInterface(
    profile_store=PostprocessProfileStore(pathlib.Path(tempfile.mkdtemp()) / "profiles.json")
)
# P3 修复（spec 决策 7「运行耗时」）：任务起点未记（未 start）时无耗时行；
# 记起点后运行中详情行「任务已运行 Xs」由 _render_detail 渲染。
assert widget._task_started_at is None
widget._task_started_at = __import__("time").perf_counter() - 2.5
widget._on_progress_event(diagnostics.round_event(
    round_index=1, open_problems=12, batches=2, window=2, concurrency_gate=2, percent=59))
widget._on_progress_event(diagnostics.waiting_event(
    round_index=1, wait_elapsed_ms=2400, inflight=2, queued=1,
    window=2, request_window_s=128.0, role="main"))
# 并发等待分槽（票 08 点验修复）：校对等待与主修复等待并列，
# 不互相覆盖；主修复的最新刷新保留校对槽。
widget._on_progress_event(diagnostics.waiting_event(
    round_index=1, wait_elapsed_ms=900, inflight=3, queued=0,
    window=3, request_window_s=64.0, role="review"))
widget._on_progress_event(diagnostics.waiting_event(
    round_index=1, wait_elapsed_ms=2500, inflight=1, queued=0,
    window=1, request_window_s=128.0, role="main"))
# 未展开：不渲染但保留最新字段。
assert widget.detail_text.isHidden()
widget._toggle_detail()
assert not widget.detail_text.isHidden()
text = widget.detail_text.text()
# P3 修复（spec 决策 7「运行耗时」）：运行中任务耗时行常驻详情首行。
assert "任务已运行" in text, text
# P5 修复（spec 决策 4「显示实际生效值」）：本轮实际并发进详情。
assert "本轮实际并发 2" in text, text
# 并发等待并列：主修复（已等待 2.5s / 在途 1）与校对（0.9s / 在途 3）
# 同屏，单槽口径的互相覆盖闪烁消除。
assert "主修复" in text and "高级校对" in text, text
assert "已等待 2.5s" in text, text  # 主修复最新（非 2.4s 旧值）
assert "已等待 0.9s" in text, text  # 校对未被主修复覆盖
assert "在途 1" in text and "在途 3" in text, text
assert "128" in text and "64" in text, text  # 两角色等待期限
widget._toggle_detail()
# batch 事件：一批归并验收完成 → 主修复请求已返回，槽清理
# （陈旧主修复等待不再挂着）；校对窗口等待保留。
widget._on_progress_event(diagnostics.batch_event(
    round_index=1, batch_index=0, accepted_in_batch=2, accepted_total=2, subjects=2))
widget._toggle_detail()
text = widget.detail_text.text()
assert "已通过 2" in text, text
assert "未解决 12" in text, text
assert "主修复等待中" not in text, text  # 主修复槽已清理
assert "高级校对等待中" in text, text  # 校对槽保留
widget._toggle_detail()
# P3：终态（completed/failed/cancelled/error）后运行中耗时行无意义——
# 终态事件自带耗时（wall_seconds），_task_started_at 由终态回调清零。
widget._on_progress_event(diagnostics.terminal_event(
    status="completed", counts={"segments": 12}, wall_seconds=8.4))
assert widget._task_started_at is None

# P4 修复（spec 决策 6 末条「明示本地停止不保证服务端撤销/停止计费」）：
# 停止文案明示经济边界（用户故事 40），不只「等待安全结束」。
class _RunningThread:
    def isRunning(self):
        return True
    def stop(self):
        pass
widget._thread = _RunningThread()
widget.cancel()
assert "不保证服务端停止计费" in widget.status_label.text(), widget.status_label.text()
widget._thread = None
print(json.dumps({"detail": text}))
"""
    )
    completed = _run_python(code, tmp_path)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_cli_modes_render_from_the_same_event_facts(tmp_path):
    """CLI：安静全静默、普通只刷进度行、详细逐行渲染（同一事件事实）。"""
    code = (
        _task_code(tmp_path)
        + """
from argparse import Namespace
from videocaptioner.cli.commands import postprocess as command
import videocaptioner.cli.output as cli_output
from videocaptioner.core.postprocess.diagnostics import (
    round_event, waiting_event, batch_event, terminal_event,
)

updates = []
class RecordingProgress:
    def __init__(self, message=""):
        self.message = message
    def start(self):
        return self
    def update(self, percent, message=""):
        updates.append((percent, message))
    def finish(self, message=""):
        pass
    def fail(self, message=""):
        pass

real_progress_line = cli_output.ProgressLine
cli_output.ProgressLine = RecordingProgress
info_lines = []
real_info = cli_output.info
cli_output.info = lambda msg: info_lines.append(msg)

def fake_run(task, **kwargs):
    on_event = kwargs.get("on_event")
    assert on_event is not None, "CLI must pass on_event into the core entry"
    on_event(round_event(round_index=1, open_problems=6, batches=1, window=1, percent=59))
    on_event(waiting_event(round_index=1, wait_elapsed_ms=900, inflight=1,
                           queued=0, window=1, request_window_s=128.0, role="main"))
    on_event(batch_event(round_index=1, batch_index=0, accepted_in_batch=4,
                         accepted_total=4, subjects=4))
    on_event(terminal_event(status="completed", counts={"rounds": 1}))

    class FakeReport:
        speed = None
        viewing_repair = None
        viewing_problems = []
        stages = {}
        placeholder_review = []
        compress_failures = []
        audit = None
        segment_count = 1
        def unresolved_viewing_problems(self):
            return []

    class FakeResult:
        succeeded = True
        used_fallback = False
        warnings = ()
        continue_downstream = True
        report = FakeReport()
        layout = None
        layout_confidence = 1.0
        input_data = None
        precise_timing_outcome = None
        precise_timing_grades = None
        def __init__(self, task):
            self._task = task
        @property
        def output_data(self):
            from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
            return ASRData([ASRDataSeg("你好", 0, 1000, "hello")])
        @property
        def task(self):
            return self._task

    task.status = "completed"
    task.active_subtitle_path = str(ROOT / "result.srt")
    task.postprocessed_subtitle_path = task.active_subtitle_path
    return FakeResult(task)

# 命令入口是函数级延迟导入（from videocaptioner.core.postprocess import
# ...）：fake 挂在包导出处，命令每次导入都解析到 fake。
import videocaptioner.core.postprocess as postprocess_package
_real_run = postprocess_package.run_postprocess_task
postprocess_package.run_postprocess_task = fake_run
try:
    task = make_task()
    gateway = ControlledGateway()
    config = {"postprocess": {"speed_optimize": False, "qa_report": True}}
    args = Namespace(
        input=task.source_subtitle_path, output=None, layout="source-above",
        profile=None, speed_profile=None, media=None, speed_media=None,
        quiet=False, verbose=False, thread_num=None,
        translation_execution_snapshot=task.translation_snapshot,
        gateway=gateway,
    )
    assert command.run(args, config) == 0
    # 普通模式：waiting 刷进度行（percent + 消息）；round/batch/retry/terminal
    # 不逐行渲染。报告位置行（票 08 用户可见展示）与事件无关。
    assert any("已等待" in message for _p, message in updates), updates
    assert not any("round=" in line for line in info_lines), info_lines

    # 详细模式：逐行渲染全部事件。
    info_lines.clear(); updates.clear()
    args2 = Namespace(
        input=task.source_subtitle_path, output=None, layout="source-above",
        profile=None, speed_profile=None, media=None, speed_media=None,
        quiet=False, verbose=True, thread_num=None,
        translation_execution_snapshot=task.translation_snapshot,
        gateway=gateway,
    )
    assert command.run(args2, config) == 0
    assert any("round=1" in line for line in info_lines), info_lines
    assert any("batch=0" in line for line in info_lines), info_lines
    assert any("归并验收" in line for line in info_lines), info_lines
    assert any("已等待" in line for line in info_lines), info_lines

    # 安静模式：progress=None → on_event 首行直接返回，无任何输出。
    info_lines.clear(); updates.clear()
    args3 = Namespace(
        input=task.source_subtitle_path, output=None, layout="source-above",
        profile=None, speed_profile=None, media=None, speed_media=None,
        quiet=True, verbose=False, thread_num=None,
        translation_execution_snapshot=task.translation_snapshot,
        gateway=gateway,
    )
    assert command.run(args3, config) == 0
    assert not updates and not info_lines
finally:
    postprocess_package.run_postprocess_task = _real_run
    cli_output.ProgressLine = real_progress_line
    cli_output.info = real_info
print(json.dumps({"ok": True}))
"""
    )
    completed = _run_python(code, tmp_path)
    assert completed.returncode == 0, completed.stdout + completed.stderr
