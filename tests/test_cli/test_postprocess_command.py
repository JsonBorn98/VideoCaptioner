from argparse import Namespace

import pytest

from videocaptioner.cli import exit_codes as EXIT
from videocaptioner.cli.commands import postprocess as command
from videocaptioner.cli.config import build_config
from videocaptioner.core.llm.models import LLMModelProfile, LLMTransport, ProviderDialect
from videocaptioner.core.llm.profiles import LLMModelProfileStore
from videocaptioner.core.postprocess import PostprocessProfileStore


@pytest.fixture(autouse=True)
def _seed_llm_profile_store(tmp_path, monkeypatch):
    """Seed a main model profile for the default balanced postprocess profile.

    The balanced template enables semantic repair, which resolves its model from
    the profile store (derived from the main translation profile) — the same
    requirement the GUI task factory has. Without a seeded store every default
    postprocess run would fail fast on profile resolution.
    """

    store_path = tmp_path / "llm_model_profiles.json"
    LLMModelProfileStore(store_path).save(
        LLMModelProfile(
            profile_id="main-profile",
            name="Main Profile",
            transport=LLMTransport.OPENAI_COMPATIBLE,
            dialect=ProviderDialect.GENERIC,
            base_url="https://main.test/v1",
            api_key="main-secret",
            model="main-model",
            work_context_tokens=16_384,
        )
    )
    monkeypatch.setattr(
        "videocaptioner.core.llm.profiles.DEFAULT_LLM_PROFILES_PATH", store_path
    )
    return store_path


def _config(**overrides):
    """Build a config whose default postprocess profile can resolve a model."""

    values = {"llm": {"profile_id": "main-profile"}}
    values.update(overrides)
    return build_config(values)


def test_standalone_command_preserves_input_and_writes_named_output(
    monkeypatch, tmp_path, capsys
):
    import videocaptioner.core.postprocess as postprocess_package

    source = tmp_path / "sample.srt"
    original = "1\n00:00:00,000 --> 00:00:02,000\n你好。\n"
    source.write_text(original, encoding="utf-8")
    store = PostprocessProfileStore(tmp_path / "profiles.json")
    monkeypatch.setattr(postprocess_package, "PostprocessProfileStore", lambda: store)
    args = Namespace(
        input=str(source),
        output=None,
        layout="source-only",
        profile="balanced",
        speed_profile=None,
        media=None,
        speed_media=None,
        quiet=True,
        verbose=False,
    )

    result = command.run(args, _config())

    assert result == EXIT.SUCCESS
    output_path = tmp_path / "【后处理字幕】sample.srt"
    assert source.read_text(encoding="utf-8") == original
    assert output_path.exists()
    assert "你好。" not in output_path.read_text(encoding="utf-8")
    assert str(output_path) in capsys.readouterr().out


def test_explicit_ass_output_is_normalized_to_srt(monkeypatch, tmp_path, capsys):
    import videocaptioner.core.postprocess as postprocess_package

    source = tmp_path / "sample.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n你好。\n", encoding="utf-8"
    )
    store = PostprocessProfileStore(tmp_path / "profiles.json")
    monkeypatch.setattr(postprocess_package, "PostprocessProfileStore", lambda: store)
    requested = tmp_path / "delivery.ass"
    args = Namespace(
        input=str(source),
        output=str(requested),
        layout="source-only",
        profile="balanced",
        speed_profile=None,
        media=None,
        speed_media=None,
        quiet=False,
        verbose=False,
    )

    result = command.run(args, _config())

    canonical = tmp_path / "delivery.srt"
    assert result == EXIT.SUCCESS
    assert canonical.exists()
    assert not requested.exists()
    assert str(canonical) in capsys.readouterr().err


def test_target_only_uses_translation_side(monkeypatch, tmp_path):
    import videocaptioner.core.postprocess as postprocess_package

    source = tmp_path / "bilingual.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nSource\n译文\n",
        encoding="utf-8",
    )
    store = PostprocessProfileStore(tmp_path / "profiles.json")
    monkeypatch.setattr(postprocess_package, "PostprocessProfileStore", lambda: store)
    args = Namespace(
        input=str(source),
        output=None,
        layout="target-only",
        profile="balanced",
        speed_profile=None,
        media=None,
        speed_media=None,
        quiet=True,
        verbose=False,
    )

    assert command.run(args, _config()) == EXIT.SUCCESS
    saved = (tmp_path / "【后处理字幕】bilingual.srt").read_text(encoding="utf-8")
    assert "译文" in saved
    assert "Source" not in saved


def test_analyze_mode_writes_reports_without_subtitle_output(monkeypatch, tmp_path):
    import videocaptioner.core.postprocess as postprocess_package

    source = tmp_path / "sample.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:00,500\n一段很长很长的字幕文本\n",
        encoding="utf-8",
    )
    store = PostprocessProfileStore(tmp_path / "profiles.json")
    monkeypatch.setattr(postprocess_package, "PostprocessProfileStore", lambda: store)
    args = Namespace(
        input=str(source),
        output=None,
        layout="source-only",
        profile="balanced",
        speed_profile=None,
        media=None,
        speed_media=None,
        quiet=True,
        verbose=False,
    )
    config = _config(postprocess={"mode": "analyze", "qa_report": True})

    assert command.run(args, config) == EXIT.SUCCESS
    assert not (tmp_path / "【后处理字幕】sample.srt").exists()
    assert (tmp_path / "【后处理字幕】sample.qa.md").exists()
    assert (tmp_path / "【后处理字幕】sample.speed-changes.json").exists()
