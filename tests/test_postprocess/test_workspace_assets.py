"""过程资产目录与 manifest 发现（票 03）。

切面是 ``run_postprocess_task``：用临时输入目录验证外部行为，
不锁定内部函数或文件写入实现细节。
"""

from __future__ import annotations

import json
from pathlib import Path

from videocaptioner.core.postprocess.config import PostprocessConfig
from videocaptioner.core.postprocess.models import PostprocessLayoutMode, PostprocessTask
from videocaptioner.core.postprocess.runner import run_postprocess_task
from videocaptioner.core.postprocess.workspace import normalize_language

_UPSTREAM_KINDS = (
    "glossary",
    "audit",
    "checkpoint",
    "translation_snapshot",
    "context",
)
_FORBIDDEN_MANIFEST_KEYS = {
    "api_key",
    "apikey",
    "api-key",
    "prompt",
    "response",
    "reasoning",
    "completion",
}


def _write_srt(path: Path, text: str = "Hello there.", translated: str = "") -> None:
    if translated:
        body = f"1\n00:00:00,000 --> 00:00:02,000\n{text}\n{translated}\n"
    else:
        body = f"1\n00:00:00,000 --> 00:00:02,000\n{text}\n"
    path.write_text(body, encoding="utf-8")


def _config() -> PostprocessConfig:
    return PostprocessConfig(trim_trailing_punct=False, speed_semantic_repair=False)


def _run(
    tmp_path: Path,
    *,
    source: Path | None = None,
    name: str = "demo",
    text: str = "Hello there.",
    translated: str = "你好。",
    source_language: str = "en",
    target_language: str = "zh",
    media_path: str | None = None,
    explicit_assets: dict[str, str] | None = None,
    translation_method: str = "enhanced",
) -> object:
    if source is None:
        source = tmp_path / f"{name}.srt"
        _write_srt(source, text, translated)
    output = tmp_path / f"{name}-{source_language}-{target_language}-out.srt"
    task = PostprocessTask(
        str(source),
        postprocessed_subtitle_path=str(output),
        media_path=media_path,
        workflow_base_name=name,
        source_language=source_language,
        target_language=target_language,
        translation_method=translation_method,
        explicit_assets=explicit_assets or {},
        layout_mode=PostprocessLayoutMode.ORIGINAL_ON_TOP,
        config_snapshot=_config(),
    )
    return run_postprocess_task(task)


def _manifest(result) -> dict:
    discovery = result.task.asset_discovery
    assert discovery is not None
    return json.loads(Path(discovery.manifest_path).read_text(encoding="utf-8"))


def _collect_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            keys.add(str(key).casefold())
            keys.update(_collect_keys(nested))
    elif isinstance(value, list):
        for item in value:
            keys.update(_collect_keys(item))
    return keys


def test_independent_task_creates_ascii_workspace_next_to_input(tmp_path):
    result = _run(tmp_path)
    discovery = result.task.asset_discovery

    assert result.succeeded
    assert discovery is not None
    assert discovery.workspace_root.name == "videocaptioner-workspace"
    assert discovery.workspace_root.parent == tmp_path
    assert discovery.workspace_root.is_dir()
    assert discovery.task_dir.is_dir()
    assert discovery.task_dir.is_relative_to(discovery.workspace_root)
    assert discovery.manifest_path.is_file()
    for part in discovery.task_dir.relative_to(discovery.workspace_root).parts:
        assert part.isascii()
        assert part == part.lower() or part.replace("-", "").replace("_", "").isalnum()


def test_workspace_uses_media_directory_when_video_is_supplied(tmp_path):
    media_dir = tmp_path / "video"
    sub_dir = tmp_path / "subs"
    media_dir.mkdir()
    sub_dir.mkdir()
    source = sub_dir / "input.srt"
    _write_srt(source, "Hello there.", "你好。")
    media = media_dir / "clip.mp4"
    media.write_bytes(b"not-a-real-video")

    result = _run(
        tmp_path,
        source=source,
        name="clip",
        media_path=str(media),
    )

    assert result.task.asset_discovery.workspace_root == media_dir / "videocaptioner-workspace"
    assert not (sub_dir / "videocaptioner-workspace").exists()


def test_task_directories_are_isolated_by_name_fingerprint_and_language(tmp_path):
    same_lang = _run(tmp_path, name="alpha")
    other_name = _run(tmp_path, name="beta")
    other_text = _run(tmp_path, name="alpha", text="Completely different cue.")
    other_lang = _run(tmp_path, name="alpha", source_language="ja", target_language="zh")

    dirs = {
        same_lang.task.asset_discovery.task_dir,
        other_name.task.asset_discovery.task_dir,
        other_text.task.asset_discovery.task_dir,
        other_lang.task.asset_discovery.task_dir,
    }
    assert len(dirs) == 4
    workspace = tmp_path / "videocaptioner-workspace"
    assert all(item.is_relative_to(workspace) for item in dirs)


def test_display_language_names_normalize_to_stable_ascii_tokens():
    assert normalize_language("简体中文") == "zh-hans"
    assert normalize_language("zh-CN") == "zh-hans"
    assert normalize_language("zh-Hans") == "zh-hans"
    assert normalize_language("auto") == "auto"
    assert normalize_language("") == "und"
    assert normalize_language("简体中文").isascii()


def test_standalone_postprocess_without_languages_reuses_same_fingerprint_assets(tmp_path):
    glossary = tmp_path / "seed-glossary.json"
    glossary.write_text('{"schema": "videocaptioner.project_glossary"}\n', encoding="utf-8")
    seeded = _run(
        tmp_path,
        name="clip",
        source_language="auto",
        target_language="简体中文",
        explicit_assets={"glossary": str(glossary)},
    )
    source = tmp_path / "clip.srt"
    result = _run(
        tmp_path,
        source=source,
        name="clip",
        source_language="",
        target_language="",
    )

    discovery = result.task.asset_discovery
    assert discovery.asset_path("glossary") is not None
    assert "glossary" not in discovery.missing
    assert discovery.task_dir == seeded.task.asset_discovery.task_dir


def test_non_ascii_task_name_still_uses_ascii_directory_names(tmp_path):
    result = _run(tmp_path, name="你好世界")
    discovery = result.task.asset_discovery
    relative = discovery.task_dir.relative_to(discovery.workspace_root)
    assert all(part.isascii() for part in relative.parts)
    assert "你好" not in str(discovery.task_dir)


def test_manifest_records_identity_and_omits_secrets(tmp_path):
    result = _run(tmp_path, name="demo")
    manifest = _manifest(result)

    assert manifest["schema"] == "videocaptioner.workspace_manifest"
    assert manifest["task_name"]
    assert manifest["input_subtitle"]
    assert str(manifest["subtitle_fingerprint"]).startswith("sha256:")
    assert manifest["source_language"] == "en"
    assert manifest["target_language"] == "zh"
    assert manifest["translation_method"] == "enhanced"
    assert manifest["generated_at"]
    assert manifest["software_version"]
    assert isinstance(manifest["assets"], dict)
    assert _FORBIDDEN_MANIFEST_KEYS.isdisjoint(_collect_keys(manifest))
    dumped = json.dumps(manifest)
    assert "sk-" not in dumped
    assert "api_key" not in dumped


def test_verified_assets_are_reused_and_untrusted_assets_are_rejected(tmp_path):
    glossary = tmp_path / "seed-glossary.json"
    glossary.write_text('{"schema": "videocaptioner.project_glossary", "terms": []}\n', encoding="utf-8")

    first = _run(tmp_path, name="reuse", explicit_assets={"glossary": str(glossary)})
    first_discovery = first.task.asset_discovery
    assert first_discovery.asset_path("glossary") is not None
    assert first_discovery.asset_path("glossary").is_relative_to(first_discovery.task_dir)
    assert "glossary" not in first_discovery.missing

    second = _run(tmp_path, name="reuse")
    assert second.task.asset_discovery.task_dir == first_discovery.task_dir
    assert second.task.asset_discovery.asset_path("glossary") is not None
    assert "glossary" not in second.task.asset_discovery.missing

    listed = first_discovery.asset_path("glossary")
    listed.write_bytes(b"\xff\xfe not-utf8")
    broken = _run(tmp_path, name="reuse")
    assert broken.task.asset_discovery.asset_path("glossary") is None
    assert "glossary" in broken.task.asset_discovery.missing
    assert "glossary" in broken.task.asset_discovery.rejected
    assert any("glossary" in warning for warning in broken.warnings)

    manifest_path = Path(first_discovery.manifest_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["subtitle_fingerprint"] = "sha256:" + ("0" * 64)
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    (first_discovery.task_dir / "glossary.vcglossary.json").write_text(
        '{"schema": "videocaptioner.project_glossary", "terms": []}\n',
        encoding="utf-8",
    )
    mismatched = _run(tmp_path, name="reuse")
    assert mismatched.task.asset_discovery.asset_path("glossary") is None
    assert "glossary" in mismatched.task.asset_discovery.missing


def test_explicit_supplement_fills_missing_assets(tmp_path):
    audit = tmp_path / "manual-audit.md"
    audit.write_text("# 翻译审计\n无问题。\n", encoding="utf-8")
    result = _run(tmp_path, name="manual", explicit_assets={"audit": str(audit)})

    discovery = result.task.asset_discovery
    assert discovery.asset_path("audit") is not None
    assert discovery.asset_path("audit").read_text(encoding="utf-8").startswith("# 翻译审计")
    assert "audit" not in discovery.missing
    assert set(discovery.missing) <= set(_UPSTREAM_KINDS)
    assert "glossary" in discovery.missing
    assert any("过程资产缺失" in warning for warning in result.warnings)


def test_ordinary_io_directory_is_not_polluted_with_process_files(tmp_path):
    glossary = tmp_path / "seed-glossary.json"
    glossary.write_text('{"schema": "videocaptioner.project_glossary"}\n', encoding="utf-8")
    result = _run(tmp_path, name="clean", explicit_assets={"glossary": str(glossary)})

    workspace = result.task.asset_discovery.workspace_root
    stray = [
        path
        for path in tmp_path.rglob("*")
        if path.is_file()
        and not path.is_relative_to(workspace)
        and path.suffix.lower() in {".md", ".json"}
        and path.name not in {"seed-glossary.json"}
    ]
    assert stray == []
    assert list(tmp_path.glob("*.qa.md")) == []
    assert list(tmp_path.glob("*checkpoint*")) == []
    assert list(tmp_path.glob("*context*")) == []
    assert result.succeeded
    assert result.continue_downstream is True


def test_interrupted_asset_read_is_cancellation_not_unreadable(tmp_path, monkeypatch):
    glossary = tmp_path / "seed-glossary.json"
    glossary.write_text('{"schema": "videocaptioner.project_glossary"}\n', encoding="utf-8")
    original_read = Path.read_text

    def patched(self, *args, **kwargs):
        if self.resolve() == glossary.resolve():
            raise InterruptedError("stop requested")
        return original_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", patched)
    result = _run(tmp_path, name="stop", explicit_assets={"glossary": str(glossary)})

    assert result.task.status == "cancelled"
    assert not result.used_fallback
    assert result.continue_downstream is False
