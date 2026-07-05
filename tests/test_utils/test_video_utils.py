from pathlib import Path
from types import SimpleNamespace

from videocaptioner.core.utils import video_utils


def _capture_ffmpeg(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        Path(cmd[-1]).write_bytes(b"fake wav")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(video_utils.subprocess, "run", fake_run)
    return calls


def test_video2audio_default_command_has_no_loudnorm(monkeypatch, tmp_path):
    calls = _capture_ffmpeg(monkeypatch)
    output = tmp_path / "source.wav"

    assert video_utils.video2audio("input.mp4", output=str(output))

    cmd, _ = calls[0]
    assert "-af" not in cmd
    assert cmd[cmd.index("-ac") + 1] == "1"
    assert cmd[cmd.index("-ar") + 1] == "16000"


def test_video2audio_loudnorm_adds_two_pass_filter(monkeypatch, tmp_path):
    calls = _capture_ffmpeg(monkeypatch)
    output = tmp_path / "source.wav"

    assert video_utils.video2audio("input.mp4", output=str(output), loudnorm=True)

    cmd, _ = calls[0]
    assert cmd[cmd.index("-af") + 1] == (
        f"{video_utils.LOUDNORM_FILTER},{video_utils.LOUDNORM_FILTER}"
    )
    assert cmd.index("-af") < cmd.index("-ac")
    assert cmd[cmd.index("-ac") + 1] == "1"
    assert cmd[cmd.index("-ar") + 1] == "16000"
