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
- Keep local Qwen ASR model access serialized inside the persistent worker, but
  submit the normal first-pass chunks as one batch request so qwen-asr can use
  `max_inference_batch_size`. Degraded chunks still fall back to the existing
  per-chunk retry ladder.
- Run local Qwen inference in a worker subprocess, not in the PyQt GUI process.
  This isolates torch/CUDA native libraries and prevents GUI cleanup from
  touching Qwen model cache in the main process.
- Keep the Qwen worker process alive for the whole task. The ASR and aligner
  caches live inside the worker process, so a one-shot process per chunk turns
  every chunk into a cold model load.
- Strip PyQt Qt DLL paths once when starting the worker, then send JSON Lines
  requests over stdin/stdout. This keeps the runtime isolated without paying
  process startup for every chunk.
- On a single CUDA device, prefer `device_map="cuda:0"` over accelerate's
  generic `auto` sharding. Use `torch.inference_mode()` for ASR/alignment
  calls and only clear CUDA cache at task/process boundaries.
- Treat FlashAttention as an optimization, not a requirement. If
  `flash_attn` is available, request `flash_attention_2`; otherwise use SDPA
  and fall back again if the loader rejects the attention hint.
- Keep `torch.compile` for Qwen3-ForcedAligner opt-in and best-effort. It may
  help long alignment-heavy jobs after warmup, but it can also fail on runtime
  or model internals, so failures should log a warning and continue with the
  original aligner.

## Timestamp Lessons

A high-quality ASR transcript is not the same as a subtitle-ready result.

For SRT/ASS output, the backend needs one of these:

- native segment timestamps
- native word/character timestamps
- a forced aligner that maps transcript text back to audio time

Plain text can be split into readable cues, but the timings are only estimated.
When `need_word_time_stamp=True`, estimated timing should not be silently used
as if it were real alignment.

When silence-aware chunking has already detected relative speech ranges, pass
those ranges into any estimated-timestamp fallback. The fallback is still
degraded and should be logged as such, but cue timing should be distributed over
detected speech islands instead of smeared across long silence.

For Qwen3-ForcedAligner, normalize runtime outputs defensively:

- some calls return lists of timestamp dicts
- some calls return objects
- `align()` can return `ForcedAlignResult` wrappers whose timestamps live in
  `.items`
- when MiMo uses automatic language detection and does not return a language
  label, infer only clear aligner-supported languages from transcript script or
  distinctive words; otherwise fall back to English/Chinese rather than making
  a brittle guess

Tests should cover all of these shapes.

## Chunking Lessons

Long media should be chunked automatically. The user should not have to
manually split a long video before using Qwen or MiMo.

Audio preprocessing should remain conservative by default. Keep EBU R128
loudnorm opt-in, and apply it while extracting the shared 16 kHz mono WAV
source so every ASR backend sees the same normalized input when the user enables
it. This is useful for uneven meeting/class recordings, but stable sources do
not need the extra FFmpeg filtering.

Current policy:

- use five-minute chunks for Qwen local ASR
- send Qwen local first-pass chunks to the persistent worker as one batch
  request after per-chunk cache checks; failed/degraded batch items retry
  independently through the existing single-chunk/sub-chunk path
- use three-minute MiMo chunks when Qwen word alignment is enabled, otherwise
  five-minute MiMo text-only chunks
- run MiMo through an explicit two-stage chunk pipeline: remote API transcript
  requests run with modest network concurrency, while local Qwen alignment is
  consumed in chunk order through the single persistent Qwen worker. This keeps
  later API requests moving while earlier chunks wait for GPU alignment.
- keep an overlap setting, defaulting to 10 seconds
- for MiMo/Qwen, snap chunk boundaries to nearby non-speech gaps. Prefer
  Silero VAD when its package or torch hub model can be loaded; fall back to
  energy-based detection and then fixed boundaries when no safe candidate
  exists
- skip chunks whose VAD/energy profile is effectively silent; if a non-silent
  chunk returns no subtitles, treat it as degraded and retry instead of
  silently accepting an empty transcript
- avoid exporting audio payloads for chunks already classified as pure silence
- when retrying by splitting a degraded chunk, reuse silence-aware split points
  and add a short overlap between retry sub-chunks
- pass relative non-silent ranges into MiMo/Qwen backends so degraded estimated
  timestamps avoid long detected silence gaps
- pass the real exported chunk start as the offset
- pass the chunk duration from the splitter into ASR instances so each backend
  does not have to decode the same chunk again just to compute duration
- split retry sub-chunks from the already loaded source audio when available,
  with chunk-byte decoding only as a fallback
- for local Qwen ASR, pass the original audio path plus source start/duration
  to the worker instead of exporting chunk payloads in the GUI process; when a
  range or byte payload is needed, the isolated worker decodes it once into
  qwen-asr's in-memory `(float32_ndarray, 16000)` input form instead of writing
  a temporary WAV
- pass a stable chunk cache identity based on the original audio hash and
  source time range, not the re-exported chunk bytes; retry sub-chunks should
  get identities for their actual source ranges
- convert MiMo's base64 payload limit to raw audio bytes before chunk export;
  if an exported payload still exceeds the limit, split it again by measured
  output bitrate instead of waiting for the API backend to raise
- use the original media directory for temporary audio workspace
- delete temporary audio/workspace on success, error, or cancellation
- keep the path-based temp WAV helper only as a compatibility fallback when
  in-memory PCM preparation is unavailable

## Cache and Failure Handling

Avoid caching bad partial results:

- run `_make_segments()` before writing the raw ASR response to cache
- version cache keys when post-processing semantics change
- include model, language, timestamp flag, dtype/device, and aligner in the key
- keep transport hashes separate from cache identity: upload protocols such as
  JianYing still need the real current-byte CRC32, while cache keys may use a
  stable caller-provided identity
- keep anomaly thresholds grouped in an `AnomalyThresholds` dataclass. The
  module-level constants should remain as compatibility aliases, but new checks
  should accept the dataclass so density/repetition/coverage gates can be tuned
  without scattering magic numbers.
- keep MiMo's task-local API request memo separate from the persistent ASR
  cache. The memo can reuse the same remote transcript response across retry
  paths for one transcription task, while the persistent cache should still
  only be written after `_make_segments()` has produced a usable result.

For text-only backends:

- when timestamps are not requested, split text into readable estimated cues
- when timestamps are requested, fail if no real timestamps can be produced

For LLM subtitle splitting:

- do not let a run of unmatched LLM sentences abort or discard an otherwise
  usable ASR span. Preserve already matched groups, pass skipped or remaining
  ASR word segments through local rule splitting, and return the best mixed
  result.

## Verification Status

Current automated coverage proves the worker lifecycle, batch request protocol,
cache keys, silence boundary behavior, degraded-result handling, CLI wiring,
and factory wiring. Real 20 minute Chinese/English acceptance was also run on
the target Windows/CUDA development machine after the optimization work.

Recorded acceptance reports:

- Qwen local CUDA report:
  `benchmark-output/asr-20min/20260705T054725Z/report.json`
  - `acceptance.passed = true`
  - Chinese CUDA: 1200.0s media, 100.907s elapsed, realtime factor 0.084,
    6492 cues
  - Chinese CUDA + compile aligner: 84.762s elapsed, realtime factor 0.071,
    6492 cues
  - English CUDA: 132.687s elapsed, realtime factor 0.111, 4199 cues
  - English CUDA + compile aligner: 124.937s elapsed, realtime factor 0.104,
    4199 cues
- MiMo word-timestamp report:
  `benchmark-output/asr-mimo-20min/20260705T061259Z/report.json`
  - `acceptance.passed = true`
  - Chinese word mode: 1200.0s media, 57.721s elapsed, realtime factor 0.048,
    6410 cues
  - English word mode: 1200.006s media, 38.389s elapsed, realtime factor 0.032,
    4147 cues
  - API key was supplied through `VIDEOCAPTIONER_MIMO_ASR_API_KEY`; it was not
    written to the command array or benchmark report
- Both reports used `--check-qwen-cleanup`; no new Qwen worker processes
  remained after the cases finished.

Known implementation boundaries:

- silence-aware chunking prefers optional Silero VAD and falls back to pydub
  energy detection when Silero is unavailable or fails; Silero is not a hard
  dependency of the main application
- local Qwen receives original source paths and source ranges, then decodes
  source ranges or byte payloads into qwen-asr's numpy PCM input form inside
  the worker; it does not yet use shared memory between the GUI and worker
  processes
- local Qwen batch mode is implemented at the worker/runtime/chunker layers,
  and has been validated by the 20 minute CUDA benchmark above. Further tuning
  of `max_inference_batch_size` can still be measured separately if the target
  qwen-asr runtime changes.
- MiMo has an explicit two-stage path inside `ChunkedASR` for backends that
  expose transcript and alignment stages. It is covered by unit tests and the
  20 minute MiMo word-timestamp acceptance run above.
- CPU Qwen runtime remains a validation/troubleshooting path, not a production
  performance target

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

For performance validation, keep a fixed local media set and run:

```bash
uv run python scripts/asr_benchmark.py \
  --case zh=tests/fixtures/audio/zh_20min.mp3 \
  --case en=tests/fixtures/audio/en_20min.mp3 \
  --asr qwen-local \
  --variant cuda="--word-timestamps --qwen-device cuda:0" \
  --variant cuda-compile="--word-timestamps --qwen-device cuda:0 --qwen-compile-aligner" \
  --required-label zh \
  --required-label en \
  --required-variant cuda \
  --required-variant cuda-compile \
  --min-duration-seconds 1200 \
  --check-qwen-cleanup
```

The script writes a timestamped `benchmark-output/asr/.../report.json` with the
exact command, elapsed time, media duration, realtime factor, cue count,
stdout/stderr log paths, variant metadata, and an `acceptance` summary that
flags missing labels, missing variants, missing label/variant matrix cases,
failed cases, samples shorter than the requested minimum, or newly leaked Qwen
worker processes when `--check-qwen-cleanup` is enabled. This is only a
repeatable harness; real CUDA/Qwen/MiMo acceptance still requires running it on
the target machine with real long media and, for MiMo, valid API credentials.

MiMo word-timestamp validation uses the same harness, but requires real API
credentials and a local Qwen aligner runtime:

```bash
VIDEOCAPTIONER_MIMO_ASR_API_KEY=your-key \
uv run python scripts/asr_benchmark.py \
  --case zh=tests/fixtures/audio/zh_20min.mp3 \
  --case en=tests/fixtures/audio/en_20min.mp3 \
  --asr mimo-asr \
  --variant word="--word-timestamps" \
  --required-label zh \
  --required-label en \
  --required-variant word \
  --min-duration-seconds 1200 \
  --check-qwen-cleanup
```
