"""Deterministic, reversible structural subtitle candidates.

This module deliberately does not decide whether a candidate is better.  It
creates one minimal transaction at a time; the caller owns M3 comparison,
semantic validation, and commit/rollback.  Input ``ASRData`` and snapshots are
never mutated.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Iterable, Literal, Sequence

from ..asr.asr_data import ASRData, ASRDataSeg
from ..entities import SubtitleLayoutEnum
from .layout import PrimarySide, resolve_primary_text
from .models import CueSnapshot, Lineage, make_stable_id
from .reading_load import GraphemeKind, classify_grapheme, split_graphemes
from .timing_evidence import (
    TimingAnchor,
    TimingEvidenceWindow,
    TimingOperation,
    TimingQualityGrade,
)

StructuralTextSide = Literal["original", "translate", "both"]


class StructuralOperationKind(str, Enum):
    MERGE = "merge"
    SPLIT = "split"
    MIGRATE = "migrate"


@dataclass(frozen=True)
class StructuralOperationRecord:
    """Auditable identity and ancestry for one structural edit."""

    operation_id: str
    kind: StructuralOperationKind
    before_cue_ids: tuple[str, ...]
    after_cue_ids: tuple[str, ...]
    text_side: StructuralTextSide
    before_lineage: tuple[Lineage | None, ...]
    after_lineage: tuple[Lineage | None, ...]


@dataclass(frozen=True)
class StructuralCandidate:
    """A complete before/after snapshot suitable for atomic acceptance."""

    evidence_window_id: str
    before: tuple[CueSnapshot, ...]
    after: tuple[CueSnapshot, ...]
    operations: tuple[StructuralOperationRecord, ...] = ()
    skipped_reason: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.operations)

    def materialize(self, *, accept: bool) -> ASRData:
        """Return a fresh ASRData for commit or rollback."""

        selected = self.after if accept else self.before
        return ASRData(
            [
                ASRDataSeg(
                    text=cue.text,
                    start_time=cue.start_ms,
                    end_time=cue.end_ms,
                    translated_text=cue.translated_text,
                )
                for cue in selected
            ]
        )


def _snapshot_data(data: ASRData) -> tuple[CueSnapshot, ...]:
    return tuple(
        CueSnapshot.from_input(
            index=index,
            start_ms=segment.start_time,
            end_ms=segment.end_time,
            text=segment.text,
            translated_text=segment.translated_text,
        )
        for index, segment in enumerate(data.segments)
    )


def _validate_snapshots(data: ASRData, snapshots: Sequence[CueSnapshot]) -> None:
    if len(data.segments) != len(snapshots):
        raise ValueError("snapshots must describe every ASRData segment")
    for index, (segment, cue) in enumerate(zip(data.segments, snapshots)):
        actual = (segment.start_time, segment.end_time, segment.text, segment.translated_text)
        expected = (cue.start_ms, cue.end_ms, cue.text, cue.translated_text)
        if actual != expected or cue.index != index:
            raise ValueError(f"snapshot {index} does not match ASRData")


def _noop(
    before: tuple[CueSnapshot, ...], evidence: TimingEvidenceWindow, reason: str
) -> StructuralCandidate:
    return StructuralCandidate(evidence.window_id, before, before, skipped_reason=reason)


def _quality_allows_structure(evidence: TimingEvidenceWindow) -> bool:
    return evidence.quality_grade in (
        TimingQualityGrade.MEDIUM,
        TimingQualityGrade.HIGH,
    )


def _generation(cues: Sequence[CueSnapshot]) -> int:
    return max((cue.lineage.generation if cue.lineage else 0 for cue in cues), default=0)


def _derived_cue(
    *,
    parents: Sequence[CueSnapshot],
    operation: StructuralOperationKind,
    ordinal: int,
    index: int,
    start_ms: int,
    end_ms: int,
    text: str,
    translated_text: str,
) -> CueSnapshot:
    payload = {
        "index": index,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "text": text,
        "translated_text": translated_text,
    }
    lineage = Lineage.derive(
        kind="cue",
        parent_ids=[cue.cue_id for cue in parents],
        operation=operation.value,
        ordinal=ordinal,
        payload=payload,
        parent_generation=_generation(parents),
    )
    return CueSnapshot(
        cue_id=lineage.entity_id,
        index=index,
        start_ms=start_ms,
        end_ms=end_ms,
        text=text,
        translated_text=translated_text,
        lineage=lineage,
    )


def _record(
    kind: StructuralOperationKind,
    before: Sequence[CueSnapshot],
    after: Sequence[CueSnapshot],
    text_side: StructuralTextSide,
) -> StructuralOperationRecord:
    before_ids = tuple(cue.cue_id for cue in before)
    after_ids = tuple(cue.cue_id for cue in after)
    operation_id = make_stable_id(
        "structural-operation",
        {
            "kind": kind.value,
            "before": before_ids,
            "after": after_ids,
            "text_side": text_side,
        },
    )
    return StructuralOperationRecord(
        operation_id=operation_id,
        kind=kind,
        before_cue_ids=before_ids,
        after_cue_ids=after_ids,
        text_side=text_side,
        before_lineage=tuple(cue.lineage for cue in before),
        after_lineage=tuple(cue.lineage for cue in after),
    )


def _reindex(cues: Sequence[CueSnapshot]) -> tuple[CueSnapshot, ...]:
    return tuple(replace(cue, index=index) for index, cue in enumerate(cues))


def _smart_join(left: str, right: str) -> str:
    left = left.rstrip()
    right = right.lstrip()
    if not left:
        return right
    if not right:
        return left
    left_graphemes = split_graphemes(left)
    left_last = left_graphemes[-1]
    right_first = split_graphemes(right)[0]
    left_word = next(
        (
            grapheme
            for grapheme in reversed(left_graphemes)
            if classify_grapheme(grapheme)
            not in (GraphemeKind.WEAK_PUNCTUATION, GraphemeKind.STRONG_PUNCTUATION)
        ),
        left_last,
    )
    needs_space = (
        classify_grapheme(left_word) is GraphemeKind.NON_CJK
        and classify_grapheme(right_first) is GraphemeKind.NON_CJK
        and left_word[-1].isalnum()
        and right_first[0].isalnum()
    )
    return f"{left}{' ' if needs_space else ''}{right}"


def _window_positions(
    snapshots: Sequence[CueSnapshot], evidence: TimingEvidenceWindow
) -> tuple[int, ...]:
    allowed = set(evidence.cue_ids)
    return tuple(index for index, cue in enumerate(snapshots) if cue.cue_id in allowed)


def _anchors_for(evidence: TimingEvidenceWindow, cue_id: str) -> tuple[TimingAnchor, ...]:
    return tuple(
        sorted(
            (
                anchor
                for anchor in evidence.anchors
                if anchor.cue_id == cue_id
                and anchor.quality_grade in (TimingQualityGrade.MEDIUM, TimingQualityGrade.HIGH)
            ),
            key=lambda anchor: (anchor.start_ms, anchor.end_ms, anchor.anchor_id),
        )
    )


def _safe_anchor_boundary(
    left: CueSnapshot,
    right: CueSnapshot,
    evidence: TimingEvidenceWindow,
    max_pause_ms: int,
) -> bool:
    left_anchors = _anchors_for(evidence, left.cue_id)
    right_anchors = _anchors_for(evidence, right.cue_id)
    if not left_anchors or not right_anchors:
        return False
    speech_gap = right_anchors[0].start_ms - left_anchors[-1].end_ms
    cue_gap = right.start_ms - left.end_ms
    return 0 <= speech_gap <= max_pause_ms and 0 <= cue_gap <= max_pause_ms


def _crosses_reset(left: CueSnapshot, right: CueSnapshot, rhythm_reset_ms: int) -> bool:
    return right.start_ms - left.end_ms >= rhythm_reset_ms


def _primary_side_name(
    cue: CueSnapshot,
    layout: SubtitleLayoutEnum,
    primary_side: PrimarySide,
) -> Literal["original", "translate"]:
    segment = ASRDataSeg(cue.text, cue.start_ms, cue.end_ms, cue.translated_text)
    primary = resolve_primary_text(segment, layout, primary_side)
    if primary == cue.translated_text.strip() and cue.translated_text.strip():
        return "translate"
    return "original"


def _primary_text(
    cue: CueSnapshot,
    layout: SubtitleLayoutEnum,
    primary_side: PrimarySide,
) -> str:
    return resolve_primary_text(
        ASRDataSeg(cue.text, cue.start_ms, cue.end_ms, cue.translated_text),
        layout,
        primary_side,
    )


def _split_at_nearest_boundary(text: str, fraction: float) -> tuple[str, str] | None:
    graphemes = split_graphemes(text)
    if len(graphemes) < 2:
        return None
    target = min(len(graphemes) - 1, max(1, round(len(graphemes) * fraction)))
    boundaries = [
        index
        for index in range(1, len(graphemes))
        if graphemes[index - 1].isspace()
        or graphemes[index].isspace()
        or graphemes[index - 1][-1] in ",.;:!?，。；：！？、"
    ]
    split_at = (
        min(boundaries, key=lambda value: (abs(value - target), value)) if boundaries else target
    )
    left = "".join(graphemes[:split_at]).strip()
    right = "".join(graphemes[split_at:]).strip()
    return (left, right) if left and right else None


def _source_anchor_split(
    text: str, left_anchors: Sequence[TimingAnchor], fraction: float
) -> tuple[str, str] | None:
    """Prefer the exact final left anchor, then fall back to a text boundary."""

    cursor = 0
    boundary = -1
    folded = text.casefold()
    for anchor in left_anchors:
        token = anchor.text.strip().casefold()
        if not token:
            continue
        found = folded.find(token, cursor)
        if found < 0:
            return _split_at_nearest_boundary(text, fraction)
        boundary = found + len(token)
        cursor = boundary
    if boundary <= 0 or boundary >= len(text):
        return _split_at_nearest_boundary(text, fraction)
    left, right = text[:boundary].strip(), text[boundary:].strip()
    return (left, right) if left and right else _split_at_nearest_boundary(text, fraction)


def _replace_pair(
    snapshots: Sequence[CueSnapshot], first_index: int, replacements: Sequence[CueSnapshot]
) -> tuple[CueSnapshot, ...]:
    updated = [*snapshots[:first_index], *replacements, *snapshots[first_index + 2 :]]
    return _reindex(updated)


def _replace_one(
    snapshots: Sequence[CueSnapshot], index: int, replacements: Sequence[CueSnapshot]
) -> tuple[CueSnapshot, ...]:
    updated = [*snapshots[:index], *replacements, *snapshots[index + 1 :]]
    return _reindex(updated)


def propose_merge_candidate(
    data: ASRData,
    snapshots: Sequence[CueSnapshot],
    evidence: TimingEvidenceWindow,
    *,
    protected_indices: Iterable[int] = (),
    rhythm_reset_ms: int = 800,
    short_cue_ms: int = 700,
    max_pause_ms: int = 250,
) -> StructuralCandidate:
    """Merge the first safely adjacent pair containing an extremely short cue."""

    _validate_snapshots(data, snapshots)
    before = tuple(snapshots)
    if not _quality_allows_structure(evidence):
        return _noop(before, evidence, "timing quality is below medium")
    if TimingOperation.MERGE_CUES not in evidence.allowed_operations:
        return _noop(before, evidence, "merge is not allowed by timing evidence")
    protected = set(protected_indices)
    positions = _window_positions(before, evidence)
    for left_index, right_index in zip(positions, positions[1:]):
        if right_index != left_index + 1 or {left_index, right_index} & protected:
            continue
        left, right = before[left_index], before[right_index]
        if _crosses_reset(left, right, rhythm_reset_ms):
            continue
        if min(left.end_ms - left.start_ms, right.end_ms - right.start_ms) > short_cue_ms:
            continue
        if not _safe_anchor_boundary(left, right, evidence, max_pause_ms):
            continue
        merged = _derived_cue(
            parents=(left, right),
            operation=StructuralOperationKind.MERGE,
            ordinal=0,
            index=left_index,
            start_ms=left.start_ms,
            end_ms=right.end_ms,
            text=_smart_join(left.text, right.text),
            translated_text=_smart_join(left.translated_text, right.translated_text),
        )
        after = _replace_pair(before, left_index, (merged,))
        operation = _record(StructuralOperationKind.MERGE, (left, right), (merged,), "both")
        return StructuralCandidate(evidence.window_id, before, after, (operation,))
    return _noop(before, evidence, "no safely mergeable short cue pair")


def propose_split_candidate(
    data: ASRData,
    snapshots: Sequence[CueSnapshot],
    evidence: TimingEvidenceWindow,
    *,
    protected_indices: Iterable[int] = (),
    max_duration_ms: int = 6000,
    technical_min_duration_ms: int = 500,
    minimum_pause_ms: int = 120,
) -> StructuralCandidate:
    """Split the first long cue at the safest anchor boundary near its midpoint."""

    _validate_snapshots(data, snapshots)
    before = tuple(snapshots)
    if not _quality_allows_structure(evidence):
        return _noop(before, evidence, "timing quality is below medium")
    allowed = evidence.allowed_operations
    if not ({TimingOperation.SPLIT_AT_WORD, TimingOperation.SPLIT_AT_PAUSE} & allowed):
        return _noop(before, evidence, "split is not allowed by timing evidence")
    protected = set(protected_indices)
    for index in _window_positions(before, evidence):
        cue = before[index]
        if index in protected or cue.end_ms - cue.start_ms <= max_duration_ms:
            continue
        anchors = _anchors_for(evidence, cue.cue_id)
        if len(anchors) < 2:
            continue
        boundaries: list[tuple[int, int]] = []
        word_split_allowed = TimingOperation.SPLIT_AT_WORD in allowed
        for anchor_index, (left_anchor, right_anchor) in enumerate(zip(anchors, anchors[1:])):
            if right_anchor.start_ms < left_anchor.end_ms:
                continue
            pause_ms = right_anchor.start_ms - left_anchor.end_ms
            if not word_split_allowed and pause_ms < minimum_pause_ms:
                continue
            boundary_ms = (left_anchor.end_ms + right_anchor.start_ms) // 2
            if (
                boundary_ms - cue.start_ms >= technical_min_duration_ms
                and cue.end_ms - boundary_ms >= technical_min_duration_ms
            ):
                boundaries.append((anchor_index, boundary_ms))
        if not boundaries:
            continue
        midpoint = (cue.start_ms + cue.end_ms) // 2
        anchor_index, boundary_ms = min(
            boundaries, key=lambda item: (abs(item[1] - midpoint), item[1], item[0])
        )
        fraction = (boundary_ms - cue.start_ms) / (cue.end_ms - cue.start_ms)
        original_parts = _source_anchor_split(cue.text, anchors[: anchor_index + 1], fraction)
        translated_parts = (
            _split_at_nearest_boundary(cue.translated_text, fraction)
            if cue.translated_text.strip()
            else ("", "")
        )
        if original_parts is None or translated_parts is None:
            continue
        left = _derived_cue(
            parents=(cue,),
            operation=StructuralOperationKind.SPLIT,
            ordinal=0,
            index=index,
            start_ms=cue.start_ms,
            end_ms=boundary_ms,
            text=original_parts[0],
            translated_text=translated_parts[0],
        )
        right = _derived_cue(
            parents=(cue,),
            operation=StructuralOperationKind.SPLIT,
            ordinal=1,
            index=index + 1,
            start_ms=boundary_ms,
            end_ms=cue.end_ms,
            text=original_parts[1],
            translated_text=translated_parts[1],
        )
        after = _replace_one(before, index, (left, right))
        operation = _record(StructuralOperationKind.SPLIT, (cue,), (left, right), "both")
        return StructuralCandidate(evidence.window_id, before, after, (operation,))
    return _noop(before, evidence, "no safely splittable long cue")


def _with_primary_text(
    cue: CueSnapshot,
    side: Literal["original", "translate"],
    value: str,
    *,
    parents: Sequence[CueSnapshot],
    ordinal: int,
) -> CueSnapshot:
    return _derived_cue(
        parents=parents,
        operation=StructuralOperationKind.MIGRATE,
        ordinal=ordinal,
        index=cue.index,
        start_ms=cue.start_ms,
        end_ms=cue.end_ms,
        text=value if side == "original" else cue.text,
        translated_text=value if side == "translate" else cue.translated_text,
    )


def propose_migration_candidate(
    data: ASRData,
    snapshots: Sequence[CueSnapshot],
    evidence: TimingEvidenceWindow,
    *,
    layout: SubtitleLayoutEnum = SubtitleLayoutEnum.ORIGINAL_ON_TOP,
    primary_side: PrimarySide = "translate",
    protected_indices: Iterable[int] = (),
    rhythm_reset_ms: int = 800,
    max_pause_ms: int = 250,
    minimum_density_ratio: float = 1.5,
) -> StructuralCandidate:
    """Move one ordered text fragment from a faster cue to its slower neighbor."""

    _validate_snapshots(data, snapshots)
    if minimum_density_ratio <= 1:
        raise ValueError("minimum_density_ratio must be greater than 1")
    before = tuple(snapshots)
    if not _quality_allows_structure(evidence):
        return _noop(before, evidence, "timing quality is below medium")
    if TimingOperation.MIGRATE_TEXT not in evidence.allowed_operations:
        return _noop(before, evidence, "text migration is not allowed by timing evidence")
    protected = set(protected_indices)
    positions = _window_positions(before, evidence)
    for left_index, right_index in zip(positions, positions[1:]):
        if right_index != left_index + 1 or {left_index, right_index} & protected:
            continue
        left, right = before[left_index], before[right_index]
        if _crosses_reset(left, right, rhythm_reset_ms):
            continue
        if not _safe_anchor_boundary(left, right, evidence, max_pause_ms):
            continue
        left_text = _primary_text(left, layout, primary_side)
        right_text = _primary_text(right, layout, primary_side)
        left_duration = max(left.end_ms - left.start_ms, 1)
        right_duration = max(right.end_ms - right.start_ms, 1)
        left_density = len(split_graphemes(left_text)) / left_duration
        right_density = len(split_graphemes(right_text)) / right_duration
        if not left_text or not right_text:
            continue
        if left_density >= right_density * minimum_density_ratio:
            parts = _split_at_nearest_boundary(left_text, 0.6)
            if parts is None:
                continue
            new_left_text, moved = parts
            new_right_text = _smart_join(moved, right_text)
        elif right_density >= left_density * minimum_density_ratio:
            parts = _split_at_nearest_boundary(right_text, 0.4)
            if parts is None:
                continue
            moved, new_right_text = parts
            new_left_text = _smart_join(left_text, moved)
        else:
            continue
        side = _primary_side_name(left, layout, primary_side)
        if _primary_side_name(right, layout, primary_side) != side:
            continue
        parents = (left, right)
        updated_left = _with_primary_text(left, side, new_left_text, parents=parents, ordinal=0)
        updated_right = _with_primary_text(right, side, new_right_text, parents=parents, ordinal=1)
        after = _replace_pair(before, left_index, (updated_left, updated_right))
        operation = _record(
            StructuralOperationKind.MIGRATE,
            parents,
            (updated_left, updated_right),
            side,
        )
        return StructuralCandidate(evidence.window_id, before, after, (operation,))
    return _noop(before, evidence, "no safely migratable adjacent cue pair")


def propose_structural_candidate(
    data: ASRData,
    snapshots: Sequence[CueSnapshot] | None,
    evidence: TimingEvidenceWindow,
    *,
    layout: SubtitleLayoutEnum = SubtitleLayoutEnum.ORIGINAL_ON_TOP,
    primary_side: PrimarySide = "translate",
    protected_indices: Iterable[int] = (),
    rhythm_reset_ms: int = 800,
    short_cue_ms: int = 700,
    max_duration_ms: int = 6000,
    technical_min_duration_ms: int = 500,
    minimum_pause_ms: int = 120,
    max_pause_ms: int = 250,
    minimum_density_ratio: float = 1.5,
) -> StructuralCandidate:
    """Propose one deterministic structural transaction in risk-first order.

    Merging removes pathological fragments first, splitting handles overlong
    cues second, and text migration is attempted only when neither applies.
    Re-running after an accepted transaction advances to the next candidate.
    """

    source = tuple(snapshots) if snapshots is not None else _snapshot_data(data)
    common = {
        "protected_indices": tuple(protected_indices),
    }
    merged = propose_merge_candidate(
        data,
        source,
        evidence,
        rhythm_reset_ms=rhythm_reset_ms,
        short_cue_ms=short_cue_ms,
        max_pause_ms=max_pause_ms,
        **common,
    )
    if merged.changed:
        return merged
    split = propose_split_candidate(
        data,
        source,
        evidence,
        max_duration_ms=max_duration_ms,
        technical_min_duration_ms=technical_min_duration_ms,
        minimum_pause_ms=minimum_pause_ms,
        **common,
    )
    if split.changed:
        return split
    migrated = propose_migration_candidate(
        data,
        source,
        evidence,
        layout=layout,
        primary_side=primary_side,
        rhythm_reset_ms=rhythm_reset_ms,
        max_pause_ms=max_pause_ms,
        minimum_density_ratio=minimum_density_ratio,
        **common,
    )
    if migrated.changed:
        return migrated
    reasons = tuple(
        reason
        for reason in (merged.skipped_reason, split.skipped_reason, migrated.skipped_reason)
        if reason
    )
    return _noop(source, evidence, "; ".join(reasons))
