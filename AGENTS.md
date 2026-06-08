# VideoCaptioner Agent Guide

This file is the entry point for Codex/Claude-style agents working in this
repository. `CLAUDE.md` is intentionally a symlink to this file so the project
has one source of truth.

## Product

VideoCaptioner is a desktop and CLI app for video subtitle workflows:

```text
video/audio input
  -> ASR transcription
  -> subtitle splitting / LLM polish / translation
  -> optional dubbing
  -> soft subtitle, hard subtitle, or dubbed final video
```

The app has two product surfaces:

- CLI: `videocaptioner transcribe|subtitle|synthesize|dub|process|doctor|config`
- GUI: PyQt5 desktop app launched by `uv run videocaptioner`,
  `.venv/bin/python -m videocaptioner`, or `videocaptioner gui`

The UX is Chinese-first in the current desktop app. Many provider names and
settings labels are user-facing Chinese strings; keep them natural and concise.

## Current Architecture

Keep these boundaries strict:

```text
videocaptioner/core/        business logic, no PyQt dependency
videocaptioner/core/application/
  config_store.py           shared TOML persistence and defaults
  app_config.py             plain dataclass settings consumed by business flows
  task_builder.py           builds workflow task configs from AppConfig
videocaptioner/cli/         argparse commands, no UI imports
videocaptioner/cli/config_adapter.py
                            TOML/CLI dict -> AppConfig
videocaptioner/ui/          PyQt desktop app, may import core
videocaptioner/ui/config_adapter.py
                            UI state -> AppConfig
videocaptioner/ui/common/config.py
                            in-memory UI settings over the shared TOML store
videocaptioner/ui/common/settings_state.py
                            first-party SettingField state, not qfluent QConfig
videocaptioner/ui/thread/   QThread wrappers around long-running core tasks
videocaptioner/ui/view/     pages
videocaptioner/ui/components/
                            reusable first-party widgets
```

`core` must not import `ui`. `cli` must not import `ui`. GUI pages should not
construct business config directly from widgets; use `ui.config_adapter` /
`TaskFactory` / `TaskBuilder`.

## Shared Configuration

The current source of truth is TOML:

- Store: `videocaptioner.core.application.config_store`
- Default path: `platformdirs.user_config_dir("videocaptioner") / "config.toml"`
- Test override: `VIDEOCAPTIONER_CONFIG_FILE=/tmp/some-config.toml`
- Priority: CLI flags > environment variables > TOML file > defaults

Important sections:

- `[ui]`: theme, language, preview-only UI state
- `[llm]` and `[llm.providers.*]`: LLM provider settings and cached model options
- `[whisper_api]`, `[fun_asr]`: ASR provider settings
- `[transcribe]`: ASR workflow defaults
- `[subtitle]`, `[translate]`: split/polish/translate workflow defaults
- `[synthesize]`: video/subtitle synthesis defaults
- `[dubbing]`: TTS/dubbing defaults and clone reference state

Secrets must be stripped before saving or sending. API-key newline bugs have
caused invalid request headers such as `Bearer ...\n`; do not bypass the strip
paths in `SettingField`, `config_store`, `TTSConfig`, `SpeechProviderConfig`,
or the CLI/UI adapters.

The settings page taxonomy accepted by the user is:

- 转录配置
- LLM 配置
- 翻译服务
- 翻译与优化
- 字幕合成配置
- 配音配置
- 保存配置
- 个性化
- 关于

Provider-specific fields should only appear when that provider needs them. For
example: Edge dubbing hides key rows; SiliconFlow/Gemini show TTS key/model
rows; Whisper API and Fun-ASR show their own base/key/model fields.

## UI Direction

The current migration direction is first-party UI components with qfluentwidgets
only as a low-level widget/icon source while the shell is being migrated.

Do:

- Use `videocaptioner.ui.common.theme_tokens.app_palette()` for colors.
- Use `videocaptioner.ui.common.app_icons` for app-owned SVG icons.
- Put SVGs in `resource/assets/icons/`.
- Prefer reusable widgets in:
  - `ui/components/form_cards.py`
  - `ui/components/settings_controls.py`
  - `ui/components/subtitle_style_controls.py`
  - `ui/components/workflow_widgets.py`
- Keep manual stylesheet inside reusable components or page-specific media
  preview areas. Avoid scattering large anonymous styles across pages.
- Keep page layouts stable at compact widths; button clicks must not resize
  columns or create large blank bands.

Do not:

- Reintroduce qfluentwidgets native setting-card/config binding.
- Recreate deleted legacy files such as `MySettingCard.py`,
  `app_setting_cards.py`, `WhisperAPISettingWidget.py`, or
  `TranscriptionOutputDialog.py`.
- Use raw unicode arrows for production icons; use `app_icons` or FluentIcon.
- Add explanatory cards just to fill space. This project prefers compact,
  task-oriented pages.

Useful current design anchors:

- `docs/dev/dubbing-page-layout-demos.html`
- `docs/dev/dubbing-clone-layout-demo.html`
- `docs/dev/video-synthesis-layout-demo.html`
- `docs/dev/doctor-page-layout-demos.html`
- `docs/dev/home-page-layout-demos.html`
- `docs/dev/settings-page-layout-demos.html`

For visual work, follow this rhythm:

1. Inspect the current PyQt page and the relevant HTML demo.
2. Make the smallest coherent component/page changes.
3. Run offscreen UI smoke screenshots in dark and light themes.
4. Open the contact sheets and inspect alignment, spacing, text overflow,
   button centering, blank areas, and theme contrast.
5. Only then call the visual work done.

UI smoke commands:

```bash
.venv/bin/python scripts/ui_smoke_check.py /tmp/vc-ui-check-dark --theme dark
.venv/bin/python scripts/ui_smoke_check.py /tmp/vc-ui-check-light --theme light
```

The script exercises page construction, settings navigation, provider switching,
dubbing clone UI, video synthesis mode changes, subtitle-style fullscreen state,
and compact-window states. It also writes contact sheets in the output folders.

## Dubbing And Provider Rules

Dubbing providers currently include Edge, Gemini TTS, and SiliconFlow CosyVoice.

- Edge is the no-key baseline, but still needs network access for real TTS.
- Gemini and SiliconFlow require a TTS API key before preview/generation.
- SiliconFlow CosyVoice is the provider that exposes voice-clone controls:
  upload audio, record, clear, `clone_audio`, and `clone_text`.
- Edge and Gemini should not show unsupported clone controls.
- If Gemini/SiliconFlow preview fails while Edge works, first check provider key
  state; do not diagnose it as a generic audio playback bug.
- If preview errors mention `Invalid header value` or `Bearer ...\n`, inspect
  shared config sanitation across `task_factory.py`, `core/entities.py`,
  `core/speech/models.py`, `core/tts/tts_data.py`, and adapters.

Provider switching has historically caused stale base URL/model/voice state.
Verify switching in the real app and in `scripts/ui_smoke_check.py`; do not
patch only one page.

## FFmpeg And Subtitle Rendering

Do not treat `ffmpeg` existence as enough. ASS/rounded subtitle rendering needs
real filter support.

Quick checks:

```bash
.venv/bin/python - <<'PY'
from videocaptioner.core.subtitle.ass_renderer import ffmpeg_supports_ass_filter
print(ffmpeg_supports_ass_filter())
PY
.venv/bin/python -m videocaptioner doctor
```

Known failure signatures:

- `Unknown filter 'ass'`
- `Unknown filter 'subtitles'`
- `No option name near ... ass=...:fontsdir=...`
- `Error parsing filterchain`
- `Exception: FFmpeg Return code: 234`

If `resource/bin/ffmpeg` or `resource/bin/ffprobe` is relinked or replaced,
restart the running desktop app before retesting. The live process can keep an
old binary/path snapshot.

## Testing Standards

Use fast local checks before expensive or online checks:

```bash
.venv/bin/python -m ruff check videocaptioner tests scripts
.venv/bin/python -m compileall videocaptioner scripts tests
.venv/bin/python -m pytest tests/test_cli/test_config.py tests/test_cli/test_parser.py
.venv/bin/python -m pytest tests/test_asr/test_chunking.py tests/test_asr/test_chunked_asr.py
.venv/bin/python -m pytest tests/test_tts/test_tts_core.py tests/test_subtitle/test_ass_renderer.py
.venv/bin/python -m pytest tests/test_dubbing/test_pipeline.py tests/test_dubbing/test_presets.py
```

Full `pytest` includes tests that hit online ASR, Bing/Google translation, and
LLM paths. In restricted shell environments these often fail with DNS or missing
key errors. Classify those failures honestly instead of calling the whole app
broken. For external-service checks, report which host/provider failed and
whether the local validation path passed.

When the user asks for broad acceptance or says the app should be "actually
tested", use `docs/dev/e2e-acceptance-checklist.md` as the working checklist.
Do not replace that with a single unit test run. A useful acceptance pass
includes:

- GUI click smoke through home, transcription, subtitle processing, subtitle
  style, video synthesis, dubbing, doctor, logs, and settings.
- Settings changes with an isolated `VIDEOCAPTIONER_CONFIG_FILE`, followed by a
  reload check that proves TOML persistence and UI state agree.
- Provider switching for ASR, LLM, translation, and dubbing, including
  provider-specific row visibility and key/no-key error states.
- Local fixture-based CLI checks for subtitle synthesis and other flows that do
  not require a paid or online provider.
- Clear separation between deterministic local proof, mocked-provider proof,
  and live external-provider proof.
- Screenshot/contact-sheet inspection for visual regressions, not just "script
  exited 0".

Useful real CLI smoke paths with local fixture assets:

```bash
# Create a tiny input video from fixture audio in /tmp.
mkdir -p /tmp/vc-e2e-assets /tmp/vc-e2e-out
cp tests/fixtures/audio/zh.mp3 /tmp/vc-e2e-assets/source-zh.mp3
cp tests/fixtures/audio/zh.srt /tmp/vc-e2e-assets/source-zh.srt
ffmpeg -y -hide_banner -loglevel error \
  -f lavfi -i color=c=0x202323:s=1280x720:d=3 \
  -i /tmp/vc-e2e-assets/source-zh.mp3 \
  -shortest -c:v libx264 -pix_fmt yuv420p -c:a aac \
  /tmp/vc-e2e-assets/input-video.mp4

# Soft subtitles.
.venv/bin/python -m videocaptioner synthesize \
  /tmp/vc-e2e-assets/input-video.mp4 \
  -s /tmp/vc-e2e-assets/source-zh.srt \
  --subtitle-mode soft \
  -o /tmp/vc-e2e-out/synth-soft.mp4

# Hard ASS subtitles.
.venv/bin/python -m videocaptioner synthesize \
  /tmp/vc-e2e-assets/input-video.mp4 \
  -s /tmp/vc-e2e-assets/source-zh.srt \
  --subtitle-mode hard --render-mode ass --quality low \
  -o /tmp/vc-e2e-out/synth-hard-ass.mp4

# Rounded subtitle rendering with style override.
.venv/bin/python -m videocaptioner synthesize \
  /tmp/vc-e2e-assets/input-video.mp4 \
  -s /tmp/vc-e2e-assets/source-zh.srt \
  --subtitle-mode hard --render-mode rounded --quality low \
  --style-override '{"font_size":52,"background_radius":28}' \
  -o /tmp/vc-e2e-out/synth-hard-rounded.mp4
```

For UI tests, prefer `/tmp` output folders. Do not commit generated screenshots,
`__pycache__`, `.pytest_cache`, or `.DS_Store`. `work-dir/`, `AppData/`, and
`screenshots/` are local/runtime artifact areas.

Keep acceptance artifacts out of the source tree unless the user explicitly
asks for a persistent design/reference artifact. Use `/tmp/vc-*` for generated
videos, audios, subtitles, screenshots, and isolated config files. Before
calling a broad pass done, clean or at least report any generated artifacts that
remain in the worktree.

## Code Quality Rules

- Prefer deletion and simplification over compatibility layers when old code is
  no longer part of the desired architecture.
- Keep names literal and domain-specific: provider, preset, voice, clone_audio,
  subtitle_mode, render_mode, etc.
- Keep core functions testable without PyQt.
- Keep UI state and business config connected through adapters, not widget
  imports in core/CLI.
- Add focused tests for shared config, parser behavior, rendering command
  quoting, provider normalization, and cache behavior when changing those areas.
- When fixing a visual bug, include screenshot verification. When fixing a
  runtime bug, include the command, test, or log signature that proves the path.
- Be explicit about external limits: no network, missing API key, provider quota,
  or stale running app process.
- Treat open IDE tabs as hints only. If the tab points at a deleted legacy file,
  inspect the current filesystem and imports before recreating it.
- Prefer a small shared component over repeated page-local styling when two
  pages share the same shape: card rows, segmented controls, status pills,
  file/action rows, provider cards, or preview panels.
- When extracting UI components, keep them visually boring and predictable:
  stable width/height, no surprise relayout on click, no one-off color tokens,
  no hidden persistence side effects.
- For provider model lists, separate "load available models" from "test this
  connection". Cache loaded model options per provider in shared config state
  only when they are safe to reuse.

## Workspace Hygiene

This repo often contains large user-approved refactors. Before deleting or
renaming anything, inspect current imports with `rg` and preserve unrelated user
changes. Good cleanup targets are generated artifacts and dead legacy UI files;
bad cleanup targets are user-created design demos or work-in-progress docs that
are still referenced by the conversation.

Legacy files intentionally removed during the settings/component migration
should stay removed unless the user explicitly asks to restore them:

- `videocaptioner/ui/components/MySettingCard.py`
- `videocaptioner/ui/components/app_setting_cards.py`
- `videocaptioner/ui/components/WhisperAPISettingWidget.py`
- `videocaptioner/ui/components/WhisperCppSettingWidget.py`
- `videocaptioner/ui/components/FasterWhisperSettingWidget.py`
- `videocaptioner/ui/components/TranscriptionSettingDialog.py`
- `videocaptioner/ui/components/TranscriptionOutputDialog.py`
- `videocaptioner/ui/components/SubtitleSettingDialog.py`

If an old import is still needed, replace the usage with the current first-party
component or settings page section instead of reviving the old file.

## Current High-Risk Files

Large page files still contain too much UI and state logic:

- `videocaptioner/ui/view/setting_interface.py`
- `videocaptioner/ui/view/dubbing_interface.py`
- `videocaptioner/ui/view/video_synthesis_interface.py`
- `videocaptioner/ui/view/subtitle_style_interface.py`
- `videocaptioner/ui/view/subtitle_interface.py`

Refactor them by extracting reusable rows/panels into `ui/components/`, not by
adding more page-local helper classes. Keep every extraction backed by a smoke
screenshot if it touches layout.

`signal_bus.py` currently exists mostly for video preview playback events. Do
not use it for broad configuration propagation; config changes should flow
through the shared settings/config store.

## Useful Docs

- `docs/dev/config-architecture-notes.md`
- `docs/dev/ui-component-audit.md`
- `docs/dev/asr-chunked-usage.md`
- `docs/dev/asr-chunk-merger.md`
- `docs/dev/translate-module.md`
- `docs/dev/siliconflow-gemini-api-research.md`
- `videocaptioner/core/subtitle/README.md`

## Common Commands

```bash
# Launch desktop app.
uv run videocaptioner
.venv/bin/python -m videocaptioner gui

# CLI help.
.venv/bin/python -m videocaptioner --help
.venv/bin/python -m videocaptioner process --help

# Config with an isolated test TOML.
VIDEOCAPTIONER_CONFIG_FILE=/tmp/vc-config.toml \
  .venv/bin/python -m videocaptioner config init --non-interactive --force
VIDEOCAPTIONER_CONFIG_FILE=/tmp/vc-config.toml \
  .venv/bin/python -m videocaptioner config show
```

Always check the actual worktree state before editing. This repo often has
large in-progress diffs, HTML design mocks, and generated comparison artifacts.
Do not revert user changes unless explicitly asked.
