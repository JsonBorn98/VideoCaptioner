"""编码器能力探测（见 ADR 0008、方案 §10.1/§16.14）。

在策划目录之上判定"某编码器在当前 ffmpeg 核心 + 硬件上能否实际运行"：
- 解析 `ffmpeg -encoders` 得到"编译进核心"的编码器集（便宜、可缓存）；
- 对硬件编码器做一次真实初始化探测（tiny lavfi 编码）判定"运行可用"。
用于 GUI 置灰不可用项，以及"可用性测试"按钮的报告。
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

from videocaptioner.core.utils.logger import setup_logger

from .encoder_catalog import available_encoder_keys, get_encoder_spec
from .ffmpeg_env import get_ffmpeg_path, get_ffprobe_path

logger = setup_logger("synthesis.encoder_probe")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
_ENCODER_LINE = re.compile(r"^\s*[VAS][A-Za-z0-9.]{5}\s+(\S+)")


@dataclass
class EncoderAvailability:
    key: str
    ffmpeg_name: str
    compiled: bool  # 编译进当前核心
    functional: Optional[bool]  # 硬件真实初始化结果；None = 未探测（CPU 无需）
    available: bool  # UI 是否可选
    reason: str = ""  # 不可用原因（用于提示）


@dataclass
class AvailabilityReport:
    ffmpeg_path: str
    ffprobe_path: str
    version: str
    expected_arch: str
    hwaccels: list[str] = field(default_factory=list)
    encoders: dict[str, EncoderAvailability] = field(default_factory=dict)


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", creationflags=_NO_WINDOW,
    )


def _parse_encoders(text: str) -> set[str]:
    """解析 `ffmpeg -encoders` 输出为编码器名集合。"""
    names: set[str] = set()
    started = False
    for line in text.splitlines():
        if not started:
            if line.strip() and set(line.strip()) == {"-"}:
                started = True
            continue
        if m := _ENCODER_LINE.match(line):
            names.add(m.group(1))
    return names


@lru_cache(maxsize=8)
def compiled_encoders(ffmpeg: str) -> frozenset[str]:
    """编译进当前核心的编码器名集合（缓存）。"""
    try:
        result = _run([ffmpeg, "-hide_banner", "-encoders"])
    except OSError:
        return frozenset()
    return frozenset(_parse_encoders(result.stdout or ""))


@lru_cache(maxsize=32)
def functional_probe(ffmpeg: str, ffmpeg_name: str) -> bool:
    """对（视频）编码器做真实初始化：编码一小段 lavfi 源，返回码 0 视为可用。"""
    try:
        result = _run([
            ffmpeg, "-hide_banner", "-f", "lavfi",
            "-i", "testsrc=size=256x256:rate=5:duration=0.2",
            "-c:v", ffmpeg_name, "-f", "null", "-",
        ])
    except OSError:
        return False
    return result.returncode == 0


@lru_cache(maxsize=8)
def _version(ffmpeg: str) -> str:
    try:
        out = _run([ffmpeg, "-version"]).stdout or ""
    except OSError:
        return "unknown"
    return out.splitlines()[0].strip() if out else "unknown"


@lru_cache(maxsize=8)
def _hwaccels(ffmpeg: str) -> tuple[str, ...]:
    try:
        out = _run([ffmpeg, "-hide_banner", "-hwaccels"]).stdout or ""
    except OSError:
        return ()
    lines = [ln.strip() for ln in out.splitlines()[1:] if ln.strip()]
    return tuple(lines)


def clear_probe_cache() -> None:
    """用户替换核心 / 重新测试时清空探测缓存。"""
    compiled_encoders.cache_clear()
    functional_probe.cache_clear()
    _version.cache_clear()
    _hwaccels.cache_clear()


def available_encoders(
    source: str = "default",
    custom_dir: Optional[str] = None,
    probe_hardware: bool = False,
) -> dict[str, EncoderAvailability]:
    """目录中每个编码器的可用性；probe_hardware=True 时对硬件编码器做真实探测。"""
    ffmpeg = get_ffmpeg_path(source, custom_dir)
    compiled = compiled_encoders(ffmpeg)
    result: dict[str, EncoderAvailability] = {}
    for key in available_encoder_keys():
        spec = get_encoder_spec(key)
        if spec is None:
            continue
        is_compiled = spec.ffmpeg_name in compiled
        functional: Optional[bool] = None
        reason = ""
        if not is_compiled:
            reason = "未编译进当前 ffmpeg 核心（可替换更完整的构建）"
        elif spec.is_hardware and probe_hardware:
            functional = functional_probe(ffmpeg, spec.ffmpeg_name)
            if not functional:
                reason = "硬件或驱动不可用"
        available = is_compiled and functional is not False
        result[key] = EncoderAvailability(
            key=key, ffmpeg_name=spec.ffmpeg_name, compiled=is_compiled,
            functional=functional, available=available, reason=reason,
        )
    return result


def run_availability_test(
    source: str = "default", custom_dir: Optional[str] = None
) -> AvailabilityReport:
    """"可用性测试"按钮：版本 + 架构 + hwaccels + 逐编码器可用性（含硬件真实探测）。"""
    ffmpeg = get_ffmpeg_path(source, custom_dir)
    ffprobe = get_ffprobe_path(source, custom_dir)
    return AvailabilityReport(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        version=_version(ffmpeg),
        expected_arch=platform.machine(),
        hwaccels=list(_hwaccels(ffmpeg)),
        encoders=available_encoders(source, custom_dir, probe_hardware=True),
    )
