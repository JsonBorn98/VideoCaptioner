"""CLI synthesize: encode flags -> EncodeSettings, --print-command, --raw-ffmpeg passthrough."""

from argparse import Namespace
from pathlib import Path

from videocaptioner.cli import exit_codes as EXIT
from videocaptioner.cli.commands import synthesize as syn


def _ns(**kw):
    return Namespace(**kw)


def test_build_encode_settings_defaults():
    es = syn._build_encode_settings(_ns(), "medium")
    assert es.video_encoder == "x264"
    assert es.encode_mode == "cq"
    assert es.quality == 28  # medium tier fallback
    assert es.audio_encoder == "copy"
    assert es.container == "mp4"


def test_build_encode_settings_from_flags():
    es = syn._build_encode_settings(
        _ns(
            video_encoder="hevc_nvenc", encode_mode="abr", bitrate=6000, cq=None,
            height=1080, audio_encoder="aac", audio_bitrate=128, container="mkv",
            enc_preset="p5",
        ),
        "high",
    )
    assert es.video_encoder == "hevc_nvenc"
    assert es.encode_mode == "abr" and es.bitrate_kbps == 6000
    assert es.quality == 23  # high tier fallback (cq not given)
    assert es.target_height == 1080
    assert es.audio_encoder == "aac" and es.audio_bitrate_kbps == 128
    assert es.container == "mkv" and es.enc_preset == "p5"


def test_build_encode_settings_cq_overrides_tier():
    assert syn._build_encode_settings(_ns(cq=30), "medium").quality == 30


def test_raw_ffmpeg_forces_managed_binary(monkeypatch):
    captured = {}

    def fake_run_encode(cmd, progress_callback=None, total_duration=None):
        captured["cmd"] = list(cmd)

    monkeypatch.setattr("videocaptioner.core.synthesis.runner.run_encode", fake_run_encode)
    monkeypatch.setattr("videocaptioner.core.synthesis.get_ffmpeg_path", lambda *a, **k: "/managed/ffmpeg")

    rc = syn._run_raw_ffmpeg(_ns(raw_ffmpeg="ffmpeg -i in.mp4 out.mp4", quiet=True))
    assert rc == EXIT.SUCCESS
    assert captured["cmd"][0] == "/managed/ffmpeg"  # argv[0] forced to managed binary
    assert captured["cmd"][1:] == ["-i", "in.mp4", "out.mp4"]


def test_raw_ffmpeg_rejects_non_ffmpeg(monkeypatch):
    monkeypatch.setattr("videocaptioner.core.synthesis.runner.run_encode", lambda *a, **k: None)
    rc = syn._run_raw_ffmpeg(_ns(raw_ffmpeg="rm -rf /tmp/x", quiet=True))
    assert rc == EXIT.USAGE_ERROR


def test_print_command(monkeypatch, capsys):
    from videocaptioner.core.synthesis.models import MediaProbe

    monkeypatch.setattr(
        "videocaptioner.core.synthesis.media_probe.probe",
        lambda *a, **k: MediaProbe(width=1920, height=1080, has_video=True),
    )
    monkeypatch.setattr("videocaptioner.core.synthesis.get_ffmpeg_path", lambda *a, **k: "ffmpeg")

    es = syn._build_encode_settings(_ns(video_encoder="hevc_nvenc"), "medium")
    rc = syn._print_command(es, Path("v.mp4"), Path("s.ass"), "out.mp4")
    assert rc == EXIT.SUCCESS
    out = capsys.readouterr().out
    assert "hevc_nvenc" in out and "ass=" in out
