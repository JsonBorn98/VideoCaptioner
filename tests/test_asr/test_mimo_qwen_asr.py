from types import SimpleNamespace

import pytest

import videocaptioner.core.asr.mimo_asr as mimo_asr_module
import videocaptioner.core.asr.qwen_local_asr as qwen_local_module
from videocaptioner.core.asr.mimo_asr import MiMoASR
from videocaptioner.core.asr.qwen_local_asr import QwenLocalASR
from videocaptioner.core.asr.qwen_runtime import timestamp_items_to_segments
from videocaptioner.core.asr.transcribe import _create_mimo_asr, _create_qwen_local_asr
from videocaptioner.core.entities import TranscribeConfig, TranscribeModelEnum


class FakeCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="  你好世界  "),
                )
            ],
            usage=SimpleNamespace(seconds=2.0),
        )


def test_mimo_asr_uses_qwen_aligner_for_word_timestamps(monkeypatch):
    completions = FakeCompletions()
    client_kwargs = {}
    align_calls = []

    class FakeOpenAI:
        def __init__(self, **kwargs):
            client_kwargs.update(kwargs)
            self.chat = SimpleNamespace(completions=completions)

    def fake_align_with_qwen(**kwargs):
        align_calls.append(kwargs)
        return [
            {"text": "你", "start_time": 0.1, "end_time": 0.3},
            {"text": "好", "start_time": 0.3, "end_time": 0.5},
        ]

    monkeypatch.setattr(mimo_asr_module, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(mimo_asr_module, "align_with_qwen", fake_align_with_qwen)

    asr = MiMoASR(
        audio_input=b"fake mp3",
        api_key="sk-test",
        base_url="https://api.xiaomimimo.com/v1",
        model="mimo-v2.5-asr",
        language="zh",
        timeout=601,
        aligner_model="Qwen/Qwen3-ForcedAligner-0.6B",
        aligner_device="cpu",
        aligner_dtype="float32",
        need_word_time_stamp=True,
    )

    response = asr._run()
    segments = asr._make_segments(response)

    assert client_kwargs["base_url"] == "https://api.xiaomimimo.com/v1"
    assert completions.calls[0]["model"] == "mimo-v2.5-asr"
    assert completions.calls[0]["extra_body"] == {"asr_options": {"language": "zh"}}
    assert align_calls[0]["transcript"] == "你好世界"
    assert align_calls[0]["language"] == "zh"
    assert align_calls[0]["aligner_model"] == "Qwen/Qwen3-ForcedAligner-0.6B"
    assert align_calls[0]["device"] == "cpu"
    assert [seg.text for seg in segments] == ["你", "好"]
    assert segments[0].start_time == 100
    assert segments[1].end_time == 500


def test_mimo_asr_requires_real_timestamps_when_requested():
    asr = MiMoASR(
        audio_input=b"fake mp3",
        api_key="sk-test",
        need_word_time_stamp=True,
    )

    with pytest.raises(RuntimeError, match="ForcedAligner"):
        asr._make_segments({"text": "plain text", "seconds": 1})


def test_timestamp_items_to_segments_accepts_objects_and_dicts():
    item = SimpleNamespace(text="hello", start_time=1.2, end_time=1.5)

    segments = timestamp_items_to_segments([[item, {"text": "world", "start": 1.5, "end": 2.0}]])

    assert [(seg.text, seg.start_time, seg.end_time) for seg in segments] == [
        ("hello", 1200, 1500),
        ("world", 1500, 2000),
    ]


def test_timestamp_items_to_segments_accepts_forced_aligner_result_wrapper():
    result = SimpleNamespace(
        items=[
            SimpleNamespace(text="hello", start_time=0.1, end_time=0.4),
            SimpleNamespace(text="world", start_time=0.4, end_time=0.8),
        ]
    )

    segments = timestamp_items_to_segments([result])

    assert [(seg.text, seg.start_time, seg.end_time) for seg in segments] == [
        ("hello", 100, 400),
        ("world", 400, 800),
    ]


def test_qwen_local_asr_uses_runtime_and_returns_timestamps(monkeypatch):
    calls = []

    def fake_transcribe_with_qwen(**kwargs):
        calls.append(kwargs)
        return {
            "text": "hello world",
            "language": "English",
            "time_stamps": [
                {"text": "hello", "start_time": 0.0, "end_time": 0.4},
                {"text": "world", "start_time": 0.4, "end_time": 0.8},
            ],
        }

    monkeypatch.setattr(qwen_local_module, "transcribe_with_qwen", fake_transcribe_with_qwen)

    asr = QwenLocalASR(
        audio_input=b"fake mp3",
        asr_model="Qwen/Qwen3-ASR-0.6B",
        aligner_model="Qwen/Qwen3-ForcedAligner-0.6B",
        model_dir="C:/models",
        language="en",
        device="cpu",
        dtype="float32",
        max_new_tokens=512,
        need_word_time_stamp=True,
    )

    result = asr._run()
    segments = asr._make_segments(result)

    assert calls[0]["asr_model"] == "Qwen/Qwen3-ASR-0.6B"
    assert calls[0]["aligner_model"] == "Qwen/Qwen3-ForcedAligner-0.6B"
    assert calls[0]["model_dir"] == "C:/models"
    assert calls[0]["return_time_stamps"] is True
    assert [seg.text for seg in segments] == ["hello", "world"]
    assert segments[1].start_time == 400


def test_qwen_local_asr_requires_timestamps_when_requested():
    asr = QwenLocalASR(
        audio_input=b"fake mp3",
        need_word_time_stamp=True,
    )

    with pytest.raises(RuntimeError, match="timestamps"):
        asr._make_segments({"text": "plain text without timestamps", "time_stamps": []})


def test_qwen_local_asr_splits_plain_text_when_timestamps_not_requested():
    asr = QwenLocalASR(
        audio_input=b"fake mp3",
        need_word_time_stamp=False,
    )
    text = (
        "This is the first sentence. This is the second sentence with enough words. "
        "This is the third sentence."
    )

    segments = asr._make_segments({"text": text, "time_stamps": []})

    assert len(segments) > 1
    assert all(seg.end_time > seg.start_time for seg in segments)
    assert "first sentence" in segments[0].text


def test_transcribe_factories_preserve_requested_word_timestamp_flag(tmp_path):
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"fake wav")
    config = TranscribeConfig(
        transcribe_model=TranscribeModelEnum.MIMO_ASR_API,
        transcribe_language="zh",
        need_word_time_stamp=True,
        mimo_asr_api_key="sk-test",
        qwen_aligner_model="Qwen/Qwen3-ForcedAligner-0.6B",
        qwen_model_dir="C:/models",
        qwen_device="cpu",
        qwen_dtype="float32",
        qwen_chunk_overlap_seconds=12,
    )

    mimo_chunked = _create_mimo_asr(str(audio_path), config)
    assert mimo_chunked.asr_kwargs["need_word_time_stamp"] is True
    assert mimo_chunked.asr_kwargs["aligner_model_dir"] == "C:/models"
    assert mimo_chunked.chunk_overlap_ms == 12_000

    config.transcribe_model = TranscribeModelEnum.QWEN_LOCAL_ASR
    config.qwen_asr_model = "Qwen/Qwen3-ASR-1.7B"
    qwen_chunked = _create_qwen_local_asr(str(audio_path), config)
    assert qwen_chunked.asr_kwargs["need_word_time_stamp"] is True
    assert qwen_chunked.asr_kwargs["asr_model"] == "Qwen/Qwen3-ASR-1.7B"
    assert qwen_chunked.chunk_overlap_ms == 12_000
