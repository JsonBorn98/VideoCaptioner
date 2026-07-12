"""集中 ffmpeg 命令构建器（唯一事实源 -> argv；见 ADR 0007、方案 §7/§16）。

被软/copy、ASS 硬烧、圆角最终编码共用。纯函数：给定输入/字幕滤镜/设置/探测，
产出 argv，不做 I/O。字幕滤镜与 I/O 路径由调用方传入（系统托管）。
"""

from __future__ import annotations

import os
import shlex
from typing import Optional

from . import encoder_catalog as cat
from .models import EncodeSettings, MediaProbe

# 音频编码器键 -> ffmpeg 编码器名（AAC 用原生 aac，非 fdk，见 §10.2）
_AUDIO_FFMPEG = {
    "aac": "aac",
    "opus": "libopus",
    "libopus": "libopus",
    "ac3": "ac3",
    "mp3": "libmp3lame",
    "libmp3lame": "libmp3lame",
    "flac": "flac",
}
# 解码侧硬件加速 api（不加 -hwaccel_output_format，帧留系统内存供 ass 滤镜，见 §16.3）
_DECODE_HWACCEL = {"nvenc": "cuda", "qsv": "qsv", "amf": "auto"}


def build_video_args(settings: EncodeSettings, probe: Optional[MediaProbe]) -> list[str]:
    """视频编码器 + 质量 + 预设/微调/配置/级别 + 像素格式。"""
    if settings.is_copy:
        return ["-c:v", "copy"]

    spec = cat.get_encoder_spec(settings.video_encoder)
    if spec is None:
        # 自定义编码器：CQ 不可推导，质量经 extra_args/raw 提供（见 §16.11）
        args = ["-c:v", settings.video_encoder]
        if settings.encode_mode == "abr":
            args += ["-b:v", f"{settings.bitrate_kbps}k"]
        return args

    args = ["-c:v", spec.ffmpeg_name]
    args += cat.render_quality_args(spec, settings)
    args += cat.render_preset_args(spec, settings)
    args += cat.render_tune_args(spec, settings)
    args += cat.render_profile_level_args(spec, settings)
    if settings.preserve_color and probe and probe.pix_fmt:
        args += ["-pix_fmt", probe.pix_fmt]
    return args


def build_audio_args(settings: EncodeSettings) -> list[str]:
    if settings.audio_encoder == "copy":
        return ["-c:a", "copy"]
    name = _AUDIO_FFMPEG.get(settings.audio_encoder, settings.audio_encoder)
    args = ["-c:a", name]
    if name != "flac":  # 无损不设码率
        args += ["-b:a", f"{settings.audio_bitrate_kbps}k"]
    return args


def build_color_args(settings: EncodeSettings, probe: Optional[MediaProbe]) -> list[str]:
    """色彩标签透传（copy 时跳过，见 §16.10）。"""
    if settings.is_copy or not settings.preserve_color or probe is None:
        return []
    args: list[str] = []
    if probe.color_range:
        args += ["-color_range", probe.color_range]
    if probe.color_primaries:
        args += ["-color_primaries", probe.color_primaries]
    if probe.color_transfer:
        args += ["-color_trc", probe.color_transfer]
    if probe.color_space:
        args += ["-colorspace", probe.color_space]
    return args


def build_misc_args(settings: EncodeSettings) -> list[str]:
    """faststart / 元数据 / 帧率+VFR / 起始归零（输出侧）。"""
    args: list[str] = []
    if settings.faststart and settings.container in ("mp4", "mov"):
        args += ["-movflags", "+faststart"]
    if settings.keep_metadata:
        args += ["-map_metadata", "0", "-map_chapters", "0"]
    if settings.fps:
        args += ["-r", str(settings.fps)]
    if settings.vfr:
        args += ["-fps_mode", "vfr"]
    if settings.start_zero:
        args += ["-avoid_negative_ts", "make_zero"]
    return args


def _input_opts(settings: EncodeSettings, has_filter: bool) -> list[str]:
    """输入侧选项：硬件解码加速 + genpts（配合起始归零）。"""
    opts: list[str] = []
    spec = cat.get_encoder_spec(settings.video_encoder)
    if spec is not None and spec.is_hardware:
        api = _DECODE_HWACCEL.get(spec.backend)
        if api:
            # 存在字幕滤镜时不加 -hwaccel_output_format，帧落系统内存（见 §16.3）
            opts += ["-hwaccel", api]
    if settings.start_zero:
        opts += ["-fflags", "+genpts"]
    return opts


def build_ffmpeg_command(
    *,
    ffmpeg: str,
    input_path: str,
    output_path: str,
    video_filter: Optional[str],
    settings: EncodeSettings,
    probe: Optional[MediaProbe],
) -> list[str]:
    """组装完整 ffmpeg 命令（单遍 / 2-pass 的 pass2）。"""
    cmd = [ffmpeg, "-y"]
    cmd += _input_opts(settings, bool(video_filter))
    cmd += ["-i", input_path]
    if video_filter:
        cmd += ["-vf", video_filter]
    cmd += build_video_args(settings, probe)
    cmd += build_color_args(settings, probe)
    cmd += build_audio_args(settings)
    cmd += build_misc_args(settings)
    if settings.extra_args.strip():
        cmd += shlex.split(settings.extra_args)
    cmd += [output_path]
    return cmd


def supports_two_pass(settings: EncodeSettings) -> bool:
    """2-pass 仅对 CPU 编码器 + 平均码率模式可用（见 §7）。"""
    if settings.encode_mode != "abr" or not settings.two_pass:
        return False
    spec = cat.get_encoder_spec(settings.video_encoder)
    return spec is not None and spec.supports_two_pass


def build_two_pass_commands(
    *,
    ffmpeg: str,
    input_path: str,
    output_path: str,
    video_filter: Optional[str],
    settings: EncodeSettings,
    probe: Optional[MediaProbe],
    passlog: str,
) -> tuple[list[str], list[str]]:
    """返回 (pass1, pass2)。pass1 仍带 -vf 字幕滤镜使码率统计与成片一致（见 §16.8）。"""
    spec = cat.get_encoder_spec(settings.video_encoder)
    enc = spec.ffmpeg_name if spec else settings.video_encoder

    pass1 = [ffmpeg, "-y"]
    pass1 += _input_opts(settings, bool(video_filter))
    pass1 += ["-i", input_path]
    if video_filter:
        pass1 += ["-vf", video_filter]
    pass1 += ["-c:v", enc, "-b:v", f"{settings.bitrate_kbps}k"]
    pass1 += cat.render_preset_args(spec, settings) if spec else []
    pass1 += ["-pass", "1", "-passlogfile", passlog, "-an", "-f", "null", os.devnull]

    pass2 = build_ffmpeg_command(
        ffmpeg=ffmpeg, input_path=input_path, output_path=output_path,
        video_filter=video_filter, settings=settings, probe=probe,
    )
    # 在输出路径前插入 -pass 2 -passlogfile
    pass2 = pass2[:-1] + ["-pass", "2", "-passlogfile", passlog, pass2[-1]]
    return pass1, pass2
