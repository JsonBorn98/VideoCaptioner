"""Complete, editable postprocess profiles with immutable factory baselines."""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from videocaptioner.config import APPDATA_PATH

from .config import PostprocessConfig

PROFILE_SCHEMA = "videocaptioner.postprocess-profile-collection"
PROFILE_SCHEMA_VERSION = 1
DEFAULT_POSTPROCESS_PROFILES_PATH = APPDATA_PATH / "postprocess_profiles.json"
TEMPLATE_IDS = ("loose", "balanced", "smooth")

_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_CONFIG_FIELDS = frozenset(item.name for item in fields(PostprocessConfig))


class PostprocessProfileError(ValueError):
    pass


class PostprocessProfileNotFoundError(KeyError):
    pass


class PostprocessProfileConflictError(PostprocessProfileError):
    pass


class FactoryTemplateError(PostprocessProfileError):
    pass


def _factory_config(template_id: str) -> PostprocessConfig:
    if template_id not in TEMPLATE_IDS:
        raise PostprocessProfileError(f"Unknown base template: {template_id}")
    return PostprocessConfig(
        trim_trailing_punct=True,
        remove_placeholders=False,
        normalize_quotes=False,
        fix_gaps=False,
        qa_report=False,
        speed_optimize=True,
        speed_profile=template_id,
        speed_semantic_repair=True,
        precise_timing=False,
        optimize_both_sides=False,
    )


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(item) for item in value]
    return deepcopy(value)


# Recursively frozen snapshots make accidental baseline mutation impossible.
FACTORY_BASELINES: Mapping[str, Mapping[str, Any]] = MappingProxyType(
    {
        template_id: _deep_freeze(asdict(_factory_config(template_id)))
        for template_id in TEMPLATE_IDS
    }
)


def get_factory_baseline(template_id: str) -> PostprocessConfig:
    """Return a fresh config copied from the immutable shipped baseline."""

    try:
        data = FACTORY_BASELINES[template_id]
    except KeyError as exc:
        raise PostprocessProfileError(f"Unknown base template: {template_id}") from exc
    return PostprocessConfig(**_deep_thaw(data))


def _validate_id(profile_id: str, *, allow_template: bool = True) -> str:
    if not isinstance(profile_id, str) or not _ID_RE.fullmatch(profile_id):
        raise PostprocessProfileError("profile id must be 1-64 lowercase ASCII id characters")
    if not allow_template and profile_id in TEMPLATE_IDS:
        raise PostprocessProfileConflictError(f"Profile id is reserved: {profile_id}")
    return profile_id


def _validate_name(name: str) -> str:
    if not isinstance(name, str):
        raise PostprocessProfileError("profile name must be a string")
    value = name.strip()
    if not value or len(value) > 80 or any(ord(char) < 32 for char in value):
        raise PostprocessProfileError("profile name must contain 1-80 printable characters")
    return value


_LEGACY_DROP_FIELDS = frozenset(
    {
        "tail_dwell_short_ms",
        "tail_dwell_long_ms",
        "tail_dwell_long_gap_ms",
        "tail_dwell_scene_cut_ms",
        "tail_dwell_min_blank_ms",
    }
)


def _migrate_legacy_config(data: Mapping[str, Any]) -> dict[str, Any]:
    """把旧版 tail_dwell 分档字段迁移到尾部补偿曲线字段（见 docs/adr/0005）。

    - ``tail_dwell`` → ``tail_compensation``（保留启用状态）
    - ``tail_dwell_long_ms`` → ``max_compensation_ms``
    - ``tail_dwell_long_gap_ms`` → ``max_compensation_gap_ms``
    - short / scene_cut / min_blank 无对应项，丢弃（最小补偿取默认）

    迁移值若与新约束冲突，由 :func:`_config_from_dict` 回退到默认补偿参数。
    """
    result = dict(data)
    if "tail_dwell" in result and "tail_compensation" not in result:
        result["tail_compensation"] = result["tail_dwell"]
    result.pop("tail_dwell", None)
    if "tail_dwell_long_ms" in result and "max_compensation_ms" not in result:
        result["max_compensation_ms"] = result["tail_dwell_long_ms"]
    if "tail_dwell_long_gap_ms" in result and "max_compensation_gap_ms" not in result:
        result["max_compensation_gap_ms"] = result["tail_dwell_long_gap_ms"]
    for key in _LEGACY_DROP_FIELDS:
        result.pop(key, None)
    return result


def _config_from_dict(data: Any) -> PostprocessConfig:
    if not isinstance(data, Mapping):
        raise PostprocessProfileError("profile config must be an object")
    migrated = _migrate_legacy_config(data)
    unknown = sorted(set(migrated) - _CONFIG_FIELDS)
    if unknown:
        raise PostprocessProfileError(f"Unknown config field: {unknown[0]}")
    # 向前兼容：新增后处理选项后，老版本持久化的 profile 会缺少这些字段。
    # 用权威默认值补齐缺失字段，而非报错，避免既有 profile 集合无法加载。
    defaults = asdict(PostprocessConfig())
    merged = {**defaults, **deepcopy(dict(migrated))}
    try:
        return PostprocessConfig(**merged)
    except (TypeError, ValueError):
        # 迁移（或手改）得到的补偿参数可能违反新约束（如旧值与斜率约束冲突）；
        # 回退这三个补偿数值到默认、保留其余（含启用状态），保证加载始终不中断。
        # 非补偿相关的错误在重试时仍会抛出。
        for key in ("min_compensation_ms", "max_compensation_gap_ms", "max_compensation_ms"):
            merged[key] = defaults[key]
        try:
            return PostprocessConfig(**merged)
        except (TypeError, ValueError) as exc:
            raise PostprocessProfileError(str(exc)) from exc


@dataclass(frozen=True)
class PostprocessProfile:
    profile_id: str
    name: str
    base_template_id: str
    config: PostprocessConfig
    is_template: bool = False

    def __post_init__(self) -> None:
        _validate_id(self.profile_id)
        object.__setattr__(self, "name", _validate_name(self.name))
        if self.base_template_id not in TEMPLATE_IDS:
            raise PostprocessProfileError(f"Unknown base template: {self.base_template_id}")
        if self.is_template and self.profile_id != self.base_template_id:
            raise PostprocessProfileError("template profile id must equal base_template_id")
        # Copy mutable list/dict fields so callers cannot mutate persisted state by alias.
        object.__setattr__(self, "config", _config_from_dict(asdict(self.config)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.profile_id,
            "name": self.name,
            "base_template_id": self.base_template_id,
            "is_template": self.is_template,
            "config": asdict(self.config),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "PostprocessProfile":
        if not isinstance(data, Mapping):
            raise PostprocessProfileError("profile must be an object")
        allowed = {"id", "name", "base_template_id", "is_template", "config"}
        if set(data) != allowed:
            raise PostprocessProfileError("profile fields do not match schema")
        if type(data["is_template"]) is not bool:
            raise PostprocessProfileError("is_template must be a boolean")
        return cls(
            profile_id=data["id"],
            name=data["name"],
            base_template_id=data["base_template_id"],
            is_template=data["is_template"],
            config=_config_from_dict(data["config"]),
        )


def _template_profiles() -> dict[str, PostprocessProfile]:
    names = {"loose": "宽松", "balanced": "均衡", "smooth": "平滑优先"}
    return {
        template_id: PostprocessProfile(
            template_id,
            names[template_id],
            template_id,
            get_factory_baseline(template_id),
            True,
        )
        for template_id in TEMPLATE_IDS
    }


def _atomic_write(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


class PostprocessProfileStore:
    """Persist profile edits immediately; template baselines remain code-owned."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else DEFAULT_POSTPROCESS_PROFILES_PATH
        self._profiles = self._load() if self.path.exists() else _template_profiles()

    def reload(self) -> None:
        """Re-read persisted profiles so edits by another store instance apply.

        A long-lived store (held by a GUI page or the workflow) keeps the
        in-memory snapshot taken at construction and would otherwise silently
        ignore profile edits saved by the settings page's separate store.
        """
        self._profiles = self._load() if self.path.exists() else _template_profiles()

    def _load(self) -> dict[str, PostprocessProfile]:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PostprocessProfileError(f"Cannot read profile collection: {self.path}") from exc
        if not isinstance(document, Mapping) or set(document) != {"schema", "version", "profiles"}:
            raise PostprocessProfileError("profile collection fields do not match schema")
        if document["schema"] != PROFILE_SCHEMA or document["version"] != PROFILE_SCHEMA_VERSION:
            raise PostprocessProfileError("Unsupported postprocess profile collection")
        if not isinstance(document["profiles"], list):
            raise PostprocessProfileError("profiles must be an array")
        profiles: dict[str, PostprocessProfile] = {}
        names: set[str] = set()
        for item in document["profiles"]:
            profile = PostprocessProfile.from_dict(item)
            if profile.profile_id in profiles or profile.name.casefold() in names:
                raise PostprocessProfileError("duplicate profile id or name")
            profiles[profile.profile_id] = profile
            names.add(profile.name.casefold())
        for template_id, template in _template_profiles().items():
            profiles.setdefault(template_id, template)
            if not profiles[template_id].is_template:
                raise PostprocessProfileError(f"reserved template is invalid: {template_id}")
        return profiles

    def _save(self) -> None:
        _atomic_write(
            self.path,
            {
                "schema": PROFILE_SCHEMA,
                "version": PROFILE_SCHEMA_VERSION,
                "profiles": [self._profiles[key].to_dict() for key in sorted(self._profiles)],
            },
        )

    def _commit(self, profile: PostprocessProfile) -> PostprocessProfile:
        profile = PostprocessProfile.from_dict(profile.to_dict())
        previous = self._profiles.get(profile.profile_id)
        for item in self._profiles.values():
            if item.profile_id != profile.profile_id and item.name.casefold() == profile.name.casefold():
                raise PostprocessProfileConflictError(f"Profile name already exists: {profile.name}")
        self._profiles[profile.profile_id] = profile
        try:
            self._save()
        except BaseException:
            if previous is None:
                self._profiles.pop(profile.profile_id, None)
            else:
                self._profiles[profile.profile_id] = previous
            raise
        return PostprocessProfile.from_dict(profile.to_dict())

    def list(self) -> tuple[PostprocessProfile, ...]:
        profiles = tuple(self._profiles[key] for key in TEMPLATE_IDS) + tuple(
            sorted(
                (item for item in self._profiles.values() if not item.is_template),
                key=lambda item: (item.name.casefold(), item.profile_id),
            )
        )
        return tuple(PostprocessProfile.from_dict(item.to_dict()) for item in profiles)

    def get(self, profile_id: str) -> PostprocessProfile:
        try:
            profile = self._profiles[profile_id]
        except KeyError as exc:
            raise PostprocessProfileNotFoundError(profile_id) from exc
        return PostprocessProfile.from_dict(profile.to_dict())

    def resolve_config(self, profile_id: str) -> PostprocessConfig:
        return _config_from_dict(asdict(self.get(profile_id).config))

    def copy_template(
        self, template_id: str, name: str, *, profile_id: str | None = None
    ) -> PostprocessProfile:
        """Copy the template's current working values, retaining its factory origin."""

        if template_id not in TEMPLATE_IDS:
            raise FactoryTemplateError(f"Custom profiles must originate from a template: {template_id}")
        selected_id = profile_id or f"custom-{uuid.uuid4().hex}"
        _validate_id(selected_id, allow_template=False)
        if selected_id in self._profiles:
            raise PostprocessProfileConflictError(f"Profile id already exists: {selected_id}")
        template = self.get(template_id)
        return self._commit(
            PostprocessProfile(selected_id, name, template_id, template.config, False)
        )

    def set_config(self, profile_id: str, config: PostprocessConfig) -> PostprocessProfile:
        current = self.get(profile_id)
        return self._commit(replace(current, config=config))

    def set_field(self, profile_id: str, field_name: str, value: Any) -> PostprocessProfile:
        current = self.get(profile_id)
        if field_name not in _CONFIG_FIELDS:
            raise PostprocessProfileError(f"Unknown config field: {field_name}")
        baseline_value = getattr(get_factory_baseline(current.base_template_id), field_name)
        if isinstance(baseline_value, bool) and type(value) is not bool:
            raise PostprocessProfileError(f"{field_name} must be a boolean")
        if isinstance(baseline_value, dict) and not isinstance(value, Mapping):
            raise PostprocessProfileError(f"{field_name} must be an object")
        if isinstance(baseline_value, list) and not isinstance(value, list):
            raise PostprocessProfileError(f"{field_name} must be an array")
        config = replace(current.config, **{field_name: deepcopy(value)})
        return self.set_config(profile_id, config)

    def reset_field(self, profile_id: str, field_name: str) -> PostprocessProfile:
        current = self.get(profile_id)
        if field_name not in _CONFIG_FIELDS:
            raise PostprocessProfileError(f"Unknown config field: {field_name}")
        baseline = get_factory_baseline(current.base_template_id)
        return self.set_field(profile_id, field_name, getattr(baseline, field_name))

    def reset_profile(self, profile_id: str) -> PostprocessProfile:
        current = self.get(profile_id)
        return self.set_config(profile_id, get_factory_baseline(current.base_template_id))

    def rename(self, profile_id: str, name: str) -> PostprocessProfile:
        current = self.get(profile_id)
        if current.is_template:
            raise FactoryTemplateError("factory template names are fixed")
        return self._commit(replace(current, name=name))

    def delete(self, profile_id: str) -> None:
        current = self.get(profile_id)
        if current.is_template:
            raise FactoryTemplateError("factory templates cannot be deleted")
        del self._profiles[profile_id]
        try:
            self._save()
        except BaseException:
            self._profiles[profile_id] = current
            raise


__all__ = [
    "DEFAULT_POSTPROCESS_PROFILES_PATH",
    "FACTORY_BASELINES",
    "PROFILE_SCHEMA",
    "PROFILE_SCHEMA_VERSION",
    "TEMPLATE_IDS",
    "FactoryTemplateError",
    "PostprocessProfile",
    "PostprocessProfileConflictError",
    "PostprocessProfileError",
    "PostprocessProfileNotFoundError",
    "PostprocessProfileStore",
    "get_factory_baseline",
]
