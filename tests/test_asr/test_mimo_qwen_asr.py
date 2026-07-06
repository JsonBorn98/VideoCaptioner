import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydub import AudioSegment
from pydub.generators import Sine

import videocaptioner.core.asr.mimo_asr as mimo_asr_module
import videocaptioner.core.asr.qwen_local_asr as qwen_local_module
import videocaptioner.core.asr.qwen_runtime as qwen_runtime_module
import videocaptioner.core.asr.qwen_worker as qwen_worker_module
from videocaptioner.core.asr.mimo_asr import MiMoASR
from videocaptioner.core.asr.qwen_local_asr import QwenLocalASR
from videocaptioner.core.asr.qwen_runtime import timestamp_items_to_segments
from videocaptioner.core.asr.text_timing import make_timed_segments
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
        aligner_compile=True,
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
    assert align_calls[0]["compile_aligner"] is True
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
    assert asr._get_key().startswith("v4-")


def test_mimo_asr_reuses_request_memo_for_same_audio(monkeypatch):
    completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=completions)

    monkeypatch.setattr(mimo_asr_module, "OpenAI", FakeOpenAI)
    request_memo = {}

    first = MiMoASR(
        audio_input=b"fake mp3",
        api_key="sk-test",
        language="zh",
        request_memo=request_memo,
    )
    second = MiMoASR(
        audio_input=b"fake mp3",
        api_key="sk-test",
        language="zh",
        request_memo=request_memo,
    )

    assert first._run()["text"] == "你好世界"
    assert second._run()["text"] == "你好世界"
    assert len(completions.calls) == 1


def test_mimo_base64_payload_size_limit_uses_raw_byte_boundary():
    max_raw = mimo_asr_module.MAX_RAW_AUDIO_BYTES_FOR_BASE64

    assert (
        mimo_asr_module._base64_encoded_size(max_raw)
        <= mimo_asr_module.MAX_BASE64_AUDIO_SIZE
    )
    assert (
        mimo_asr_module._base64_encoded_size(max_raw + 1)
        > mimo_asr_module.MAX_BASE64_AUDIO_SIZE
    )


def test_make_timed_segments_constrains_estimates_to_speech_ranges():
    segments = make_timed_segments(
        ["alpha beta gamma delta", "epsilon zeta eta theta"],
        10_000,
        speech_ranges_ms=[(1_000, 3_000), (7_000, 9_000)],
    )

    assert [(seg.start_time, seg.end_time) for seg in segments] == [
        (1_000, 3_000),
        (7_000, 9_000),
    ]


def test_mimo_asr_falls_back_to_estimated_segments_without_timestamps():
    asr = MiMoASR(
        audio_input=b"fake mp3",
        api_key="sk-test",
        need_word_time_stamp=True,
    )

    # Without _allow_degraded, missing timestamps now raise so ChunkedASR
    # can retry. Degraded fallback only happens when retries are exhausted.
    with pytest.raises(mimo_asr_module.ASRResultDegradedError):
        asr._make_segments(
            {
                "text": "plain text with enough words here. More text with enough words here.",
                "seconds": 2,
            }
        )

    segments = asr._make_segments(
        {
            "text": "plain text with enough words here. More text with enough words here.",
            "seconds": 2,
        },
        _allow_degraded=True,
    )

    assert segments[0].text.startswith("plain text")
    assert "More text" in segments[-1].text
    assert segments[-1].end_time == 2000
    assert asr._should_cache_response({"text": "plain text"}, segments) is False


def test_mimo_estimated_timestamps_use_speech_ranges():
    asr = MiMoASR(
        audio_input=b"fake mp3",
        api_key="sk-test",
        need_word_time_stamp=True,
        audio_duration=10.0,
        speech_ranges_ms=[(1_000, 3_000), (7_000, 9_000)],
    )
    text = (
        "one two three four five six seven eight nine ten. "
        "eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen "
        "nineteen twenty."
    )

    segments = asr._make_segments(
        {"text": text, "seconds": 10},
        _allow_degraded=True,
    )

    assert [(seg.start_time, seg.end_time) for seg in segments] == [
        (1_000, 3_000),
        (7_000, 9_000),
    ]


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

    # Without _allow_degraded, low coverage raises so ChunkedASR can retry.
    with pytest.raises(mimo_asr_module.ASRResultDegradedError):
        asr._make_segments(response)

    segments = asr._make_segments(response, _allow_degraded=True)

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
        compile_aligner=True,
        need_word_time_stamp=True,
    )
    asr.audio_duration = 0.8

    result = asr._run()
    segments = asr._make_segments(result)

    assert calls[0]["asr_model"] == "Qwen/Qwen3-ASR-0.6B"
    assert calls[0]["aligner_model"] == "Qwen/Qwen3-ForcedAligner-0.6B"
    assert calls[0]["model_dir"] == "C:/models"
    assert calls[0]["return_time_stamps"] is True
    assert calls[0]["compile_aligner"] is True
    assert calls[0]["callback"] is None
    assert [seg.text for seg in segments] == ["hello", "world"]
    assert segments[1].start_time == 400


def test_qwen_runtime_compile_model_best_effort(monkeypatch):
    model = object()
    compiled = object()
    fake_torch = SimpleNamespace(compile=lambda value: compiled)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    assert (
        qwen_runtime_module._compile_model_if_requested(
            model,
            enabled=True,
            label="test model",
        )
        is compiled
    )


def test_qwen_runtime_compile_model_falls_back_on_error(monkeypatch):
    model = object()

    def fail_compile(value):
        raise RuntimeError("compile unsupported")

    fake_torch = SimpleNamespace(compile=fail_compile)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    assert (
        qwen_runtime_module._compile_model_if_requested(
            model,
            enabled=True,
            label="test model",
        )
        is model
    )


def test_qwen_local_asr_passes_source_range_to_worker(monkeypatch, tmp_path):
    calls = []

    def fake_run_qwen_worker(**kwargs):
        calls.append(kwargs)
        return {"text": "hello", "time_stamps": [], "seconds": 2.0}

    monkeypatch.setattr(qwen_local_module, "run_qwen_worker", fake_run_qwen_worker)
    audio_path = tmp_path / "source.wav"
    audio_path.write_bytes(b"fake wav")

    asr = QwenLocalASR(
        audio_input=b"",
        need_word_time_stamp=False,
        audio_duration=2.0,
        source_audio_path=str(audio_path),
        source_start_ms=1200,
        source_duration_ms=2000,
    )

    result = asr._run()

    assert result["text"] == "hello"
    assert calls[0]["audio_input"] == str(audio_path)
    assert calls[0]["clip_start_ms"] == 1200
    assert calls[0]["clip_duration_ms"] == 2000


def test_qwen_local_asr_batch_instances_use_source_ranges(monkeypatch, tmp_path):
    calls = []

    def fake_run_qwen_batch_worker(**kwargs):
        calls.append(kwargs)
        return [
            {
                "text": "hello",
                "time_stamps": [
                    {"text": "hello", "start_time": 0.0, "end_time": 0.5},
                ],
            },
            {
                "text": "world",
                "time_stamps": [
                    {"text": "world", "start_time": 0.0, "end_time": 0.5},
                ],
            },
        ]

    monkeypatch.setattr(
        qwen_local_module,
        "run_qwen_batch_worker",
        fake_run_qwen_batch_worker,
    )
    audio_path = tmp_path / "source.wav"
    audio_path.write_bytes(b"fake wav")

    instances = [
        QwenLocalASR(
            audio_input=b"",
            need_word_time_stamp=True,
            audio_duration=1.0,
            source_audio_path=str(audio_path),
            source_start_ms=0,
            source_duration_ms=1000,
            cache_identity="chunk-0",
        ),
        QwenLocalASR(
            audio_input=b"",
            need_word_time_stamp=True,
            audio_duration=1.0,
            source_audio_path=str(audio_path),
            source_start_ms=1000,
            source_duration_ms=1000,
            cache_identity="chunk-1",
        ),
    ]

    results = QwenLocalASR.run_batch_instances(instances)

    assert calls[0]["items"][0]["audio_input"] == str(audio_path)
    assert calls[0]["items"][0]["clip_start_ms"] == 0
    assert calls[0]["items"][1]["clip_start_ms"] == 1000
    assert [result.segments[0].text for result in results] == ["hello", "world"]


def test_qwen_runtime_audio_input_as_path_can_clip_source_range(tmp_path):
    audio_path = tmp_path / "tone.wav"
    Sine(440).to_audio_segment(duration=2000).export(audio_path, format="wav")

    with qwen_runtime_module.audio_input_as_path(
        str(audio_path),
        temp_dir=str(tmp_path),
        clip_start_ms=500,
        clip_duration_ms=700,
    ) as clip_path:
        clip_path_obj = Path(clip_path)
        clipped_audio = AudioSegment.from_file(clip_path)
        assert clip_path_obj != audio_path
        assert abs(len(clipped_audio) - 700) <= 25

    assert not clip_path_obj.exists()


def test_qwen_runtime_audio_input_as_qwen_audio_clips_to_pcm_tuple(tmp_path):
    numpy = pytest.importorskip("numpy")
    audio_path = tmp_path / "tone.wav"
    Sine(440).to_audio_segment(duration=2000).export(audio_path, format="wav")

    with qwen_runtime_module.audio_input_as_qwen_audio(
        str(audio_path),
        temp_dir=str(tmp_path),
        clip_start_ms=500,
        clip_duration_ms=700,
    ) as audio_arg:
        samples, sample_rate = audio_arg

    assert sample_rate == 16_000
    assert samples.dtype == numpy.float32
    assert abs(len(samples) - int(0.7 * sample_rate)) <= sample_rate * 0.03


def test_qwen_transcribe_passes_source_range_as_pcm_tuple(monkeypatch, tmp_path):
    captured = {}
    pcm_audio_arg = (object(), 16_000)
    audio_path = tmp_path / "tone.wav"
    Sine(440).to_audio_segment(duration=2000).export(audio_path, format="wav")

    class FakeASRModel:
        @classmethod
        def from_pretrained(cls, model_ref, **kwargs):
            captured["model_ref"] = model_ref
            captured["load_kwargs"] = kwargs
            return cls()

        def transcribe(self, **kwargs):
            captured["transcribe_kwargs"] = kwargs
            return [
                SimpleNamespace(
                    text="hello",
                    language="English",
                    time_stamps=[],
                )
            ]

    fake_qwen_asr = SimpleNamespace(Qwen3ASRModel=FakeASRModel)
    monkeypatch.setitem(sys.modules, "qwen_asr", fake_qwen_asr)
    monkeypatch.setattr(qwen_runtime_module, "ensure_qwen_runtime_on_path", lambda: None)
    monkeypatch.setattr(
        qwen_runtime_module,
        "_audio_input_as_pcm_tuple",
        lambda *args, **kwargs: pcm_audio_arg,
    )
    qwen_runtime_module._asr_cache.clear()

    result = qwen_runtime_module.transcribe_with_qwen(
        audio_input=str(audio_path),
        language="en",
        asr_model="Qwen/Qwen3-ASR-1.7B",
        aligner_model="Qwen/Qwen3-ForcedAligner-0.6B",
        device="cpu",
        dtype="auto",
        return_time_stamps=False,
        clip_start_ms=500,
        clip_duration_ms=700,
    )

    audio_arg = captured["transcribe_kwargs"]["audio"]
    assert audio_arg is pcm_audio_arg
    assert captured["transcribe_kwargs"]["language"] == "English"
    assert result["text"] == "hello"
    qwen_runtime_module._asr_cache.clear()


def test_qwen_transcribe_batch_passes_audio_list_to_runtime(monkeypatch, tmp_path):
    captured = {}
    audio_path = tmp_path / "tone.wav"
    Sine(440).to_audio_segment(duration=2000).export(audio_path, format="wav")

    class FakeASRModel:
        @classmethod
        def from_pretrained(cls, model_ref, **kwargs):
            captured["model_ref"] = model_ref
            captured["load_kwargs"] = kwargs
            return cls()

        def transcribe(self, **kwargs):
            captured["transcribe_kwargs"] = kwargs
            return [
                SimpleNamespace(text="first", language="English", time_stamps=[]),
                SimpleNamespace(text="second", language="English", time_stamps=[]),
            ]

    fake_qwen_asr = SimpleNamespace(Qwen3ASRModel=FakeASRModel)
    monkeypatch.setitem(sys.modules, "qwen_asr", fake_qwen_asr)
    monkeypatch.setattr(qwen_runtime_module, "ensure_qwen_runtime_on_path", lambda: None)
    monkeypatch.setattr(
        qwen_runtime_module,
        "_audio_input_as_pcm_tuple",
        lambda *args, **kwargs: (f"pcm-{kwargs.get('clip_start_ms')}", 16_000),
    )
    qwen_runtime_module._asr_cache.clear()

    results = qwen_runtime_module.transcribe_batch_with_qwen(
        requests=[
            {
                "audio_input": str(audio_path),
                "clip_start_ms": 0,
                "clip_duration_ms": 500,
            },
            {
                "audio_input": str(audio_path),
                "clip_start_ms": 500,
                "clip_duration_ms": 500,
            },
        ],
        language="en",
        asr_model="Qwen/Qwen3-ASR-1.7B",
        aligner_model="Qwen/Qwen3-ForcedAligner-0.6B",
        device="cpu",
        dtype="auto",
        return_time_stamps=False,
    )

    assert captured["transcribe_kwargs"]["audio"] == [
        ("pcm-0", 16_000),
        ("pcm-500", 16_000),
    ]
    assert captured["transcribe_kwargs"]["language"] == "English"
    assert [result["text"] for result in results] == ["first", "second"]
    qwen_runtime_module._asr_cache.clear()


def test_qwen_align_passes_bytes_as_pcm_tuple(monkeypatch):
    captured = {}
    pcm_audio_arg = (object(), 16_000)

    class FakeAligner:
        @classmethod
        def from_pretrained(cls, model_ref, **kwargs):
            captured["model_ref"] = model_ref
            captured["load_kwargs"] = kwargs
            return cls()

        def align(self, **kwargs):
            captured["align_kwargs"] = kwargs
            return [{"text": "hello", "start_time": 0.0, "end_time": 0.5}]

    fake_qwen_asr = SimpleNamespace(Qwen3ForcedAligner=FakeAligner)
    monkeypatch.setitem(sys.modules, "qwen_asr", fake_qwen_asr)
    monkeypatch.setattr(qwen_runtime_module, "ensure_qwen_runtime_on_path", lambda: None)
    monkeypatch.setattr(
        qwen_runtime_module,
        "_audio_input_as_pcm_tuple",
        lambda *args, **kwargs: pcm_audio_arg,
    )
    qwen_runtime_module._aligner_cache.clear()

    items = qwen_runtime_module.align_with_qwen(
        audio_input=b"fake wav",
        transcript="hello",
        language="en",
        aligner_model="Qwen/Qwen3-ForcedAligner-0.6B",
        device="cpu",
        dtype="auto",
    )

    assert captured["align_kwargs"]["audio"] is pcm_audio_arg
    assert captured["align_kwargs"]["language"] == "English"
    assert items == [{"text": "hello", "start_time": 0.0, "end_time": 0.5}]
    qwen_runtime_module._aligner_cache.clear()


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
          "compile_aligner": true,
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
    assert calls[0]["compile_aligner"] is True
    assert '"time_stamps"' in output_path.read_text(encoding="utf-8")


def test_qwen_worker_supports_transcribe_batch_mode(monkeypatch, tmp_path):
    calls = []
    request_path = tmp_path / "request.json"
    output_path = tmp_path / "output.json"
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"fake wav")
    request_path.write_text(
        """
        {
          "items": [
            {
              "audio_path": "%s",
              "clip_start_ms": 0,
              "clip_duration_ms": 1000
            },
            {
              "audio_path": "%s",
              "clip_start_ms": 1000,
              "clip_duration_ms": 1000
            }
          ],
          "language": "en",
          "asr_model": "Qwen/Qwen3-ASR-1.7B",
          "aligner_model": "Qwen/Qwen3-ForcedAligner-0.6B",
          "model_dir": "C:/models",
          "device": "cpu",
          "dtype": "float32",
          "max_new_tokens": 512,
          "max_inference_batch_size": 4,
          "return_time_stamps": false,
          "compile_aligner": true,
          "temp_dir": "%s"
        }
        """
        % (
            str(audio_path).replace("\\", "\\\\"),
            str(audio_path).replace("\\", "\\\\"),
            str(tmp_path).replace("\\", "\\\\"),
        ),
        encoding="utf-8",
    )

    def fake_transcribe_batch_with_qwen(**kwargs):
        calls.append(kwargs)
        return [
            {"text": "first", "time_stamps": []},
            {"text": "second", "time_stamps": []},
        ]

    monkeypatch.setattr(
        qwen_worker_module,
        "transcribe_batch_with_qwen",
        fake_transcribe_batch_with_qwen,
    )

    exit_code = qwen_worker_module.main(
        [
            "--mode",
            "transcribe_batch",
            "--request",
            str(request_path),
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 0
    assert calls[0]["requests"][0]["audio_input"] == str(audio_path)
    assert calls[0]["requests"][1]["clip_start_ms"] == 1000
    assert calls[0]["max_inference_batch_size"] == 4
    assert calls[0]["compile_aligner"] is True
    assert '"results"' in output_path.read_text(encoding="utf-8")


def test_qwen_cuda_device_fails_fast_with_cpu_torch(monkeypatch):
    fake_torch = SimpleNamespace(
        __version__="2.12.1+cpu",
        version=SimpleNamespace(cuda=None),
        _C=SimpleNamespace(),
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    with pytest.raises(RuntimeError, match="PyTorch 是 CPU 版"):
        qwen_runtime_module._validate_requested_device("cuda:0")


def test_qwen_aligner_language_guesses_supported_languages_from_text():
    assert (
        qwen_runtime_module.normalize_aligner_language("auto", "今日は良い天気です")
        == "Japanese"
    )
    assert (
        qwen_runtime_module.normalize_aligner_language("auto", "오늘 날씨가 좋습니다")
        == "Korean"
    )
    assert (
        qwen_runtime_module.normalize_aligner_language("auto", "Привет как дела")
        == "Russian"
    )
    assert (
        qwen_runtime_module.normalize_aligner_language("auto", "¿Qué está pasando?")
        == "Spanish"
    )
    assert (
        qwen_runtime_module.normalize_aligner_language("auto", "não é uma opção")
        == "Portuguese"
    )
    assert (
        qwen_runtime_module.normalize_aligner_language("auto", "this is plain English")
        == "English"
    )


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


def test_qwen_estimated_timestamps_use_speech_ranges():
    asr = QwenLocalASR(
        audio_input=b"fake mp3",
        need_word_time_stamp=False,
        audio_duration=10.0,
        speech_ranges_ms=[(1_000, 3_000), (7_000, 9_000)],
    )
    text = (
        "one two three four five six seven eight nine ten. "
        "eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen "
        "nineteen twenty."
    )

    segments = asr._make_segments({"text": text, "seconds": 10, "time_stamps": []})

    assert [(seg.start_time, seg.end_time) for seg in segments] == [
        (1_000, 3_000),
        (7_000, 9_000),
    ]


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
        qwen_compile_aligner=True,
    )

    mimo_chunked = _create_mimo_asr(str(audio_path), config)
    assert mimo_chunked.asr_kwargs["need_word_time_stamp"] is True
    assert mimo_chunked.asr_kwargs["aligner_model_dir"] == "C:/models"
    assert mimo_chunked.asr_kwargs["aligner_compile"] is True
    assert isinstance(mimo_chunked.asr_kwargs["request_memo"], dict)
    assert mimo_chunked.chunk_overlap_ms == 12_000
    assert mimo_chunked.chunk_length_ms == 180_000
    assert mimo_chunked.chunk_concurrency == 2  # default lowered to avoid 429 rate limits
    assert mimo_chunked.chunk_boundary_mode == "vad"
    assert (
        mimo_chunked.max_chunk_payload_bytes
        == mimo_asr_module.MAX_RAW_AUDIO_BYTES_FOR_BASE64
    )

    config.need_word_time_stamp = False
    mimo_text_only_chunked = _create_mimo_asr(str(audio_path), config)
    assert mimo_text_only_chunked.chunk_length_ms == 300_000
    config.need_word_time_stamp = True

    config.transcribe_model = TranscribeModelEnum.QWEN_LOCAL_ASR
    config.qwen_asr_model = "Qwen/Qwen3-ASR-1.7B"
    qwen_chunked = _create_qwen_local_asr(str(audio_path), config)
    assert qwen_chunked.asr_kwargs["need_word_time_stamp"] is True
    assert qwen_chunked.asr_kwargs["asr_model"] == "Qwen/Qwen3-ASR-1.7B"
    assert qwen_chunked.asr_kwargs["compile_aligner"] is True
    assert qwen_chunked.chunk_overlap_ms == 12_000
    assert qwen_chunked.chunk_audio_format == "wav"
    assert qwen_chunked.retry_same_chunk is False
    assert qwen_chunked.chunk_boundary_mode == "vad"
    assert qwen_chunked.pass_source_range is True


def test_mimo_asr_concurrency_config_is_honored(tmp_path):
    """MiMo chunk concurrency comes from config and is clamped to at least 1."""
    audio_path = tmp_path / "input.wav"
    audio_path.write_bytes(b"fake wav")
    config = TranscribeConfig(
        transcribe_model=TranscribeModelEnum.MIMO_ASR_API,
        transcribe_language="zh",
        need_word_time_stamp=True,
        mimo_asr_api_key="sk-test",
        mimo_asr_concurrency=5,
    )
    assert _create_mimo_asr(str(audio_path), config).chunk_concurrency == 5

    config.mimo_asr_concurrency = 0
    assert _create_mimo_asr(str(audio_path), config).chunk_concurrency == 1


def _rate_limit_error() -> "Exception":
    import httpx
    import openai

    request = httpx.Request("POST", "https://api.xiaomimimo.com/v1/chat/completions")
    response = httpx.Response(429, headers={"retry-after": "0"}, request=request)
    return openai.RateLimitError("Too many requests", response=response, body=None)


class _RateLimitedThenOK:
    """Raise 429 the first ``fail_times`` calls, then return a valid response."""

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise _rate_limit_error()
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="你好世界"))],
            usage=SimpleNamespace(seconds=2.0),
        )


def test_mimo_asr_retries_then_succeeds_on_rate_limit(monkeypatch):
    """A transient 429 is retried with backoff instead of aborting the chunk."""
    completions = _RateLimitedThenOK(fail_times=2)
    sleeps: list[float] = []

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=completions)

    monkeypatch.setattr(mimo_asr_module, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(mimo_asr_module.time, "sleep", lambda s: sleeps.append(s))

    asr = MiMoASR(audio_input=b"fake mp3", api_key="sk-test", need_word_time_stamp=False)
    result = asr._run()

    assert result["text"] == "你好世界"
    assert completions.calls == 3  # 2 failures + 1 success
    assert len(sleeps) == 2


def test_mimo_asr_raises_after_exhausting_rate_limit_retries(monkeypatch):
    """A sustained 429 still surfaces after the bounded retry budget."""
    import openai

    completions = _RateLimitedThenOK(fail_times=999)

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=completions)

    monkeypatch.setattr(mimo_asr_module, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(mimo_asr_module.time, "sleep", lambda s: None)

    asr = MiMoASR(audio_input=b"fake mp3", api_key="sk-test", need_word_time_stamp=False)
    with pytest.raises(openai.RateLimitError):
        asr._run()

    assert completions.calls == mimo_asr_module.MIMO_RATE_LIMIT_MAX_ATTEMPTS + 1


# ---------------------------------------------------------------------------
# Transcript anomaly detection (quality gate before running the aligner)
# ---------------------------------------------------------------------------


def test_detect_repetition_flags_hallucination():
    """Repeated phrase fragments (MiMo hallucination signature) are detected."""
    repeated = "you could create a modification that was typed. " * 4
    assert mimo_asr_module._detect_repetition(repeated)


def test_detect_repetition_ignores_normal_text():
    """Normal sentences without consecutive repeats are not flagged."""
    normal = "The quick brown fox jumps over the lazy dog. " * 2
    assert not mimo_asr_module._detect_repetition(normal)


def test_check_transcript_anomaly_high_density():
    """180s audio with ~1000 words (5.5+ words/s) is flagged as anomalous."""
    text = " ".join(f"word{i}" for i in range(1000))
    reason = mimo_asr_module._check_transcript_anomaly(text, audio_duration=180.0)
    assert reason is not None
    assert "density" in reason


def test_anomaly_thresholds_can_relax_density_gate():
    """Thresholds are configurable without changing module-level defaults."""
    text = " ".join(f"word{i}" for i in range(1000))
    thresholds = mimo_asr_module._asr_anomaly.AnomalyThresholds(
        max_words_per_second_en=10.0
    )

    assert (
        mimo_asr_module._asr_anomaly.check_transcript_anomaly(
            text,
            audio_duration=180.0,
            thresholds=thresholds,
        )
        is None
    )
    assert mimo_asr_module._check_transcript_anomaly(text, 180.0) is not None


def test_check_transcript_anomaly_normal_text():
    """180s audio with ~400 words (~2.2 words/s) passes the check."""
    text = " ".join(f"word{i}" for i in range(400))
    assert mimo_asr_module._check_transcript_anomaly(text, audio_duration=180.0) is None


def test_check_transcript_anomaly_repetition():
    """An extreme repetition loop is flagged even at normal word density."""
    # ~300 words in 180s is normal density, but the text is a pure loop.
    phrase = "this is a repeated hallucination phrase that goes on. "
    text = phrase * 12  # ~108 words, well under density threshold
    reason = mimo_asr_module._check_transcript_anomaly(text, audio_duration=180.0)
    assert reason is not None
    assert "repetition" in reason or "hallucination" in reason


def test_check_transcript_anomaly_allows_genuine_speech_repetition():
    """Moderate repetition is genuine speech, not a hard anomaly.

    Regression: a real lecture chunk saying "make that perpendicular to that,
    perpendicular to that, perpendicular to that" burned the whole retry
    ladder (4 extra API calls) although its alignment was perfectly healthy.
    Three consecutive copies must only raise a *suspicion* that defers to the
    post-alignment checks.
    """
    filler_a = " ".join(f"before{i}" for i in range(30))
    filler_b = " ".join(f"after{i}" for i in range(30))
    text = (
        f"{filler_a} make that perpendicular to that, perpendicular to that, "
        f"perpendicular to that, which you cannot do {filler_b}"
    )
    assert mimo_asr_module._check_transcript_anomaly(text, audio_duration=180.0) is None
    # ...but it is still surfaced as a suspicion for logging/alignment review.
    assert mimo_asr_module._transcript_repetition_suspicion(text)


def test_transcript_repetition_suspicion_ignores_normal_text():
    text = " ".join(f"word{i}" for i in range(200))
    assert not mimo_asr_module._transcript_repetition_suspicion(text)


def test_mimo_asr_raises_on_high_density_before_aligner(monkeypatch):
    """High-density transcript raises before the aligner is ever called."""
    align_calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            # Return ~1000 words for a 180s chunk → 5.5+ words/s
            text = " ".join(f"word{i}" for i in range(1000))
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
                usage=SimpleNamespace(seconds=180.0),
            )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    def fake_align_worker(**kwargs):
        align_calls.append(kwargs)
        return []

    monkeypatch.setattr(mimo_asr_module, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(
        mimo_asr_module, "run_qwen_alignment_worker", fake_align_worker
    )

    asr = MiMoASR(
        audio_input=b"fake mp3",
        api_key="sk-test",
        need_word_time_stamp=True,
        aligner_device="cpu",
    )
    # Patch audio_duration so density check sees 180s
    asr.audio_duration = 180.0

    with pytest.raises(mimo_asr_module.ASRResultDegradedError) as exc_info:
        asr._run()

    assert "density" in exc_info.value.reason or "anomaly" in exc_info.value.reason
    # The aligner must NOT have been called — that's the whole point.
    assert align_calls == []


def test_mimo_asr_degraded_allows_aligner_on_high_density(monkeypatch):
    """With _allow_degraded, high-density text proceeds to the aligner."""
    align_calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            text = " ".join(f"word{i}" for i in range(1000))
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
                usage=SimpleNamespace(seconds=180.0),
            )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    def fake_align_worker(**kwargs):
        align_calls.append(kwargs)
        return [{"text": "word0", "start_time": 0.0, "end_time": 0.1}]

    monkeypatch.setattr(mimo_asr_module, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(
        mimo_asr_module, "run_qwen_alignment_worker", fake_align_worker
    )

    asr = MiMoASR(
        audio_input=b"fake mp3",
        api_key="sk-test",
        need_word_time_stamp=True,
        aligner_device="cpu",
    )
    asr.audio_duration = 180.0

    result = asr._run(_allow_degraded=True)
    # Aligner was called despite the anomaly warning.
    assert len(align_calls) == 1
    assert result["text"].startswith("word0")


# ---------------------------------------------------------------------------
# Non-consecutive repetition detection
# ---------------------------------------------------------------------------


def test_detect_repeated_ngram_flags_non_consecutive_repetition():
    """A long phrase restated several times (not adjacent) is flagged."""
    phrase = "I just want to say we're not going to be dealing with any of that here"
    filler = "and this is some different intervening content that varies each time"
    text = f"{phrase}. {filler} one. {phrase}. {filler} two. {phrase}. {phrase}."
    assert mimo_asr_module._detect_repeated_ngram(text)


def test_detect_repeated_ngram_normalizes_punctuation_and_case():
    """Repeated phrases with punctuation/case drift are still flagged."""
    phrase = "I just want to say we're not going to be dealing with any of that here"
    lower = "i just want to say we're not going to be dealing with any of that here"
    filler = "this intervening content changes enough to avoid adjacent repetition"
    text = f"{phrase}. {filler} one. {lower}! {filler} two. {phrase}? {lower}."
    assert mimo_asr_module._detect_repeated_ngram(text)


def test_detect_repeated_ngram_ignores_normal_text():
    """Distinct words never trip the n-gram repetition detector."""
    text = " ".join(f"word{i}" for i in range(200))
    assert not mimo_asr_module._detect_repeated_ngram(text)


def test_mimo_asr_genuine_repetition_with_healthy_alignment_passes(monkeypatch):
    """Moderately repetitive speech + healthy alignment succeeds first try.

    Regression for the "perpendicular to that x3" chunk: the transcript trips
    the repetition detectors, but the alignment is perfectly healthy, so the
    chunk must NOT raise ASRResultDegradedError (which used to burn 4 retries)
    and must be cached.
    """
    words = [f"word{i}" for i in range(200)]
    # Splice a genuine 3x rhetorical repetition into normal speech.
    words[100:100] = ["perpendicular", "to", "that,"] * 3
    text = " ".join(words)

    class FakeCompletions:
        def create(self, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
                usage=SimpleNamespace(seconds=181.0),
            )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    def fake_align_worker(**kwargs):
        # Healthy alignment: every word placed, spanning the full audio.
        return _make_word_timestamps(len(words), start=0.2, step=180.0 / len(words))

    monkeypatch.setattr(mimo_asr_module, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(
        mimo_asr_module, "run_qwen_alignment_worker", fake_align_worker
    )

    asr = MiMoASR(
        audio_input=b"fake mp3",
        api_key="sk-test",
        need_word_time_stamp=True,
        aligner_device="cpu",
    )
    asr.audio_duration = 181.0

    # Strict mode (no _allow_degraded): must not raise.
    response = asr._run()
    segments = asr._make_segments(response)
    assert len(segments) == len(words)
    assert asr._should_cache_response(response, segments) is True


# ---------------------------------------------------------------------------
# Alignment time-coverage / overflow (silent truncation + hallucination)
# ---------------------------------------------------------------------------


def _make_word_timestamps(count: int, start: float, step: float):
    """Build `count` word timestamp dicts spanning [start, start + count*step]."""
    items = []
    t = start
    for i in range(count):
        items.append({"text": f"word{i}", "start_time": round(t, 3), "end_time": round(t + step, 3)})
        t += step
    return items


def _mimo_asr_with_duration(duration_s: float) -> MiMoASR:
    asr = MiMoASR(
        audio_input=b"fake mp3",
        api_key="sk-test",
        need_word_time_stamp=True,
        aligner_device="cpu",
    )
    asr.audio_duration = duration_s
    return asr


def _qwen_asr_with_duration(duration_s: float) -> QwenLocalASR:
    asr = QwenLocalASR(
        audio_input=b"fake mp3",
        need_word_time_stamp=True,
    )
    asr.audio_duration = duration_s
    return asr


def test_qwen_local_asr_flags_high_density_transcript_with_timestamps():
    asr = _qwen_asr_with_duration(180.0)
    text = " ".join(f"word{i}" for i in range(1000))
    response = {
        "text": text,
        "seconds": 180.0,
        "time_stamps": _make_word_timestamps(1000, start=0.0, step=0.18),
    }

    with pytest.raises(qwen_local_module.ASRResultDegradedError) as exc_info:
        asr._make_segments(response)

    assert "density" in exc_info.value.reason
    segments = asr._make_segments(response, _allow_degraded=True)
    assert segments
    assert asr._should_cache_response(response, segments) is False


def test_qwen_local_asr_clamps_and_flags_timestamp_overflow():
    asr = _qwen_asr_with_duration(180.0)
    text = " ".join(f"word{i}" for i in range(230))
    response = {
        "text": text,
        "seconds": 181.0,
        "time_stamps": _make_word_timestamps(230, start=0.0, step=0.88),
    }

    with pytest.raises(qwen_local_module.ASRResultDegradedError) as exc_info:
        asr._make_segments(response)
    assert "overflow" in exc_info.value.reason

    segments = asr._make_segments(response, _allow_degraded=True)
    assert segments
    assert max(seg.end_time for seg in segments) <= 181_000
    assert asr._should_cache_response(response, segments) is False


def test_qwen_local_asr_degraded_truncation_keeps_clamped_alignment():
    asr = _qwen_asr_with_duration(180.0)
    text = " ".join(f"word{i}" for i in range(60))
    response = {
        "text": text,
        "seconds": 180.0,
        "time_stamps": _make_word_timestamps(60, start=0.0, step=0.78),
    }

    with pytest.raises(qwen_local_module.ASRResultDegradedError) as exc_info:
        asr._make_segments(response)
    assert "coverage" in exc_info.value.reason

    segments = asr._make_segments(response, _allow_degraded=True)
    assert len(segments) == 60
    assert segments[-1].end_time <= 60 * 780 + 1
    assert asr._should_cache_response(response, segments) is False


def test_qwen_local_asr_degraded_pathological_alignment_uses_estimated_timings():
    asr = _qwen_asr_with_duration(180.0)
    text = " ".join(f"word{i}" for i in range(200))
    response = {
        "text": text,
        "seconds": 180.0,
        "time_stamps": _make_word_timestamps(2, start=0.0, step=0.5),
    }

    with pytest.raises(qwen_local_module.ASRResultDegradedError):
        asr._make_segments(response)

    segments = asr._make_segments(response, _allow_degraded=True)
    merged_text = " ".join(seg.text for seg in segments)
    assert "word199" in merged_text
    assert segments[-1].end_time == 180_000


def test_mimo_asr_flags_silent_truncation_by_time_coverage():
    """Aligned words covering only the first third of the audio must degrade.

    The transcript matches its (short) word timestamps perfectly, so the
    text-based coverage check passes; only comparing the aligned span against
    the audio duration catches that 2/3 of the chunk has no subtitles.
    """
    asr = _mimo_asr_with_duration(180.0)
    text = " ".join(f"word{i}" for i in range(60))
    # 60 words aligned across only the first ~47s of a 180s chunk.
    response = {
        "text": text,
        "seconds": 180.0,
        "time_stamps": _make_word_timestamps(60, start=0.0, step=0.78),
    }

    with pytest.raises(mimo_asr_module.ASRResultDegradedError) as exc_info:
        asr._make_segments(response)
    assert "coverage" in exc_info.value.reason

    # Retries exhausted → degraded fallback keeps the clamped aligned segments:
    # they carry correct timings for everything MiMo did transcribe, instead of
    # smearing the truncated text across the whole 180s chunk.
    segments = asr._make_segments(response, _allow_degraded=True)
    assert len(segments) == 60
    assert segments[-1].end_time <= 60 * 780 + 1  # real aligned tail (~47s)
    # And the poisoned response must never be cached.
    assert asr._should_cache_response(response, segments) is False


def test_mimo_asr_clamps_and_flags_timestamp_overflow():
    """Timestamps extrapolated past the audio end are clamped and flagged.

    MiMo hallucinations add extra (often repeated) words; the forced aligner
    spaces them past the audio boundary, so the tail collides with the next
    chunk and breaks ChunkMerger. The overflow must be detected as degraded and
    any surviving segments clamped to the boundary.
    """
    asr = _mimo_asr_with_duration(180.0)
    text = " ".join(f"word{i}" for i in range(230))
    # 230 words at ~0.88s each → last word ends near 202s, ~11% past 181s.
    response = {
        "text": text,
        "seconds": 181.0,
        "time_stamps": _make_word_timestamps(230, start=0.0, step=0.88),
    }

    with pytest.raises(mimo_asr_module.ASRResultDegradedError) as exc_info:
        asr._make_segments(response)
    assert "overflow" in exc_info.value.reason

    # Degraded fallback keeps the clamped aligned segments (overflow words are
    # dropped at the boundary), and the response is not cached.
    segments = asr._make_segments(response, _allow_degraded=True)
    assert segments
    assert max(seg.end_time for seg in segments) <= 181_000
    assert asr._should_cache_response(response, segments) is False


def test_mimo_asr_degraded_pathological_alignment_uses_estimated_timings():
    """When almost nothing aligned, degraded mode falls back to estimation.

    Keeping 2 aligned words out of 200 would produce a nearly-empty chunk;
    estimated cue timings at least preserve the transcript text.
    """
    asr = _mimo_asr_with_duration(180.0)
    text = " ".join(f"word{i}" for i in range(200))
    response = {
        "text": text,
        "seconds": 180.0,
        "time_stamps": _make_word_timestamps(2, start=0.0, step=0.5),
    }

    with pytest.raises(mimo_asr_module.ASRResultDegradedError):
        asr._make_segments(response)

    segments = asr._make_segments(response, _allow_degraded=True)
    # Estimated: the full transcript survives, spread over the chunk.
    merged_text = " ".join(seg.text for seg in segments)
    assert "word199" in merged_text
    assert segments[-1].end_time == 180_000


def test_clamp_segments_to_duration_clips_and_reports_overflow():
    segments = timestamp_items_to_segments(
        [
            {"text": "a", "start_time": 0.0, "end_time": 1.0},
            {"text": "b", "start_time": 1.0, "end_time": 2.0},
            {"text": "c", "start_time": 2.0, "end_time": 3.0},  # past 1.5s boundary
        ]
    )
    clamped, overflow = mimo_asr_module._clamp_segments_to_duration(segments, 1500)

    # The word starting at/after the boundary is dropped, the straddling one clipped.
    assert [seg.text for seg in clamped] == ["a", "b"]
    assert clamped[-1].end_time == 1500
    assert overflow > 0.0


def test_mimo_asr_healthy_full_coverage_segments_are_cached():
    """A well-aligned chunk (near-full time coverage) is returned and cached."""
    asr = _mimo_asr_with_duration(181.0)
    text = " ".join(f"word{i}" for i in range(200))
    response = {
        "text": text,
        "seconds": 181.0,
        "time_stamps": _make_word_timestamps(200, start=0.2, step=0.9),
    }

    segments = asr._make_segments(response)
    assert len(segments) == 200
    assert segments[-1].end_time <= 181_000
    assert asr._should_cache_response(response, segments) is True
