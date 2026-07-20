"""Versioned persistence for named LLM model profiles."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Mapping

from videocaptioner.config import APPDATA_PATH

from .models import LLMModelProfile

PROFILE_SCHEMA = "videocaptioner.llm-model-profile-collection"
PROFILE_SCHEMA_VERSION = 1
DEFAULT_LLM_PROFILES_PATH = APPDATA_PATH / "llm_model_profiles.json"


class LLMProfileError(ValueError):
    pass


class LLMProfileNotFoundError(KeyError):
    pass


class LLMProfileConflictError(LLMProfileError):
    pass


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


class LLMModelProfileStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_LLM_PROFILES_PATH
        self._profiles = self._load() if self.path.exists() else {}

    def _load(self) -> dict[str, LLMModelProfile]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LLMProfileError(f"Cannot read model profiles: {self.path}") from exc
        if not isinstance(value, Mapping) or set(value) != {"schema", "version", "profiles"}:
            raise LLMProfileError("model profile collection fields do not match schema")
        if value["schema"] != PROFILE_SCHEMA or value["version"] != PROFILE_SCHEMA_VERSION:
            raise LLMProfileError("unsupported model profile collection")
        if not isinstance(value["profiles"], list):
            raise LLMProfileError("profiles must be an array")
        profiles: dict[str, LLMModelProfile] = {}
        names: set[str] = set()
        try:
            for item in value["profiles"]:
                if not isinstance(item, Mapping):
                    raise ValueError("profile must be an object")
                profile = LLMModelProfile.from_dict(item)
                if profile.profile_id in profiles or profile.name.casefold() in names:
                    raise LLMProfileConflictError("duplicate model profile id or name")
                profiles[profile.profile_id] = profile
                names.add(profile.name.casefold())
        except (TypeError, ValueError) as exc:
            if isinstance(exc, LLMProfileError):
                raise
            raise LLMProfileError(str(exc)) from exc
        return profiles

    def reload(self) -> None:
        self._profiles = self._load() if self.path.exists() else {}

    def _save(self) -> None:
        _atomic_write(
            self.path,
            {
                "schema": PROFILE_SCHEMA,
                "version": PROFILE_SCHEMA_VERSION,
                "profiles": [
                    self._profiles[key].to_dict() for key in sorted(self._profiles)
                ],
            },
        )

    def list(self) -> tuple[LLMModelProfile, ...]:
        return tuple(
            LLMModelProfile.from_dict(profile.to_dict())
            for profile in sorted(
                self._profiles.values(), key=lambda item: item.name.casefold()
            )
        )

    def get(self, profile_id: str) -> LLMModelProfile:
        try:
            profile = self._profiles[profile_id]
        except KeyError as exc:
            raise LLMProfileNotFoundError(profile_id) from exc
        return LLMModelProfile.from_dict(profile.to_dict())

    def save(self, profile: LLMModelProfile) -> LLMModelProfile:
        candidate = LLMModelProfile.from_dict(profile.to_dict())
        for existing in self._profiles.values():
            if (
                existing.profile_id != candidate.profile_id
                and existing.name.casefold() == candidate.name.casefold()
            ):
                raise LLMProfileConflictError(
                    f"Model profile name already exists: {candidate.name}"
                )
        previous = self._profiles.get(candidate.profile_id)
        self._profiles[candidate.profile_id] = candidate
        try:
            self._save()
        except BaseException:
            if previous is None:
                self._profiles.pop(candidate.profile_id, None)
            else:
                self._profiles[candidate.profile_id] = previous
            raise
        return self.get(candidate.profile_id)

    def create(self, **values: Any) -> LLMModelProfile:
        profile_id = str(values.pop("profile_id", "") or f"model-{uuid.uuid4().hex}")
        return self.save(LLMModelProfile(profile_id=profile_id, **values))

    def delete(self, profile_id: str) -> None:
        previous = self.get(profile_id)
        del self._profiles[profile_id]
        try:
            self._save()
        except BaseException:
            self._profiles[profile_id] = previous
            raise


__all__ = [
    "DEFAULT_LLM_PROFILES_PATH",
    "LLMModelProfileStore",
    "LLMProfileConflictError",
    "LLMProfileError",
    "LLMProfileNotFoundError",
    "PROFILE_SCHEMA",
    "PROFILE_SCHEMA_VERSION",
]
