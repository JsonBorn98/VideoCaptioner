"""软字幕容器感知编码：mp4/mov/m4v -> mov_text，mkv -> srt。

软字幕路径经 runner.run_encode 执行（支持取消/日志），故在 runner 层捕获命令。
"""

from pathlib import Path


def _capture_ffmpeg(monkeypatch):
    calls = []

    def fake_run_encode(cmd, progress_callback=None, total_duration=None, control=None):
        calls.append(list(cmd))

    monkeypatch.setattr(
        "videocaptioner.core.synthesis.runner.run_encode", fake_run_encode
    )
    return calls


def _make_subtitle_file(tmp_path: Path, suffix: str = ".srt") -> Path:
    subtitle_path = tmp_path / f"input{suffix}"
    subtitle_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
    return subtitle_path


def _run_soft(tmp_path, output_name: str):
    from videocaptioner.core.utils import video_utils

    input_video = tmp_path / "input.mp4"
    input_video.write_bytes(b"fake video")
    subtitle_file = _make_subtitle_file(tmp_path)
    video_utils.add_subtitles(
        input_file=str(input_video),
        subtitle_file=str(subtitle_file),
        output=str(tmp_path / output_name),
        soft_subtitle=True,
    )


def test_soft_subtitle_mp4_uses_mov_text(monkeypatch, tmp_path):
    calls = _capture_ffmpeg(monkeypatch)
    _run_soft(tmp_path, "output.mp4")
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[cmd.index("-c:s") + 1] == "mov_text"


def test_soft_subtitle_mkv_uses_srt(monkeypatch, tmp_path):
    calls = _capture_ffmpeg(monkeypatch)
    _run_soft(tmp_path, "output.mkv")
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[cmd.index("-c:s") + 1] == "srt"
