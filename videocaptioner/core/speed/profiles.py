"""Versioned persistence and management for custom speed profiles."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, fields
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from videocaptioner.config import APPDATA_PATH

from .policy import SpeedPolicy, SpeedPreset, available_speed_presets, get_speed_policy

PROFILE_SCHEMA = "videocaptioner.speed-profile"
PROFILE_COLLECTION_SCHEMA = "videocaptioner.speed-profile-collection"
PROFILE_SCHEMA_VERSION = 1
DEFAULT_SPEED_PROFILES_PATH = APPDATA_PATH / "speed_profiles.json"

_PROFILE_KEYS = frozenset({"id", "name", "base_preset", "overrides"})
_DOCUMENT_KEYS = frozenset({"schema", "version", "profile"})
_COLLECTION_KEYS = frozenset({"schema", "version", "profiles"})
_PROFILE_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")


class ProfileValidationError(ValueError):
    """Raised when profile data does not satisfy the persisted contract."""


class ProfileNotFoundError(KeyError):
    """Raised when a custom profile identifier does not exist."""


class ProfileConflictError(ValueError):
    """Raised when a profile identifier or name is already in use."""


class BuiltinProfileError(ValueError):
    """Raised when a caller attempts to mutate a read-only built-in preset."""


@dataclass(frozen=True)
class _FieldSpec:
    value_type: type
    minimum: float | int | None = None
    maximum: float | int | None = None


_FIELD_SPECS: Mapping[str, _FieldSpec] = MappingProxyType(
    {
        "comfort_cps_cjk": _FieldSpec(float, 1.0, 60.0),
        "hard_cps_cjk": _FieldSpec(float, 1.0, 60.0),
        "comfort_cps_latin": _FieldSpec(float, 1.0, 100.0),
        "hard_cps_latin": _FieldSpec(float, 1.0, 100.0),
        "adjacent_p90_target": _FieldSpec(float, 1.0, 10.0),
        "adjacent_emergency_limit": _FieldSpec(float, 1.0, 10.0),
        "min_duration_seconds": _FieldSpec(float, 0.1, 30.0),
        "max_duration_seconds": _FieldSpec(float, 0.1, 60.0),
        "technical_min_duration_seconds": _FieldSpec(float, 0.1, 10.0),
        "bidirectional_smoothing": _FieldSpec(bool),
        "effective_jump_load": _FieldSpec(float, 0.0, 10.0),
        "whitespace_weight": _FieldSpec(float, 0.0, 2.0),
        "weak_punctuation_weight": _FieldSpec(float, 0.0, 2.0),
        "strong_punctuation_weight": _FieldSpec(float, 0.0, 2.0),
        "local_window_radius": _FieldSpec(int, 1, 50),
        "speech_density_adjustment_limit": _FieldSpec(float, 0.0, 1.0),
        "rhythm_reset_ms": _FieldSpec(int, 100, 60_000),
        "hard_rhythm_reset_ms": _FieldSpec(int, 100, 60_000),
        "low_confidence_boundary_shift_ms": _FieldSpec(int, 0, 10_000),
        "medium_confidence_boundary_shift_ms": _FieldSpec(int, 0, 10_000),
        "high_confidence_boundary_shift_ms": _FieldSpec(int, 0, 10_000),
    }
)

_POLICY_FIELDS = frozenset(field.name for field in fields(SpeedPolicy))
if frozenset(_FIELD_SPECS) != _POLICY_FIELDS - {"schema_version", "preset"}:
    raise RuntimeError("Custom profile field contract is out of sync with SpeedPolicy")


def _reject_unknown(data: Mapping[str, Any], allowed: frozenset[str], context: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ProfileValidationError(f"Unknown {context} field(s): {', '.join(unknown)}")


def _validate_profile_id(profile_id: Any) -> str:
    if not isinstance(profile_id, str) or not _PROFILE_ID_PATTERN.fullmatch(profile_id):
        raise ProfileValidationError(
            "profile id must be 1-64 lowercase ASCII letters, digits, dots, dashes, or underscores"
        )
    if profile_id in available_speed_presets():
        raise ProfileValidationError("custom profile id conflicts with a built-in preset")
    return profile_id


def _validate_name(name: Any) -> str:
    if not isinstance(name, str):
        raise ProfileValidationError("profile name must be a string")
    normalized = name.strip()
    if not normalized or len(normalized) > 80 or any(ord(char) < 32 for char in normalized):
        raise ProfileValidationError("profile name must contain 1-80 printable characters")
    return normalized


def _validate_override(field_name: str, value: Any) -> bool | float | int:
    spec = _FIELD_SPECS.get(field_name)
    if spec is None:
        raise ProfileValidationError(f"Unknown policy field: {field_name}")
    if spec.value_type is bool:
        if type(value) is not bool:
            raise ProfileValidationError(f"{field_name} must be a boolean")
        return value
    if spec.value_type is int:
        if type(value) is not int:
            raise ProfileValidationError(f"{field_name} must be an integer")
        normalized: int | float = value
    else:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ProfileValidationError(f"{field_name} must be a number")
        normalized = float(value)
        if not math.isfinite(normalized):
            raise ProfileValidationError(f"{field_name} must be finite")
    if spec.minimum is not None and normalized < spec.minimum:
        raise ProfileValidationError(f"{field_name} must be at least {spec.minimum}")
    if spec.maximum is not None and normalized > spec.maximum:
        raise ProfileValidationError(f"{field_name} must be at most {spec.maximum}")
    return normalized


def _effective_policy(base_preset: SpeedPreset, overrides: Mapping[str, Any]) -> SpeedPolicy:
    validated = {name: _validate_override(name, value) for name, value in overrides.items()}
    if validated.get(
        "adjacent_p90_target", get_speed_policy(base_preset).adjacent_p90_target
    ) > validated.get(
        "adjacent_emergency_limit", get_speed_policy(base_preset).adjacent_emergency_limit
    ):
        raise ProfileValidationError("adjacent P90 target cannot exceed the emergency limit")
    try:
        return get_speed_policy(base_preset).with_overrides(**validated)
    except (TypeError, ValueError) as exc:
        raise ProfileValidationError(str(exc)) from exc


@dataclass(frozen=True)
class SpeedProfile:
    """A named custom policy represented as overrides on a built-in preset."""

    profile_id: str
    name: str
    base_preset: SpeedPreset
    overrides: Mapping[str, bool | float | int]

    def __post_init__(self) -> None:
        profile_id = _validate_profile_id(self.profile_id)
        name = _validate_name(self.name)
        try:
            base_preset = SpeedPreset(self.base_preset)
        except ValueError as exc:
            raise ProfileValidationError(f"Unknown base preset: {self.base_preset}") from exc
        if not isinstance(self.overrides, Mapping):
            raise ProfileValidationError("profile overrides must be an object")
        normalized = {key: _validate_override(key, value) for key, value in self.overrides.items()}
        _effective_policy(base_preset, normalized)
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "base_preset", base_preset)
        object.__setattr__(self, "overrides", MappingProxyType(normalized))

    @property
    def policy(self) -> SpeedPolicy:
        """Resolve the immutable effective policy snapshot."""

        return _effective_policy(self.base_preset, self.overrides)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.profile_id,
            "name": self.name,
            "base_preset": self.base_preset.value,
            "overrides": dict(sorted(self.overrides.items())),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SpeedProfile:
        if not isinstance(data, Mapping):
            raise ProfileValidationError("profile must be an object")
        _reject_unknown(data, _PROFILE_KEYS, "profile")
        missing = sorted(_PROFILE_KEYS - set(data))
        if missing:
            raise ProfileValidationError(f"Missing profile field(s): {', '.join(missing)}")
        try:
            base_preset = SpeedPreset(data["base_preset"])
        except (TypeError, ValueError) as exc:
            raise ProfileValidationError(f"Unknown base preset: {data['base_preset']}") from exc
        return cls(
            profile_id=data["id"],
            name=data["name"],
            base_preset=base_preset,
            overrides=data["overrides"],
        )


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProfileValidationError(f"Cannot read profile JSON: {path}") from exc


def _atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _parse_profile_document(data: Any) -> SpeedProfile:
    if not isinstance(data, Mapping):
        raise ProfileValidationError("profile document must be an object")
    _reject_unknown(data, _DOCUMENT_KEYS, "document")
    if set(data) != _DOCUMENT_KEYS:
        missing = sorted(_DOCUMENT_KEYS - set(data))
        raise ProfileValidationError(f"Missing document field(s): {', '.join(missing)}")
    if data["schema"] != PROFILE_SCHEMA:
        raise ProfileValidationError("Unsupported profile schema")
    if type(data["version"]) is not int or data["version"] != PROFILE_SCHEMA_VERSION:
        raise ProfileValidationError(f"Unsupported profile version: {data['version']}")
    return SpeedProfile.from_dict(data["profile"])


class SpeedProfileStore:
    """Manage custom profiles in one atomically persisted collection."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else DEFAULT_SPEED_PROFILES_PATH
        self._profiles = self._read_collection() if self.path.exists() else {}

    def _read_collection(self) -> dict[str, SpeedProfile]:
        data = _load_json(self.path)
        if not isinstance(data, Mapping):
            raise ProfileValidationError("profile collection must be an object")
        _reject_unknown(data, _COLLECTION_KEYS, "collection")
        if set(data) != _COLLECTION_KEYS:
            missing = sorted(_COLLECTION_KEYS - set(data))
            raise ProfileValidationError(f"Missing collection field(s): {', '.join(missing)}")
        if data["schema"] != PROFILE_COLLECTION_SCHEMA:
            raise ProfileValidationError("Unsupported profile collection schema")
        if type(data["version"]) is not int or data["version"] != PROFILE_SCHEMA_VERSION:
            raise ProfileValidationError(
                f"Unsupported profile collection version: {data['version']}"
            )
        if not isinstance(data["profiles"], list):
            raise ProfileValidationError("collection profiles must be an array")
        profiles: dict[str, SpeedProfile] = {}
        names: set[str] = set()
        for item in data["profiles"]:
            profile = SpeedProfile.from_dict(item)
            if profile.profile_id in profiles:
                raise ProfileValidationError(f"Duplicate profile id: {profile.profile_id}")
            normalized_name = profile.name.casefold()
            if normalized_name in names:
                raise ProfileValidationError(f"Duplicate profile name: {profile.name}")
            profiles[profile.profile_id] = profile
            names.add(normalized_name)
        return profiles

    def _save(self) -> None:
        _atomic_write_json(
            self.path,
            {
                "schema": PROFILE_COLLECTION_SCHEMA,
                "version": PROFILE_SCHEMA_VERSION,
                "profiles": [self._profiles[key].to_dict() for key in sorted(self._profiles)],
            },
        )

    def list_custom(self) -> tuple[SpeedProfile, ...]:
        return tuple(
            sorted(
                self._profiles.values(), key=lambda item: (item.name.casefold(), item.profile_id)
            )
        )

    def get_custom(self, profile_id: str) -> SpeedProfile:
        try:
            return self._profiles[profile_id]
        except KeyError as exc:
            raise ProfileNotFoundError(profile_id) from exc

    def resolve_policy(self, profile_id: str) -> SpeedPolicy:
        if profile_id in available_speed_presets():
            return get_speed_policy(profile_id)
        return self.get_custom(profile_id).policy

    def copy_builtin(
        self,
        preset: SpeedPreset | str,
        name: str,
        *,
        profile_id: str | None = None,
    ) -> SpeedProfile:
        try:
            base_preset = SpeedPreset(preset)
        except ValueError as exc:
            raise ProfileValidationError(f"Unknown base preset: {preset}") from exc
        return self.create(name, base_preset, profile_id=profile_id)

    def create(
        self,
        name: str,
        base_preset: SpeedPreset | str = SpeedPreset.BALANCED,
        *,
        overrides: Mapping[str, Any] | None = None,
        profile_id: str | None = None,
    ) -> SpeedProfile:
        """Create a named custom profile from one immutable policy snapshot."""

        try:
            preset = SpeedPreset(base_preset)
        except ValueError as exc:
            raise ProfileValidationError(f"Unknown base preset: {base_preset}") from exc
        profile = SpeedProfile(
            profile_id=profile_id or f"custom-{uuid.uuid4().hex}",
            name=name,
            base_preset=preset,
            overrides=overrides or {},
        )
        self._insert(profile)
        return profile

    def _insert(self, profile: SpeedProfile, *, replace_existing: bool = False) -> None:
        existing = self._profiles.get(profile.profile_id)
        if existing is not None and not replace_existing:
            raise ProfileConflictError(f"Profile id already exists: {profile.profile_id}")
        for candidate in self._profiles.values():
            if (
                candidate.profile_id != profile.profile_id
                and candidate.name.casefold() == profile.name.casefold()
            ):
                raise ProfileConflictError(f"Profile name already exists: {profile.name}")
        self._profiles[profile.profile_id] = profile
        try:
            self._save()
        except BaseException:
            if existing is None:
                del self._profiles[profile.profile_id]
            else:
                self._profiles[profile.profile_id] = existing
            raise

    def rename(self, profile_id: str, name: str) -> SpeedProfile:
        self._require_mutable(profile_id)
        current = self.get_custom(profile_id)
        updated = SpeedProfile(profile_id, name, current.base_preset, current.overrides)
        self._insert(updated, replace_existing=True)
        return updated

    def delete(self, profile_id: str) -> None:
        self._require_mutable(profile_id)
        current = self.get_custom(profile_id)
        del self._profiles[profile_id]
        try:
            self._save()
        except BaseException:
            self._profiles[profile_id] = current
            raise

    def set_field(self, profile_id: str, field_name: str, value: Any) -> SpeedProfile:
        return self.set_fields(profile_id, {field_name: value})

    def set_fields(self, profile_id: str, values: Mapping[str, Any]) -> SpeedProfile:
        """Set several overrides in one validated, atomic collection write."""

        self._require_mutable(profile_id)
        current = self.get_custom(profile_id)
        overrides = dict(current.overrides)
        overrides.update(
            {
                field_name: _validate_override(field_name, value)
                for field_name, value in values.items()
            }
        )
        updated = SpeedProfile(profile_id, current.name, current.base_preset, overrides)
        self._insert(updated, replace_existing=True)
        return updated

    def reset_field(self, profile_id: str, field_name: str) -> SpeedProfile:
        return self.reset_fields(profile_id, (field_name,))

    def reset_fields(self, profile_id: str, field_names: tuple[str, ...]) -> SpeedProfile:
        """Remove several overrides in one validated, atomic collection write."""

        self._require_mutable(profile_id)
        unknown = sorted(set(field_names) - set(_FIELD_SPECS))
        if unknown:
            raise ProfileValidationError(f"Unknown policy field: {unknown[0]}")
        current = self.get_custom(profile_id)
        overrides = dict(current.overrides)
        for field_name in field_names:
            overrides.pop(field_name, None)
        updated = SpeedProfile(profile_id, current.name, current.base_preset, overrides)
        self._insert(updated, replace_existing=True)
        return updated

    def export_profile(self, profile_id: str, path: str | Path) -> None:
        profile = self.get_custom(profile_id)
        _atomic_write_json(
            Path(path),
            {
                "schema": PROFILE_SCHEMA,
                "version": PROFILE_SCHEMA_VERSION,
                "profile": profile.to_dict(),
            },
        )

    def import_profile(self, path: str | Path, *, replace_existing: bool = False) -> SpeedProfile:
        profile = _parse_profile_document(_load_json(Path(path)))
        self._insert(profile, replace_existing=replace_existing)
        return profile

    @staticmethod
    def _require_mutable(profile_id: str) -> None:
        if profile_id in available_speed_presets():
            raise BuiltinProfileError(f"Built-in speed profile is read-only: {profile_id}")


def resolve_speed_policy(
    profile_id: SpeedPreset | str = SpeedPreset.BALANCED,
    task_overrides: Mapping[str, Any] | None = None,
    *,
    store: SpeedProfileStore | None = None,
    profile_file: str | Path | None = None,
) -> SpeedPolicy:
    """Resolve a built-in or custom profile, then apply task-local overrides.

    The returned immutable snapshot is suitable for attaching to a running
    task. Task overrides are validated by the same contract as persisted
    custom-profile fields, but are never written back to the profile store.
    """

    profile_key = profile_id.value if isinstance(profile_id, SpeedPreset) else profile_id
    if profile_file is not None:
        policy = load_speed_profile(profile_file).policy
    elif profile_key in available_speed_presets():
        policy = get_speed_policy(profile_key)
    else:
        policy = (store or SpeedProfileStore()).get_custom(profile_key).policy
    if not task_overrides:
        return policy
    validated = {
        field_name: _validate_override(field_name, value)
        for field_name, value in task_overrides.items()
    }
    try:
        return policy.with_overrides(**validated)
    except (TypeError, ValueError) as exc:
        raise ProfileValidationError(str(exc)) from exc


def load_speed_profile(path: str | Path) -> SpeedProfile:
    """Load and validate one exported profile without mutating the app store."""

    return _parse_profile_document(_load_json(Path(path)))


__all__ = [
    "BuiltinProfileError",
    "DEFAULT_SPEED_PROFILES_PATH",
    "PROFILE_COLLECTION_SCHEMA",
    "PROFILE_SCHEMA",
    "PROFILE_SCHEMA_VERSION",
    "ProfileConflictError",
    "ProfileNotFoundError",
    "ProfileValidationError",
    "SpeedProfile",
    "SpeedProfileStore",
    "load_speed_profile",
    "resolve_speed_policy",
]
