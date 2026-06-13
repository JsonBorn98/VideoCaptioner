# Repository Guidelines

## Project Structure & Module Organization

VideoCaptioner is a Python 3.10+ package with CLI and PyQt desktop entry points. Core code lives in `videocaptioner/`: `core/` contains ASR, subtitle, translate, TTS, split, and utility modules; `cli/` contains command handlers; `ui/` contains Qt views, components, and threads. Tests are grouped by feature under `tests/` (`test_cli/`, `test_asr/`, `test_translate/`, etc.) with fixtures in `tests/fixtures/`. Runtime assets, fonts, styles, and translations live in `resource/`. Documentation lives in `docs/`.

## Build, Test, and Development Commands

- `uv sync`: install runtime and development dependencies from `pyproject.toml` and `uv.lock`.
- `uv run videocaptioner` or `uv run videocaptioner-gui`: launch the desktop app.
- `uv run videocaptioner --help`: inspect CLI commands.
- `uv run pytest`: run the full pytest suite.
- `uv run pytest tests/test_cli/ -q`: run a focused test subset.
- `uv run pytest -m "not integration"`: skip tests requiring external services.
- `uv run pyright`: run type checking.
- `uv run ruff check .`: run lint checks.
- `uv run ruff check . --fix`: apply safe Ruff fixes.
- `cd docs && npm install && npm run docs:dev`: run the documentation site locally.
- `uv run --with pyinstaller --with static-ffmpeg python scripts/build_desktop.py --clean`: build a desktop bundle.

## Coding Style & Naming Conventions

Use 4-space indentation and keep Python compatible with Python 3.10 through 3.12. Ruff checks `E`, `F`, `I`, and `W` with a 100-character line target; imports should be sorted by Ruff. Prefer `snake_case` for modules, functions, and variables, and `PascalCase` for classes. Keep existing Qt widget naming patterns, including files such as `WhisperCppSettingWidget.py`.

## Testing Guidelines

Pytest discovers `test_*.py`, `Test*` classes, and `test_*` functions under `tests/`. Mark external-service or slow coverage with `integration`, `slow`, `llm`, or `translator`. Tests needing API credentials should read environment variables, skip cleanly when unavailable, and never commit secrets or generated media.

## Commit & Pull Request Guidelines

Recent history uses short imperative summaries and occasional Conventional Commit prefixes, for example `feat: make Edge TTS the default dubbing provider`. Prefer messages such as `fix: handle empty subtitles` or `Add desktop smoke coverage`. Pull requests should describe the change, link issues, list tests run, and include screenshots for visible GUI or documentation changes.

## Security & Configuration Tips

Do not commit API keys, cookies, generated bundles, or local config. Use `VIDEOCAPTIONER_*` environment variables or `videocaptioner config set ...`, and keep test-only secrets in your shell or CI store.
