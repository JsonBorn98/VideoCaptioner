---
name: videocaptioner-asr
description: Work on VideoCaptioner's ASR module. Use this skill whenever the user asks about transcription, ASR backends, MiMoASR, Qwen3ASR, word timestamps, forced alignment, chunking, VAD/silence handling, ASR benchmarks, ASR regressions, Qwen runtime/worker cleanup, or debugging long-media transcription in this repository.
---

# VideoCaptioner ASR Module

This skill is for maintaining and debugging the ASR subsystem in the
VideoCaptioner repository. It captures the post-optimization architecture and
the checks that keep Qwen/MiMo long-media transcription fast and reliable.

## First Reads

Before editing code, read the current docs that match the task:

- `docs/config/asr.md` for user-facing behavior, CLI flags, env vars, and
  long-media benchmark commands.
- `docs/dev/asr-mimo-qwen-lessons.md` for backend contracts, Qwen/MiMo
  implementation decisions, known boundaries, and recorded 20 minute
  acceptance results.
- `docs/dev/archive/asr-optimization-plan.md` only as historical context. It
  is an archived execution plan, not the current source of truth.

When a change alters behavior, update `docs/config/asr.md` for user-visible
options and `docs/dev/asr-mimo-qwen-lessons.md` for engineering decisions or
acceptance evidence.

## Architecture Map

Use these files as the main orientation points:

| Area | Files |
| --- | --- |
| ASR factory and backend selection | `videocaptioner/core/asr/transcribe.py` |
| Shared backend contract and cache metadata | `videocaptioner/core/asr/base.py` |
| Long-media chunking, VAD boundaries, retries, MiMo two-stage pipeline | `videocaptioner/core/asr/chunked_asr.py` |
| MiMo chat-audio API backend and Qwen alignment stage | `videocaptioner/core/asr/mimo_asr.py` |
| Local Qwen ASR wrapper and persistent worker pool | `videocaptioner/core/asr/qwen_local_asr.py` |
| Worker process JSONL protocol | `videocaptioner/core/asr/qwen_worker.py` |
| qwen-asr runtime calls, model cache, PCM conversion, timestamp normalization | `videocaptioner/core/asr/qwen_runtime.py` |
| Qwen runtime installation and inspection | `videocaptioner/core/asr/qwen_runtime_manager.py` |
| Anomaly, repetition, coverage, and clamp checks | `videocaptioner/core/asr/anomaly.py` |
| Estimated timing fallback | `videocaptioner/core/asr/text_timing.py` |
| FFmpeg audio extraction and optional loudnorm | `videocaptioner/core/utils/video_utils.py` |
| CLI config/env/validation | `videocaptioner/cli/main.py`, `videocaptioner/cli/config.py`, `videocaptioner/cli/validators.py`, `videocaptioner/cli/commands/transcribe.py`, `videocaptioner/cli/commands/doctor.py` |
| GUI config and ASR setting widgets | `videocaptioner/ui/common/config.py`, `videocaptioner/ui/components/MimoASRSettingWidget.py`, `videocaptioner/ui/components/QwenASRSettingWidget.py`, `videocaptioner/ui/thread/transcript_thread.py`, `videocaptioner/ui/task_factory.py` |
| Repeatable ASR benchmark harness | `scripts/asr_benchmark.py` |

## Current Backend Model

Keep these invariants in mind:

- MiMo uses an OpenAI-style chat completion endpoint with inline
  `input_audio`, not `/v1/audio/transcriptions`.
- MiMo returns transcript text only. If subtitle timestamps are requested, it
  must use local Qwen3-ForcedAligner; do not silently emit fake full-duration
  cues when timestamps are required.
- Qwen local ASR and Qwen alignment run in an isolated persistent worker
  subprocess. The GUI/main process should not load torch/qwen models directly.
- The Qwen worker communicates over JSON Lines stdin/stdout and keeps model
  caches alive for the task. If the worker crashes, the pool owns restart and
  cleanup behavior.
- Strip PyQt Qt DLL paths before starting Qwen workers. This prevents native
  torch/qwen libraries from resolving Qt-bundled runtime DLLs first.
- Qwen first-pass normal chunks can be submitted as one batch after cache
  checks. Degraded chunks still retry through the single-chunk/sub-chunk path.
- MiMo word mode is a two-stage pipeline: remote API transcript requests may
  run concurrently, while local Qwen alignment is consumed in chunk order by
  the persistent worker.
- Qwen uses 5 minute source ranges. MiMo uses 3 minute chunks when Qwen word
  alignment is enabled and 5 minute chunks for text-only mode.
- MiMo/Qwen chunk boundaries default to VAD-aware mode. Prefer optional Silero
  VAD, fall back to energy detection, then fixed boundaries when needed.
- Pure-silence chunks should skip ASR. Non-silent chunks that return empty
  results should be treated as degraded and retried.
- Cache identities for chunks should be based on the original source hash and
  source time range, not re-exported MP3 bytes.
- Write ASR cache only after segment conversion succeeds. Bump cache key
  versions when timestamp semantics change.
- Audio loudnorm is opt-in. It applies EBU R128 `loudnorm=I=-16:TP=-1.5:LRA=11`
  while extracting the shared 16 kHz mono WAV.

## Change Workflow

1. Identify the backend contract first: Whisper multipart API, chat-audio API,
   local model, or text-only backend. Do not merge different contracts because
   they share "OpenAI-compatible" branding.
2. Trace factory wiring from CLI/GUI config into `TranscribeConfig`, then into
   `videocaptioner/core/asr/transcribe.py`.
3. Decide whether the change belongs in the backend, `ChunkedASR`, the worker
   pool, `qwen_runtime`, or UI/CLI config. Prefer the narrowest layer that owns
   the behavior.
4. Preserve cancellation and cleanup. Qwen/MiMo long jobs should close worker
   pools and temporary audio workspaces on success, exception, and user stop.
5. Keep secrets out of commands and reports. For MiMo validation, prefer
   `VIDEOCAPTIONER_MIMO_ASR_API_KEY` over `--mimo-api-key`; benchmark reports
   store command arrays.
6. Update tests near the layer you touched. Avoid real API calls in normal
   tests; mark credential-dependent tests as integration and skip cleanly.
7. Update docs when the user-facing behavior, troubleshooting procedure, or
   benchmark gate changes.

## Debugging Guide

Start with the symptom:

| Symptom | First checks |
| --- | --- |
| Qwen says CUDA unavailable or uses CPU | Run `uv run videocaptioner doctor --profile qwen`; inspect `AppData/logs/app.log` around `qwen_runtime_manager`; confirm runtime torch shows `+cu128` instead of `+cpu`. |
| Qwen worker remains after cancellation/error | Run `scripts/asr_benchmark.py --check-qwen-cleanup`; inspect `QwenWorkerPool.close/terminate`, `transcript_thread.py` cleanup, and process snapshots in report JSON. |
| Word timestamps are missing | Check whether the backend has native timestamps or an aligner path; for MiMo, inspect `run_alignment_stage`; for Qwen, inspect `timestamp_items_to_segments` and ForcedAligner output normalization. |
| MiMo validation works in GUI but CLI says missing key | GUI settings may live in `AppData/settings.json`, while CLI config is `AppData/Local/videocaptioner/.../config.toml`. Do not print keys; only report presence with masking. |
| Long-media output drifts across silence | Check VAD/energy speech ranges, `speech_ranges_ms`, and `text_timing.py` estimated timing fallback. |
| Repeated or hallucinated text appears | Check `anomaly.py`, alignment coverage, repeated n-gram detection, non-silent empty result retry, and whether pure silence was skipped. |
| Chunks fail near boundaries | Check `chunk_boundary_mode`, overlap seconds, boundary snapping, retry sub-chunk split points, and `ChunkMerger` warnings. |
| MiMo payload is too large | Check `MAX_RAW_AUDIO_BYTES_FOR_BASE64`, exported bitrate, and measured split logic before blaming the API. |

Useful commands:

```powershell
uv run videocaptioner doctor --profile qwen
uv run videocaptioner transcribe tests/fixtures/audio/en_20min.mp3 --asr qwen-local --word-timestamps --qwen-device cuda:0 -o work-dir/en_qwen.srt
uv run pytest tests/test_asr/ -q
uv run pytest tests/test_utils/test_asr_benchmark.py -q
uv run ruff check .
uv run pyright
```

## Benchmark And Acceptance

Use fixed 20 minute fixtures for long-media validation:

- `tests/fixtures/audio/zh_20min.mp3`
- `tests/fixtures/audio/en_20min.mp3`

Qwen CUDA acceptance:

```powershell
uv run python scripts/asr_benchmark.py `
  --case zh=tests/fixtures/audio/zh_20min.mp3 `
  --case en=tests/fixtures/audio/en_20min.mp3 `
  --asr qwen-local `
  --variant cuda="--word-timestamps --qwen-device cuda:0" `
  --variant cuda-compile="--word-timestamps --qwen-device cuda:0 --qwen-compile-aligner" `
  --required-label zh `
  --required-label en `
  --required-variant cuda `
  --required-variant cuda-compile `
  --min-duration-seconds 1200 `
  --check-qwen-cleanup
```

MiMo word-timestamp acceptance needs a real API key. Pass it as an environment
variable rather than a command-line argument:

```powershell
$env:VIDEOCAPTIONER_MIMO_ASR_API_KEY = "<key>"
uv run python scripts/asr_benchmark.py `
  --case zh=tests/fixtures/audio/zh_20min.mp3 `
  --case en=tests/fixtures/audio/en_20min.mp3 `
  --asr mimo-asr `
  --variant word="--word-timestamps --qwen-device cuda:0 --qwen-model-dir AppData/models --qwen-asr-model Qwen/Qwen3-ASR-0.6B --qwen-aligner-model Qwen/Qwen3-ForcedAligner-0.6B" `
  --required-label zh `
  --required-label en `
  --required-variant word `
  --min-duration-seconds 1200 `
  --check-qwen-cleanup
```

Read `report.json` for `acceptance.passed`, missing labels/variants, failed
cases, duration failures, and process cleanup failures. `benchmark-output/` is
ignored by git, so summarize important numbers in docs or the final response
when they matter.

## Test Coverage Checklist

For ASR changes, choose focused coverage from this list:

- `tests/test_asr/test_chunked_asr.py` for chunk orchestration, cache identity,
  stage behavior, and backend wiring through `ChunkedASR`.
- `tests/test_asr/test_chunked_retry.py` for degraded result retry behavior.
- `tests/test_asr/test_chunked_smart_boundary.py` for VAD/silence boundary
  planning and silence skipping.
- `tests/test_asr/test_mimo_qwen_asr.py` for MiMo payloads, response parsing,
  alignment normalization, anomaly checks, and timestamp-required behavior.
- `tests/test_asr/test_qwen_worker_pool.py` for persistent worker protocol,
  restart, cancellation, and cleanup.
- `tests/test_cli/test_parser.py` and `tests/test_cli/test_config.py` for
  CLI/env/config propagation.
- `tests/test_utils/test_asr_benchmark.py` for benchmark report and acceptance
  logic.

Run broader checks when touching shared contracts:

```powershell
uv run pytest -m "not integration" -q
uv run ruff check .
uv run pyright
```

## Documentation Discipline

Keep active docs current:

- `docs/config/asr.md` should describe user-facing ASR engines, CLI flags,
  env vars, runtime setup, loudnorm, chunking, and benchmark commands.
- `docs/dev/asr-mimo-qwen-lessons.md` should preserve implementation lessons,
  recorded acceptance results, known boundaries, and test strategy.
- `docs/dev/archive/` is for historical plans, stale sketches, and one-off
  research. Do not rely on archived docs as the current implementation source
  without checking active docs and code.

When archiving a doc, add or update `docs/dev/archive/README.md` with a short
reason so future agents understand why it moved.

## Safe Secret Handling

Never ask the user to paste API keys into chat when a local config or env var
can be used. If you must inspect config, parse it locally and print only a
masked value such as `sk-...abcd` plus length. Avoid passing secrets through
CLI arguments because command arrays may be written to reports.

Known config split:

- GUI settings: project/user `AppData/settings.json` style files with
  `MiMoASR.ApiKey`.
- CLI config: platform config path for `videocaptioner config`, with
  `transcribe.mimo_asr.api_key`.

These do not automatically imply each other.
