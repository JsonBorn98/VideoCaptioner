# Desktop release build

VideoCaptioner publishes desktop bundles for Windows and macOS from GitHub Actions.
Users can download the zip files from a GitHub Release, extract them, and run the
bundled `VideoCaptioner` executable without installing Python or FFmpeg.

## Local build

```bash
uv sync --python 3.12 --frozen
uv run --with pyinstaller --with static-ffmpeg python scripts/build_desktop.py --clean
uv run python scripts/smoke_desktop.py dist/VideoCaptioner
```

The build script downloads static `ffmpeg` and `ffprobe`, bundles the current
`uv` executable, and places them under `resource/bin` inside the PyInstaller app.
Runtime user data is kept in the system user-data directory, so app upgrades do
not overwrite settings, logs, cache, models, optional runtimes, or custom
subtitle styles.

## CI and releases

`.github/workflows/build-desktop.yml` builds desktop bundles on:

- `windows-latest`
- `macos-15-intel`

Each job runs a real packaged-app smoke test:

- starts the packaged executable with `--version`
- lists bundled subtitle styles
- runs `doctor --json`
- generates a short video with bundled FFmpeg
- creates both soft-subtitle and hard-subtitle videos
- validates output duration with bundled ffprobe

Pull requests and branch pushes are artifact-only checks. They upload short-lived
workflow artifacts, but they do not publish a GitHub Release.

Release publishing is tag based. Use one of these paths:

```bash
git tag v1.4.3
git push origin v1.4.3
```

Pushing a `vMAJOR.MINOR.PATCH` tag builds Windows and macOS bundles, creates the
GitHub Release if it does not already exist, and uploads all desktop zip files.
The same publishing path also works when a GitHub Release is published from the
GitHub UI for an existing `v*` tag.

For a manual re-upload, run the **Build Desktop Apps** workflow from GitHub
Actions and set `release_tag` to an existing `v*` tag. Leave `release_tag` empty
for an artifact-only manual build.

This fork does not publish to PyPI; source package artifacts are produced by CI
only for verification and short-lived workflow downloads.
