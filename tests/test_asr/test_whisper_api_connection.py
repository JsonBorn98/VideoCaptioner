from types import SimpleNamespace

import videocaptioner.core.llm.check_whisper as check_whisper_module
from videocaptioner.core.asr.whisper_api import WhisperAPI


class FakeBadRequestError(Exception):
    pass


class FakeTranscriptions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if "timestamp_granularities" in kwargs:
            raise FakeBadRequestError("timestamp_granularities is not supported")
        return SimpleNamespace(text="hello world")


def test_check_whisper_connection_falls_back_without_timestamp_args(
    monkeypatch, tmp_path
):
    audio_path = tmp_path / "en.mp3"
    audio_path.write_bytes(b"fake mp3")
    transcriptions = FakeTranscriptions()

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.audio = SimpleNamespace(transcriptions=transcriptions)

    monkeypatch.setattr(check_whisper_module, "TEST_AUDIO_PATH", audio_path)
    monkeypatch.setattr(check_whisper_module.openai, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(
        check_whisper_module.openai, "BadRequestError", FakeBadRequestError
    )

    success, text = check_whisper_module.check_whisper_connection(
        "https://example.com/v1", "sk-test", "custom-whisper"
    )

    assert success is True
    assert text == "hello world"
    assert len(transcriptions.calls) == 2
    assert "timestamp_granularities" in transcriptions.calls[0]
    assert "timestamp_granularities" not in transcriptions.calls[1]


def test_whisper_api_can_make_single_segment_from_text_only_response():
    asr = WhisperAPI(
        audio_input=b"fake audio",
        whisper_model="custom-whisper",
        base_url="https://example.com/v1",
        api_key="sk-test",
    )

    segments = asr._make_segments({"text": "plain transcript"})

    assert len(segments) == 1
    assert segments[0].text == "plain transcript"
    assert segments[0].start_time == 0
    assert segments[0].end_time > 0
