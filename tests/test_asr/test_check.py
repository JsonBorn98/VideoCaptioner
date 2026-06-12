"""转录连通性检查（core.asr.check）契约测试，不联网。

关键契约：检查必须绕过缓存（use_cache=False），否则坏掉的 Key 会
因缓存命中误报成功。
"""

from pathlib import Path

from videocaptioner.core.asr.check import TEST_AUDIO_PATH, check_transcribe
from videocaptioner.core.asr.transcribe import _create_asr_instance
from videocaptioner.core.entities import TranscribeConfig, TranscribeModelEnum


def _config(model=TranscribeModelEnum.BIJIAN) -> TranscribeConfig:
    return TranscribeConfig(transcribe_model=model)


class TestCheckTranscribe:
    def test_bundled_audio_exists(self):
        assert TEST_AUDIO_PATH.exists(), "内置测试音频缺失，测试转录与 doctor 都会失效"

    def test_missing_audio_returns_failure(self):
        result = check_transcribe(_config(), audio_path="/nonexistent/audio.mp3")
        assert not result.success
        assert "测试音频不存在" in result.detail

    def test_exception_collapsed_to_result(self, monkeypatch, tmp_path):
        audio = tmp_path / "a.mp3"
        audio.write_bytes(b"\0" * 64)
        monkeypatch.setattr(
            "videocaptioner.core.asr.check.video2audio",
            lambda src, output="": Path(output).write_bytes(b"\0" * 64) or True,
        )

        def boom(path, config, callback=None, *, use_cache=True):
            raise RuntimeError("provider exploded")

        monkeypatch.setattr("videocaptioner.core.asr.check.transcribe", boom)
        result = check_transcribe(_config(), audio_path=audio)
        assert not result.success
        assert "provider exploded" in result.detail


class TestUseCachePassthrough:
    """use_cache 必须贯通到每个提供商的 ASR 构造参数。"""

    def test_all_providers_disable_cache(self, tmp_path):
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"\0" * 64)
        configs = [
            _config(TranscribeModelEnum.BIJIAN),
            _config(TranscribeModelEnum.JIANYING),
            TranscribeConfig(
                transcribe_model=TranscribeModelEnum.WHISPER_API,
                whisper_api_key="k",
                whisper_api_base="https://example.com/v1",
                whisper_api_model="whisper-1",
            ),
            TranscribeConfig(
                transcribe_model=TranscribeModelEnum.BAILIAN_FUN_ASR,
                fun_asr_api_key="k",
            ),
        ]
        for config in configs:
            asr = _create_asr_instance(str(audio), config, use_cache=False)
            assert asr.asr_kwargs["use_cache"] is False, config.transcribe_model
            asr = _create_asr_instance(str(audio), config)
            assert asr.asr_kwargs["use_cache"] is True, config.transcribe_model
