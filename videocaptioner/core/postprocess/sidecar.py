"""对齐时间轴 sidecar 的共享守卫（票 08：CLI 与 Qt 不再各写一份条件链）。

Sidecar 是跟随字幕输出的可复用时间证据档案，不是过程报告——
过程报告 / 状态由核心任务入口写入专用过程目录（D21/D28，见 workspace.py）。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import PostprocessResult


def write_timing_sidecar_if_applied(result: "PostprocessResult", output_path: str) -> Path | None:
    """按结果守卫保存 ``<subtitle stem>.vctiming.json``；不满足条件返回 None。

    守卫条件：配置请求保存 sidecar、任务持有时间证据 bundle、
    且对齐时间轴结果为 ``applied``（降级路径不保存）。
    """

    config = result.task.config_snapshot
    bundle = result.task.timing_bundle
    if (
        config is None
        or not output_path
        or not config.save_timing_sidecar
        or bundle is None
        or result.precise_timing_outcome != "applied"
    ):
        return None
    from ..speed.timing_archive import timing_sidecar_path, write_timing_archive

    sidecar_path = timing_sidecar_path(output_path)
    write_timing_archive(sidecar_path, bundle)
    return sidecar_path


__all__ = ["write_timing_sidecar_if_applied"]
