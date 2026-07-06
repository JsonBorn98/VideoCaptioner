# Subtitle Style

VideoCaptioner uses style presets under `resource/subtitle_style/ass-*.json` to
control burned subtitle appearance. Run `videocaptioner style` to list available
presets and their fields.

## Render Modes

- **ASS mode**: traditional outline and shadow rendering. Bilingual output uses
  `Default` for the primary line and `Secondary` for the secondary line.
- **Rounded background mode**: Pillow-rendered rounded background boxes.

## ASS Style Fields

| Field | Description |
|-------|-------------|
| `font_name` / `font_size` | Primary style font family and size |
| `primary_color` | Fill color as `#RRGGBB` |
| `outline_color` / `outline_width` | Outline color and width |
| `bold` | Whether the primary style is bold |
| `spacing` | Letter spacing |
| `margin_bottom` | Bottom margin (`MarginV`) |
| `shadow` | Shadow depth, default `0` |
| `margin_l` / `margin_r` | Left and right margins, default `10` |
| `wrap_style` | ASS wrap mode, default `1`; `0` is balanced wrapping and `2` disables automatic wrapping. This is currently kept as style metadata |
| `secondary` | Secondary style fields: `{font_name, font_size, color, outline_color, outline_width, spacing, shadow, margin_bottom}`. If `margin_bottom` is `null`, the primary style margin is reused |

Style sizes are designed against `reference_width` / `reference_height`,
normally 1280x720, and scaled during synthesis for the target resolution.
Generated ASS files include `ScaledBorderAndShadow: yes` so outlines and shadows
scale consistently across resolutions.

## Built-in `house` Preset

The `house` preset follows an integrated bilingual style: both lines use LXGW
WenKai and the same near-white fill color (`#F8F8F6`). Language separation comes
only from two low-saturation outline colors: deep blue for the primary Chinese
line (`#182030`) and deep brown for the secondary English line (`#331E15`).
It uses a single-layer style with no shadow.

```bash
videocaptioner synthesize video.mp4 -s sub.srt --subtitle-mode hard --style house
```

## Custom Overrides

Use `--style-override` to override style fields inline:

```bash
videocaptioner synthesize video.mp4 -s sub.srt --subtitle-mode hard \
  --style house --style-override '{"outline_width": 3.5, "shadow": 0.5}'
```
