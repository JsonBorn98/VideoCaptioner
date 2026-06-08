# UI Component Audit

Date: 2026-06-09

## Direction

Use first-party components for app settings and workflow surfaces by default.
qfluentwidgets can still provide low-level controls while the shell is being
migrated, but configuration rows and settings pages should not depend on
qfluent's native setting-card/config system. Prefer:

- `FormGroup` + `FormCard` variants for form-like settings.
- `videocaptioner.ui.common.settings_state` for UI config state.
- Shared TOML config through `videocaptioner.core.application.config_store`.
- `ScrollArea` or `SingleDirectionScrollArea` for pages that can exceed one viewport.
- Reusable first-party rows around qfluent controls where low-level widgets are
  still useful.

Manual stylesheet should be limited to first-party component internals,
transparent containers, media preview backgrounds, or places where no reusable
component exists yet.

## Icon Usage

- Prefer `qfluentwidgets.FluentIcon` for common UI actions and navigation.
- Put app-owned SVG files under `resource/assets/icons/` and load them through
  `videocaptioner.ui.common.app_icons`.
- Use `apply_button_icon(button, icon, size)` for regular Qt/qfluent buttons
  that need an icon. Keep raw unicode arrows or ad hoc file paths out of
  production widgets.

## Fixed In This Pass

- `video_synthesis_interface.py`
  - Replaced hand-built input/output/dubbing cards with qfluent setting groups and setting cards.
  - Added a scrollable content area so advanced dubbing controls remain reachable.
  - Removed custom `LineEdit` border styling.
  - Localized switch text to `开/关`.

- `DubbingVoiceDialog.py`
  - Replaced bare `QDialog` with `MessageBoxBase`.
  - Removed hand-written dialog background, card border, and button color styles.

- `task_creation_interface.py`
  - Replaced hand-styled tool button with `PrimaryToolButton`.
  - Removed custom search input and footer hyperlink styles.
  - Replaced status/copyright labels with qfluent label components.

- `home_interface.py`
  - Removed hard-coded white background from the home page.

## Remaining UI Debt

- `subtitle_style_interface.py`
  - Uses first-party setting groups and dedicated subtitle-style setting cards
    built on `FormCard`.
  - Remaining debt: it still has many specialized controls and manual preview
    layout code. Keep custom controls for live subtitle preview and the
    specialized color/font pickers, but move any reusable rows into
    `form_cards`.

- `subtitle_style_controls.py`
  - Dedicated first-party wrappers for subtitle-style combo, spin, and color
    rows.
  - Legacy qfluent-derived cards have been removed; new subtitle-style rows
    should import explicit `SubtitleStyle*Card` classes.

- `transcription_interface.py`
  - Uses a custom `VideoInfoCard` and thumbnail `QLabel`. This is acceptable for media preview, but the fixed thumbnail background and info layout should be reviewed visually.

- `video_widget.py`
  - Media rendering naturally needs custom widgets. Keep custom video surface, but avoid styling regular controls manually.

- Transcription settings
  - Provider-specific Whisper, Faster Whisper, WhisperCpp, and Fun-ASR options now
    live in the settings page instead of separate dialogs.

## 2026-06-09 Verification

- Old setting-card helpers are removed from runtime imports. `rg` found no
  references to `MySettingCard`, `SimpleSettingCard`, `LineEditSettingCard`,
  `SpinBoxSettingCard`, `EditComboBoxSettingCard`, old transcription dialogs,
  old Whisper setting widgets, or `MyVideoWidget`.
- Runtime UI code no longer imports qfluentwidgets native `SettingCard`,
  `SettingCardGroup`, `OptionsSettingCard`, `SwitchSettingCard`,
  `RangeSettingCard`, `ColorSettingCard`, or `PushSettingCard` surfaces.
- Settings page controls are centralized in
  `videocaptioner.ui.components.settings_controls`.
- Workflow/video-synthesis controls are centralized in
  `videocaptioner.ui.components.workflow_widgets`; the old generic
  `SettingRow` name was replaced with `WorkflowSettingRow` to avoid confusion
  with settings rows.
- App-owned SVG icon loading is centralized through
  `videocaptioner.ui.common.app_icons`; the settings back action now uses
  `resource/assets/icons/arrow-left.svg` instead of a raw text arrow.
- UI smoke screenshots passed for dark and light themes:
  `/tmp/vc-acceptance/ui-dark-final` and `/tmp/vc-acceptance/ui-light-final`.
- Code checks passed for the changed UI/test infrastructure files with
  `uv run ruff check ...`.
- Full default test suite passed after isolating test cache state and making
  live external Google/JianYing service tests opt-in.

## Current Cleanup Decision

Keep:

- `docs/dev/*layout-demo*.html`: user-requested design references. They are not
  imported by runtime code, but are still useful while the UI migration is in
  progress.
- `videocaptioner/ui/components/donate_dialog.py`: lower-case replacement for
  the old `DonateDialog.py`; still used by task creation and the main window.
- `videocaptioner/ui/view/dubbing_interface.py`: still self-contained and large.
  It should be split later into provider cards, voice table, and preview panel
  components, but doing so now would be a structural refactor without immediate
  behavior need.

Remove/ignore locally:

- `.pytest_cache`, `.ruff_cache`, `screenshots`, and source-tree `__pycache__`
  are generated artifacts and should not remain in the workspace.
- `.venv/**/__pycache__` is ignored as dependency environment noise and should
  not be manually curated.

## Remaining Product/UI Debt Observed From Screenshots

- `home_interface.py` still places the primary task area in a very small center
  footprint with large empty space around it. It is functional, but it still
  reads visually sparse compared with the denser settings/dubbing/synthesis
  pages.
- Several passing negative-path tests intentionally log `ERROR` traces
  (missing files, invalid input, LLM fallback). They are behaviorally correct,
  but the log volume can make successful test runs look worse than they are.
