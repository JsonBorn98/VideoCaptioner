"""Resolve the displayed side used by reading-speed optimization."""

from __future__ import annotations

from typing import Literal

from ..asr.asr_data import ASRDataSeg
from ..entities import SubtitleLayoutEnum

PrimarySide = Literal["translate", "original", "layout"]


def resolve_primary_text(
    segment: ASRDataSeg,
    layout: SubtitleLayoutEnum,
    primary_side: PrimarySide = "translate",
) -> str:
    """Return the main displayed text with a per-cue missing-side fallback."""

    original = segment.text.strip()
    translated = segment.translated_text.strip()
    if primary_side == "original":
        return original or translated
    if primary_side == "translate":
        return translated or original
    if layout is SubtitleLayoutEnum.ONLY_ORIGINAL:
        return original or translated
    if layout is SubtitleLayoutEnum.ONLY_TRANSLATE:
        return translated or original
    if layout is SubtitleLayoutEnum.TRANSLATE_ON_TOP:
        return translated or original
    return original or translated


def resolve_reference_text(
    segment: ASRDataSeg,
    layout: SubtitleLayoutEnum,
    primary_side: PrimarySide = "translate",
) -> str:
    """Return the visible reference side, or empty text for a single-side layout."""

    original = segment.text.strip()
    translated = segment.translated_text.strip()
    if layout in (SubtitleLayoutEnum.ONLY_ORIGINAL, SubtitleLayoutEnum.ONLY_TRANSLATE):
        return ""
    if primary_side == "original":
        return translated
    if primary_side == "translate":
        return original
    if layout is SubtitleLayoutEnum.TRANSLATE_ON_TOP:
        return original
    return translated
