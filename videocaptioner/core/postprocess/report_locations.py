"""过程报告位置的共享描述（票 08，验收 7：CLI 与 Qt 展示同一事实）。

过程报告 / 状态由核心任务入口写入专用过程目录（D21/D28，见 workspace.py）；
适配层只描述位置，不再向普通输出目录复制过程文件。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import PostprocessTask

# 标签映射的单一来源（CLI 文案 / Qt 日志共用）。
PERSISTED_OUTPUT_LABELS = {
    "qa_report": "QA 报告",
    "speed_changes": "速度变更记录",
    "postprocess_state": "后处理状态",
}


def describe_persisted_outputs(task: "PostprocessTask") -> list[tuple[str, str]]:
    """已写入过程目录的下游产物 ``(标签, 路径)`` 列表，按标签映射顺序。"""

    persisted = getattr(task, "persisted_outputs", {})
    return [
        (label, persisted[kind])
        for kind, label in PERSISTED_OUTPUT_LABELS.items()
        if persisted.get(kind)
    ]


__all__ = ["PERSISTED_OUTPUT_LABELS", "describe_persisted_outputs"]
