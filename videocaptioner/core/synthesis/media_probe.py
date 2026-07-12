"""源媒体探测（ffprobe -of json，失败回退现有正则；见方案 §3.1、§16.1）。

拿宽高比/帧率/色彩标签/像素位深/音频编码/时长，供分辨率、色彩透传、命名使用。
"""

from __future__ import annotations

import json
import os
import subprocess
from fractions import Fraction
from typing import Optional

from videocaptioner.core.utils.logger import setup_logger

from .ffmpeg_env import get_ffprobe_path
from .models import MediaProbe

logger = setup_logger("synthesis.media_probe")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _parse_fps(rate: str) -> float:
    """'30000/1001' -> 29.97。"""
    try:
        if "/" in rate:
            f = Fraction(rate)
            return float(f) if f else 0.0
        return float(rate)
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe(path: str, source: str = "default", custom_dir: Optional[str] = None) -> MediaProbe:
    """探测媒体信息；ffprobe 不可用或失败时回退 get_video_info 正则。"""
    ffprobe = get_ffprobe_path(source, custom_dir)
    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error", "-print_format", "json",
                "-show_streams", "-show_format", path,
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=_NO_WINDOW,
        )
        if result.returncode == 0 and result.stdout:
            return _from_json(json.loads(result.stdout))
        logger.debug(f"ffprobe returned {result.returncode}; falling back to regex probe")
    except (OSError, ValueError, json.JSONDecodeError) as e:
        logger.debug(f"ffprobe failed ({e}); falling back to regex probe")

    return _fallback(path)


def _from_json(data: dict) -> MediaProbe:
    m = MediaProbe()
    for s in data.get("streams", []):
        kind = s.get("codec_type")
        if kind == "video" and not m.has_video:
            m.has_video = True
            m.width = int(s.get("width") or 0)
            m.height = int(s.get("height") or 0)
            m.fps = _parse_fps(s.get("avg_frame_rate") or s.get("r_frame_rate") or "0")
            m.pix_fmt = s.get("pix_fmt") or ""
            m.color_range = s.get("color_range") or ""
            m.color_primaries = s.get("color_primaries") or ""
            m.color_transfer = s.get("color_transfer") or ""
            m.color_space = s.get("color_space") or ""
            m.video_codec = s.get("codec_name") or ""
        elif kind == "audio" and not m.has_audio:
            m.has_audio = True
            m.audio_codec = s.get("codec_name") or ""
            m.audio_sample_rate = int(s.get("sample_rate") or 0)
    fmt = data.get("format", {})
    try:
        m.duration_seconds = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        m.duration_seconds = 0.0
    m.color_tags = {
        "range": m.color_range,
        "primaries": m.color_primaries,
        "transfer": m.color_transfer,
        "space": m.color_space,
    }
    return m


def _fallback(path: str) -> MediaProbe:
    """ffprobe 不可用时用现有 ffmpeg-stderr 正则探测（信息较少）。"""
    from videocaptioner.core.utils.video_utils import get_video_info

    info = get_video_info(path)
    if info is None:
        return MediaProbe()
    return MediaProbe(
        width=info.width,
        height=info.height,
        fps=info.fps,
        duration_seconds=info.duration_seconds,
        video_codec=info.video_codec,
        audio_codec=info.audio_codec,
        audio_sample_rate=info.audio_sampling_rate,
        has_video=info.width > 0,
        has_audio=bool(info.audio_streams),
    )
