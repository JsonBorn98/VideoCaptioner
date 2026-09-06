"""上游过程资产的共享收集（票 08，D21：CLI 与 Qt 不再各写一份收集循环）。

完整 workflow 把字幕阶段产物（术语表 / 翻译审计 / 翻译检查点）交给后处理
复制进专用过程目录；独立调用由资产发现按 manifest 验证（见 workspace.py）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# 字幕阶段产物属性 → 过程资产种类的唯一映射（两个适配器共用）。
_UPSTREAM_ASSET_ATTRS = (
    ("glossary", "glossary_path"),
    ("audit", "translation_audit_report_path"),
    ("checkpoint", "translation_checkpoint_path"),
)


def collect_upstream_assets(source: Any) -> dict[str, str]:
    """从字幕阶段产物载体收集存在且可读的上游过程资产。

    ``source`` 是字幕阶段任务/args 对象（CLI ``sub_args`` 或 Qt ``subtitle_task``）；
    属性缺失或文件不存在的种类直接跳过，不进入 ``explicit_assets``。
    """

    assets: dict[str, str] = {}
    for kind, attr in _UPSTREAM_ASSET_ATTRS:
        raw = getattr(source, attr, None)
        if not raw:
            continue
        path = Path(str(raw))
        if path.is_file():
            assets[kind] = str(path)
    return assets


__all__ = ["collect_upstream_assets"]
