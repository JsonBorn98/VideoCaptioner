"""Serialize speed optimization analysis and change records."""

from __future__ import annotations

import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

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


def result_from_dict(data: dict) -> SpeedOptimizationResult:
    """Rebuild a ``SpeedOptimizationResult`` from ``result_to_dict`` output.

    后处理阶段末恢复检查点（票 06，ADR-0022）用它恢复 ``report.speed``
    对象：QA 报告等价性要求速度结果与不中断运行相同，光存速度变更
    JSON 不够。枚举 / 元组从 ``asdict`` 的裸值重建；未知字段忽略，
    已知字段缺失时按模型默认值兜底。
    """

    from dataclasses import fields

    from .deterministic import TimingChange
    from .metrics import AdjacentJump, SpeedMetrics
    from .models import Lineage
    from .policy import SpeedPolicy, SpeedPreset
    from .protection import ProtectionMatch
    from .semantic import SemanticRepairRecord
    from .structural import StructuralOperationKind, StructuralOperationRecord
    from .validation import ValidationReason, ValidationReasonCode, ValidationStatus

    def _require(key: str) -> Any:
        value = data.get(key)
        if value is None:
            raise ValueError(f"speed result payload missing {key!r}")
        return value

    def _metrics(payload: Mapping[str, Any]) -> SpeedMetrics:
        return SpeedMetrics(
            hard_deficit=float(payload["hard_deficit"]),
            unresolved_hard_count=int(payload["unresolved_hard_count"]),
            speed_spread=(
                float(payload["speed_spread"]) if payload.get("speed_spread") is not None else None
            ),
            adjacent_jump=AdjacentJump(
                p90=(
                    float(payload["adjacent_jump"]["p90"])
                    if payload["adjacent_jump"].get("p90") is not None
                    else None
                ),
                emergency_count=int(payload["adjacent_jump"]["emergency_count"]),
                compared_count=int(payload["adjacent_jump"]["compared_count"]),
            ),
            invalid_count=int(payload["invalid_count"]),
        )

    policy_data = dict(_require("policy"))
    policy_data["preset"] = SpeedPreset(policy_data.get("preset", "balanced"))
    policy = SpeedPolicy(
        **{f.name: policy_data[f.name] for f in fields(SpeedPolicy) if f.name in policy_data}
    )

    def _structural(payload: Mapping[str, Any]) -> StructuralOperationRecord:
        text_side = payload["text_side"]
        if text_side not in ("original", "translate", "both"):
            raise ValueError(f"unknown text_side: {text_side!r}")
        return StructuralOperationRecord(
            operation_id=str(payload["operation_id"]),
            kind=StructuralOperationKind(payload["kind"]),
            before_cue_ids=tuple(str(value) for value in payload["before_cue_ids"]),
            after_cue_ids=tuple(str(value) for value in payload["after_cue_ids"]),
            text_side=text_side,  # type: ignore[arg-type]  # 上面已收窄
            before_lineage=tuple(
                Lineage.from_dict(item) if item else None for item in payload["before_lineage"]
            ),
            after_lineage=tuple(
                Lineage.from_dict(item) if item else None for item in payload["after_lineage"]
            ),
        )

    def _semantic(payload: Mapping[str, Any]) -> SemanticRepairRecord:
        return SemanticRepairRecord(
            window_id=str(payload["window_id"]),
            cue_ids=tuple(str(value) for value in payload["cue_ids"]),
            target_cue_ids=tuple(str(value) for value in payload["target_cue_ids"]),
            cache_key=str(payload["cache_key"]),
            status=ValidationStatus(payload["status"]),
            status_history=tuple(
                ValidationStatus(value) for value in payload.get("status_history", ())
            ),
            attempts=int(payload["attempts"]),
            before=tuple(str(value) for value in payload["before"]),
            after=tuple(str(value) for value in payload["after"]),
            reasons=tuple(
                ValidationReason(
                    code=ValidationReasonCode(item["code"]),
                    message=str(item["message"]),
                    details=tuple(str(detail) for detail in item.get("details", ())),
                )
                for item in payload.get("reasons", ())
            ),
            feedback=tuple(str(value) for value in payload.get("feedback", ())),
            from_cache=bool(payload.get("from_cache", False)),
        )

    mode = data.get("mode", "apply")
    if mode not in ("apply", "analyze"):
        raise ValueError(f"unknown speed mode: {mode!r}")
    return SpeedOptimizationResult(
        policy=policy,
        profile_id=str(data.get("profile_id", "")),
        mode=mode,  # type: ignore[arg-type]  # 上面已收窄到 SpeedMode
        before=_metrics(_require("before")),
        after=_metrics(_require("after")),
        changes=tuple(
            TimingChange(
                cue_id=str(change["cue_id"]),
                boundary=str(change["boundary"]),
                before_ms=int(change["before_ms"]),
                after_ms=int(change["after_ms"]),
                reason=str(change["reason"]),
            )
            for change in _require("changes")
        ),
        unresolved_cue_ids=tuple(str(value) for value in data.get("unresolved_cue_ids", ())),
        invalid_cue_ids=tuple(str(value) for value in data.get("invalid_cue_ids", ())),
        protected=tuple(
            ProtectionMatch(
                index=int(match["index"]),
                reason=str(match["reason"]),
                confidence=str(match.get("confidence", "high")),
            )
            for match in _require("protected")
        ),
        reference_before=_metrics(_require("reference_before"))
        if data.get("reference_before")
        else None,
        reference_after=_metrics(_require("reference_after"))
        if data.get("reference_after")
        else None,
        structural_operations=tuple(
            _structural(operation) for operation in data.get("structural_operations", ())
        ),
        semantic_records=tuple(_semantic(record) for record in data.get("semantic_records", ())),
    )


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
