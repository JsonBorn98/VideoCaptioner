"""执行 ffmpeg 编码命令：进度解析 + 取消/暂停 + 日志回调（见方案 §3.1、§16.8）。

统一的单调进度：单遍 0-100；2-pass 首遍 0-50、次遍 50-100。
通过 SynthesisControl 支持取消（kill 进程）、进程注册（供暂停/停止）与逐行日志（供控制台）。
被集中命令构建器产出的命令共用（ASS 硬烧 / 软/copy / 圆角）。
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from threading import Event
from typing import Callable, Optional, Sequence

from videocaptioner.core.utils.logger import setup_logger

logger = setup_logger("synthesis.runner")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
_DUR_RE = re.compile(r"Duration: (\d{2}):(\d{2}):(\d{2}\.\d{2})")
_TIME_RE = re.compile(r"time=(\d{2}):(\d{2}):(\d{2}\.\d{2})")

ProgressCallback = Callable[..., None]


class SynthesisCancelled(Exception):
    """用户主动取消了合成。"""


@dataclass
class SynthesisControl:
    """贯穿各渲染路径的取消/日志控制。

    cancel_event: 置位后循环检测到即 kill 当前 ffmpeg；
    on_process: 每启动一个 ffmpeg 进程即回调注册（供 GUI 停止/暂停）；
    log_callback: 逐行回传 ffmpeg stderr（供控制台显示）。
    """

    cancel_event: Optional[Event] = None
    log_callback: Optional[Callable[[str], None]] = None
    on_process: Optional[Callable[["subprocess.Popen"], None]] = None

    def cancelled(self) -> bool:
        return bool(self.cancel_event is not None and self.cancel_event.is_set())


def _hms(match: "re.Match[str]") -> float:
    h, m, s = map(float, match.groups())
    return h * 3600 + m * 60 + s


def _run_one(
    cmd: Sequence[str],
    progress_callback: Optional[ProgressCallback],
    span: tuple[int, int],
    total_duration: Optional[float],
    stage_msg: str,
    control: Optional[SynthesisControl] = None,
) -> None:
    """执行一条命令；把本段 0-100 的进度映射进全局 span=(lo,hi)。"""
    lo, hi = span
    cmd_str = subprocess.list2cmdline(list(cmd))
    logger.debug(f"ffmpeg cmd: {cmd_str}")
    if control is not None and control.cancelled():
        raise SynthesisCancelled("已取消")

    process = None
    try:
        process = subprocess.Popen(
            list(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_NO_WINDOW,
        )
        if control is not None and control.on_process:
            control.on_process(process)
        assert process.stderr is not None
        duration = total_duration
        while True:
            line = process.stderr.readline()
            if not line or (process.poll() is not None):
                break
            if control is not None:
                if control.cancelled():
                    process.kill()
                    raise SynthesisCancelled("已取消")
                if control.log_callback:
                    control.log_callback(line.rstrip("\r\n"))
            if not progress_callback:
                continue
            if duration is None and (m := _DUR_RE.search(line)):
                duration = _hms(m)
            if duration and (m := _TIME_RE.search(line)):
                local = min(100.0, _hms(m) / duration * 100)
                glob = lo + local * (hi - lo) / 100
                progress_callback(f"{round(glob)}", stage_msg)

        code = process.wait()
        if control is not None and control.cancelled():
            raise SynthesisCancelled("已取消")
        if code != 0:
            err = process.stderr.read() if process.stderr else ""
            logger.error(f"ffmpeg failed (code {code}); cmd: {cmd_str}")
            if err:
                logger.error(f"stderr: {err}")
            raise RuntimeError(f"FFmpeg 编码失败（返回码 {code}）")
    except SynthesisCancelled:
        if process and process.poll() is None:
            process.kill()
        raise
    except Exception:
        if process and process.poll() is None:
            process.kill()
        raise


def run_encode(
    cmd: Sequence[str],
    progress_callback: Optional[ProgressCallback] = None,
    total_duration: Optional[float] = None,
    control: Optional[SynthesisControl] = None,
) -> None:
    """执行单遍编码命令。"""
    _run_one(cmd, progress_callback, (0, 100), total_duration, "正在合成", control)
    if progress_callback:
        progress_callback("100", "合成完成")


def run_two_pass(
    pass1: Sequence[str],
    pass2: Sequence[str],
    progress_callback: Optional[ProgressCallback] = None,
    total_duration: Optional[float] = None,
    control: Optional[SynthesisControl] = None,
) -> None:
    """执行 2-pass 编码：首遍 0-50%、次遍 50-100%（统一单调进度）。"""
    _run_one(pass1, progress_callback, (0, 50), total_duration, "分析中（第一遍）", control)
    _run_one(pass2, progress_callback, (50, 100), total_duration, "正在合成（第二遍）", control)
    if progress_callback:
        progress_callback("100", "合成完成")


def run_plain(cmd: Sequence[str], control: Optional[SynthesisControl] = None) -> None:
    """运行一条命令（不解析进度），支持取消/日志——用于圆角分批等场景。"""
    _run_one(cmd, None, (0, 100), None, "", control)
