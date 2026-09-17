"""Shared, module-neutral contracts and storage for resumable work checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

RECOVERY_MANIFEST_SCHEMA = "videocaptioner.recovery_manifest"
RECOVERY_MANIFEST_VERSION = 1


class RecoveryDecision(str, Enum):
    """The choice made when a matching unfinished checkpoint is discovered."""

    CONTINUE = "continue"
    START_FRESH = "start_fresh"


@dataclass(frozen=True)
class RecoverySummary:
    """Safe, module-neutral information presented before resuming work.

    ``identity`` and ``completed`` deliberately contain only durable identifiers and
    counts. Prompt content, model responses, and connection credentials never
    belong in a recovery prompt.
    """

    module: str
    identity: Mapping[str, str]
    completed: Mapping[str, int]
    checkpoint_time: str
    configuration_drift: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecoveryProvenance:
    """Resumed-run provenance recorded into deliverables (ADR-0022).

    Module-neutral on purpose: the translation audit report, the translation
    execution snapshot, and the workspace manifest all persist this same
    shape, and the postprocess module reuses it for its QA report and state
    (ticket 07). Only durable identifiers and hashes belong here — never
    prompt text, API keys, or raw model responses.
    """

    checkpoint_time: str
    completed: Mapping[str, int] = field(default_factory=dict)
    configuration_drift: tuple[str, ...] = ()
    drifted_keys: tuple[str, ...] = ()

    def to_persisted(self) -> dict[str, Any]:
        return {
            "checkpoint_time": self.checkpoint_time,
            "completed": dict(self.completed),
            "configuration_drift": list(self.configuration_drift),
            "drifted_keys": list(self.drifted_keys),
        }

    @classmethod
    def from_persisted(cls, payload: Any) -> "RecoveryProvenance | None":
        """Rebuild persisted provenance; malformed payloads yield ``None``."""

        if not isinstance(payload, dict):
            return None
        checkpoint_time = payload.get("checkpoint_time")
        completed = payload.get("completed")
        drift = payload.get("configuration_drift")
        keys = payload.get("drifted_keys")
        if not isinstance(checkpoint_time, str):
            return None
        if completed is None:
            completed = {}
        if not isinstance(completed, dict) or not all(
            isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool)
            for key, value in completed.items()
        ):
            return None

        def _string_tuple(value: Any) -> tuple[str, ...] | None:
            if value is None:
                return ()
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                return None
            return tuple(value)

        drift_items = _string_tuple(drift)
        key_items = _string_tuple(keys)
        if drift_items is None or key_items is None:
            return None
        return cls(
            checkpoint_time=checkpoint_time,
            completed=MappingProxyType(dict(completed)),
            configuration_drift=drift_items,
            drifted_keys=key_items,
        )


def text_digest(text: str) -> str:
    """Stable digest of prompt-like text; only the digest is ever persisted."""

    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


# 旧 manifest（票 05 之前）没有冻结配置摘要可比对时的唯一漂移项。
UNRECORDED_CONFIG_DIGEST_DRIFT = "检查点未记录配置摘要，无法比对配置漂移"


def render_recovery_provenance_lines(
    recovery: "RecoveryProvenance",
    *,
    completed_labels: Mapping[str, str],
    drift_annotations: Mapping[str, tuple[str, str]],
    line_suffix: str = "\n",
) -> list[str]:
    """恢复来源一节的共享渲染（票 07 审查：两模块同形渲染收编）。

    ``completed_labels`` 是已完成级别 → 显示名；``drift_annotations``
    是漂移比对项 → (显示名, 受影响成果句)——两侧各自的唯一标注表。
    ``line_suffix`` 适配两种拼接风格：翻译侧 ``"\\n".join``（空后缀）、
    后处理 QA 报告 ``"".join``（行带 ``\\n`` 后缀）。
    """

    lines = [
        f"## 恢复来源{line_suffix * 2}",
        f"- 检查点时间：{recovery.checkpoint_time or '未知'}{line_suffix}",
    ]
    completed = [
        f"{completed_labels.get(key, key)} {value}"
        for key, value in recovery.completed.items()
        if value
    ]
    lines.append(f"- 恢复时已复用：{'、'.join(completed) if completed else '无'}{line_suffix}")
    drift = list(recovery.configuration_drift)
    lines.append(f"- 配置漂移：{'；'.join(drift) if drift else '无'}{line_suffix}")
    if recovery.configuration_drift == (UNRECORDED_CONFIG_DIGEST_DRIFT,):
        lines.append(f"- 受影响成果：检查点未记录配置摘要，无法追溯受影响部分{line_suffix}")
    elif not drift:
        lines.append(f"- 受影响成果：无（无配置漂移）{line_suffix}")
    else:
        # 有漂移项就绝不写「无」：逐项给效果句；比对项增删（无旧值可归属）
        # 或未标注的项也给一句归属说明，不与上一行自相矛盾。
        effects = [
            drift_annotations.get(key, (None, None))[1]
            or f"部分成果受「{key}」漂移影响，无法进一步归属"
            for key in recovery.drifted_keys
        ]
        if not effects:
            effects = ["部分成果受配置漂移影响，无法逐项归属（比对项在检查点与当前配置间增删）"]
        lines.append(f"- 受影响成果：{'；'.join(effects)}{line_suffix}")
    lines.append(line_suffix)
    return lines


def _canonical(value: Any) -> Any:
    """Freeze one fingerprint value into a comparable, hash-stable form."""

    if isinstance(value, Mapping):
        return {key: _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _values_equal(left: Any, right: Any) -> bool:
    """Deep equality of fingerprint values, immune to list/tuple and key order."""

    return _canonical(left) == _canonical(right)


def _format_value(value: Any) -> str:
    if isinstance(value, Mapping):
        return json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True)
    if isinstance(value, (list, tuple)):
        return json.dumps(_canonical(value), ensure_ascii=False)
    return str(value)


def _drift_comparisons(
    frozen: Mapping[str, Any], current: Mapping[str, Any]
) -> tuple[tuple[str, str, Any, Any], ...]:
    """One shared walk for both drift views: (key, category, frozen, current).

    Categories: ``changed`` (both sides recorded, values differ),
    ``missing_frozen`` (checkpoint never recorded the item),
    ``missing_current`` (current config no longer provides the item).
    """

    items: list[tuple[str, str, Any, Any]] = []
    for key in sorted(set(frozen) | set(current)):
        if key not in current:
            items.append((key, "missing_current", frozen[key], None))
        elif key not in frozen:
            items.append((key, "missing_frozen", None, current[key]))
        elif not _values_equal(frozen[key], current[key]):
            items.append((key, "changed", frozen[key], current[key]))
    return tuple(items)


def config_drift(
    frozen: Mapping[str, Any] | None,
    current: Mapping[str, Any],
    labels: Mapping[str, str],
) -> tuple[str, ...]:
    """Item-by-item comparison of a frozen config summary against the current one.

    Any drift never invalidates a checkpoint (ADR-0022): the returned items go
    into the recovery summary and the deliverable provenance only. ``labels``
    maps each fingerprint key to its display name; keys without a label are
    reported under the key itself so a new fingerprint item can never drift
    silently. Values are compared deeply (mappings, sequences, scalars) with
    key order and list/tuple distinctions normalized away.
    """

    if frozen is None:
        return (UNRECORDED_CONFIG_DIGEST_DRIFT,)
    items: list[str] = []
    for key, category, frozen_value, current_value in _drift_comparisons(frozen, current):
        label = labels.get(key, key)
        if category == "missing_current":
            items.append(f"{label}：检查点记录了该项，当前配置未提供，无法比对")
        elif category == "missing_frozen":
            items.append(f"{label}：检查点未记录该项（当前 {_format_value(current_value)}）")
        else:
            items.append(
                f"{label}：检查点 {_format_value(frozen_value)}，"
                f"当前 {_format_value(current_value)}"
            )
    return tuple(items)


def drifted_keys(
    frozen: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> tuple[str, ...]:
    """Raw fingerprint keys whose values differ, in ``sorted`` order.

    ``config_drift`` renders the human-readable items; this companion returns
    the bare keys so callers can attach per-item provenance (e.g. which part
    of a deliverable came from an older configuration) without re-parsing the
    rendered strings. Keys the checkpoint never recorded are excluded: they
    have no old value to attribute deliverables to.
    """

    if frozen is None:
        return ()
    return tuple(
        key for key, category, _, _ in _drift_comparisons(frozen, current) if category == "changed"
    )


def resumed_count(summary: "RecoverySummary | None", key: str) -> int:
    """Return one completed-level count, treating no-resume as zero."""

    return 0 if summary is None else int(summary.completed.get(key, 0))


def resumed_flag(summary: "RecoverySummary | None", key: str) -> bool:
    """Return whether one completed level was actually resumed."""

    return resumed_count(summary, key) > 0


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def software_version() -> str:
    """Return the installed application version without making recovery fail."""

    try:
        from videocaptioner._version import version
    except InterruptedError:
        raise
    except Exception:  # noqa: BLE001 — version module is generated and may be absent
        return "unknown"
    return str(version or "unknown")


def atomic_write_json(path: str | Path, payload: Mapping[str, Any]) -> Path:
    """Atomically replace a canonical JSON document via a sibling temp file.

    This is the one atomic-write shape shared by recovery-adjacent persisted
    files (recovery manifest, project glossary, translation brief): fsync the
    temporary file before ``os.replace`` so a reader never sees a half-written
    document, and never trust a temp file that survived an error.
    """

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = (
        json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return destination


def write_recovery_manifest(path: str | Path, manifest: Mapping[str, Any]) -> Path:
    """Atomically replace a manifest after its corresponding data file is durable."""

    return atomic_write_json(path, manifest)


def load_recovery_manifest(path: str | Path) -> dict[str, Any] | None:
    """Return a JSON-object manifest, treating absent or corrupted data as unusable."""

    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except InterruptedError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def matching_recovery_manifest(
    path: str | Path,
    *,
    module: str,
    identity: Mapping[str, str],
) -> dict[str, Any] | None:
    """Load and validate a manifest: schema / version / module / identity must match.

    两个模块（翻译、后处理）的同一套校验（票 06 审查：此前逐行同形
    地散在两侧 runner 里，改 schema 要同改两处）。``completed`` 不是
    dict 时同样视为不可用——恢复只信 manifest，形状不对即未完成。
    """

    manifest = load_recovery_manifest(path)
    if manifest is None:
        return None
    if (
        manifest.get("schema") != RECOVERY_MANIFEST_SCHEMA
        or manifest.get("version") != RECOVERY_MANIFEST_VERSION
        or manifest.get("module") != module
        or manifest.get("identity") != dict(identity)
    ):
        return None
    completed = manifest.get("completed")
    return manifest if isinstance(completed, dict) else None
