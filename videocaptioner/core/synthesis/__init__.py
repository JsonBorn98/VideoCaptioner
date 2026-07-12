"""视频合成编码核心（引擎层，无 Qt 依赖）。

集中命令构建器 + 编码器目录 + ffmpeg 解析 + ffprobe 探测 + 输出命名。
详见 docs/plans/video-synthesis-encoding-overhaul.md。
"""

from .encoder_catalog import (
    EncoderSpec,
    available_encoder_keys,
    get_encoder_spec,
)
from .ffmpeg_env import get_ffmpeg_path, get_ffprobe_path
from .models import EncodeSettings, MediaProbe
from .naming import build_output_name

__all__ = [
    "EncodeSettings",
    "MediaProbe",
    "EncoderSpec",
    "get_encoder_spec",
    "available_encoder_keys",
    "get_ffmpeg_path",
    "get_ffprobe_path",
    "build_output_name",
]
