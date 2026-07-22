---
layout: home
title: VideoCaptioner - Video Subtitle Workbench
titleTemplate: false
description: A desktop and CLI workbench for transcription and forced alignment, dual-role LLM translation review, subtitle postprocessing, and controllable FFmpeg export.

hero:
  name: VideoCaptioner
  text: Video Subtitle Workbench
  tagline: Transcribe · Translate and Review · Postprocess · Synthesize
  image:
    src: /logo.png
    alt: VideoCaptioner
  actions:
    - theme: brand
      text: Read the Guide
      link: /guide/getting-started
    - theme: alt
      text: GitHub Repository
      link: https://github.com/JsonBorn98/VideoCaptioner

features:
  - icon: 🎙️
    title: Transcription and Precise Timing
    details: MiMo API and managed local Qwen3 ASR, Qwen3-ForcedAligner, VAD-aware chunking, persistent workers, and guarded fallback timelines.
  - icon: 🌐
    title: Dual-Role LLM Translation
    details: A main translator and an independent senior reviewer collaborate through full-context analysis, terminology adjudication, reusable glossaries, and a final audit.
  - icon: 📐
    title: Adaptive Subtitle Postprocessing
    details: Repair short gaps, cue duration, reading speed, punctuation, overlap, and difficult overspeed cases with deterministic validation and optional LLM assistance.
  - icon: 🎞️
    title: Controllable FFmpeg Export
    details: Fast stream-copy soft-subtitle muxing plus controllable hard-subtitle encoding with software or NVENC/QSV/AMF encoders, CQ/ABR, command preview, custom arguments, and live logs.
  - icon: 🎨
    title: Bilingual Styling and Stage Delivery
    details: Preserve bilingual layouts, use extended ASS fields and font presets, keep canonical SRT checkpoints, and optionally export ASS or VTT after each completed subtitle stage.
  - icon: 📋
    title: Observable Long-Running Tasks
    details: A dedicated GUI run log and structured GUI/CLI stage summaries expose actual artifacts, alignment fallbacks, rule fallbacks, and failures.
---

## Run from source

This is a personal-use fork updated as needed. Run it from source:

```bash
git clone https://github.com/JsonBorn98/VideoCaptioner.git
cd VideoCaptioner
uv sync --python 3.12
uv run videocaptioner
```

The optional Qwen runtime and model weights are installed separately from the main
application environment through the GUI component manager. Source-only CLI users can
instead run `uv sync --python 3.12 --extra qwen`, which deliberately installs the heavy
Qwen and PyTorch dependencies into the project environment.

## Workflow

```text
media
  → ASR and word-level alignment
  → local merging or LLM segmentation
  → subtitle optimization
  → non-LLM, single-LLM, or dual-role LLM translation
  → subtitle postprocessing and QA
  → soft-subtitle muxing or hard-subtitle synthesis
```

See the [Chinese user guide](/guide/getting-started) and
[CLI reference](/cli) for usage details.

## License and attribution

Released under GPL-3.0. Original copyright notices remain in the LICENSE file
and Git history.
