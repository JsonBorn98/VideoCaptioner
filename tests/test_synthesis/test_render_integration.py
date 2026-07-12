"""端到端集成：真实 ffmpeg 经新引擎（EncodeSettings -> 命令构建器）烧录 ASS。

需要 ffmpeg/ffprobe；不可用时整文件跳过。
"""

import shutil
import subprocess

import pytest

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.entities import SubtitleLayoutEnum
from videocaptioner.core.subtitle import ass_renderer
from videocaptioner.core.synthesis import media_probe
from videocaptioner.core.synthesis.models import EncodeSettings

pytestmark = pytest.mark.integration

_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
_SKIP = pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not available")

MINIMAL_ASS_STYLE = """[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,40,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,40,1
Style: Secondary,Arial,32,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,8,10,10,40,1
"""


@pytest.fixture(autouse=True)
def use_qapp():
    """Override conftest use_qapp — these tests don't touch Qt."""
    yield


def _make_source(path, size="640x360"):
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"testsrc=size={size}:rate=10:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-pix_fmt", "yuv420p", "-shortest", str(path),
        ],
        check=True, capture_output=True,
    )


def _asr():
    return ASRData([ASRDataSeg("hello world", 0, 900)])


@_SKIP
def test_render_ass_via_new_engine_default(tmp_path):
    src = tmp_path / "src.mp4"
    out = tmp_path / "out.mp4"
    _make_source(src)

    settings = EncodeSettings(
        video_encoder="x264", encode_mode="cq", quality=30,
        enc_preset="ultrafast", audio_encoder="copy", container="mp4",
    )
    ass_renderer.render_ass_video(
        video_path=str(src),
        asr_data=_asr(),
        output_path=str(out),
        style_str=MINIMAL_ASS_STYLE,
        layout=SubtitleLayoutEnum.ONLY_ORIGINAL,
        encode_settings=settings,
    )

    assert out.exists() and out.stat().st_size > 0
    p = media_probe.probe(str(out))
    assert p.has_video and p.height == 360  # 与源相同、未缩放
    assert p.video_codec == "h264"


@_SKIP
def test_render_ass_via_new_engine_downscale(tmp_path):
    src = tmp_path / "src720.mp4"
    out = tmp_path / "out360.mp4"
    _make_source(src, size="1280x720")

    settings = EncodeSettings(
        video_encoder="x264", encode_mode="cq", quality=30,
        enc_preset="ultrafast", target_height=360, container="mp4",
    )
    ass_renderer.render_ass_video(
        video_path=str(src),
        asr_data=_asr(),
        output_path=str(out),
        style_str=MINIMAL_ASS_STYLE,
        layout=SubtitleLayoutEnum.ONLY_ORIGINAL,
        encode_settings=settings,
    )

    assert out.exists()
    p = media_probe.probe(str(out))
    assert p.height == 360  # 下缩到目标高度
    assert p.width == 640   # 宽按 16:9 自适应且为偶数


@_SKIP
def test_render_ass_via_new_engine_two_pass(tmp_path):
    """真实执行 CPU 平均码率 2-pass 路径（build_two_pass_commands + run_two_pass）。"""
    src = tmp_path / "src2p.mp4"
    out = tmp_path / "out2p.mp4"
    _make_source(src)

    settings = EncodeSettings(
        video_encoder="x264", encode_mode="abr", bitrate_kbps=500,
        two_pass=True, enc_preset="ultrafast", container="mp4",
    )
    ass_renderer.render_ass_video(
        video_path=str(src),
        asr_data=_asr(),
        output_path=str(out),
        style_str=MINIMAL_ASS_STYLE,
        layout=SubtitleLayoutEnum.ONLY_ORIGINAL,
        encode_settings=settings,
    )

    assert out.exists() and out.stat().st_size > 0
    p = media_probe.probe(str(out))
    assert p.has_video and p.height == 360 and p.video_codec == "h264"
