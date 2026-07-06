# Configuration

This page covers advanced configuration values for VideoCaptioner.

## Global Configuration

More general configuration details are coming soon.

## Subtitle Postprocess (`[subtitle]`)

Rule-based cleanup and quality audit options live under the `[subtitle]` table in
`config.toml`. They are optional. All new behavior is disabled by default, except
`trim_trailing_punct`, which preserves the previous subtitle optimizer behavior
after LLM optimization or translation.

| Key | Default | Description |
|-----|---------|-------------|
| `remove_placeholders` | `false` | Remove placeholder captions such as `[Music]`, `[Applause]`, `[音乐]`, or `♪` |
| `normalize_quotes` | `false` | Normalize Chinese quotes to `「」` / `『』` and trim extended weak trailing punctuation on Chinese lines |
| `trim_trailing_punct` | `true` | Trim trailing weak punctuation, matching the previous optimizer cleanup path; disable to preserve punctuation |
| `fix_gaps` | `false` | Close small positive gaps between neighboring subtitles to reduce flicker |
| `max_gap_ms` | `800` | Maximum gap to close; music-heavy edits may prefer `500` |
| `gap_mode` | `"extend"` | `extend` extends the previous subtitle to the next start time; `midpoint` keeps the older midpoint-style behavior |
| `audit_reading_speed` | `false` | Audit CPS and long-duration anomalies without changing subtitles |
| `max_cps_cjk` | `11.0` | Hard CPS limit for CJK text |
| `max_cps_latin` | `20.0` | Hard CPS limit for Latin text |
| `comfort_cps_cjk` | `9.0` | Comfort CPS threshold for CJK text |
| `comfort_cps_latin` | `16.0` | Comfort CPS threshold for Latin text |
| `min_duration_ms` | `1000` | Perceptual minimum display duration |
| `max_duration_ms` | `7000` | Long-duration anomaly threshold |
| `compress_fast_subtitles` | `false` | Use an LLM to locally compress over-fast Chinese lines |
| `qa_report` | `false` | Write a Markdown QA report next to the output subtitle; implies reading-speed audit |

The same postprocess options are available on both `subtitle` and `process`.
See the CLI reference for the command-line flags.
