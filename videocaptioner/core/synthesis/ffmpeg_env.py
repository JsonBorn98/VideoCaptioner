"""ffmpeg / ffprobe 可执行文件解析（见方案 §10.1、ADR 0006）。

单一解析入口，让所有调用点 + 命令预览用同一个"当前生效的核心"。
来源：默认（内置，不可变）/ 自定义（用户专用 git 忽略目录）。缺失回退。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Optional

from videocaptioner.config import BIN_PATH, BUNDLED_BIN_PATH
from videocaptioner.core.utils.logger import setup_logger

logger = setup_logger("synthesis.ffmpeg_env")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
_VERSION_RE = re.compile(r"ffmpeg version n?(\d+)\.(\d+)")


def _resolve(name: str, source: str, custom_dir: Optional[str]) -> str:
    """在候选目录里找可执行文件，最后回退系统 PATH / 裸名。"""
    candidates: list[Path] = []
    if source == "custom":
        candidates.append(Path(custom_dir) if custom_dir else BIN_PATH)
        candidates.append(BUNDLED_BIN_PATH)  # 自定义缺失/损坏时回退内置
    else:
        candidates.append(BUNDLED_BIN_PATH)
        candidates.append(BIN_PATH)

    for d in candidates:
        try:
            found = shutil.which(name, path=str(d))
        except (OSError, ValueError):
            found = None
        if found:
            return found

    on_path = shutil.which(name)
    if on_path:
        return on_path
    logger.warning(f"{name} not found in bin dirs or PATH; falling back to bare name")
    return name


def get_ffmpeg_path(source: str = "default", custom_dir: Optional[str] = None) -> str:
    """解析当前生效的 ffmpeg 可执行文件路径。"""
    return _resolve("ffmpeg", source, custom_dir)


def get_ffprobe_path(source: str = "default", custom_dir: Optional[str] = None) -> str:
    """解析当前生效的 ffprobe 可执行文件路径。"""
    return _resolve("ffprobe", source, custom_dir)


@lru_cache(maxsize=8)
def supports_fps_mode(ffmpeg: str) -> bool:
    """当前 ffmpeg 是否支持 -fps_mode（5.1+）；旧版应回退 -vsync（见 §16.1）。

    解析失败时保守假定支持（现代构建居多）；缓存按二进制路径。
    """
    try:
        result = subprocess.run(
            [ffmpeg, "-version"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=_NO_WINDOW,
        )
    except OSError:
        return True
    m = _VERSION_RE.search(result.stdout or "")
    if not m:
        return True
    major, minor = int(m.group(1)), int(m.group(2))
    return (major, minor) >= (5, 1)
