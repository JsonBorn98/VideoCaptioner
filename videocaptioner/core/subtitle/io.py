"""Canonical subtitle import, persistence, and delivery export helpers.

Subtitle processing stages exchange :class:`ASRData` in memory and persist SRT
as their canonical user-visible artifact. Other formats are delivery exports;
in particular, imported ASS presentation data is intentionally not retained.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ..asr.asr_data import ASRData, ASRDataSeg
from ..entities import OutputSubtitleFormatEnum, SubtitleLayoutEnum

if TYPE_CHECKING:
    from .style_manager import SubtitleStyle

SubtitleExportFormat = Literal["srt", "vtt", "ass", "json", "txt"]

_LAYOUT_MARKER = re.compile(
    r"^;\s*SubtitleLayout:\s*(.+?)\s*$",
    flags=re.MULTILINE,
)
_STAGE_PREFIX = re.compile(
    r"^(?:(?:【转录字幕】)|(?:【初版字幕】)|(?:【后处理字幕】)|"
    r"(?:【字幕】)|(?:【样式字幕】))+"
)


@dataclass(frozen=True)
class SubtitleImportResult:
    """Normalized subtitle content plus structural interpretation evidence."""

    data: ASRData
    layout: SubtitleLayoutEnum
    confidence: float
    warnings: tuple[str, ...] = ()
    metadata_layout: SubtitleLayoutEnum | None = None


def clone_subtitle_data(data: ASRData) -> ASRData:
    """Return a detached subtitle snapshot suitable for stage hand-off."""

    return ASRData(
        [
            ASRDataSeg(seg.text, seg.start_time, seg.end_time, seg.translated_text)
            for seg in data.segments
        ]
    )


def canonical_stage_path(
    source_path: str | Path,
    stage_prefix: str,
) -> Path:
    """Build a stable canonical SRT path without carrying prior stage prefixes."""

    source = Path(source_path)
    clean_stem = _STAGE_PREFIX.sub("", source.stem)
    return source.with_name(f"【{stage_prefix}】{clean_stem}.srt")


def read_videocaptioner_layout(path: str | Path) -> SubtitleLayoutEnum | None:
    """Read VideoCaptioner's ASS layout marker without importing ASS styling."""

    subtitle_path = Path(path)
    if subtitle_path.suffix.lower() != ".ass":
        return None
    try:
        content = subtitle_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = subtitle_path.read_text(encoding="gbk")
    match = _LAYOUT_MARKER.search(content)
    if not match:
        return None
    token = match.group(1).strip()
    try:
        return SubtitleLayoutEnum[token]
    except KeyError:
        try:
            return SubtitleLayoutEnum(token)
        except ValueError:
            return None


def import_subtitle(
    path: str | Path,
    *,
    layout_hint: SubtitleLayoutEnum | None = None,
) -> SubtitleImportResult:
    """Import a plain-text subtitle into the canonical in-memory model.

    ASS styles, positioning, effects, and reference resolution are deliberately
    discarded. A VideoCaptioner ``SubtitleLayout`` marker remains structural
    evidence and is honored unless the caller supplies an explicit layout.
    """

    subtitle_path = Path(path)
    metadata_layout = read_videocaptioner_layout(subtitle_path)
    resolved_hint = layout_hint or metadata_layout
    data = ASRData.from_subtitle_file(str(subtitle_path), layout=resolved_hint)

    if resolved_hint is not None:
        return SubtitleImportResult(
            data=data,
            layout=resolved_hint,
            confidence=1.0,
            metadata_layout=metadata_layout,
        )

    bilingual = bool(data.segments) and all(
        bool(segment.text.strip() and segment.translated_text.strip())
        for segment in data.segments
    )
    if bilingual:
        return SubtitleImportResult(
            data=data,
            layout=SubtitleLayoutEnum.ORIGINAL_ON_TOP,
            confidence=0.5,
            warnings=("双语字幕缺少可信的上下行角色信息，请确认原文与译文布局",),
            metadata_layout=metadata_layout,
        )
    return SubtitleImportResult(
        data=data,
        layout=SubtitleLayoutEnum.ONLY_ORIGINAL,
        confidence=0.5,
        warnings=("字幕结构识别置信度不足，已按单语字幕处理",),
        metadata_layout=metadata_layout,
    )


def _normalize_export_format(
    value: SubtitleExportFormat | OutputSubtitleFormatEnum | str,
) -> SubtitleExportFormat:
    if isinstance(value, OutputSubtitleFormatEnum):
        value = value.value
    normalized = str(value).lower().lstrip(".")
    if normalized not in {"srt", "vtt", "ass", "json", "txt"}:
        raise ValueError(f"Unsupported subtitle export format: {value}")
    return normalized  # type: ignore[return-value]


def _ass_style_text(style: str | "SubtitleStyle" | None) -> str | None:
    if style is None or isinstance(style, str):
        return style
    return style.to_ass_string()


def export_subtitle_atomic(
    data: ASRData,
    path: str | Path,
    *,
    export_format: SubtitleExportFormat | OutputSubtitleFormatEnum | str | None = None,
    layout: SubtitleLayoutEnum = SubtitleLayoutEnum.ORIGINAL_ON_TOP,
    ass_style: str | "SubtitleStyle" | None = None,
    reference_resolution: tuple[int, int] = (1280, 720),
) -> Path:
    """Atomically serialize a subtitle working snapshot to a delivery file."""

    target = Path(path)
    format_value = _normalize_export_format(export_format or target.suffix)
    target = target.with_suffix(f".{format_value}")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.stem}.", suffix=target.suffix, dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        width, height = reference_resolution
        data.save(
            str(temporary),
            ass_style=_ass_style_text(ass_style),
            layout=layout,
            video_width=width,
            video_height=height,
        )
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


def save_canonical_srt(
    data: ASRData,
    path: str | Path,
    *,
    layout: SubtitleLayoutEnum = SubtitleLayoutEnum.ORIGINAL_ON_TOP,
) -> Path:
    """Atomically save a stage's mandatory canonical SRT artifact."""

    return export_subtitle_atomic(data, Path(path).with_suffix(".srt"), layout=layout)


__all__ = [
    "SubtitleExportFormat",
    "SubtitleImportResult",
    "canonical_stage_path",
    "clone_subtitle_data",
    "export_subtitle_atomic",
    "import_subtitle",
    "read_videocaptioner_layout",
    "save_canonical_srt",
]
