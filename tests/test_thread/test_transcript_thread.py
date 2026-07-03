"""Tests for TranscriptThread."""

import os
from pathlib import Path

import pytest

from tests.test_thread.conftest import run_thread_with_timeout
from videocaptioner.core.entities import TranscribeConfig, TranscribeModelEnum, TranscribeTask
from videocaptioner.ui.thread.transcript_thread import TranscriptThread


def test_force_cancel_qwen_runtime_does_not_terminate_qthread(monkeypatch, qapp):
    task = TranscribeTask(
        file_path="video.mp4",
        transcribe_config=TranscribeConfig(
            transcribe_model=TranscribeModelEnum.QWEN_LOCAL_ASR,
        ),
        output_path="video.srt",
    )
    thread = TranscriptThread(task)
    terminate_called = False

    monkeypatch.setattr(TranscriptThread, "isRunning", lambda self: True)

    def fake_terminate(self):
        nonlocal terminate_called
        terminate_called = True

    monkeypatch.setattr(TranscriptThread, "terminate", fake_terminate)

    thread.cancel(force=True)

    assert thread._cancel_requested is True
    assert terminate_called is False


def test_qwen_local_cleanup_does_not_touch_main_process_torch(monkeypatch, qapp):
    task = TranscribeTask(
        file_path="video.mp4",
        transcribe_config=TranscribeConfig(
            transcribe_model=TranscribeModelEnum.QWEN_LOCAL_ASR,
        ),
        output_path="video.srt",
    )
    thread = TranscriptThread(task)
    cleanup_called = False

    def fake_cleanup():
        nonlocal cleanup_called
        cleanup_called = True

    monkeypatch.setattr(
        "videocaptioner.ui.thread.transcript_thread.clear_qwen_model_cache",
        fake_cleanup,
    )

    thread._cleanup_runtime_cache()

    assert cleanup_called is False


@pytest.mark.integration
class TestTranscriptThread:
    """Test suite for TranscriptThread."""

    @pytest.fixture
    def base_config(self) -> TranscribeConfig:
        """Create base transcription configuration."""
        return TranscribeConfig(
            transcribe_model=TranscribeModelEnum.FASTER_WHISPER,
            transcribe_language="zh",
            need_word_time_stamp=True,
        )

    @pytest.mark.skipif(
        not Path("resource/bin/faster-whisper-xxl").exists()
        and not Path("resource/bin/faster-whisper-xxl.exe").exists(),
        reason="FasterWhisper executable not found - 需要本地 FasterWhisper 可执行文件",
    )
    def test_transcribe_audio_with_faster_whisper(
        self,
        sample_audio_path: str,
        output_dir: str,
        base_config: TranscribeConfig,
        qapp,
    ):
        """Test transcription using FasterWhisper model with audio file."""
        output_path = os.path.join(output_dir, "transcript_audio.srt")
        task = TranscribeTask(
            file_path=sample_audio_path,
            transcribe_config=base_config,
            output_path=output_path,
        )

        thread = TranscriptThread(task)
        results = run_thread_with_timeout(thread, timeout_ms=60000)

        assert results["error"] is None, f"Thread failed: {results.get('error')}"
        assert results["finished"], "Thread did not finish"
        assert Path(output_path).exists(), f"Output file not created: {output_path}"

    @pytest.mark.skipif(
        not Path("resource/bin/faster-whisper-xxl").exists()
        and not Path("resource/bin/faster-whisper-xxl.exe").exists(),
        reason="FasterWhisper executable not found - 需要本地 FasterWhisper 可执行文件",
    )
    def test_transcribe_video_with_faster_whisper(
        self,
        sample_video_path: str,
        output_dir: str,
        base_config: TranscribeConfig,
        qapp,
    ):
        """Test transcription using FasterWhisper model with video file."""
        output_path = os.path.join(output_dir, "transcript_video.srt")
        task = TranscribeTask(
            file_path=sample_video_path,
            transcribe_config=base_config,
            output_path=output_path,
        )

        thread = TranscriptThread(task)
        results = run_thread_with_timeout(thread, timeout_ms=60000)

        assert results["error"] is None, f"Thread failed: {results.get('error')}"
        assert results["finished"], "Thread did not finish"
        assert Path(output_path).exists(), f"Output file not created: {output_path}"

    def test_transcribe_missing_video(
        self, output_dir: str, base_config: TranscribeConfig, qapp
    ):
        """Test transcription with missing video file."""
        output_path = os.path.join(output_dir, "transcript.srt")
        task = TranscribeTask(
            file_path="/nonexistent/video.mp4",
            transcribe_config=base_config,
            output_path=output_path,
        )

        thread = TranscriptThread(task)
        results = run_thread_with_timeout(thread, timeout_ms=5000)

        assert results["error"] is not None, "Expected error for missing video"
        assert not results["finished"], "Thread should not finish successfully"

    def test_transcribe_empty_path(
        self, output_dir: str, base_config: TranscribeConfig, qapp
    ):
        """Test transcription with empty file path."""
        output_path = os.path.join(output_dir, "transcript.srt")
        task = TranscribeTask(
            file_path="",
            transcribe_config=base_config,
            output_path=output_path,
        )

        thread = TranscriptThread(task)
        results = run_thread_with_timeout(thread, timeout_ms=5000)

        assert results["error"] is not None, "Expected error for empty path"
