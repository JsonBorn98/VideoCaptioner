"""First vertical slice of the unified subtitle speed pipeline."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Iterable, Literal

import regex

from ..asr.asr_data import ASRData, ASRDataSeg
from ..entities import SubtitleLayoutEnum
from ..utils.logger import setup_logger
from .deterministic import (
    TimingChange,
    TimingCue,
    accepts_candidate,
    build_speed_samples,
    candidate_is_non_worsening,
    measure,
    optimize_subtitle_timing,
)
from .layout import PrimarySide, resolve_primary_text, resolve_reference_text
from .metrics import SpeedMetrics
from .models import CueSnapshot
from .policy import SpeedPolicy, get_speed_policy
from .protection import ProtectionMatch, detect_protected_cues
from .semantic import (
    RewriteCache,
    SemanticRepairCue,
    SemanticRepairRecord,
    SemanticReviewer,
    SemanticRewriter,
    repair_semantic_windows,
)
from .structural import StructuralOperationRecord, propose_structural_candidate
from .timing_evidence import (
    TimingEvidenceWindow,
    TimingOperation,
    TimingQualityGrade,
)
from .validation import SemanticWindow, ValidationStatus, validate_semantic_window

SpeedMode = Literal["apply", "analyze"]

logger = setup_logger("speed.pipeline")


@dataclass(frozen=True)
class SpeedOptimizationResult:
    policy: SpeedPolicy
    profile_id: str
    mode: SpeedMode
    before: SpeedMetrics
    after: SpeedMetrics
    changes: tuple[TimingChange, ...]
    unresolved_cue_ids: tuple[str, ...]
    invalid_cue_ids: tuple[str, ...]
    protected: tuple[ProtectionMatch, ...]
    reference_before: SpeedMetrics | None = None
    reference_after: SpeedMetrics | None = None
    structural_operations: tuple[StructuralOperationRecord, ...] = ()
    semantic_records: tuple[SemanticRepairRecord, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(
            self.changes
            or self.structural_operations
            or any(record.status is ValidationStatus.ACCEPTED for record in self.semantic_records)
        )


_SEMANTIC_CACHE: RewriteCache = {}
_SEMANTIC_CACHE_LIMIT = 256


def _rhythm_ids(segments: list[ASRDataSeg], reset_gap_ms: int) -> list[str]:
    rhythm = 0
    values: list[str] = []
    for index, segment in enumerate(segments):
        if index:
            gap = segment.start_time - segments[index - 1].end_time
            if gap >= reset_gap_ms:
                rhythm += 1
        values.append(f"rhythm-{rhythm}")
    return values


def _to_timing_cues(
    data: ASRData,
    layout: SubtitleLayoutEnum,
    primary_side: PrimarySide,
    protected_indices: set[int],
    policy: SpeedPolicy,
    timing_windows: tuple[TimingEvidenceWindow, ...],
) -> list[TimingCue]:
    rhythms = _rhythm_ids(data.segments, policy.rhythm_reset_ms)
    cues: list[TimingCue] = []
    evidence_by_cue = {cue_id: window for window in timing_windows for cue_id in window.cue_ids}
    budget_by_quality = {
        TimingQualityGrade.LOW: policy.low_confidence_boundary_shift_ms,
        TimingQualityGrade.MEDIUM: policy.medium_confidence_boundary_shift_ms,
        TimingQualityGrade.HIGH: policy.high_confidence_boundary_shift_ms,
    }
    for index, segment in enumerate(data.segments):
        snapshot = CueSnapshot.from_input(
            index=index,
            start_ms=segment.start_time,
            end_ms=segment.end_time,
            text=segment.text,
            translated_text=segment.translated_text,
        )
        evidence = evidence_by_cue.get(snapshot.cue_id)
        anchors = (
            [anchor for anchor in evidence.anchors if anchor.cue_id == snapshot.cue_id]
            if evidence is not None
            else []
        )
        quality = (
            evidence.quality_grade
            if evidence is not None
            and anchors
            and TimingOperation.MOVE_SHARED_BOUNDARY in evidence.allowed_operations
            else TimingQualityGrade.LOW
        )
        cues.append(
            TimingCue(
                cue_id=snapshot.cue_id,
                order=index,
                start_ms=segment.start_time,
                end_ms=segment.end_time,
                text=resolve_primary_text(segment, layout, primary_side),
                rhythm_id=rhythms[index],
                protected=index in protected_indices,
                boundary_budget_ms=budget_by_quality.get(quality),
                speech_start_ms=min((anchor.start_ms for anchor in anchors), default=None),
                speech_end_ms=max((anchor.end_ms for anchor in anchors), default=None),
            )
        )
    return cues


def _clone_with_timings(data: ASRData, cues: Iterable[TimingCue]) -> ASRData:
    timings = list(cues)
    return ASRData(
        [
            ASRDataSeg(
                text=segment.text,
                start_time=timings[index].start_ms,
                end_time=timings[index].end_ms,
                translated_text=segment.translated_text,
            )
            for index, segment in enumerate(data.segments)
        ]
    )


def _snapshots(data: ASRData) -> tuple[CueSnapshot, ...]:
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


def _optimize_timing_once(
    data: ASRData,
    *,
    policy: SpeedPolicy,
    layout: SubtitleLayoutEnum,
    primary_side: PrimarySide,
    timing_windows: tuple[TimingEvidenceWindow, ...],
) -> tuple[ASRData, SpeedMetrics, tuple[TimingChange, ...], tuple[ProtectionMatch, ...]]:
    primary_texts = [
        resolve_primary_text(segment, layout, primary_side) for segment in data.segments
    ]
    protected = detect_protected_cues(data.segments, primary_texts, policy)
    cues = _to_timing_cues(
        data,
        layout,
        primary_side,
        {match.index for match in protected},
        policy,
        timing_windows,
    )
    optimized, changes = optimize_subtitle_timing(cues, policy)
    return (
        _clone_with_timings(data, optimized),
        measure(optimized, policy),
        tuple(changes),
        protected,
    )


def _snapshot_primary_text(
    cue: CueSnapshot,
    layout: SubtitleLayoutEnum,
    primary_side: PrimarySide,
) -> str:
    return resolve_primary_text(
        ASRDataSeg(cue.text, cue.start_ms, cue.end_ms, cue.translated_text),
        layout,
        primary_side,
    )


def _structural_semantics_are_valid(
    operations: tuple[StructuralOperationRecord, ...],
    before: tuple[CueSnapshot, ...],
    after: tuple[CueSnapshot, ...],
    *,
    layout: SubtitleLayoutEnum,
    primary_side: PrimarySide,
) -> bool:
    before_by_id = {cue.cue_id: cue for cue in before}
    after_by_id = {cue.cue_id: cue for cue in after}
    for operation in operations:
        source = tuple(
            _snapshot_primary_text(before_by_id[cue_id], layout, primary_side)
            for cue_id in operation.before_cue_ids
        )
        candidate = tuple(
            _snapshot_primary_text(after_by_id[cue_id], layout, primary_side)
            for cue_id in operation.after_cue_ids
        )
        validation = validate_semantic_window(
            SemanticWindow(operation.operation_id, source, candidate)
        )
        if validation.status is not ValidationStatus.ACCEPTED:
            return False
    return True


def _apply_structural_candidates(
    data: ASRData,
    metrics: SpeedMetrics,
    *,
    policy: SpeedPolicy,
    layout: SubtitleLayoutEnum,
    primary_side: PrimarySide,
    timing_windows: tuple[TimingEvidenceWindow, ...],
) -> tuple[
    ASRData,
    SpeedMetrics,
    tuple[StructuralOperationRecord, ...],
    tuple[TimingChange, ...],
]:
    current = data
    current_metrics = metrics
    accepted: list[StructuralOperationRecord] = []
    accepted_timing_changes: list[TimingChange] = []

    initial_positions = {cue.cue_id: cue.index for cue in _snapshots(current)}
    ordered_windows = sorted(
        timing_windows,
        key=lambda window: max(
            (initial_positions.get(cue_id, -1) for cue_id in window.cue_ids),
            default=-1,
        ),
        reverse=True,
    )
    for evidence in ordered_windows:
        snapshots = _snapshots(current)
        positions = {cue.cue_id: cue.index for cue in snapshots}
        if not any(cue_id in positions for cue_id in evidence.cue_ids):
            continue
        primary_texts = [
            resolve_primary_text(segment, layout, primary_side) for segment in current.segments
        ]
        protected = detect_protected_cues(current.segments, primary_texts, policy)
        candidate = propose_structural_candidate(
            current,
            snapshots,
            evidence,
            layout=layout,
            primary_side=primary_side,
            protected_indices=(match.index for match in protected),
            rhythm_reset_ms=policy.rhythm_reset_ms,
            max_duration_ms=round(policy.max_duration_seconds * 1000),
            technical_min_duration_ms=round(policy.technical_min_duration_seconds * 1000),
        )
        if not candidate.changed or not _structural_semantics_are_valid(
            candidate.operations,
            candidate.before,
            candidate.after,
            layout=layout,
            primary_side=primary_side,
        ):
            continue
        proposed = candidate.materialize(accept=True)
        proposed, proposed_metrics, proposed_changes, _ = _optimize_timing_once(
            proposed,
            policy=policy,
            layout=layout,
            primary_side=primary_side,
            timing_windows=(),
        )
        if accepts_candidate(current_metrics, proposed_metrics):
            current = proposed
            current_metrics = proposed_metrics
            accepted.extend(candidate.operations)
            accepted_timing_changes.extend(proposed_changes)
    return current, current_metrics, tuple(accepted), tuple(accepted_timing_changes)


def _primary_field(
    segment: ASRDataSeg,
    layout: SubtitleLayoutEnum,
    primary_side: PrimarySide,
) -> Literal["text", "translated_text"] | None:
    if primary_side == "original":
        return "text"
    if primary_side == "translate":
        return "translated_text" if segment.translated_text.strip() else None
    if layout is SubtitleLayoutEnum.ONLY_ORIGINAL:
        return "text"
    if layout in (SubtitleLayoutEnum.ONLY_TRANSLATE, SubtitleLayoutEnum.TRANSLATE_ON_TOP):
        return "translated_text" if segment.translated_text.strip() else None
    return "text"


def _apply_semantic_repairs(
    data: ASRData,
    metrics: SpeedMetrics,
    *,
    policy: SpeedPolicy,
    layout: SubtitleLayoutEnum,
    primary_side: PrimarySide,
    model: str,
    reviewer_model: str | None,
    window_size: int,
    uncertain_review: bool,
    cache: RewriteCache,
    rewriter: SemanticRewriter | None,
    reviewer: SemanticReviewer | None,
) -> tuple[
    ASRData,
    SpeedMetrics,
    tuple[SemanticRepairRecord, ...],
    tuple[TimingChange, ...],
]:
    timing_cues = _to_timing_cues(data, layout, primary_side, set(), policy, ())
    samples = {sample.stable_id: sample for sample in build_speed_samples(timing_cues, policy)}
    protected = detect_protected_cues(
        data.segments,
        [cue.text for cue in timing_cues],
        policy,
    )
    protected_indices = {match.index for match in protected}
    repair_cues: list[SemanticRepairCue] = []
    fields_by_id: dict[str, Literal["text", "translated_text"]] = {}
    for index, (segment, cue) in enumerate(zip(data.segments, timing_cues)):
        field = _primary_field(segment, layout, primary_side)
        sample = samples[cue.cue_id]
        unresolved = (sample.hard_load or 0.0) > 1.0 and field is not None
        grapheme_count = max(len(regex.findall(r"\X", cue.text)), 1)
        target = (
            max(1, math.floor(grapheme_count / sample.hard_load))
            if unresolved and sample.hard_load
            else None
        )
        repair_cues.append(
            SemanticRepairCue(
                cue_id=cue.cue_id,
                text=cue.text,
                unresolved=unresolved,
                protected=index in protected_indices,
                rhythm_id=cue.rhythm_id,
                target_max_graphemes=target,
            )
        )
        if field is not None:
            fields_by_id[cue.cue_id] = field

    if not any(cue.unresolved and not cue.protected for cue in repair_cues):
        return data, metrics, (), ()

    selected_reviewer = reviewer
    if not uncertain_review and selected_reviewer is None:
        from .validation import ReviewDecision, SemanticReviewResponse

        selected_reviewer = lambda request: SemanticReviewResponse(  # noqa: E731
            request.window_id,
            ReviewDecision.UNCERTAIN,
            "independent LLM review is disabled",
        )
    result = repair_semantic_windows(
        repair_cues,
        model=model,
        reviewer_model=reviewer_model,
        rewriter=rewriter,
        reviewer=selected_reviewer,
        cache=cache,
        window_size=window_size,
    )
    repaired_by_id = {cue.cue_id: cue.text for cue in result.cues}
    current = data
    current_metrics = metrics
    final_records: list[SemanticRepairRecord] = []
    accepted_timing_changes: list[TimingChange] = []
    for record in result.records:
        if record.status is not ValidationStatus.ACCEPTED:
            final_records.append(record)
            continue
        selected = set(record.target_cue_ids)
        candidate_segments: list[ASRDataSeg] = []
        changed = False
        for segment, cue in zip(
            current.segments, _to_timing_cues(current, layout, primary_side, set(), policy, ())
        ):
            text = segment.text
            translated = segment.translated_text
            if cue.cue_id in selected and cue.cue_id in fields_by_id:
                replacement = repaired_by_id[cue.cue_id].strip()
                if fields_by_id[cue.cue_id] == "text":
                    changed = changed or replacement != text
                    text = replacement
                else:
                    changed = changed or replacement != translated
                    translated = replacement
            candidate_segments.append(
                ASRDataSeg(text, segment.start_time, segment.end_time, translated)
            )
        if not changed:
            final_records.append(replace(record, status=ValidationStatus.ROLLED_BACK))
            continue
        candidate_data, candidate_metrics, candidate_changes, _ = _optimize_timing_once(
            ASRData(candidate_segments),
            policy=policy,
            layout=layout,
            primary_side=primary_side,
            timing_windows=(),
        )
        if accepts_candidate(current_metrics, candidate_metrics):
            current = candidate_data
            current_metrics = candidate_metrics
            final_records.append(record)
            accepted_timing_changes.extend(candidate_changes)
        else:
            final_records.append(replace(record, status=ValidationStatus.ROLLED_BACK))
    while len(cache) > _SEMANTIC_CACHE_LIMIT:
        cache.pop(next(iter(cache)))
    return current, current_metrics, tuple(final_records), tuple(accepted_timing_changes)


def optimize_speed(
    data: ASRData,
    *,
    policy: SpeedPolicy | None = None,
    profile_id: str | None = None,
    mode: SpeedMode = "apply",
    layout: SubtitleLayoutEnum = SubtitleLayoutEnum.ORIGINAL_ON_TOP,
    primary_side: PrimarySide = "translate",
    protected_indices: Iterable[int] = (),
    timing_windows: Iterable[TimingEvidenceWindow] = (),
    reference_audit: bool = False,
    optimize_both_sides: bool = False,
    semantic_repair: bool = False,
    semantic_model: str | None = None,
    semantic_reviewer_model: str | None = None,
    semantic_window_size: int = 5,
    semantic_uncertain_review: bool = True,
    semantic_cache: RewriteCache | None = None,
    semantic_rewriter: SemanticRewriter | None = None,
    semantic_reviewer: SemanticReviewer | None = None,
) -> tuple[ASRData, SpeedOptimizationResult]:
    """Analyze or apply the validated timing, structure, and semantic pipeline."""

    if mode not in ("apply", "analyze"):
        raise ValueError("speed mode must be 'apply' or 'analyze'")
    selected_policy = policy or get_speed_policy()
    evidence = tuple(timing_windows)
    resolved_profile = profile_id or selected_policy.preset.value
    logger.info(
        "语速优化开始：cues=%d 主字幕=%s profile=%s mode=%s 时间轴证据窗口=%d",
        len(data.segments),
        primary_side,
        resolved_profile,
        mode,
        len(evidence),
    )
    primary_texts = [
        resolve_primary_text(segment, layout, primary_side) for segment in data.segments
    ]
    protected = detect_protected_cues(
        data.segments,
        primary_texts,
        selected_policy,
        explicit_indices=protected_indices,
    )
    cues = _to_timing_cues(
        data,
        layout,
        primary_side,
        {match.index for match in protected},
        selected_policy,
        evidence,
    )
    before = measure(cues, selected_policy)
    optimized, changes = optimize_subtitle_timing(cues, selected_policy)
    after = measure(optimized, selected_policy)
    output = _clone_with_timings(data, optimized) if mode == "apply" else data
    structural_operations: tuple[StructuralOperationRecord, ...] = ()
    semantic_records: tuple[SemanticRepairRecord, ...] = ()
    if mode == "apply":
        output, after, structural_operations, structural_timing_changes = (
            _apply_structural_candidates(
                output,
                after,
                policy=selected_policy,
                layout=layout,
                primary_side=primary_side,
                timing_windows=evidence,
            )
        )
        changes.extend(structural_timing_changes)
        if semantic_repair and semantic_model:
            output, after, semantic_records, semantic_timing_changes = _apply_semantic_repairs(
                output,
                after,
                policy=selected_policy,
                layout=layout,
                primary_side=primary_side,
                model=semantic_model,
                reviewer_model=semantic_reviewer_model,
                window_size=semantic_window_size,
                uncertain_review=semantic_uncertain_review,
                cache=semantic_cache if semantic_cache is not None else _SEMANTIC_CACHE,
                rewriter=semantic_rewriter,
                reviewer=semantic_reviewer,
            )
            changes.extend(semantic_timing_changes)
    measured_output = output if mode == "apply" else _clone_with_timings(data, optimized)
    both_sides_applied = False
    secondary_result = None
    if (
        optimize_both_sides
        and mode == "apply"
        and any(segment.translated_text.strip() for segment in output.segments)
    ):
        if primary_side == "original":
            secondary_side: PrimarySide = "translate"
        elif primary_side == "translate":
            secondary_side = "original"
        elif layout in (
            SubtitleLayoutEnum.TRANSLATE_ON_TOP,
            SubtitleLayoutEnum.ONLY_TRANSLATE,
        ):
            secondary_side = "original"
        else:
            secondary_side = "translate"
        secondary_output, secondary_result = optimize_speed(
            output,
            policy=selected_policy,
            profile_id=profile_id,
            mode=mode,
            layout=layout,
            primary_side=secondary_side,
            protected_indices=protected_indices,
            timing_windows=evidence,
            reference_audit=False,
            optimize_both_sides=False,
            semantic_repair=semantic_repair,
            semantic_model=semantic_model,
            semantic_reviewer_model=semantic_reviewer_model,
            semantic_window_size=semantic_window_size,
            semantic_uncertain_review=semantic_uncertain_review,
            semantic_cache=semantic_cache,
            semantic_rewriter=semantic_rewriter,
            semantic_reviewer=semantic_reviewer,
        )
        primary_candidate = measure(
            _to_timing_cues(
                secondary_output,
                layout,
                primary_side,
                set(),
                selected_policy,
                (),
            ),
            selected_policy,
        )
        if candidate_is_non_worsening(after, primary_candidate):
            output = secondary_output
            measured_output = secondary_output
            after = primary_candidate
            changes.extend(secondary_result.changes)
            structural_operations = (
                *structural_operations,
                *secondary_result.structural_operations,
            )
            semantic_records = (*semantic_records, *secondary_result.semantic_records)
            both_sides_applied = True
    reference_before = None
    reference_after = None
    if secondary_result is not None and both_sides_applied:
        reference_before = secondary_result.before
        reference_after = secondary_result.after
    elif reference_audit or optimize_both_sides:
        reference_before_cues = [
            replace(
                cue,
                text=resolve_reference_text(data.segments[index], layout, primary_side),
            )
            for index, cue in enumerate(cues)
        ]
        reference_after_cues = [
            replace(
                cue,
                text=resolve_reference_text(measured_output.segments[index], layout, primary_side),
            )
            for index, cue in enumerate(
                _to_timing_cues(measured_output, layout, primary_side, set(), selected_policy, ())
            )
        ]
        reference_before = measure(reference_before_cues, selected_policy)
        reference_after = measure(reference_after_cues, selected_policy)
    final_cues = _to_timing_cues(measured_output, layout, primary_side, set(), selected_policy, ())
    samples = {
        sample.stable_id: sample for sample in build_speed_samples(final_cues, selected_policy)
    }
    unresolved = tuple(
        cue.cue_id for cue in final_cues if (samples[cue.cue_id].hard_load or 0.0) > 1.0
    )
    invalid = tuple(cue.cue_id for cue in final_cues if not samples[cue.cue_id].valid)
    result = SpeedOptimizationResult(
        policy=selected_policy,
        profile_id=profile_id or selected_policy.preset.value,
        mode=mode,
        before=before,
        after=after,
        changes=tuple(changes),
        unresolved_cue_ids=unresolved,
        invalid_cue_ids=invalid,
        protected=protected,
        reference_before=reference_before,
        reference_after=reference_after,
        structural_operations=structural_operations,
        semantic_records=semantic_records,
    )
    changed_cue_count = len({change.cue_id for change in result.changes})
    semantic_accepted = sum(
        1 for record in semantic_records if record.status is ValidationStatus.ACCEPTED
    )
    semantic_rolled_back = sum(
        1 for record in semantic_records if record.status is ValidationStatus.ROLLED_BACK
    )
    if semantic_rolled_back:
        logger.warning(
            "语义修复回滚 %d 个窗口（未通过校验/未改善指标，主字幕=%s）",
            semantic_rolled_back,
            primary_side,
        )
    logger.info(
        "语速优化结束：变更cue=%d 边界移动=%d 结构操作=%d 语义修复=%d 未解决超速=%d 无效cue=%d 主字幕=%s",
        changed_cue_count,
        len(result.changes),
        len(structural_operations),
        semantic_accepted,
        len(unresolved),
        len(invalid),
        primary_side,
    )
    return output, result
