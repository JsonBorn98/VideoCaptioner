# E2E Acceptance Checklist

Date: 2026-06-09

## Scope

This checklist tracks a hands-on acceptance pass for VideoCaptioner after the
settings/page/component refactor. It covers user-facing UI behavior, CLI/core
workflow behavior, generated artifacts, cross-theme screenshots, and code
structure.

## Test Assets

- Short Chinese audio: `tests/fixtures/audio/zh.mp3`
- Short English audio: `tests/fixtures/audio/en.mp3`
- Sample subtitle: `tests/fixtures/subtitle/sample_en.srt`
- Generated temporary video: `/tmp/vc-e2e-assets/sample.mp4`
- Generated outputs: `/tmp/vc-e2e-output`

## Acceptance Matrix

### 1. Environment And Assets

- [ ] FFmpeg exists and reports version.
- [ ] FFprobe exists and reports version.
- [ ] ASS/subtitles filter capability is detected.
- [ ] Temporary sample video can be generated.
- [ ] Temporary output directory is clean before test.

### 2. Settings And Config Sync

- [ ] Settings pages render: transcribe, LLM, translate service, translate,
  subtitle synthesis, dubbing, save, personal, about.
- [ ] Provider-specific rows hide/show correctly.
- [ ] LLM model loading and connection testing are separate UI states.
- [ ] Changed settings persist to TOML and reload into UI state.
- [ ] Secrets are stripped before persistence/use.

### 3. Page Click And Layout Smoke

- [ ] Home, task creation, transcription, subtitle, subtitle style, synthesis,
  dubbing, doctor, settings, logs construct without exceptions.
- [ ] Dark theme screenshot sheet is visually inspected.
- [ ] Light theme screenshot sheet is visually inspected.
- [ ] Compact-width screenshot sheet is visually inspected.
- [ ] Buttons remain centered and columns do not resize unexpectedly.
- [ ] No obvious text clipping, bottom-border clipping, or large accidental
  blank bands.

### 4. Standalone Transcription

- [ ] CLI parser accepts transcription options.
- [ ] A local audio/video path can be submitted to an available transcription
  provider.
- [ ] Output subtitle exists and has non-empty text.
- [ ] Bad input paths fail with a clear error and do not hang.

### 5. Subtitle Processing

- [ ] Existing SRT can be split.
- [ ] Existing SRT can be optimized through mocked/local LLM path.
- [ ] Existing SRT can be translated through mocked/local LLM path.
- [ ] Existing SRT can be translated through available free translator if
  network allows.
- [ ] Output SRT/ASS files are valid and keep timestamps.

### 6. Subtitle Style And Video Synthesis

- [ ] Subtitle preview supports short, medium, long, and custom text states.
- [ ] ASS style synthesis writes a playable video.
- [ ] Rounded subtitle style synthesis writes a playable video.
- [ ] Soft subtitle synthesis writes a playable video/container.
- [ ] Mode toggles do not cause layout jumps.

### 7. Dubbing And Voice Clone

- [ ] Provider switching keeps valid voice lists.
- [ ] Edge preview/generation path handles no-key baseline correctly.
- [ ] Gemini/SiliconFlow fail clearly without key.
- [ ] SiliconFlow clone UI supports upload/clear/reference text states.
- [ ] Dubbing pipeline can create a timeline audio artifact with mocked TTS.
- [ ] Dubbing output can be muxed or composed into a video when prerequisites
  are present.

### 8. Full Process

- [ ] Local video path can run through transcribe -> subtitle processing ->
  optional dubbing -> video synthesis using available local/mock paths.
- [ ] URL input path validates state and fails clearly when network/provider is
  unavailable.
- [ ] Progress and action button states are sane during processing.
- [ ] Final output is written to the selected output directory.

### 9. Code Structure Review

- [ ] Each page file has a clear responsibility.
- [ ] New reusable widgets are used where appropriate.
- [ ] Legacy setting-card files have no runtime references.
- [ ] `core` does not import `ui`; `cli` does not import `ui`.
- [ ] Provider-specific configuration lives behind adapters/config store.
- [ ] Obvious naming collisions and overly generic component names are avoided.
- [ ] Cross-platform path/font/FFmpeg assumptions are documented or handled.

## Notes

- Live provider checks that need real keys/network should be recorded separately
  from local deterministic checks.
- Passing negative-path tests may log `ERROR`; that should be recorded as log
  noise only when the behavior is intentionally under test.
