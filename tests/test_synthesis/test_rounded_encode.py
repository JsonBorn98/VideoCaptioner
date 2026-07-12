"""圆角背景字幕最终批经集中命令构建器的单测（见方案 §16.2）。

单测覆盖 `build_ffmpeg_command_multi` 纯函数行为；集成测试（标记 `integration`，
无 ffmpeg 时跳过）验证 `render_rounded_video` 传入 `encode_settings` 时真实产出视频。
"""

import shutil
import subprocess

import pytest

from videocaptioner.core.synthesis import command_builder as cb
from videocaptioner.core.synthesis.models import EncodeSettings, MediaProbe

FF = "ffmpeg"
FILTER_COMPLEX = "[0:v][1:v]overlay=0:0:enable='between(t,0,1)'[v1]"


def _probe():
    return MediaProbe(
        width=1920, height=1080, fps=25.0, duration_seconds=60.0,
        pix_fmt="yuv420p", color_range="tv", color_primaries="bt709",
        color_transfer="bt709", color_space="bt709",
        video_codec="h264", audio_codec="aac", has_video=True, has_audio=True,
    )


def test_build_multi_basic_shape():
    cmd = cb.build_ffmpeg_command_multi(
        ffmpeg=FF,
        input_args=["-i", "current.mp4", "-i", "sub_000.png"],
        filter_complex=FILTER_COMPLEX,
        final_map="[v1]",
        output_path="out.mp4",
        settings=EncodeSettings(),
        probe=_probe(),
        extra_output_args=["-t", "12.5"],
    )
    assert cmd[0] == FF and cmd[1] == "-y"
    assert cmd[cmd.index("-filter_complex") + 1] == FILTER_COMPLEX
    # both maps present: final video map + passthrough audio map
    map_indices = [i for i, tok in enumerate(cmd) if tok == "-map"]
    mapped = [cmd[i + 1] for i in map_indices]
    assert "[v1]" in mapped and "0:a?" in mapped
    assert cmd[cmd.index("-t") + 1] == "12.5"
    assert cmd[-1] == "out.mp4"


def test_build_multi_no_color_or_metadata_even_when_requested():
    """圆角是 8-bit SDR 管线：即使 settings 要求保色/保元数据，最终命令也不应带。"""
    s = EncodeSettings(preserve_color=True, keep_metadata=True)
    cmd = cb.build_ffmpeg_command_multi(
        ffmpeg=FF,
        input_args=["-i", "current.mp4", "-i", "sub_000.png"],
        filter_complex=FILTER_COMPLEX,
        final_map="[v1]",
        output_path="out.mp4",
        settings=s,
        probe=_probe(),
    )
    assert not any(tok.startswith("-color_") or tok == "-colorspace" for tok in cmd)
    assert "-map_metadata" not in cmd
    # settings passed in is untouched (function must not mutate caller's dataclass)
    assert s.preserve_color is True
    assert s.keep_metadata is True


def test_build_multi_selects_chosen_encoder():
    s = EncodeSettings(video_encoder="hevc_nvenc", quality=26)
    cmd = cb.build_ffmpeg_command_multi(
        ffmpeg=FF,
        input_args=["-i", "current.mp4", "-i", "sub_000.png"],
        filter_complex=FILTER_COMPLEX,
        final_map="[v1]",
        output_path="out.mp4",
        settings=s,
        probe=None,
    )
    assert cmd[cmd.index("-c:v") + 1] == "hevc_nvenc"


def test_build_multi_misc_args_still_applied():
    """faststart/起始归零等其他 misc 参数不受色彩/元数据强制关闭影响。"""
    s = EncodeSettings(faststart=True, start_zero=True, container="mp4")
    cmd = cb.build_ffmpeg_command_multi(
        ffmpeg=FF,
        input_args=["-i", "current.mp4", "-i", "sub_000.png"],
        filter_complex=FILTER_COMPLEX,
        final_map="[v1]",
        output_path="out.mp4",
        settings=s,
        probe=None,
    )
    assert "+faststart" in cmd
    assert "make_zero" in cmd


def test_build_multi_vfr_flag_fallback():
    cmd = cb.build_ffmpeg_command_multi(
        ffmpeg=FF,
        input_args=["-i", "current.mp4", "-i", "sub_000.png"],
        filter_complex=FILTER_COMPLEX,
        final_map="[v1]",
        output_path="out.mp4",
        settings=EncodeSettings(),
        probe=None,
        vfr_flag="-vsync",
    )
    assert cmd[cmd.index("-vsync") + 1] == "vfr"
    assert "-fps_mode" not in cmd


# ---- integration: real ffmpeg through render_rounded_video ----

pytestmark_integration = pytest.mark.integration
_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
_SKIP = pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not available")


@pytest.fixture(autouse=True)
def use_qapp():
    """Override conftest use_qapp — these tests don't touch Qt."""
    yield


def _make_source(path, size="320x240"):
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"testsrc=size={size}:rate=10:duration=1",
            "-pix_fmt", "yuv420p", str(path),
        ],
        check=True, capture_output=True,
    )


@pytest.mark.integration
@_SKIP
def test_render_rounded_video_via_new_engine(tmp_path):
    from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
    from videocaptioner.core.entities import SubtitleLayoutEnum
    from videocaptioner.core.subtitle.rounded_renderer import render_rounded_video
    from videocaptioner.core.synthesis import media_probe

    src = tmp_path / "src.mp4"
    out = tmp_path / "out.mp4"
    _make_source(src)

    asr_data = ASRData([ASRDataSeg("hello", 0, 900)])
    settings = EncodeSettings(
        video_encoder="x264", encode_mode="cq", quality=30,
        enc_preset="ultrafast", audio_encoder="copy", container="mp4",
    )

    render_rounded_video(
        video_path=str(src),
        asr_data=asr_data,
        output_path=str(out),
        layout=SubtitleLayoutEnum.ONLY_ORIGINAL,
        encode_settings=settings,
    )

    assert out.exists() and out.stat().st_size > 0
    p = media_probe.probe(str(out))
    assert p.has_video and p.video_codec == "h264"
