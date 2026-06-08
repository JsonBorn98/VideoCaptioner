# Configuration Architecture Notes

## Original Problem

`qfluentwidgets` setting cards directly mutated global config state:

- The native switch and options cards wrote through qfluent's global config
  setter.
- The setter mutated the item, wrote JSON immediately, and emitted restart/theme
  signals.

That makes the widget, persistence format, validation, and business config source the same object. It is hard to reuse in CLI and hard to replace visually.

## Current Boundary

Shared persistence now lives in core:

- `videocaptioner.core.application.config_store`
- default file: `platformdirs.user_config_dir("videocaptioner") / "config.toml"`
- test override: `VIDEOCAPTIONER_CONFIG_FILE=/path/to/config.toml`

There is no separate CLI config module anymore. CLI code imports the core store
directly.

Business execution should consume plain application config:

- `videocaptioner.core.application.app_config.AppConfig`
- `videocaptioner.core.application.task_builder.TaskBuilder`

Adapters are responsible for translating existing surfaces:

- shared TOML/CLI dict -> `videocaptioner.cli.config_adapter.app_config_from_cli`
- UI memory state -> `videocaptioner.ui.config_adapter.app_config_from_ui`

`videocaptioner.ui.common.config` is an in-memory binding over the shared TOML
file. It does not read or write qfluentwidgets' old JSON settings file.
`cfg.set(...)` and direct `SettingField.value` changes are synced back to TOML.
The UI config state is implemented by `videocaptioner.ui.common.settings_state`,
not qfluentwidgets' `QConfig`.

Existing UI pages still call `TaskFactory`, but task construction itself lives
in `TaskBuilder`.

The settings page no longer uses qfluentwidgets' native setting-card classes.
`videocaptioner.ui.components.form_cards` owns the page's card/group
widgets and writes through the shared settings state.

Legacy setting-card helpers that are still used by older dialogs now read from
our `SettingField` objects and write through `cfg.set(...)`. The remaining
`qconfig` bridge is only for qfluentwidgets' global theme signal/color behavior.

## Next Migration Direction

Next UI refactors should avoid qfluent setting cards entirely:

- Read and write through `config_store` or a small repository wrapper around it.
- Keep UI-only settings under `[ui]`; keep workflow settings under `[transcribe]`,
  `[subtitle]`, `[translate]`, `[synthesize]`, `[dubbing]`, and provider sections.
- Keep moving remaining pages from qfluent setting/dialog helpers to first-party
  widgets when those pages are redesigned.
- Replace the qfluent theme bridge with first-party theme state when the base UI
  shell no longer depends on qfluentwidgets' global theme manager.

`signal_bus` should not own configuration change propagation long term. Its video playback events can become a local player controller; config events should move to the config store.
