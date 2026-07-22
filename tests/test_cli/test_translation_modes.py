from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

from videocaptioner.cli import exit_codes as EXIT
from videocaptioner.cli.commands import subtitle as subtitle_command
from videocaptioner.cli.config import build_config
from videocaptioner.cli.main import _build_cli_overrides
from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.llm import LLMTransport, ProviderDialect
from videocaptioner.core.translate.enhanced.models import (
    TermConfirmationMode,
    TranslationAuditMode,
    TranslationExecutionMode,
)


def _args(input_path: Path, output_path: Path, **overrides) -> Namespace:
    values = {
        "input": str(input_path),
        "input_data": ASRData([ASRDataSeg("Hello", 0, 1000)]),
        "output": str(output_path),
        "no_translate": False,
        "translator": None,
        "translation_mode": None,
        "target_language": None,
        "layout": None,
        "prompt": None,
        "prompt_file": None,
        "verbose": False,
        "quiet": True,
    }
    values.update(overrides)
    return Namespace(**values)


def test_legacy_llm_flag_selects_enhanced_mode() -> None:
    overrides = _build_cli_overrides(Namespace(translator="llm"))

    assert overrides["translate"]["service"] == "llm"
    assert overrides["translate"]["mode"] == "enhanced_llm"


def test_explicit_translation_mode_wins_over_legacy_flag() -> None:
    overrides = _build_cli_overrides(
        Namespace(translator="llm", translation_mode="single_llm")
    )

    assert overrides["translate"]["mode"] == "single_llm"


def test_old_non_llm_config_is_classified_without_changing_service(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[translate]\nservice = "deeplx"\n', encoding="utf-8")

    config = build_config(config_path=config_file)

    assert config["translate"]["mode"] == "non_llm"
    assert config["translate"]["service"] == "deeplx"


def test_cli_enhanced_translation_is_automatic_and_persists_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.srt"
    destination = tmp_path / "initial.srt"
    captured = {}

    def fake_run(data, config, **kwargs):
        captured["config"] = config
        captured["kwargs"] = kwargs
        translated = ASRData.from_json(data.to_json())
        translated.segments[0].translated_text = "你好"
        glossary = tmp_path / "【项目术语表】source.vcglossary.json"
        audit = tmp_path / "【翻译审计】source.md"
        glossary.write_text("{}", encoding="utf-8")
        audit.write_text("# audit", encoding="utf-8")
        return SimpleNamespace(
            subtitle_data=translated,
            artifacts=SimpleNamespace(glossary_path=glossary, audit_report_path=audit),
            result=SimpleNamespace(audit_report=SimpleNamespace(issues=())),
        )

    import videocaptioner.core.translate.enhanced as enhanced_package

    monkeypatch.setattr(enhanced_package, "run_enhanced_translation", fake_run)
    config = build_config(
        {
            "llm": {"api_key": "test-key", "model": "test-model"},
            "subtitle": {"optimize": False, "split": False, "translate": True},
            "translate": {
                "mode": "enhanced_llm",
                "main_prompt": "main role",
                "review_prompt": "review role",
            },
        }
    )
    args = _args(source, destination)

    result = subtitle_command.run(args, config)

    enhanced_config = captured["config"]
    assert result == EXIT.SUCCESS
    assert enhanced_config.term_confirmation is TermConfirmationMode.AUTOMATIC
    assert enhanced_config.audit_mode is TranslationAuditMode.AUTO_APPLY_REVIEW
    assert enhanced_config.execution_mode is TranslationExecutionMode.CLI
    assert enhanced_config.main_role.profile == enhanced_config.review_role.profile
    assert enhanced_config.main_role.profile.transport is LLMTransport.OPENAI_COMPATIBLE
    assert enhanced_config.main_role.profile.dialect is ProviderDialect.GENERIC
    assert enhanced_config.main_role.user_prompt == "main role"
    assert enhanced_config.review_role.user_prompt == "review role"
    assert args.glossary_path.endswith(".vcglossary.json")
    assert args.translation_audit_report_path.endswith(".md")


def test_non_llm_deeplx_is_mapped_explicitly(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.srt"
    destination = tmp_path / "initial.srt"
    captured = {}

    class FakeTranslator:
        failed_count = 0

        def translate_subtitle(self, data):
            data.segments[0].translated_text = "你好"
            return data

    from videocaptioner.core.translate import factory as factory_module

    def fake_create(**kwargs):
        captured.update(kwargs)
        return FakeTranslator()

    monkeypatch.setattr(factory_module.TranslatorFactory, "create_translator", fake_create)
    config = build_config(
        {
            "subtitle": {"optimize": False, "split": False, "translate": True},
            "translate": {"mode": "non_llm", "service": "deeplx"},
        }
    )

    result = subtitle_command.run(_args(source, destination), config)

    from videocaptioner.core.translate.types import TranslatorType

    assert result == EXIT.SUCCESS
    assert captured["translator_type"] is TranslatorType.DEEPLX
    assert captured["profile"] is None


def test_single_llm_keeps_reflection_and_uses_profile(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.srt"
    destination = tmp_path / "initial.srt"
    captured = {}

    class FakeTranslator:
        failed_count = 0

        def translate_subtitle(self, data):
            data.segments[0].translated_text = "你好"
            return data

    from videocaptioner.core.translate import factory as factory_module

    def fake_create(**kwargs):
        captured.update(kwargs)
        return FakeTranslator()

    monkeypatch.setattr(factory_module.TranslatorFactory, "create_translator", fake_create)
    config = build_config(
        {
            "llm": {"api_key": "test-key", "model": "test-model"},
            "subtitle": {"optimize": False, "split": False, "translate": True},
            "translate": {"mode": "single_llm", "reflect": True},
        }
    )

    result = subtitle_command.run(_args(source, destination), config)

    from videocaptioner.core.translate.types import TranslatorType

    assert result == EXIT.SUCCESS
    assert captured["translator_type"] is TranslatorType.OPENAI
    assert captured["is_reflect"] is True
    assert captured["profile"].dialect is ProviderDialect.GENERIC


def test_process_does_not_postprocess_after_translation_failure(
    tmp_path: Path, monkeypatch
) -> None:
    from videocaptioner.cli.commands import postprocess, process, transcribe

    media = tmp_path / "talk.mp3"
    media.write_bytes(b"fake")
    calls = {"postprocess": 0}

    def fake_transcribe(args, config):
        Path(args.output).write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8"
        )
        return EXIT.SUCCESS

    def fake_subtitle(args, config):
        return EXIT.RUNTIME_ERROR

    def fake_postprocess(args, config):
        calls["postprocess"] += 1
        return EXIT.SUCCESS

    monkeypatch.setattr(transcribe, "run", fake_transcribe)
    monkeypatch.setattr(subtitle_command, "run", fake_subtitle)
    monkeypatch.setattr(postprocess, "run", fake_postprocess)
    config = build_config(
        {
            "subtitle": {"optimize": False, "split": False, "translate": True},
            "translate": {"mode": "non_llm", "service": "bing"},
        }
    )
    args = Namespace(
        input=str(media),
        output=str(tmp_path),
        verbose=False,
        quiet=True,
        no_synthesize=True,
        dub=False,
        dub_only=False,
        no_postprocess=False,
        translator=None,
        translation_mode=None,
        target_language=None,
        config=None,
    )

    result = process.run(args, config)

    assert result == EXIT.RUNTIME_ERROR
    assert calls["postprocess"] == 0
