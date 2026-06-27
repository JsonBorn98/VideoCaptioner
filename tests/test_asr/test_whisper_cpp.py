from videocaptioner.core.asr.whisper_cpp import WhisperCppASR


def _write_model(tmp_path, model_name="large-v3-turbo"):
    model_path = tmp_path / f"ggml-{model_name}.bin"
    model_path.write_bytes(b"model")
    return model_path


def test_whisper_cpp_prefers_exact_model_match(monkeypatch, tmp_path):
    exact_model = tmp_path / "ggml-large-v3.bin"
    turbo_model = tmp_path / "ggml-large-v3-turbo.bin"
    exact_model.write_bytes(b"exact")
    turbo_model.write_bytes(b"turbo")

    monkeypatch.setattr("videocaptioner.core.asr.whisper_cpp.MODEL_PATH", tmp_path)

    asr = WhisperCppASR(
        b"",
        whisper_cpp_path="whisper-cli",
        whisper_model="large-v3",
    )

    assert asr.model_path == str(exact_model)


def test_whisper_cpp_can_select_large_v3_turbo(monkeypatch, tmp_path):
    turbo_model = tmp_path / "ggml-large-v3-turbo.bin"
    turbo_model.write_bytes(b"turbo")

    monkeypatch.setattr("videocaptioner.core.asr.whisper_cpp.MODEL_PATH", tmp_path)

    asr = WhisperCppASR(
        b"",
        whisper_cpp_path="whisper-cli",
        whisper_model="large-v3-turbo",
    )

    assert asr.model_path == str(turbo_model)


def test_whisper_cli_command_sets_expected_output_file(monkeypatch, tmp_path):
    _write_model(tmp_path)
    monkeypatch.setattr("videocaptioner.core.asr.whisper_cpp.MODEL_PATH", tmp_path)

    asr = WhisperCppASR(
        b"",
        whisper_cpp_path="whisper-cli",
        whisper_model="large-v3-turbo",
    )

    wav_path = tmp_path / "whisper_cpp_audio.wav"
    output_path = tmp_path / "whisper_cpp_audio.srt"
    cmd = asr._build_command(wav_path, output_path, asr._supports_output_file_arg())

    assert "--output-file" in cmd
    assert str(output_path.with_suffix("")) in cmd
    assert "--no-gpu" not in cmd


def test_legacy_windows_whisper_cpp_command_keeps_default_output(monkeypatch, tmp_path):
    _write_model(tmp_path, "large-v2")
    monkeypatch.setattr("videocaptioner.core.asr.whisper_cpp.MODEL_PATH", tmp_path)

    asr = WhisperCppASR(
        b"",
        whisper_cpp_path="whisper-cpp",
        whisper_model="large-v2",
    )

    wav_path = tmp_path / "whisper_cpp_audio.wav"
    output_path = tmp_path / "whisper_cpp_audio.srt"
    cmd = asr._build_command(wav_path, output_path, asr._supports_output_file_arg())

    assert "--output-file" not in cmd
