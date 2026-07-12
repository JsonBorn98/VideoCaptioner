"""策划的编码器目录 + 每编码器的 ffmpeg 参数映射（见 ADR 0008、方案 §12）。

目录是 UI 选项渲染与命令构建的权威来源；某编码器"能否实际运行"另由能力探测
（encoder_probe，后续增量）判定并置灰——目录只描述"若可用则怎么用"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Optional

if TYPE_CHECKING:
    from .models import EncodeSettings

Backend = Literal["cpu", "nvenc", "qsv", "amf"]
# 固定品质参数族：决定 CQ 数值映射到哪些 ffmpeg 标志
QualityKind = Literal["crf", "crf_b0", "nvenc_cq", "qsv_gq", "amf_cqp"]


@dataclass(frozen=True)
class EncoderSpec:
    key: str  # 目录键，也是 EncodeSettings.video_encoder 的值
    ffmpeg_name: str  # -c:v 的实际编码器名
    codec_token: str  # 命名用：h264/h265/av1/vp9
    backend_token: str  # 命名用：x264/x265/svt/aom/nvenc/qsv/amf/vpx
    backend: Backend
    label: str  # UI 显示名，如 "H.264 (NVIDIA NVENC)"
    quality_kind: QualityKind
    quality_default: int
    quality_min: int
    quality_max: int
    supports_two_pass: bool = False
    preset_flag: Optional[str] = None
    presets: tuple[str, ...] = ()
    default_preset: Optional[str] = None
    tunes: tuple[str, ...] = ()
    supports_fastdecode: bool = False
    profiles: tuple[str, ...] = ()
    levels: tuple[str, ...] = ()

    @property
    def is_hardware(self) -> bool:
        return self.backend != "cpu"


_X264_LEVELS = ("3.0", "3.1", "4.0", "4.1", "5.0", "5.1", "6.0", "6.1", "6.2")
_X26X_PRESETS = (
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
)
_QSV_PRESETS = ("veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow")
_NVENC_PRESETS = ("p1", "p2", "p3", "p4", "p5", "p6", "p7")
_AMF_PRESETS = ("speed", "balanced", "quality")


# 目录：键 -> EncoderSpec。开箱可用性由能力探测另行判定（如 svt_av1 essentials 无）。
_CATALOG: dict[str, EncoderSpec] = {
    "x264": EncoderSpec(
        key="x264", ffmpeg_name="libx264", codec_token="h264", backend_token="x264",
        backend="cpu", label="H.264 (x264)",
        quality_kind="crf", quality_default=23, quality_min=0, quality_max=51,
        supports_two_pass=True, preset_flag="-preset", presets=_X26X_PRESETS,
        default_preset="medium",
        tunes=("film", "animation", "grain", "stillimage", "zerolatency", "psnr", "ssim"),
        supports_fastdecode=True,
        profiles=("baseline", "main", "high", "high10", "high422", "high444"),
        levels=_X264_LEVELS,
    ),
    "x265": EncoderSpec(
        key="x265", ffmpeg_name="libx265", codec_token="h265", backend_token="x265",
        backend="cpu", label="H.265 (x265)",
        quality_kind="crf", quality_default=28, quality_min=0, quality_max=51,
        supports_two_pass=True, preset_flag="-preset", presets=_X26X_PRESETS,
        default_preset="medium",
        tunes=("psnr", "ssim", "grain", "zerolatency", "animation"),
        supports_fastdecode=True,
        profiles=("main", "main10", "main12"),
        levels=_X264_LEVELS,
    ),
    "svt_av1": EncoderSpec(
        key="svt_av1", ffmpeg_name="libsvtav1", codec_token="av1", backend_token="svt",
        backend="cpu", label="AV1 (SVT)",
        quality_kind="crf", quality_default=30, quality_min=0, quality_max=63,
        supports_two_pass=False, preset_flag="-preset",
        presets=tuple(str(i) for i in range(14)), default_preset="8",
    ),
    "aom_av1": EncoderSpec(
        key="aom_av1", ffmpeg_name="libaom-av1", codec_token="av1", backend_token="aom",
        backend="cpu", label="AV1 (aom)",
        quality_kind="crf_b0", quality_default=30, quality_min=0, quality_max=63,
        supports_two_pass=True, preset_flag="-cpu-used",
        presets=tuple(str(i) for i in range(9)), default_preset="6",
    ),
    "vp9": EncoderSpec(
        key="vp9", ffmpeg_name="libvpx-vp9", codec_token="vp9", backend_token="vpx",
        backend="cpu", label="VP9 (libvpx)",
        quality_kind="crf_b0", quality_default=31, quality_min=0, quality_max=63,
        supports_two_pass=True, preset_flag="-cpu-used",
        presets=tuple(str(i) for i in range(9)), default_preset="2",
    ),
    "h264_nvenc": EncoderSpec(
        key="h264_nvenc", ffmpeg_name="h264_nvenc", codec_token="h264", backend_token="nvenc",
        backend="nvenc", label="H.264 (NVIDIA NVENC)",
        quality_kind="nvenc_cq", quality_default=23, quality_min=0, quality_max=51,
        preset_flag="-preset", presets=_NVENC_PRESETS, default_preset="p5",
        tunes=("hq", "ll", "ull", "lossless"),
        profiles=("baseline", "main", "high"),
    ),
    "hevc_nvenc": EncoderSpec(
        key="hevc_nvenc", ffmpeg_name="hevc_nvenc", codec_token="h265", backend_token="nvenc",
        backend="nvenc", label="H.265 (NVIDIA NVENC)",
        quality_kind="nvenc_cq", quality_default=26, quality_min=0, quality_max=51,
        preset_flag="-preset", presets=_NVENC_PRESETS, default_preset="p5",
        tunes=("hq", "ll", "ull", "lossless"),
        profiles=("main", "main10"),
    ),
    "av1_nvenc": EncoderSpec(
        key="av1_nvenc", ffmpeg_name="av1_nvenc", codec_token="av1", backend_token="nvenc",
        backend="nvenc", label="AV1 (NVIDIA NVENC)",
        quality_kind="nvenc_cq", quality_default=30, quality_min=0, quality_max=51,
        preset_flag="-preset", presets=_NVENC_PRESETS, default_preset="p5",
        tunes=("hq", "ll", "ull", "lossless"),
    ),
    "h264_qsv": EncoderSpec(
        key="h264_qsv", ffmpeg_name="h264_qsv", codec_token="h264", backend_token="qsv",
        backend="qsv", label="H.264 (Intel QSV)",
        quality_kind="qsv_gq", quality_default=23, quality_min=1, quality_max=51,
        preset_flag="-preset", presets=_QSV_PRESETS, default_preset="medium",
        profiles=("baseline", "main", "high"),
    ),
    "hevc_qsv": EncoderSpec(
        key="hevc_qsv", ffmpeg_name="hevc_qsv", codec_token="h265", backend_token="qsv",
        backend="qsv", label="H.265 (Intel QSV)",
        quality_kind="qsv_gq", quality_default=26, quality_min=1, quality_max=51,
        preset_flag="-preset", presets=_QSV_PRESETS, default_preset="medium",
        profiles=("main", "main10"),
    ),
    "av1_qsv": EncoderSpec(
        key="av1_qsv", ffmpeg_name="av1_qsv", codec_token="av1", backend_token="qsv",
        backend="qsv", label="AV1 (Intel QSV)",
        quality_kind="qsv_gq", quality_default=30, quality_min=1, quality_max=51,
        preset_flag="-preset", presets=_QSV_PRESETS, default_preset="medium",
    ),
    "h264_amf": EncoderSpec(
        key="h264_amf", ffmpeg_name="h264_amf", codec_token="h264", backend_token="amf",
        backend="amf", label="H.264 (AMD AMF)",
        quality_kind="amf_cqp", quality_default=23, quality_min=0, quality_max=51,
        preset_flag="-quality", presets=_AMF_PRESETS, default_preset="balanced",
        profiles=("main", "high"),
    ),
    "hevc_amf": EncoderSpec(
        key="hevc_amf", ffmpeg_name="hevc_amf", codec_token="h265", backend_token="amf",
        backend="amf", label="H.265 (AMD AMF)",
        quality_kind="amf_cqp", quality_default=26, quality_min=0, quality_max=51,
        preset_flag="-quality", presets=_AMF_PRESETS, default_preset="balanced",
    ),
    "av1_amf": EncoderSpec(
        key="av1_amf", ffmpeg_name="av1_amf", codec_token="av1", backend_token="amf",
        backend="amf", label="AV1 (AMD AMF)",
        quality_kind="amf_cqp", quality_default=30, quality_min=0, quality_max=51,
        preset_flag="-quality", presets=_AMF_PRESETS, default_preset="balanced",
    ),
}


def get_encoder_spec(key: str) -> Optional[EncoderSpec]:
    """按目录键取 EncoderSpec；'copy'/未列出（自定义）返回 None。"""
    return _CATALOG.get(key)


def available_encoder_keys() -> list[str]:
    """目录中全部编码器键（不含 copy/自定义）。"""
    return list(_CATALOG.keys())


def render_quality_args(spec: EncoderSpec, settings: "EncodeSettings") -> list[str]:
    """按编码器原生刻度渲染固定品质 / 平均码率参数（见 §12）。"""
    if settings.encode_mode == "abr":
        # 平均码率；2-pass 由上层 two-pass runner 处理，命令层只表达单遍码率
        return ["-b:v", f"{settings.bitrate_kbps}k"]

    q = str(settings.quality)
    kind = spec.quality_kind
    if kind == "crf":
        return ["-crf", q]
    if kind == "crf_b0":
        return ["-crf", q, "-b:v", "0"]
    if kind == "nvenc_cq":
        return ["-rc", "vbr", "-cq", q]
    if kind == "qsv_gq":
        return ["-global_quality", q]
    if kind == "amf_cqp":
        return ["-rc", "cqp", "-qp_i", q, "-qp_p", q, "-qp_b", q]
    return []


def render_preset_args(spec: EncoderSpec, settings: "EncodeSettings") -> list[str]:
    preset = settings.enc_preset or spec.default_preset
    if spec.preset_flag and preset:
        return [spec.preset_flag, preset]
    return []


def render_tune_args(spec: EncoderSpec, settings: "EncodeSettings") -> list[str]:
    """微调 + 快速解码（x264/x265 的 fastdecode 是一个 tune 值，可与另一 tune 叠加）。"""
    tunes: list[str] = []
    if settings.enc_tune:
        tunes.append(settings.enc_tune)
    if settings.fast_decode and spec.supports_fastdecode:
        tunes.append("fastdecode")
    if not tunes:
        return []
    # x264/x265 支持逗号叠加多个 tune；硬件编码器只取第一个
    if spec.backend == "cpu":
        return ["-tune", ",".join(tunes)]
    return ["-tune", tunes[0]]


def render_profile_level_args(spec: EncoderSpec, settings: "EncodeSettings") -> list[str]:
    args: list[str] = []
    if settings.enc_profile:
        args += ["-profile:v", settings.enc_profile]
    if settings.enc_level:
        # x265 的 level 走 -x265-params level-idc=（见 §16.13）
        if spec.ffmpeg_name == "libx265":
            args += ["-x265-params", f"level-idc={settings.enc_level}"]
        else:
            args += ["-level", settings.enc_level]
    return args
