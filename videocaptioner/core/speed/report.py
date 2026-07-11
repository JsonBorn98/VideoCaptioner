"""Serialize speed optimization analysis and change records."""

from __future__ import annotations

import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from .models import canonical_json_bytes
from .pipeline import SpeedOptimizationResult


def result_to_dict(result: SpeedOptimizationResult) -> dict:
    return {
        "schema_version": 1,
        "mode": result.mode,
        "profile_id": result.profile_id,
        "policy": asdict(result.policy),
        "before": asdict(result.before),
        "after": asdict(result.after),
        "changes": [asdict(change) for change in result.changes],
        "unresolved_cue_ids": list(result.unresolved_cue_ids),
        "invalid_cue_ids": list(result.invalid_cue_ids),
        "protected": [asdict(match) for match in result.protected],
        "structural_operations": [asdict(operation) for operation in result.structural_operations],
        "semantic_records": [asdict(record) for record in result.semantic_records],
        "reference_before": asdict(result.reference_before) if result.reference_before else None,
        "reference_after": asdict(result.reference_after) if result.reference_after else None,
    }


def write_changes(path: str | Path, result: SpeedOptimizationResult) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_bytes(result_to_dict(result)) + b"\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return destination


def build_speed_qa(result: SpeedOptimizationResult) -> str:
    before = result.before
    after = result.after
    lines = [
        "# 字幕速度优化 QA 报告\n\n",
        f"- 模式: `{result.mode}`\n",
        f"- 方案: `{result.profile_id}`\n",
        f"- 已接受边界修改: {len(result.changes)}\n",
        f"- 已接受结构修改: {len(result.structural_operations)}\n",
        f"- 已接受语义修复: "
        f"{sum(record.status.value == 'accepted' for record in result.semantic_records)}\n",
        f"- 已回滚/未解决语义窗口: "
        f"{sum(record.status.value != 'accepted' for record in result.semantic_records)}\n",
        f"- 未解决硬超速: {len(result.unresolved_cue_ids)}\n\n",
        f"- 非法时长字幕: {len(result.invalid_cue_ids)}\n\n",
        f"- 受保护字幕: {len(result.protected)}\n\n",
        "## M3 前后对比\n\n",
        "| 指标 | 优化前 | 优化后 |\n",
        "| --- | ---: | ---: |\n",
        f"| HardDeficit | {before.hard_deficit:.6f} | {after.hard_deficit:.6f} |\n",
        f"| 硬超速段 | {before.unresolved_hard_count} | {after.unresolved_hard_count} |\n",
        f"| SpeedSpread | {before.speed_spread} | {after.speed_spread} |\n",
        f"| AdjacentJump P90 | {before.adjacent_jump.p90} | {after.adjacent_jump.p90} |\n",
        f"| 紧急跳变 | {before.adjacent_jump.emergency_count} | "
        f"{after.adjacent_jump.emergency_count} |\n",
    ]
    if result.unresolved_cue_ids:
        lines.extend(["\n## 未解决窗口\n\n"])
        lines.extend(f"- `{cue_id}`\n" for cue_id in result.unresolved_cue_ids)
    if result.invalid_cue_ids:
        lines.extend(["\n## 非法时长字幕\n\n"])
        lines.extend(f"- `{cue_id}`\n" for cue_id in result.invalid_cue_ids)
    if result.protected:
        lines.extend(["\n## 受保护字幕\n\n"])
        lines.extend(f"- 第 {match.index + 1} 段：{match.reason}\n" for match in result.protected)
    if result.reference_before is not None and result.reference_after is not None:
        lines.extend(
            [
                "\n## 参考侧审计\n\n",
                f"- 硬超速段: {result.reference_before.unresolved_hard_count} -> "
                f"{result.reference_after.unresolved_hard_count}\n",
                f"- HardDeficit: {result.reference_before.hard_deficit:.6f} -> "
                f"{result.reference_after.hard_deficit:.6f}\n",
            ]
        )
    return "".join(lines)
