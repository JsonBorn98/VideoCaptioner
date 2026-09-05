"""过程资产目录、身份 manifest 与独立任务的资产发现。

过程资产目录固定为输入资产旁的 ASCII ``videocaptioner-workspace``（D21/D23）。
任务过程目录由规范化任务名、输入指纹和语言对组成；Windows 路径不能包含冒号，
因此目录名使用指纹的 hex 段，完整 ``sha256:`` 值只写在 manifest 里。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from ..utils.logger import setup_logger

if TYPE_CHECKING:
    from ..asr.asr_data import ASRData
    from .models import PostprocessTask

logger = setup_logger("postprocess.workspace")

WORKSPACE_DIRNAME = "videocaptioner-workspace"
MANIFEST_FILENAME = "manifest.json"
MANIFEST_SCHEMA = "videocaptioner.workspace_manifest"
MANIFEST_VERSION = 1

UPSTREAM_ASSET_KINDS = (
    "glossary",
    "audit",
    "checkpoint",
    "translation_snapshot",
    "context",
)
ASSET_FILENAMES = {
    "glossary": "glossary.vcglossary.json",
    "audit": "translation-audit.md",
    "checkpoint": "translation-checkpoint.json",
    "translation_snapshot": "translation-snapshot.json",
    "context": "context.json",
    "qa_report": "qa-report.md",
    "postprocess_state": "postprocess-state.json",
}
_JSON_ASSET_KINDS = frozenset(
    {
        "glossary",
        "checkpoint",
        "translation_snapshot",
        "context",
        "postprocess_state",
    }
)
_STAGE_PREFIXES = (
    "【转录字幕】",
    "【初版字幕】",
    "【后处理字幕】",
    "【字幕】",
    "【样式字幕】",
)


@dataclass(frozen=True)
class ProcessAssetDiscovery:
    """Verified process-asset lookup for one frozen postprocess task."""

    workspace_root: Path
    task_dir: Path
    manifest_path: Path
    verified_assets: tuple[tuple[str, Path], ...] = ()
    missing: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()

    def asset_path(self, kind: str) -> Path | None:
        for name, path in self.verified_assets:
            if name == kind:
                return path
        return None


def fingerprint_subtitle(data: "ASRData") -> str:
    """Stable identity of the 初版快照, including timing and both sides."""

    payload = [
        {
            "end": segment.end_time,
            "index": index,
            "start": segment.start_time,
            "text": _normalize_text(segment.text),
            "translated": _normalize_text(segment.translated_text),
        }
        for index, segment in enumerate(data.segments)
    ]
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def normalize_task_name(name: str) -> str:
    """Turn a display task name into a stable ASCII directory component."""

    original = unicodedata.normalize("NFKC", name).strip()
    slug = re.sub(r"[^a-z0-9]+", "-", original.casefold()).strip("-")
    dropped = bool(re.search(r"[^a-zA-Z0-9._\s-]", original))
    if not slug:
        slug = "task"
    if len(slug) > 40:
        slug = slug[:40].rstrip("-")
        dropped = True
    if dropped:
        digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:8]
        slug = f"{slug}-{digest}"
    return slug


def normalize_language(value: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKC", value).strip().casefold())
    token = token.strip("-")
    return token or "und"


def resolve_workspace_root(task: "PostprocessTask") -> Path:
    """过程资产目录 sits beside the input video when present, else the subtitle."""

    anchor = Path(task.media_path or task.source_subtitle_path).expanduser()
    if not anchor.is_absolute():
        anchor = Path.cwd() / anchor
    return anchor.parent / WORKSPACE_DIRNAME


class FilesystemAssetStore:
    """Default process-asset adapter: create, validate, and reuse workspace files."""

    def discover(self, task: "PostprocessTask") -> None:
        workspace_root = resolve_workspace_root(task)
        workspace_root.mkdir(parents=True, exist_ok=True)

        name = normalize_task_name(_task_name_source(task))
        fingerprint = task.subtitle_fingerprint
        source_language = normalize_language(task.source_language)
        target_language = normalize_language(task.target_language)
        fingerprint_dir = fingerprint.removeprefix("sha256:") or "unknown"
        language_dir = f"{source_language}_{target_language}"
        task_dir = workspace_root / name / fingerprint_dir / language_dir
        task_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = task_dir / MANIFEST_FILENAME

        verified: dict[str, Path] = {}
        rejected: list[str] = []
        existing = _load_manifest(manifest_path)
        identity_ok = existing is not None and _identity_matches(
            existing, fingerprint, source_language, target_language
        )
        if existing is not None and not identity_ok:
            rejected.append("manifest")
            existing = None

        if identity_ok and existing is not None:
            raw_assets = existing.get("assets")
            listed = raw_assets if isinstance(raw_assets, dict) else {}
            for kind, filename in listed.items():
                if not isinstance(kind, str) or kind not in ASSET_FILENAMES:
                    continue
                path = _resolve_listed_asset(task_dir, filename)
                if path is not None and _asset_readable(path, kind):
                    verified[kind] = path
                else:
                    rejected.append(kind)

        for kind, raw_path in dict(task.explicit_assets).items():
            if kind not in UPSTREAM_ASSET_KINDS:
                rejected.append(kind)
                continue
            source = Path(raw_path)
            if not _asset_readable(source, kind):
                rejected.append(kind)
                continue
            destination = task_dir / ASSET_FILENAMES[kind]
            _copy_asset(source, destination)
            if _asset_readable(destination, kind):
                verified[kind] = destination
            else:
                rejected.append(kind)

        missing = tuple(kind for kind in UPSTREAM_ASSET_KINDS if kind not in verified)
        rejected_unique = tuple(dict.fromkeys(rejected))
        manifest = _build_manifest(
            task,
            workspace_root=workspace_root,
            task_name=name,
            fingerprint=fingerprint,
            source_language=source_language,
            target_language=target_language,
            verified=verified,
            previous=existing if identity_ok else None,
        )
        _write_json(manifest_path, manifest)
        task.asset_discovery = ProcessAssetDiscovery(
            workspace_root=workspace_root,
            task_dir=task_dir,
            manifest_path=manifest_path,
            verified_assets=tuple(verified.items()),
            missing=missing,
            rejected=rejected_unique,
        )
        if rejected_unique:
            task.warnings.append("过程资产不可用: " + ", ".join(rejected_unique))
        if missing:
            task.warnings.append("过程资产缺失: " + ", ".join(missing))
        logger.info(
            "过程资产发现：已验证 %d，缺失 %d，目录 %s",
            len(verified),
            len(missing),
            task_dir.name,
        )


def _normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    return " ".join(normalized.split())


def _task_name_source(task: "PostprocessTask") -> str:
    name = task.workflow_base_name.strip()
    if name:
        return name
    stem = Path(task.initial_subtitle_path or task.source_subtitle_path).stem
    for prefix in _STAGE_PREFIXES:
        stem = stem.removeprefix(prefix)
    return stem.strip() or "task"


def _software_version() -> str:
    try:
        from videocaptioner._version import version
    except Exception:  # noqa: BLE001 — version module is generated and may be absent
        return "unknown"
    return str(version or "unknown")


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _relative_identity(path: Path | None, workspace_root: Path) -> str | None:
    if path is None:
        return None
    resolved = path.expanduser()
    if not resolved.is_absolute():
        resolved = Path.cwd() / resolved
    try:
        return resolved.resolve().relative_to(workspace_root.parent.resolve()).as_posix()
    except ValueError:
        return path.name


def _load_manifest(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _identity_matches(
    manifest: Mapping[str, Any],
    fingerprint: str,
    source_language: str,
    target_language: str,
) -> bool:
    return (
        manifest.get("subtitle_fingerprint") == fingerprint
        and normalize_language(str(manifest.get("source_language", ""))) == source_language
        and normalize_language(str(manifest.get("target_language", ""))) == target_language
    )


def _resolve_listed_asset(task_dir: Path, filename: object) -> Path | None:
    if not isinstance(filename, str) or not filename or filename != Path(filename).name:
        return None
    if filename in {".", ".."} or not filename.isascii():
        return None
    return task_dir / filename


def _asset_readable(path: Path, kind: str) -> bool:
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    if not text.strip():
        return False
    if kind in _JSON_ASSET_KINDS:
        try:
            json.loads(text)
        except json.JSONDecodeError:
            return False
    return True


def _copy_asset(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() == destination.resolve():
        return
    shutil.copy2(source, destination)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _build_manifest(
    task: "PostprocessTask",
    *,
    workspace_root: Path,
    task_name: str,
    fingerprint: str,
    source_language: str,
    target_language: str,
    verified: Mapping[str, Path],
    previous: Mapping[str, Any] | None,
) -> dict[str, Any]:
    generated_at = _now_utc()
    software_version = _software_version()
    translation_method = task.translation_method.strip()
    if previous is not None:
        previous_generated = previous.get("generated_at")
        if isinstance(previous_generated, str) and previous_generated.strip():
            generated_at = previous_generated
        previous_version = previous.get("software_version")
        if isinstance(previous_version, str) and previous_version.strip():
            software_version = previous_version
        if not translation_method:
            previous_method = previous.get("translation_method")
            if isinstance(previous_method, str):
                translation_method = previous_method
    subtitle_path = Path(task.initial_subtitle_path or task.source_subtitle_path)
    media_path = Path(task.media_path) if task.media_path else None
    return {
        "schema": MANIFEST_SCHEMA,
        "version": MANIFEST_VERSION,
        "task_name": task_name,
        "input_subtitle": _relative_identity(subtitle_path, workspace_root),
        "input_media": _relative_identity(media_path, workspace_root),
        "subtitle_fingerprint": fingerprint,
        "source_language": source_language,
        "target_language": target_language,
        "translation_method": translation_method,
        "generated_at": generated_at,
        "software_version": software_version,
        "assets": {kind: path.name for kind, path in verified.items()},
    }
