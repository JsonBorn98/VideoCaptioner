"""执行 ffmpeg 编码命令并解析进度（见方案 §3.1、§16.8）。

统一的单调进度：单遍 0-100；2-pass 首遍 0-50、次遍 50-100。
被集中命令构建器产出的命令共用（ASS 硬烧 / 软/copy / 圆角最终编码）。
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Callable, Optional, Sequence

from videocaptioner.core.utils.logger import setup_logger

logger = setup_logger("synthesis.runner")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
_DUR_RE = re.compile(r"Duration: (\d{2}):(\d{2}):(\d{2}\.\d{2})")
_TIME_RE = re.compile(r"time=(\d{2}):(\d{2}):(\d{2}\.\d{2})")

ProgressCallback = Callable[..., None]


def _hms(match: "re.Match[str]") -> float:
    h, m, s = map(float, match.groups())
    return h * 3600 + m * 60 + s


def _run_one(
    cmd: Sequence[str],
    progress_callback: Optional[ProgressCallback],
    span: tuple[int, int],
    total_duration: Optional[float],
    stage_msg: str,
) -> None:
    """执行一条命令；把本段 0-100 的进度映射进全局 span=(lo,hi)。"""
    lo, hi = span
    cmd_str = subprocess.list2cmdline(list(cmd))
    logger.debug(f"ffmpeg cmd: {cmd_str}")

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
        assert process.stderr is not None
        duration = total_duration
        while True:
            line = process.stderr.readline()
            if not line or (process.poll() is not None):
                break
            if not progress_callback:
                continue
            if duration is None and (m := _DUR_RE.search(line)):
                duration = _hms(m)
            if duration and (m := _TIME_RE.search(line)):
                local = min(100.0, _hms(m) / duration * 100)
                glob = lo + local * (hi - lo) / 100
                progress_callback(f"{round(glob)}", stage_msg)

        code = process.wait()
        if code != 0:
            err = process.stderr.read() if process.stderr else ""
            logger.error(f"ffmpeg failed (code {code}); cmd: {cmd_str}")
            if err:
                logger.error(f"stderr: {err}")
            raise RuntimeError(f"FFmpeg 编码失败（返回码 {code}）")
    except Exception:
        if process and process.poll() is None:
            process.kill()
        raise


def run_encode(
    cmd: Sequence[str],
    progress_callback: Optional[ProgressCallback] = None,
    total_duration: Optional[float] = None,
) -> None:
    """执行单遍编码命令。"""
    _run_one(cmd, progress_callback, (0, 100), total_duration, "正在合成")
    if progress_callback:
        progress_callback("100", "合成完成")


def run_two_pass(
    pass1: Sequence[str],
    pass2: Sequence[str],
    progress_callback: Optional[ProgressCallback] = None,
    total_duration: Optional[float] = None,
) -> None:
    """执行 2-pass 编码：首遍 0-50%、次遍 50-100%（统一单调进度）。"""
    _run_one(pass1, progress_callback, (0, 50), total_duration, "分析中（第一遍）")
    _run_one(pass2, progress_callback, (50, 100), total_duration, "正在合成（第二遍）")
    if progress_callback:
        progress_callback("100", "合成完成")
