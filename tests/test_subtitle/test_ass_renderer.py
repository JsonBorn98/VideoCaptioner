"""Tests for ASS subtitle renderer."""

import subprocess
from pathlib import Path

import pytest
from PIL import Image

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.entities import SubtitleLayoutEnum
from videocaptioner.core.subtitle import ass_renderer


@pytest.fixture(autouse=True)
def use_qapp():
    """Override the conftest.py fixture — these tests don't touch Qt."""
    yield


MINIMAL_ASS_STYLE = """[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,40,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,40,1
Style: Secondary,Arial,32,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,8,10,10,40,1
"""


def _make_bg(tmp_path: Path) -> Path:
    bg = tmp_path / "bg.png"
    Image.new("RGB", (320, 180), (0, 0, 0)).save(bg)
    return bg


def test_render_ass_preview_quotes_ffmpeg_filter_paths(monkeypatch, tmp_path):
    """Regression for issue #1090: -vf ass=...:fontsdir=... must be single-quoted.

    Without quotes, FFmpeg parses the path's `/` as the start of a new filter
    option and aborts with `No option name near '/Python312/Lib/...'` for any
    install path containing `/`.
    """
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(ass_renderer.subprocess, "run", fake_run)
    monkeypatch.setattr(ass_renderer, "auto_wrap_ass_file", lambda p, **kw: p)

    ass_renderer.render_ass_preview(
        style_str=MINIMAL_ASS_STYLE,
        preview_text=("hello", None),
        bg_image_path=str(_make_bg(tmp_path)),
    )

    cmd = captured["cmd"]
    vf_index = cmd.index("-vf")
    vf_value = cmd[vf_index + 1]

    assert vf_value.startswith("ass='"), f"ass path is not single-quoted: {vf_value}"
    assert "':fontsdir='" in vf_value, f"fontsdir is not single-quoted: {vf_value}"
    assert vf_value.endswith("'"), f"fontsdir path is not closed: {vf_value}"


def test_get_video_resolution_decodes_ffmpeg_stderr_with_replacement(monkeypatch):
    """Windows must not use the default GBK decoder for FFmpeg stderr."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            cmd,
            1,
            "",
            "Metadata:\n  title: bad byte \ufffd\nStream #0:0: Video: h264, 1920x1080",
        )

    monkeypatch.setattr(ass_renderer.subprocess, "run", fake_run)

    assert ass_renderer._get_video_resolution("video.mp4") == (1920, 1080)
    assert captured["kwargs"]["encoding"] == "utf-8"
    assert captured["kwargs"]["errors"] == "replace"


def test_get_video_resolution_handles_missing_stderr(monkeypatch):
    """Regression guard for reader thread failures returning stderr=None."""

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", None)

    monkeypatch.setattr(ass_renderer.subprocess, "run", fake_run)

    assert ass_renderer._get_video_resolution("video.mp4") == (1920, 1080)


@pytest.mark.parametrize(
    ("video_height", "reference_height", "expected_scale"),
    [
        (1440, 720, 2.0),
        (2160, 1080, 2.0),
    ],
)
def test_render_ass_video_scales_from_reference_height(
    monkeypatch,
    video_height,
    reference_height,
    expected_scale,
):
    captured = {}

    def fake_scale(style_str, scale_factor):
        captured["scale_factor"] = scale_factor
        raise RuntimeError("stop after scale")

    monkeypatch.setattr(
        ass_renderer,
        "_get_video_resolution",
        lambda _: (3840, video_height),
    )
    monkeypatch.setattr(ass_renderer, "_scale_ass_style", fake_scale)

    asr_data = ASRData([ASRDataSeg("hello", 0, 1000)])
    with pytest.raises(RuntimeError, match="stop after scale"):
        ass_renderer.render_ass_video(
            video_path="video.mp4",
            asr_data=asr_data,
            output_path="output.mp4",
            style_str=MINIMAL_ASS_STYLE,
            layout=SubtitleLayoutEnum.ONLY_ORIGINAL,
            reference_height=reference_height,
        )

    assert captured["scale_factor"] == expected_scale
