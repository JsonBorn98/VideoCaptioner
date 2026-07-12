"""视频合成命令构建器 + 编码器目录 + 模型的纯逻辑单测。"""

import os

from videocaptioner.core.synthesis import command_builder as cb
from videocaptioner.core.synthesis import encoder_catalog as cat
from videocaptioner.core.synthesis.models import EncodeSettings, MediaProbe

FF = "ffmpeg"
FILTER = "ass='sub.ass':fontsdir='fonts'"


def _probe():
    return MediaProbe(
        width=1920, height=1080, fps=25.0, duration_seconds=60.0,
        pix_fmt="yuv420p", color_range="tv", color_primaries="bt709",
        color_transfer="bt709", color_space="bt709",
        video_codec="h264", audio_codec="aac", has_video=True, has_audio=True,
    )


# ---- encoder catalog ----

def test_catalog_keys_present():
    keys = cat.available_encoder_keys()
    for k in ("x264", "x265", "svt_av1", "aom_av1", "h264_nvenc", "hevc_qsv", "av1_amf"):
        assert k in keys


def test_quality_args_native_scales():
    x264 = cat.get_encoder_spec("x264")
    assert cat.render_quality_args(x264, EncodeSettings(quality=23)) == ["-crf", "23"]
    aom = cat.get_encoder_spec("aom_av1")
    assert cat.render_quality_args(aom, EncodeSettings(video_encoder="aom_av1", quality=30)) == [
        "-crf", "30", "-b:v", "0",
    ]
    nv = cat.get_encoder_spec("h264_nvenc")
    assert cat.render_quality_args(nv, EncodeSettings(video_encoder="h264_nvenc", quality=24)) == [
        "-rc", "vbr", "-cq", "24",
    ]
    qsv = cat.get_encoder_spec("hevc_qsv")
    assert cat.render_quality_args(qsv, EncodeSettings(video_encoder="hevc_qsv", quality=26)) == [
        "-global_quality", "26",
    ]
    amf = cat.get_encoder_spec("h264_amf")
    assert cat.render_quality_args(amf, EncodeSettings(video_encoder="h264_amf", quality=22)) == [
        "-rc", "cqp", "-qp_i", "22", "-qp_p", "22", "-qp_b", "22",
    ]


def test_quality_args_abr():
    x264 = cat.get_encoder_spec("x264")
    s = EncodeSettings(encode_mode="abr", bitrate_kbps=5000)
    assert cat.render_quality_args(x264, s) == ["-b:v", "5000k"]


def test_tune_and_fastdecode_combine_for_cpu():
    x264 = cat.get_encoder_spec("x264")
    s = EncodeSettings(enc_tune="film", fast_decode=True)
    assert cat.render_tune_args(x264, s) == ["-tune", "film,fastdecode"]
    # hardware only takes first tune
    nv = cat.get_encoder_spec("h264_nvenc")
    s2 = EncodeSettings(video_encoder="h264_nvenc", enc_tune="hq", fast_decode=True)
    assert cat.render_tune_args(nv, s2) == ["-tune", "hq"]


def test_x265_level_uses_x265_params():
    x265 = cat.get_encoder_spec("x265")
    s = EncodeSettings(video_encoder="x265", enc_level="4.1")
    assert cat.render_profile_level_args(x265, s) == ["-x265-params", "level-idc=4.1"]
    x264 = cat.get_encoder_spec("x264")
    s2 = EncodeSettings(enc_level="4.1")
    assert cat.render_profile_level_args(x264, s2) == ["-level", "4.1"]


# ---- command builder ----

def test_default_x264_hard_burn():
    cmd = cb.build_ffmpeg_command(
        ffmpeg=FF, input_path="in.mp4", output_path="out.mp4",
        video_filter=FILTER, settings=EncodeSettings(), probe=_probe(),
    )
    assert cmd[0] == FF and cmd[-1] == "out.mp4"
    assert "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "libx264"
    assert ["-crf", "23"] == [cmd[cmd.index("-crf")], cmd[cmd.index("-crf") + 1]]
    assert cmd[cmd.index("-preset") + 1] == "medium"
    assert cmd[cmd.index("-vf") + 1] == FILTER
    assert "+faststart" in cmd and "make_zero" in cmd
    assert cmd[cmd.index("-fps_mode") + 1] == "vfr"
    # color passthrough from probe
    assert cmd[cmd.index("-color_primaries") + 1] == "bt709"
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"


def test_copy_skips_quality_and_color():
    s = EncodeSettings(video_encoder="copy")
    cmd = cb.build_ffmpeg_command(
        ffmpeg=FF, input_path="in.mp4", output_path="out.mp4",
        video_filter=None, settings=s, probe=_probe(),
    )
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    assert "-crf" not in cmd
    assert "-color_primaries" not in cmd
    assert "-pix_fmt" not in cmd
    assert cmd[cmd.index("-c:a") + 1] == "copy"


def test_nvenc_adds_decode_hwaccel():
    s = EncodeSettings(video_encoder="h264_nvenc")
    cmd = cb.build_ffmpeg_command(
        ffmpeg=FF, input_path="in.mp4", output_path="out.mp4",
        video_filter=FILTER, settings=s, probe=_probe(),
    )
    assert cmd[cmd.index("-hwaccel") + 1] == "cuda"
    # no full-GPU pipeline: ass filter needs system-memory frames
    assert "-hwaccel_output_format" not in cmd
    assert cmd[cmd.index("-c:v") + 1] == "h264_nvenc"


def test_audio_reencode():
    s = EncodeSettings(audio_encoder="aac", audio_bitrate_kbps=192)
    cmd = cb.build_ffmpeg_command(
        ffmpeg=FF, input_path="in.mp4", output_path="out.mp4",
        video_filter=None, settings=s, probe=_probe(),
    )
    assert cmd[cmd.index("-c:a") + 1] == "aac"
    assert cmd[cmd.index("-b:a") + 1] == "192k"
    s2 = EncodeSettings(audio_encoder="opus")
    cmd2 = cb.build_ffmpeg_command(
        ffmpeg=FF, input_path="in.mp4", output_path="out.mp4",
        video_filter=None, settings=s2, probe=None,
    )
    assert cmd2[cmd2.index("-c:a") + 1] == "libopus"


def test_custom_encoder_no_crf():
    s = EncodeSettings(video_encoder="my_special_enc")
    cmd = cb.build_ffmpeg_command(
        ffmpeg=FF, input_path="in.mp4", output_path="out.mp4",
        video_filter=None, settings=s, probe=_probe(),
    )
    assert cmd[cmd.index("-c:v") + 1] == "my_special_enc"
    assert "-crf" not in cmd


def test_extra_args_appended():
    s = EncodeSettings(extra_args="-spatial-aq 1 -rc-lookahead 32")
    cmd = cb.build_ffmpeg_command(
        ffmpeg=FF, input_path="in.mp4", output_path="out.mp4",
        video_filter=None, settings=s, probe=None,
    )
    assert cmd[cmd.index("-spatial-aq") + 1] == "1"
    assert cmd[cmd.index("-rc-lookahead") + 1] == "32"
    assert cmd.index("-spatial-aq") < cmd.index("out.mp4")


def test_two_pass_only_cpu_abr():
    assert cb.supports_two_pass(EncodeSettings(encode_mode="abr", two_pass=True))
    assert not cb.supports_two_pass(EncodeSettings(encode_mode="cq", two_pass=True))
    assert not cb.supports_two_pass(
        EncodeSettings(video_encoder="h264_nvenc", encode_mode="abr", two_pass=True)
    )


def test_two_pass_commands():
    s = EncodeSettings(encode_mode="abr", bitrate_kbps=6000, two_pass=True)
    p1, p2 = cb.build_two_pass_commands(
        ffmpeg=FF, input_path="in.mp4", output_path="out.mp4",
        video_filter=FILTER, settings=s, probe=_probe(), passlog="log",
    )
    # pass1 keeps the subtitle filter, discards audio, writes to null
    assert p1[p1.index("-vf") + 1] == FILTER
    assert p1[p1.index("-pass") + 1] == "1"
    assert "-an" in p1 and p1[-1] == os.devnull
    # pass2 produces the real output
    assert p2[p2.index("-pass") + 1] == "2"
    assert p2[-1] == "out.mp4"
