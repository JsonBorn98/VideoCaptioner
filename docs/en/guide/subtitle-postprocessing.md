# Subtitle Postprocessing

Subtitle postprocessing is a standalone stage after subtitle optimization and translation. It accepts
a finished monolingual or bilingual subtitle and can reshape cue boundaries, timing, gaps, punctuation,
and locally repair text to make reading speed steadier.

## Stage boundary

Subtitle optimization and translation keep their existing upstream responsibilities: length-based
splitting, sentence segmentation, text optimization, and translation. Postprocessing does not rerun
those steps. The complete workflow enables postprocessing by default, while the standalone page can
process an existing SRT, VTT, or ordinary ASS file.

## Canonical SRT model

SRT is the only default handoff format between content modules. Imported ASS/VTT styling, positioning,
and effects are intentionally discarded. Advanced or effect-heavy ASS files are outside the supported
scope. Exporting ASS applies VideoCaptioner's current subtitle style instead of restoring the input
style.

For an unmarked bilingual subtitle, explicitly choose whether the first line is the source or target.
This input structure is separate from the output layout, so a file may be interpreted as target-first
and still be saved with the source on top.

## Profiles and timing

The Loose, Balanced, and Smooth-first profiles have immutable factory baselines. Edits are saved
immediately; custom profiles are copied from one of those templates and reset to that template's
baseline. Translation is the default primary reading side, with optional source-side auditing or
two-sided optimization.

The default timing path uses the existing subtitle timeline and deterministic algorithms. You may
associate video or audio and enable ForcedAligner for more precise evidence; no separate VAD input is
required, and unavailable or failed windows fall back to deterministic processing.

## Tail compensation (stop cues vanishing too fast)

Speed optimization only borrows gap time for cues that are *too fast to read*; a comfortable cue
followed by a long pause gets nothing, so it disappears at its natural end and leaves a blank — it
"blinks out" the moment speech stops. Enabling **tail compensation** adds display time to the previous
cue's end before any pause that exceeds the max close gap, following a **monotonic clamped curve** —
both the added time and the remaining blank never decrease as the gap grows:

- `gap ≤ max close gap`: left to "close short gaps"; no compensation.
- crossing the max close gap: jump straight to **min compensation** (a guaranteed perceptible
  lead-out), then ramp up linearly.
- reaching the **max compensation gap**: compensation caps at **max compensation**; larger gaps add
  nothing more and simply let the blank widen.

The four knobs are the curve's two knees: `max close gap` / `min compensation` (lower knee) and
`max compensation gap` / `max compensation` (upper knee). One linked constraint keeps them valid ——
`max compensation − min compensation ≤ max compensation gap − max close gap` (slope ≤ 1) —— which
guarantees the blank never inverts and cues never overlap; the minimum blank always equals
`max close gap − min compensation`. Compensation also never pushes a cue past the normal maximum
display duration, and protected cues (music/lyric markers, isolated title cards,
short-text-long-display) are skipped. It only extends the previous cue's end, never shortens, and runs
last after speed optimization has settled the final timeline. See `docs/adr/0005` for the rationale.

Tail compensation is off by default. Enable and tune it under **Postprocessing Settings → Timing →
Tail compensation** using sliders or precise number entry (each knob's range clamps live against the
others, so an invalid curve can't be entered); values are stored per profile and reset to the factory
baseline. The CLI configures them through the profile.


## Saving and exporting

The stage writes `【后处理字幕】<name>.srt`. Save updates the canonical SRT draft; Export creates a
derived SRT, VTT, ASS, JSON, or TXT file using the selected output layout. The input is never
overwritten. In a complete workflow, a failed postprocess stage falls back to the preserved initial
subtitle.

See the [CLI reference](/cli#postprocess-独立字幕后处理) for automation options.
