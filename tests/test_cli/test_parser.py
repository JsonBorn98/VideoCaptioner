"""Tests for CLI argument parsing — verify all commands parse correctly."""

from argparse import Namespace
from pathlib import Path

import pytest

from videocaptioner.cli import exit_codes as EXIT
from videocaptioner.cli.commands.process import _resolve_final_output_path
from videocaptioner.cli.config import build_config
from videocaptioner.cli.main import _build_cli_overrides, main
from videocaptioner.core.entities import TranscribeModelEnum


class TestMainParser:
    def test_no_args_tries_gui(self, monkeypatch):
        # No args: tries to launch GUI. Mock GUI import to avoid opening it in tests.
        import builtins
        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "videocaptioner.ui.main":
                raise ImportError("mocked")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)
        assert main([]) == EXIT.DEPENDENCY_MISSING

    def test_version(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0
        assert "videocaptioner" in capsys.readouterr().out

    def test_invalid_subcommand(self):
        with pytest.raises(SystemExit) as exc:
            main(["nonexistent"])
        assert exc.value.code == 2

    def test_help(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "transcribe" in out
        assert "gui" in out
        assert "subtitle" in out
        assert "synthesize" in out
        assert "process" in out
        assert "download" in out
        assert "config" in out
        assert "doctor" in out

    def test_doctor_accepts_qwen_profile(self, tmp_path, monkeypatch, capsys):
        import videocaptioner.config as app_config
        import videocaptioner.core.asr.qwen_runtime_manager as qwen_manager

        model_dir = tmp_path / "models"
        model_dir.mkdir()
        (model_dir / "Qwen3-ASR-0.6B").mkdir()
        monkeypatch.setattr(app_config, "MODEL_PATH", model_dir)
        monkeypatch.setattr(
            qwen_manager,
            "inspect_qwen_runtime",
            lambda: qwen_manager.QwenRuntimeStatus(
                runtime_dir=tmp_path / "runtimes" / "qwen",
                python_executable=tmp_path / "runtimes" / "qwen" / "Scripts" / "python.exe",
                site_packages=(),
                has_venv=True,
                importable=True,
                uv_executable="uv",
            ),
        )

        assert main(["doctor", "--profile", "qwen"]) == EXIT.SUCCESS
        out = capsys.readouterr().out
        assert "qwen.runtime" in out
        assert "qwen.models" in out

    def test_gui_command_reports_missing_gui_dependencies(self, monkeypatch):
        import builtins

        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "videocaptioner.ui.main":
                raise ImportError("mocked")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)
        assert main(["gui"]) == EXIT.DEPENDENCY_MISSING


class TestTranscribeParser:
    def test_missing_input(self):
        with pytest.raises(SystemExit) as exc:
            main(["transcribe"])
        assert exc.value.code == 2

    def test_invalid_asr(self):
        with pytest.raises(SystemExit) as exc:
            main(["transcribe", "test.mp4", "--asr", "invalid"])
        assert exc.value.code == 2

    def test_file_not_found(self):
        assert main(["transcribe", "/nonexistent/file.mp4"]) == EXIT.FILE_NOT_FOUND

    def test_accepts_new_asr_backends(self):
        assert main(["transcribe", "/nonexistent/file.mp4", "--asr", "faster-whisper"]) == EXIT.FILE_NOT_FOUND
        assert main(["transcribe", "/nonexistent/file.mp4", "--asr", "mimo-asr"]) == EXIT.FILE_NOT_FOUND
        assert main(["transcribe", "/nonexistent/file.mp4", "--asr", "qwen-local"]) == EXIT.FILE_NOT_FOUND

    def test_verbose_quiet_mutually_exclusive(self):
        with pytest.raises(SystemExit) as exc:
            main(["transcribe", "test.mp4", "-v", "-q"])
        assert exc.value.code == 2

    def test_asr_cli_overrides_include_mimo_and_qwen_options(self):
        overrides = _build_cli_overrides(
            Namespace(
                asr="qwen-local",
                mimo_api_key="sk-mimo",
                mimo_api_base="https://example.test/v1",
                mimo_model="mimo-test",
                mimo_timeout=120,
                qwen_asr_model="Qwen/Qwen3-ASR-0.6B",
                qwen_aligner_model="Qwen/Qwen3-ForcedAligner-0.6B",
                qwen_model_dir="C:/models",
                qwen_device="cpu",
                qwen_dtype="float32",
                qwen_max_new_tokens=4096,
                qwen_chunk_overlap=12,
                qwen_compile_aligner=True,
            )
        )

        assert overrides["transcribe"]["asr"] == "qwen-local"
        assert overrides["transcribe"]["mimo_asr"]["api_key"] == "sk-mimo"
        assert overrides["transcribe"]["mimo_asr"]["timeout"] == 120
        assert overrides["transcribe"]["qwen"]["asr_model"] == "Qwen/Qwen3-ASR-0.6B"
        assert overrides["transcribe"]["qwen"]["model_dir"] == "C:/models"
        assert overrides["transcribe"]["qwen"]["chunk_overlap_seconds"] == 12
        assert overrides["transcribe"]["qwen"]["compile_aligner"] is True

    def test_subtitle_postprocess_flags_map_to_config(self):
        overrides = _build_cli_overrides(
            Namespace(
                remove_placeholders=True,
                normalize_quotes=True,
                keep_trailing_punct=True,
                fix_gaps=True,
                max_gap_ms=500,
                gap_mode="midpoint",
                audit_speed=True,
                max_cps_cjk=13.0,
                max_cps_latin=22.0,
                compress_fast=True,
                qa_report=True,
            )
        )
        sub = overrides["subtitle"]
        assert sub["remove_placeholders"] is True
        assert sub["normalize_quotes"] is True
        assert sub["trim_trailing_punct"] is False  # --keep-trailing-punct inverts
        assert sub["fix_gaps"] is True
        assert sub["max_gap_ms"] == 500
        assert sub["gap_mode"] == "midpoint"
        assert sub["audit_reading_speed"] is True
        assert sub["max_cps_cjk"] == 13.0
        assert sub["max_cps_latin"] == 22.0
        assert sub["compress_fast_subtitles"] is True
        assert sub["qa_report"] is True

    def test_subtitle_postprocess_defaults_absent_when_flags_unset(self):
        overrides = _build_cli_overrides(Namespace())
        # No postprocess overrides should be emitted so config-file/defaults win.
        assert "remove_placeholders" not in overrides.get("subtitle", {})

    def test_transcribe_command_builds_qwen_config(self, tmp_path, monkeypatch):
        import videocaptioner.core.asr as asr_package
        from videocaptioner.cli.commands import transcribe as transcribe_cmd

        audio_path = tmp_path / "input.wav"
        audio_path.write_bytes(b"fake wav")
        output_path = tmp_path / "out.srt"
        captured = {}

        class FakeASRData:
            segments = []

            def save(self, save_path):
                Path(save_path).write_text("", encoding="utf-8")

        def fake_transcribe(audio, config, callback=None):
            captured["audio"] = audio
            captured["config"] = config
            return FakeASRData()

        monkeypatch.setattr(asr_package, "transcribe", fake_transcribe)
        config = build_config(
            cli_overrides={
                "transcribe": {
                    "asr": "qwen-local",
                    "qwen": {
                        "asr_model": "Qwen/Qwen3-ASR-0.6B",
                        "aligner_model": "Qwen/Qwen3-ForcedAligner-0.6B",
                        "model_dir": "C:/models",
                        "device": "cpu",
                        "dtype": "float32",
                        "max_new_tokens": 4096,
                        "chunk_overlap_seconds": 12,
                        "compile_aligner": True,
                    },
                }
            }
        )

        result = transcribe_cmd.run(
            Namespace(
                input=str(audio_path),
                output=str(output_path),
                verbose=False,
                quiet=True,
                word_timestamps=True,
            ),
            config,
        )

        assert result == EXIT.SUCCESS
        transcribe_config = captured["config"]
        assert transcribe_config.transcribe_model == TranscribeModelEnum.QWEN_LOCAL_ASR
        assert transcribe_config.qwen_asr_model == "Qwen/Qwen3-ASR-0.6B"
        assert transcribe_config.qwen_model_dir == "C:/models"
        assert transcribe_config.qwen_device == "cpu"
        assert transcribe_config.qwen_dtype == "float32"
        assert transcribe_config.qwen_max_new_tokens == 4096
        assert transcribe_config.qwen_chunk_overlap_seconds == 12
        assert transcribe_config.qwen_compile_aligner is True

    def test_transcribe_command_builds_mimo_config(self, tmp_path, monkeypatch):
        import videocaptioner.core.asr as asr_package
        from videocaptioner.cli.commands import transcribe as transcribe_cmd

        audio_path = tmp_path / "input.wav"
        audio_path.write_bytes(b"fake wav")
        output_path = tmp_path / "out.srt"
        captured = {}

        class FakeASRData:
            segments = []

            def save(self, save_path):
                Path(save_path).write_text("", encoding="utf-8")

        def fake_transcribe(audio, config, callback=None):
            captured["config"] = config
            return FakeASRData()

        monkeypatch.setattr(asr_package, "transcribe", fake_transcribe)
        config = build_config(
            cli_overrides={
                "transcribe": {
                    "asr": "mimo-asr",
                    "mimo_asr": {
                        "api_key": "sk-mimo",
                        "api_base": "https://example.test/v1",
                        "model": "mimo-test",
                        "timeout": 120,
                        "concurrency": 4,
                    },
                    "qwen": {
                        "aligner_model": "Qwen/Qwen3-ForcedAligner-0.6B",
                        "device": "cpu",
                        "dtype": "float32",
                        "compile_aligner": True,
                    },
                }
            }
        )

        result = transcribe_cmd.run(
            Namespace(
                input=str(audio_path),
                output=str(output_path),
                verbose=False,
                quiet=True,
                word_timestamps=True,
            ),
            config,
        )

        assert result == EXIT.SUCCESS
        transcribe_config = captured["config"]
        assert transcribe_config.transcribe_model == TranscribeModelEnum.MIMO_ASR_API
        assert transcribe_config.mimo_asr_api_key == "sk-mimo"
        assert transcribe_config.mimo_asr_api_base == "https://example.test/v1"
        assert transcribe_config.mimo_asr_model == "mimo-test"
        assert transcribe_config.mimo_asr_timeout == 120
        assert transcribe_config.mimo_asr_concurrency == 4
        assert transcribe_config.qwen_device == "cpu"
        assert transcribe_config.qwen_dtype == "float32"
        assert transcribe_config.qwen_compile_aligner is True


class TestSubtitleParser:
    def test_missing_input(self):
        with pytest.raises(SystemExit) as exc:
            main(["subtitle"])
        assert exc.value.code == 2

    def test_file_not_found(self):
        assert main(["subtitle", "/nonexistent/file.srt"]) == EXIT.FILE_NOT_FOUND

    def test_invalid_translator(self):
        with pytest.raises(SystemExit) as exc:
            main(["subtitle", "test.srt", "--translator", "invalid"])
        assert exc.value.code == 2

    def test_invalid_target_language(self, tmp_path):
        srt = tmp_path / "test.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n")
        result = main(["subtitle", str(srt), "--translator", "bing", "--target-language", "xyz"])
        assert result == EXIT.USAGE_ERROR

    def test_invalid_format(self):
        with pytest.raises(SystemExit) as exc:
            main(["subtitle", "test.srt", "--format", "vtt"])
        assert exc.value.code == 2


class TestSynthesizeParser:
    def test_missing_subtitle_flag(self):
        with pytest.raises(SystemExit) as exc:
            main(["synthesize", "video.mp4"])
        assert exc.value.code == 2

    def test_file_not_found(self):
        assert main(["synthesize", "/no/video.mp4", "-s", "/no/sub.srt"]) == EXIT.FILE_NOT_FOUND


class TestProcessParser:
    def test_dub_options_parse_with_missing_input(self):
        result = main([
            "process",
            "/no/video.mp4",
            "--dub-only",
            "--dub-provider",
            "siliconflow",
            "--dub-preset",
            "siliconflow-cn-female",
            "--tts-model",
            "FunAudioLLM/CosyVoice2-0.5B",
            "--voice",
            "FunAudioLLM/CosyVoice2-0.5B:anna",
        ])
        assert result == EXIT.FILE_NOT_FOUND

    def test_process_dub_final_output_defaults_to_dubbed_captioned(self, tmp_path):
        result = _resolve_final_output_path(None, tmp_path, tmp_path / "talk.mp4", True, False, False)

        assert result.endswith("talk_dubbed_captioned.mp4")

    def test_process_dub_only_uses_user_output_file(self, tmp_path):
        result = _resolve_final_output_path(str(tmp_path / "final.mp4"), tmp_path, tmp_path / "talk.mp4", True, True, False)

        assert result.endswith("final.mp4")

    def test_process_help_hides_advanced_dubbing_options(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["process", "--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "--preset" in out
        assert "--timing" in out
        assert "--audio-mode" in out
        assert "--style-prompt" not in out
        assert "--tts-api-base" not in out

    def test_process_runs_subtitle_stage_for_postprocess_only(
        self,
        tmp_path,
        monkeypatch,
    ):
        from videocaptioner.cli.commands import process as process_cmd
        from videocaptioner.cli.commands import subtitle as subtitle_cmd
        from videocaptioner.cli.commands import transcribe as transcribe_cmd

        input_path = tmp_path / "talk.mp3"
        input_path.write_bytes(b"fake")
        calls = {"subtitle": 0}

        def fake_transcribe(args, config):
            Path(args.output).write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n[Music]\n",
                encoding="utf-8",
            )
            return EXIT.SUCCESS

        def fake_subtitle(args, config):
            calls["subtitle"] += 1
            Path(args.output).write_text("", encoding="utf-8")
            return EXIT.SUCCESS

        monkeypatch.setattr(transcribe_cmd, "run", fake_transcribe)
        monkeypatch.setattr(subtitle_cmd, "run", fake_subtitle)
        config = build_config(
            cli_overrides={
                "subtitle": {
                    "optimize": False,
                    "translate": False,
                    "remove_placeholders": True,
                }
            }
        )

        ret = process_cmd.run(
            Namespace(
                input=str(input_path),
                output=str(tmp_path),
                verbose=False,
                quiet=True,
                no_synthesize=True,
                dub=False,
                dub_only=False,
                translator=None,
                target_language=None,
                config=None,
            ),
            config,
        )

        assert ret == EXIT.SUCCESS
        assert calls["subtitle"] == 1

    def test_process_runs_subtitle_stage_when_cli_enables_translation(
        self,
        tmp_path,
        monkeypatch,
    ):
        from videocaptioner.cli.commands import process as process_cmd
        from videocaptioner.cli.commands import subtitle as subtitle_cmd
        from videocaptioner.cli.commands import transcribe as transcribe_cmd

        input_path = tmp_path / "talk.mp3"
        input_path.write_bytes(b"fake")
        calls = {"subtitle": 0}

        def fake_transcribe(args, config):
            Path(args.output).write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello\n",
                encoding="utf-8",
            )
            return EXIT.SUCCESS

        def fake_subtitle(args, config):
            calls["subtitle"] += 1
            Path(args.output).write_text("", encoding="utf-8")
            return EXIT.SUCCESS

        monkeypatch.setattr(transcribe_cmd, "run", fake_transcribe)
        monkeypatch.setattr(subtitle_cmd, "run", fake_subtitle)
        config = build_config(
            cli_overrides={
                "subtitle": {"optimize": False, "translate": False},
                "translate": {"service": "bing"},
            }
        )

        ret = process_cmd.run(
            Namespace(
                input=str(input_path),
                output=str(tmp_path),
                verbose=False,
                quiet=True,
                no_synthesize=True,
                dub=False,
                dub_only=False,
                translator="bing",
                target_language=None,
                config=None,
            ),
            config,
        )

        assert ret == EXIT.SUCCESS
        assert calls["subtitle"] == 1

    def test_process_skip_messages_do_not_use_unrun_step_numbers(
        self,
        tmp_path,
        monkeypatch,
        capsys,
    ):
        from videocaptioner.cli.commands import process as process_cmd
        from videocaptioner.cli.commands import transcribe as transcribe_cmd

        input_path = tmp_path / "talk.mp3"
        input_path.write_bytes(b"fake")

        def fake_transcribe(args, config):
            Path(args.output).write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello\n",
                encoding="utf-8",
            )
            return EXIT.SUCCESS

        monkeypatch.setattr(transcribe_cmd, "run", fake_transcribe)
        config = build_config(
            cli_overrides={
                "subtitle": {"optimize": False, "translate": False},
                "transcribe": {"asr": "bijian"},
            }
        )

        ret = process_cmd.run(
            Namespace(
                input=str(input_path),
                output=str(tmp_path),
                verbose=False,
                quiet=False,
                no_synthesize=True,
                dub=False,
                dub_only=False,
                translator=None,
                target_language=None,
                config=None,
            ),
            config,
        )

        assert ret == EXIT.SUCCESS
        err = capsys.readouterr().err
        assert "Step 2/1" not in err
        assert "Synthesis skipped (disabled)" in err


class TestDubParser:
    def test_missing_subtitle(self):
        with pytest.raises(SystemExit) as exc:
            main(["dub"])
        assert exc.value.code == 2

    def test_file_not_found(self):
        assert main(["dub", "/no/sub.srt"]) == EXIT.FILE_NOT_FOUND

    def test_help_hides_provider_details(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["dub", "--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "--preset" in out
        assert "--speak" in out
        assert "--adapt-length" in out
        assert "--style-prompt" not in out
        assert "--tts-model" not in out
        assert "--dub-preset" not in out

    def test_gemini_clone_fails_before_synthesis(self, tmp_path):
        srt = tmp_path / "test.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        ref = tmp_path / "ref.wav"
        ref.write_bytes(b"not real audio")

        result = main([
            "dub",
            str(srt),
            "--preset",
            "gemini-en-friendly",
            "--tts-api-key",
            "test-key",
            "--clone-audio",
            str(ref),
            "--clone-text",
            "Hello",
        ])

        assert result == EXIT.USAGE_ERROR

    def test_edge_clone_fails_before_synthesis_without_api_key(self, tmp_path):
        srt = tmp_path / "test.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        ref = tmp_path / "ref.wav"
        ref.write_bytes(b"not real audio")

        result = main([
            "dub",
            str(srt),
            "--preset",
            "edge-cn-female",
            "--clone-audio",
            str(ref),
            "--clone-text",
            "Hello",
        ])

        assert result == EXIT.USAGE_ERROR


class TestConfigParser:
    def test_no_action(self):
        assert main(["config"]) == EXIT.USAGE_ERROR

    def test_set_unknown_key(self):
        assert main(["config", "set", "garbage.key", "value"]) == EXIT.GENERAL_ERROR

    def test_set_section_key(self):
        assert main(["config", "set", "subtitle", "bad"]) == EXIT.GENERAL_ERROR

    def test_set_invalid_int(self):
        assert main(["config", "set", "subtitle.thread_num", "abc"]) == EXIT.GENERAL_ERROR

    def test_set_invalid_bool(self):
        assert main(["config", "set", "subtitle.optimize", "maybe"]) == EXIT.GENERAL_ERROR

    def test_get_unknown_key(self):
        assert main(["config", "get", "nonexistent.key"]) == EXIT.GENERAL_ERROR

    def test_show(self, capsys):
        result = main(["config", "show"])
        assert result == EXIT.SUCCESS
        out = capsys.readouterr().out
        assert "llm:" in out
        assert "api_key" in out

    def test_path(self, capsys):
        result = main(["config", "path"])
        assert result == EXIT.SUCCESS
        out = capsys.readouterr().out
        assert "config.toml" in out

    def test_init_print_template(self, capsys):
        result = main(["config", "init", "--non-interactive", "--print-template", "--profile", "dubbing"])
        assert result == EXIT.SUCCESS
        out = capsys.readouterr().out
        assert "[dubbing]" in out
        assert "edge-cn-female" in out
        assert "audio_mode" in out


class TestDoctorParser:
    def test_doctor_json(self, capsys):
        result = main(["doctor", "--json"])
        assert result in {EXIT.SUCCESS, EXIT.DEPENDENCY_MISSING}
        out = capsys.readouterr().out
        assert '"checks"' in out

    def test_doctor_help(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["doctor", "--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "--json" in out
        assert "--check-api" in out
