import os
import sys
from types import SimpleNamespace

import pytest

import videocaptioner.core.asr.mimo_asr as mimo_asr_module
import videocaptioner.core.asr.qwen_local_asr as qwen_local_module
import videocaptioner.core.asr.qwen_runtime as qwen_runtime_module
import videocaptioner.core.asr.qwen_worker as qwen_worker_module
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

    def fake_run_qwen_alignment_worker(**kwargs):
        align_calls.append(kwargs)
        return [
            {"text": "你", "start_time": 0.1, "end_time": 0.3},
            {"text": "好", "start_time": 0.3, "end_time": 0.5},
        ]

    monkeypatch.setattr(mimo_asr_module, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(
        mimo_asr_module,
        "run_qwen_alignment_worker",
        fake_run_qwen_alignment_worker,
    )

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


def test_mimo_asr_cleans_gateway_markup_before_alignment(monkeypatch):
    completions = FakeCompletions()
    completions.create = lambda **kwargs: SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content="think> <chinese> Hello world. </chinese>"
                )
            )
        ],
        usage=SimpleNamespace(seconds=2.0),
    )
    align_calls = []

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=completions)

    def fake_run_qwen_alignment_worker(**kwargs):
        align_calls.append(kwargs)
        return [
            {"text": "Hello", "start_time": 0.1, "end_time": 0.3},
            {"text": "world", "start_time": 0.3, "end_time": 0.5},
        ]

    monkeypatch.setattr(mimo_asr_module, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(
        mimo_asr_module,
        "run_qwen_alignment_worker",
        fake_run_qwen_alignment_worker,
    )

    asr = MiMoASR(
        audio_input=b"fake mp3",
        api_key="sk-test",
        language="en",
        aligner_device="cpu",
        need_word_time_stamp=True,
    )

    response = asr._run()

    assert response["text"] == "Hello world."
    assert align_calls[0]["transcript"] == "Hello world."
    assert asr._get_key().startswith("v3-")


def test_mimo_asr_falls_back_to_estimated_segments_without_timestamps():
    asr = MiMoASR(
        audio_input=b"fake mp3",
        api_key="sk-test",
        need_word_time_stamp=True,
    )

    segments = asr._make_segments(
        {
            "text": "plain text with enough words here. More text with enough words here.",
            "seconds": 2,
        }
    )

    assert segments[0].text.startswith("plain text")
    assert "More text" in segments[-1].text
    assert segments[-1].end_time == 2000
    assert asr._should_cache_response({"text": "plain text"}, segments) is False


def test_mimo_asr_falls_back_when_alignment_coverage_is_low():
    asr = MiMoASR(
        audio_input=b"fake mp3",
        api_key="sk-test",
        need_word_time_stamp=True,
    )
    text = " ".join(f"word{i}" for i in range(50))
    response = {
        "text": text,
        "time_stamps": [
            {"text": "word0", "start_time": 0.0, "end_time": 0.1},
            {"text": "word1", "start_time": 0.1, "end_time": 0.2},
        ],
    }

    segments = asr._make_segments(response)

    assert len(segments) > 2
    assert "word0" in segments[0].text
    assert segments[-1].end_time > segments[0].start_time
    assert asr._should_cache_response(response, segments) is False


def test_mimo_asr_treats_empty_text_response_as_silence_chunk():
    asr = MiMoASR(
        audio_input=b"fake mp3",
        api_key="sk-test",
        need_word_time_stamp=True,
    )

    segments = asr._make_segments({"text": "", "seconds": 300})

    assert segments == []
    assert asr._should_cache_response({"text": ""}, segments) is False


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

    def fake_run_qwen_worker(**kwargs):
        calls.append(kwargs)
        return {
            "text": "hello world",
            "language": "English",
            "time_stamps": [
                {"text": "hello", "start_time": 0.0, "end_time": 0.4},
                {"text": "world", "start_time": 0.4, "end_time": 0.8},
            ],
        }

    monkeypatch.setattr(qwen_local_module, "run_qwen_worker", fake_run_qwen_worker)

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
    assert calls[0]["callback"] is None
    assert [seg.text for seg in segments] == ["hello", "world"]
    assert segments[1].start_time == 400


def test_qwen_worker_path_removes_pyqt_qt_bin():
    path_value = os.pathsep.join(
        [
            r"C:\tools",
            r"C:\repo\.venv\Lib\site-packages\PyQt5\Qt5\bin",
            r"C:\Windows\System32",
        ]
    )

    cleaned = qwen_local_module._without_qt_dll_paths(path_value)

    assert r"PyQt5\Qt5\bin" not in cleaned
    assert r"C:\tools" in cleaned
    assert r"C:\Windows\System32" in cleaned


def test_qwen_worker_supports_alignment_mode(monkeypatch, tmp_path):
    calls = []
    request_path = tmp_path / "request.json"
    output_path = tmp_path / "output.json"
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"fake mp3")
    request_path.write_text(
        """
        {
          "audio_path": "%s",
          "transcript": "hello world",
          "language": "en",
          "aligner_model": "Qwen/Qwen3-ForcedAligner-0.6B",
          "model_dir": "C:/models",
          "device": "cpu",
          "dtype": "float32",
          "temp_dir": "%s"
        }
        """
        % (str(audio_path).replace("\\", "\\\\"), str(tmp_path).replace("\\", "\\\\")),
        encoding="utf-8",
    )

    def fake_align_with_qwen(**kwargs):
        calls.append(kwargs)
        return [{"text": "hello", "start_time": 0.0, "end_time": 0.5}]

    monkeypatch.setattr(qwen_worker_module, "align_with_qwen", fake_align_with_qwen)

    exit_code = qwen_worker_module.main(
        [
            "--mode",
            "align",
            "--request",
            str(request_path),
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 0
    assert calls[0]["transcript"] == "hello world"
    assert calls[0]["device"] == "cpu"
    assert '"time_stamps"' in output_path.read_text(encoding="utf-8")


def test_qwen_cuda_device_fails_fast_with_cpu_torch(monkeypatch):
    fake_torch = SimpleNamespace(
        __version__="2.12.1+cpu",
        version=SimpleNamespace(cuda=None),
        _C=SimpleNamespace(),
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    with pytest.raises(RuntimeError, match="PyTorch 是 CPU 版"):
        qwen_runtime_module._validate_requested_device("cuda:0")


def test_qwen_local_asr_requires_timestamps_when_requested():
    asr = QwenLocalASR(
        audio_input=b"fake mp3",
        need_word_time_stamp=True,
    )

    with pytest.raises(RuntimeError, match="timestamps"):
        asr._make_segments({"text": "plain text without timestamps", "time_stamps": []})


def test_qwen_local_asr_treats_empty_text_response_as_silence_chunk():
    asr = QwenLocalASR(
        audio_input=b"fake mp3",
        need_word_time_stamp=True,
    )

    segments = asr._make_segments({"text": "", "time_stamps": []})

    assert segments == []
    assert asr._should_cache_response({"text": ""}, segments) is False


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
    assert mimo_chunked.chunk_length_ms == 180_000

    config.need_word_time_stamp = False
    mimo_text_only_chunked = _create_mimo_asr(str(audio_path), config)
    assert mimo_text_only_chunked.chunk_length_ms == 300_000
    config.need_word_time_stamp = True

    config.transcribe_model = TranscribeModelEnum.QWEN_LOCAL_ASR
    config.qwen_asr_model = "Qwen/Qwen3-ASR-1.7B"
    qwen_chunked = _create_qwen_local_asr(str(audio_path), config)
    assert qwen_chunked.asr_kwargs["need_word_time_stamp"] is True
    assert qwen_chunked.asr_kwargs["asr_model"] == "Qwen/Qwen3-ASR-1.7B"
    assert qwen_chunked.chunk_overlap_ms == 12_000
