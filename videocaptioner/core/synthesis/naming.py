"""输出文件命名（见方案 §9、§16.1）。

`【视频合成】{原名}_{高度}p_{编码器}_{编码格式}_{质量}.{容器}`；
直通/软字幕：`【视频合成】{原名}_{高度}p_copy.{容器}`。
GUI 与 CLI 共用。
"""

from __future__ import annotations

import re
from typing import Optional, Union

from .encoder_catalog import get_encoder_spec
from .models import EncodeSettings

PREFIX = "【视频合成】"


def _height_token(effective_height: Optional[Union[int, str]]) -> str:
    """探测得到的有效高度；未知时用 'src' 兜底（见 §16.1）。"""
    if effective_height is None:
        return "src"
    if isinstance(effective_height, str):
        return effective_height
    return f"{effective_height}p"


def _quality_token(settings: EncodeSettings) -> str:
    if settings.encode_mode == "abr":
        return f"{settings.bitrate_kbps}k"
    return f"{settings.quality}Q"


def _sanitize(name: str) -> str:
    """自定义编码器名净化为文件名安全 token。"""
    return re.sub(r"[^A-Za-z0-9]+", "", name) or "custom"


def build_output_name(
    base_stem: str,
    settings: EncodeSettings,
    effective_height: Optional[Union[int, str]] = None,
    container: Optional[str] = None,
) -> str:
    """构造输出文件名（含扩展名，不含目录）。"""
    ext = container or settings.container
    h = _height_token(effective_height)

    if settings.is_copy:
        return f"{PREFIX}{base_stem}_{h}_copy.{ext}"

    spec = get_encoder_spec(settings.video_encoder)
    if spec is None:
        # 自定义（未列出）编码器：编码器=净化名、编码格式=custom（见 §16.11）
        enc, codec = _sanitize(settings.video_encoder), "custom"
    else:
        enc, codec = spec.backend_token, spec.codec_token

    q = _quality_token(settings)
    return f"{PREFIX}{base_stem}_{h}_{enc}_{codec}_{q}.{ext}"
