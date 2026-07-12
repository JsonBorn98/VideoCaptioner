from pathlib import Path
from types import SimpleNamespace

from videocaptioner.core.utils import video_utils


def _capture_ffmpeg(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(video_utils.subprocess, "run", fake_run)
    return calls


def _make_subtitle_file(tmp_path: Path, suffix: str = ".srt") -> Path:
    subtitle_path = tmp_path / f"input{suffix}"
    subtitle_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
    return subtitle_path


def test_soft_subtitle_mp4_uses_mov_text(monkeypatch, tmp_path):
    calls = _capture_ffmpeg(monkeypatch)

    input_video = tmp_path / "input.mp4"
    input_video.write_bytes(b"fake video")
    subtitle_file = _make_subtitle_file(tmp_path)
    output = tmp_path / "output.mp4"

    video_utils.add_subtitles(
        input_file=str(input_video),
        subtitle_file=str(subtitle_file),
        output=str(output),
        soft_subtitle=True,
    )

    assert len(calls) == 1
    cmd, _ = calls[0]
    assert cmd[cmd.index("-c:s") + 1] == "mov_text"


def test_soft_subtitle_mkv_uses_srt(monkeypatch, tmp_path):
    calls = _capture_ffmpeg(monkeypatch)

    input_video = tmp_path / "input.mp4"
    input_video.write_bytes(b"fake video")
    subtitle_file = _make_subtitle_file(tmp_path)
    output = tmp_path / "output.mkv"

    video_utils.add_subtitles(
        input_file=str(input_video),
        subtitle_file=str(subtitle_file),
        output=str(output),
        soft_subtitle=True,
    )

    assert len(calls) == 1
    cmd, _ = calls[0]
    assert cmd[cmd.index("-c:s") + 1] == "srt"
