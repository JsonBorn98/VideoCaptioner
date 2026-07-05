# Test Fixtures

This directory contains shared test resources used across multiple test modules.

## Structure

```
tests/fixtures/
├── audio/
│   ├── en.mp3       # Short English speech audio for ASR testing
│   ├── en_20min.mp3 # 20 minute English ASR benchmark fixture
│   ├── zh.mp3       # Short Chinese speech audio for ASR testing
│   └── zh_20min.mp3 # 20 minute Chinese ASR benchmark fixture
└── subtitle/
    └── sample_en.srt # English subtitle sample for subtitle processing tests
```

## Audio Files

### zh.mp3

- **Content**: Chinese speech saying "今天深圳天气怎么样" (What's the weather like in Shenzhen today?)
- **Duration**: ~2 seconds
- **Format**: MP3
- **Usage**: Used by ASR integration tests in `tests/test_asr/`
- **Access**: Via `test_audio_path` fixture in `tests/test_asr/conftest.py`

### en.mp3

- **Content**: Short English speech sample
- **Duration**: ~3 seconds
- **Format**: MP3
- **Usage**: Used by ASR integration tests and connection smoke checks
- **Access**: Via `test_audio_path_en` fixture in `tests/test_asr/conftest.py`

### zh_20min.mp3

- **Content**: Chinese long-form speech benchmark sample. The source audio was shorter than 20 minutes, so it was looped from the beginning to meet the 20 minute ASR benchmark gate.
- **Duration**: 1200 seconds
- **Format**: 16 kHz mono MP3
- **Usage**: Used by `scripts/asr_benchmark.py` Qwen/MiMo long-media acceptance commands
- **Source command**: `ffmpeg -stream_loop 1 -i <source.m4a> -t 1200 -map 0:a:0 -vn -ac 1 -ar 16000 -c:a libmp3lame -b:a 64k tests/fixtures/audio/zh_20min.mp3`

### en_20min.mp3

- **Content**: First 20 minutes of an English conference talk
- **Duration**: ~1200 seconds
- **Format**: 16 kHz mono MP3
- **Usage**: Used by `scripts/asr_benchmark.py` Qwen/MiMo long-media acceptance commands
- **Source command**: `ffmpeg -i <source.mp4> -t 1200 -map 0:a:0 -vn -ac 1 -ar 16000 -c:a libmp3lame -b:a 64k tests/fixtures/audio/en_20min.mp3`

## Subtitle Files

### sample_en.srt

- **Content**: English tutorial about Python programming (10 segments)
- **Duration**: ~38 seconds
- **Format**: SRT (SubRip)
- **Usage**: Used by subtitle processing tests (split, optimize, translate)
- **Access**: Via fixtures in test modules

## Adding New Fixtures

When adding new shared test resources:

1. Create subdirectories by resource type (e.g., `audio/`, `video/`, `subtitle/`)
2. Use descriptive filenames indicating the content or purpose
3. Document the fixture in this README
4. Create appropriate fixtures in the relevant test module's `conftest.py`
5. Keep file sizes reasonable (commit only necessary test data)

## Guidelines

- **Keep it small**: Only commit minimal test data needed for tests
- **Reusable**: Place resources here if used by multiple test modules
- **Documented**: Update this README when adding new fixtures
- **Format**: Use common formats that don't require special codecs
