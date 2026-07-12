"""输出命名 + 有效高度的纯逻辑单测。"""

from videocaptioner.core.synthesis.models import EncodeSettings, MediaProbe
from videocaptioner.core.synthesis.naming import build_output_name


def test_name_x264_cq():
    s = EncodeSettings()  # x264 / cq 23 / mp4
    assert build_output_name("clip", s, 1080) == "【视频合成】clip_1080p_x264_h264_23Q.mp4"


def test_name_nvenc_abr():
    s = EncodeSettings(video_encoder="h264_nvenc", encode_mode="abr", bitrate_kbps=4000)
    assert build_output_name("clip", s, 720) == "【视频合成】clip_720p_nvenc_h264_4000k.mp4"


def test_name_svt_av1_mkv():
    s = EncodeSettings(video_encoder="svt_av1", quality=32, container="mkv")
    assert build_output_name("clip", s, 1080) == "【视频合成】clip_1080p_svt_av1_32Q.mkv"


def test_name_copy():
    s = EncodeSettings(video_encoder="copy")
    assert build_output_name("clip", s, 1080) == "【视频合成】clip_1080p_copy.mp4"


def test_name_custom_encoder():
    s = EncodeSettings(video_encoder="weird-enc.v2", quality=20)
    assert build_output_name("clip", s, 1080) == "【视频合成】clip_1080p_weirdencv2_custom_20Q.mp4"


def test_name_unknown_height_uses_src():
    s = EncodeSettings()
    assert build_output_name("clip", s, None) == "【视频合成】clip_src_x264_h264_23Q.mp4"


def test_effective_height_no_upscale():
    p = MediaProbe(width=1280, height=720, has_video=True)
    assert p.effective_height(1080) == 720   # 不放大
    assert p.effective_height(480) == 480    # 下缩
    assert p.effective_height(None) == 720   # 与源相同
    assert MediaProbe(height=0).effective_height(1080) is None  # 未知
