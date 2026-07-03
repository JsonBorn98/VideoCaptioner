# MiMo and Qwen ASR Backend Lessons

Date: 2026-07-03

Status: Implemented notes from the MiMoASR API and Qwen3ASR local integration.

## Summary

This note captures the practical lessons from adding:

- `MiMoASR [API]`
- `Qwen3ASR [Local]`
- local `Qwen/Qwen3-ForcedAligner-0.6B` timestamp alignment
- stop/cancel handling for long transcription tasks

The main lesson is that "OpenAI-compatible" is not enough to decide whether an
ASR service can share a backend. Audio routes, payload shape, timestamp support,
response shape, and file handling all matter.

## Backend Contract Boundaries

### Whisper API

Use this only for services compatible with:

- `POST /v1/audio/transcriptions`
- multipart local audio upload
- Whisper-style response formats such as `verbose_json`, `json`, or `text`
- optional `timestamp_granularities`

Some compatible gateways reject `timestamp_granularities` or `verbose_json`, so
the client should progressively retry simpler request shapes before failing.

### Chat Audio ASR

Do not force a chat-audio transcription service into the Whisper backend when it
uses:

- `POST /v1/chat/completions`
- `messages[].content[].type = "input_audio"`
- URL-only audio input
- plain assistant message content as transcription text

That is a different backend contract even if the gateway itself advertises
OpenAI compatibility.

### MiMo ASR API

MiMo ASR uses an OpenAI-style chat completion surface with inline `input_audio`.
The implementation sends base64 MP3/WAV chunks and expects transcription text.

Important constraints:

- MiMo returns transcription text, not native segment/word timestamps.
- If subtitle timestamps are requested, run local Qwen3-ForcedAligner on the
  same audio chunk and returned transcript.
- If alignment returns no timestamps while timestamps were requested, fail
  loudly instead of generating one unusable full-duration SRT cue.
- Keep chunking enabled. Five-minute chunks keep payloads bounded and keep the
  aligner in its reliable window.
- Connection testing must use a tiny bundled sample audio file, not the user's
  currently selected media.

### Qwen3 Local ASR

The local Qwen backend uses the official `qwen-asr` runtime. Treat it as an
optional dependency because it pulls large ML packages and is not needed by
every VideoCaptioner user.

Operational notes:

- The GUI may run from a release bundle or from the repository `uv run`
  environment. Use the managed Qwen runtime under `runtimes/qwen` instead of
  assuming `qwen-asr` is installed in the main Python environment.
- Runtime installation must control the PyTorch backend explicitly. Plain
  `uv pip install qwen-asr` resolves `torch` from the default index and can
  install CPU PyTorch even when the user clicked a CUDA path.
- For CUDA runtime installs, pass `--torch-backend cu128` while installing the
  whole `qwen-asr` dependency graph, then reinstall `torch` last with
  `--reinstall-package torch --torch-backend cu128 torch`. This handles both
  fresh installs and repair of an existing runtime polluted by CPU PyTorch.
- On Windows, prefer `UV_LINK_MODE=copy` for GUI-driven runtime installs.
  Hardlink/cache operations can fail with access denied when files are locked
  by AV/indexers or stale Python processes.
- `Qwen/Qwen3-ASR-1.7B` plus `Qwen/Qwen3-ForcedAligner-0.6B` can run close to a
  16 GB VRAM limit. `Qwen/Qwen3-ASR-0.6B` is the safer default for long jobs.
- `dtype=auto` should prefer CUDA half precision when available. Expose
  `bfloat16`, `float16`, and `float32` for troubleshooting.
- Keep `max_new_tokens` configurable. Start with `2048`; increase only when
  chunk transcripts are truncated.
- Limit inference concurrency to one. Loading both ASR and aligner models is
  already memory-heavy.
- Run local Qwen inference in a worker subprocess, not in the PyQt GUI process.
  This isolates torch/CUDA native libraries and prevents GUI cleanup from
  touching Qwen model cache in the main process.

## Timestamp Lessons

A high-quality ASR transcript is not the same as a subtitle-ready result.

For SRT/ASS output, the backend needs one of these:

- native segment timestamps
- native word/character timestamps
- a forced aligner that maps transcript text back to audio time

Plain text can be split into readable cues, but the timings are only estimated.
When `need_word_time_stamp=True`, estimated timing should not be silently used
as if it were real alignment.

For Qwen3-ForcedAligner, normalize runtime outputs defensively:

- some calls return lists of timestamp dicts
- some calls return objects
- `align()` can return `ForcedAlignResult` wrappers whose timestamps live in
  `.items`

Tests should cover all of these shapes.

## Chunking Lessons

Long media should be chunked automatically. The user should not have to
manually split a long video before using Qwen or MiMo.

Current policy:

- use five-minute chunks for Qwen and MiMo
- use one worker
- keep an overlap setting, defaulting to 10 seconds
- pass the real exported chunk start as the offset
- use the original media directory for temporary audio workspace
- delete temporary audio/workspace on success, error, or cancellation

The next improvement should be smart boundary chunking: keep the five-minute
target, but snap boundaries to nearby silence or VAD non-speech points. See
`docs/dev/asr-smart-boundary-chunking.md`.

## Cache and Failure Handling

Avoid caching bad partial results:

- run `_make_segments()` before writing the raw ASR response to cache
- version cache keys when post-processing semantics change
- include model, language, timestamp flag, dtype/device, and aligner in the key

For text-only backends:

- when timestamps are not requested, split text into readable estimated cues
- when timestamps are requested, fail if no real timestamps can be produced

## GUI Lessons

Long local ASR jobs need cancellation:

- expose a stop button while transcription is running
- request cooperative cancellation first
- force terminate only after a short grace period
- clean temporary files and release Qwen model/CUDA cache in both paths

Logging should avoid duplicate console lines. VideoCaptioner loggers should set
`propagate = False` after attaching their own handlers.

Runtime install progress should be throttled before it reaches Qt widgets.
Large `uv` installs can emit many lines; write full output to `app.log` and only
send summarized progress to the UI.

## Testing Checklist

When adding or changing an ASR backend, include focused tests for:

- factory wiring from `TranscribeConfig`
- API payload shape
- response parsing for object and dict responses
- text-only fallback behavior
- timestamp-required failure behavior
- forced-aligner wrapper normalization
- cache key versioning when segment semantics change
- connection-test retry/fallback behavior

Avoid tests that require real API keys unless they are marked integration and
skip cleanly without credentials.
