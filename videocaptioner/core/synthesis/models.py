"""视频合成编码的结构化数据模型。

`EncodeSettings` 是编码配置的唯一事实源（见 ADR 0007）；
`MediaProbe` 是源媒体探测结果（见 §3.1）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

EncodeMode = Literal["cq", "abr"]
FfmpegSource = Literal["default", "custom"]
Container = Literal["mp4", "mkv"]


@dataclass
class EncodeSettings:
    """结构化编码设置（唯一事实源）。

    默认值对应"裸调用即可跑通"的默认栈（见方案 §11）：
    x264 / H.264 / mp4 / 固定品质 23 / 源分辨率不放大 / 源帧率 VFR / 直通音频。
    """

    # 视频编码器（目录键，如 'x264'/'hevc_nvenc'/'copy'/自定义原名）
    video_encoder: str = "x264"
    encode_mode: EncodeMode = "cq"
    quality: int = 23  # 固定品质数值（按编码器原生刻度）
    bitrate_kbps: int = 4000  # 平均码率

    two_pass: bool = False  # 仅 CPU 编码器
    turbo_first_pass: bool = True  # 仅 CPU 多阶段编码首遍

    enc_preset: Optional[str] = None  # None=编码器默认
    enc_tune: Optional[str] = None
    enc_profile: Optional[str] = None
    enc_level: Optional[str] = None
    fast_decode: bool = False  # 仅 x264/x265

    target_height: Optional[int] = None  # None=与源相同、不放大
    fps: Optional[float] = None  # None=与源相同
    vfr: bool = True

    audio_encoder: str = "copy"  # 'copy'（默认）/'aac'/'libopus'/...
    audio_bitrate_kbps: int = 192

    container: Container = "mp4"
    faststart: bool = True
    keep_metadata: bool = True
    preserve_color: bool = True
    start_zero: bool = True

    extra_args: str = ""  # 追加参数（自定义 ffmpeg 参数框）
    raw_command: Optional[str] = None  # 逐字执行（CLI/预览）

    ffmpeg_source: FfmpegSource = "default"

    @property
    def is_copy(self) -> bool:
        """视频直通（不重编码）。硬烧录时该编码器不可选。"""
        return self.video_encoder == "copy"


@dataclass
class MediaProbe:
    """源媒体探测结果（ffprobe -of json，失败回退正则）。"""

    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration_seconds: float = 0.0
    pix_fmt: str = ""
    color_range: str = ""
    color_primaries: str = ""
    color_transfer: str = ""
    color_space: str = ""
    video_codec: str = ""
    audio_codec: str = ""
    audio_sample_rate: int = 0
    has_video: bool = False
    has_audio: bool = False
    color_tags: dict = field(default_factory=dict)

    def effective_height(self, target_height: Optional[int]) -> Optional[int]:
        """有效输出高度 = min(目标或源, 源)；不放大（见 §16.1）。

        源高度未知时返回 None（命名用 'src' 兜底）。
        """
        if not self.height:
            return None
        if target_height is None:
            return self.height
        return min(target_height, self.height)
