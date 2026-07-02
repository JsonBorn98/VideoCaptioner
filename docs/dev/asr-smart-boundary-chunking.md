# ASR Smart Boundary Chunking Design

Date: 2026-07-03

Status: Proposed, deferred until the MiMo/Qwen ASR backend work is complete.

## Summary

This document proposes a low-risk improvement to long-audio ASR chunking:
keep the existing fixed-length chunking model, but move each chunk boundary to
a nearby silence or non-speech point when possible.

The first implementation should not remove audio, reorder speech, or pack VAD
segments into synthetic chunks. It should only adjust the boundary between two
continuous chunks. That keeps timestamps simple, preserves the current
`ChunkedASR` and `ChunkMerger` contract, and reduces the chance of cutting a
word in half at a fixed 5-minute boundary.

## Context

Current long-audio transcription uses `ChunkedASR`:

- split audio into fixed chunks
- add overlap between adjacent chunks
- transcribe each chunk independently
- merge chunk results using `ChunkMerger`

For MiMo ASR and local Qwen3 ASR, the current target is 5 minutes per chunk.
This is driven by payload size, local memory pressure, and Qwen3-ForcedAligner
being safest on short audio windows.

The weakness is that a fixed boundary can fall inside a word, phrase, or breath.
Overlap mitigates this, but the ASR model may still produce worse text near a
chunk edge. The proposed change reduces that boundary risk before ASR is called.

## ADR

### Decision

Implement a smart-boundary splitter as an optional mode of `ChunkedASR`.

The splitter should:

- keep chunks as continuous slices of the original audio
- keep absolute chunk offsets in original media time
- prefer silence or non-speech near the nominal boundary
- keep the existing overlap mechanism
- never exceed the configured maximum chunk duration
- fall back to fixed splitting when no good boundary is found

### Non-Goals

- Do not implement pure VAD packing in the first version.
- Do not cut out silence from the audio.
- Do not introduce compressed-time to original-time mapping.
- Do not rewrite `ChunkMerger`.
- Do not make this the default for every ASR backend until tested.

### Rationale

Pure VAD packing is more powerful but changes the time model. If silence is
removed or speech segments are concatenated, every subtitle timestamp needs a
mapping back to original media time. That is unnecessary for the first quality
gain.

Boundary snapping gives most of the practical benefit while preserving the
current architecture:

```text
original audio timeline
0s ---------------- 285s .... 300s ---------------- 585s .... 600s
                  ^ choose boundary near target   ^ choose boundary near target
```

Each chunk remains a real slice from the original audio:

```text
chunk 1: 0s      - boundary_1 + overlap
chunk 2: boundary_1 - overlap - boundary_2 + overlap
chunk 3: boundary_2 - overlap - ...
```

## Domain Model

| Term | Meaning |
| --- | --- |
| `max_chunk_length` | Hard maximum chunk duration, currently 300 seconds for MiMo/Qwen. |
| `target_boundary` | The preferred boundary before adjustment. |
| `search_window` | Time range around the target boundary where the splitter searches for a better boundary. |
| `speech_interval` | A detected speech region from VAD. |
| `silence_candidate` | A non-speech or low-energy region that can be used as a chunk boundary. |
| `chunk_span` | The non-overlapped core span of a chunk in original audio time. |
| `overlap_span` | Extra context included before or after a chunk for ASR robustness. |
| `boundary_snap` | Moving a target boundary to a nearby silence candidate. |

## Recommended First Version

Use fixed-window boundary snapping.

For each chunk:

1. Start from the previous core boundary.
2. Choose a nominal target near the next 5-minute boundary.
3. Search nearby for a silence candidate.
4. Use the best candidate if it keeps the chunk within limits.
5. Otherwise fall back to the safe fixed boundary.
6. Add configured overlap when exporting the actual chunk audio.

Important constraint: if a backend requires chunks no longer than 300 seconds,
the search window must not allow the selected boundary to create a longer chunk.

A safe policy is:

```text
max_chunk_length = 300s
search_before = 20s
search_after = 10s
target_boundary = chunk_start + max_chunk_length - search_after
search_window = [target_boundary - search_before, target_boundary + search_after]
hard_limit = chunk_start + max_chunk_length
selected_boundary <= hard_limit
```

With those defaults, the splitter can still search both sides of the target
while keeping the final chunk at or below 300 seconds.

## Candidate Detection Options

### Option A: Energy-Based Silence Detection

Use pydub or ffmpeg-level audio energy to detect low-volume regions.

Pros:

- no new ML dependency
- fast
- easy to test
- works well for clean lectures and podcasts

Cons:

- background music can hide silence
- noisy rooms can look like speech
- quiet speech can be mistaken for silence

Suggested initial parameters:

| Parameter | Suggested Default |
| --- | ---: |
| `min_silence_len_ms` | 300-500 |
| `silence_thresh` | dynamic, based on local dBFS |
| `seek_step_ms` | 20-50 |
| `search_before_ms` | 20000 |
| `search_after_ms` | 10000 |

### Option B: Silero VAD

Decode the full audio to mono 16 kHz, run Silero VAD once, and use the speech
timestamp list to find non-speech gaps near chunk boundaries.

Pros:

- more robust than pure energy thresholds
- light enough for CPU use
- returns speech intervals directly
- good fit for boundary snapping

Cons:

- adds a dependency
- needs model download/runtime handling
- parameters need tuning for videos with music, cross-talk, or noise

Suggested VAD parameters:

| Parameter | Suggested Default |
| --- | ---: |
| `threshold` | 0.5 |
| `min_speech_duration_ms` | 250 |
| `min_silence_duration_ms` | 300-500 |
| `speech_pad_ms` | 100-200 |

### Option C: Pure VAD Packing

Split into speech segments, then pack speech segments into chunks.

This should not be the first implementation. It risks changing the time axis if
silence is dropped, and it complicates chunk offsets, progress reporting, cache
keys, and subtitle timestamp correctness.

## Boundary Selection Algorithm

Input:

- audio duration
- `max_chunk_length_ms`
- `chunk_overlap_ms`
- `search_before_ms`
- `search_after_ms`
- candidate silence or non-speech intervals

Output:

- list of `(chunk_bytes, offset_ms)`

Pseudo-code:

```python
boundaries = [0]
current = 0

while current < total_duration_ms:
    hard_limit = min(current + max_chunk_length_ms, total_duration_ms)
    if hard_limit >= total_duration_ms:
        boundaries.append(total_duration_ms)
        break

    target = hard_limit - search_after_ms
    search_start = max(current + min_chunk_length_ms, target - search_before_ms)
    search_end = min(hard_limit, target + search_after_ms)

    boundary = find_best_silence_candidate(search_start, search_end, target)
    if boundary is None:
        boundary = hard_limit

    boundaries.append(boundary)
    current = boundary
```

Then export chunks with overlap:

```python
for i in range(len(boundaries) - 1):
    core_start = boundaries[i]
    core_end = boundaries[i + 1]
    export_start = max(0, core_start - chunk_overlap_ms if i > 0 else core_start)
    export_end = min(total_duration_ms, core_end + chunk_overlap_ms)
    chunk = audio[export_start:export_end]
    offset = export_start
```

The `offset` must be the actual exported audio start, because chunk timestamps
are relative to the exported audio.

## Scoring Silence Candidates

When multiple candidates exist inside the search window, pick the one with the
best score.

Recommended scoring:

```text
score = silence_duration_weight
      - distance_from_target_penalty
      + low_energy_bonus
```

For Silero VAD, a candidate is a gap between speech intervals. Prefer:

- longer non-speech gap
- gap center nearest to target
- boundary near the center of the gap

For energy detection, prefer:

- longest contiguous low-energy range
- lowest average dBFS
- center nearest to target

## Integration Points

### `videocaptioner/core/asr/chunked_asr.py`

Add optional strategy parameters:

```python
chunk_boundary_mode: Literal["fixed", "silence", "vad"] = "fixed"
boundary_search_before: int = 20
boundary_search_after: int = 10
min_silence_duration: int = 400
```

Keep `_split_audio()` as the public behavior point, but split internals:

```text
_split_audio()
  -> load full audio
  -> compute core boundaries
  -> export overlapped chunks
```

### `videocaptioner/core/asr/transcribe.py`

Enable the strategy first for:

- `TranscribeModelEnum.MIMO_ASR_API`
- `TranscribeModelEnum.QWEN_LOCAL_ASR`

Keep other ASR backends on fixed splitting until tested.

### UI Configuration

Add later, not in the first internal prototype:

- Boundary mode: fixed / silence / VAD
- Boundary search before seconds
- Boundary search after seconds
- Minimum silence duration milliseconds

For the first prototype, constants are acceptable.

## Failure Modes

| Failure Mode | Expected Behavior |
| --- | --- |
| No silence in search window | Fall back to hard limit. |
| Silence found too early | Enforce `min_chunk_length_ms`. |
| Candidate would exceed max chunk length | Reject candidate. |
| VAD model missing | Fall back to energy mode or fixed mode. |
| Very noisy audio | Prefer fixed mode rather than unstable boundaries. |
| Many tiny gaps | Require minimum silence duration. |
| Empty chunk after boundary adjustment | Reject boundary and fall back. |
| Boundary causes poor merge | Existing overlap and `ChunkMerger` remain the safety net. |

## Tests

Unit tests:

- fixed mode produces the current boundary behavior
- silence mode chooses a silence near the target
- silence mode respects max chunk length
- no candidate falls back to fixed boundary
- overlap offset equals exported chunk start
- final chunk reaches full audio duration
- short audio bypasses chunking

Fixture-style tests:

- synthetic audio with tone + silence near target
- synthetic audio with no silence
- synthetic audio with silence after hard limit, which must be rejected
- noisy low-volume region to verify threshold behavior

Regression tests:

- MiMo/Qwen 5-minute configuration never produces a chunk longer than 300s
- `ChunkMerger` receives correct offsets after boundary snapping

## Acceptance Criteria

The feature is ready when:

- fixed mode remains behaviorally unchanged
- smart boundary mode never exceeds backend max chunk length
- chunk offsets are correct in merged SRT output
- no audio is removed from the timeline
- a synthetic boundary-cut word case improves or at least does not regress
- failure paths fall back to fixed splitting without crashing

## Open Questions

1. Should the first implementation use energy silence detection only, or add
   Silero VAD immediately?
2. Should smart boundary mode be hidden behind a config flag at first?
3. Should MiMo and Qwen share the same boundary parameters?
4. Should the UI expose this as an advanced setting, or keep it automatic?
5. Should `ChunkMerger` later get a central-confidence mode similar to
   chunk/stride ASR pipelines?

## Recommended Implementation Order

1. Refactor `_split_audio()` to separate boundary planning from chunk export.
2. Add fixed-boundary tests to lock current behavior.
3. Add energy-based boundary snapping.
4. Enable it only for MiMo/Qwen behind an internal flag.
5. Test on long English speech video and Chinese speech video.
6. Decide whether Silero VAD is worth the dependency after measuring failures.
7. Add UI settings only after the defaults are stable.

## Deferred Ideas

- Silero VAD full-audio pass
- pyannote-based segmentation
- pure VAD speech packing
- speaker-aware chunking
- central-confidence merge mode
- per-backend chunk limit registry

## Glossary

- **ASR**: Automatic speech recognition.
- **VAD**: Voice activity detection; detects speech and non-speech regions.
- **Forced alignment**: Aligns known transcript text back to audio time.
- **Chunk core**: The non-overlapped intended audio span for a chunk.
- **Exported chunk**: The actual audio sent to ASR, including overlap.
- **Boundary snapping**: Moving a chunk boundary to a better nearby point.
- **Hard limit**: The latest allowed boundary that keeps a chunk under backend limits.
- **Fallback boundary**: The fixed boundary used when no smart candidate is safe.
