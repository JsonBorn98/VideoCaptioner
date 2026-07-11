# Configuration

Settings are grouped by functional stage. Subtitle optimization and translation retain upstream
splitting, length limits, optimization, and translation. The standalone postprocess settings page owns
reading speed, timing, cleanup, and quality auditing.

## Postprocess profiles

Three editable templates ship with immutable factory baselines:

| Template | Intent |
|----------|--------|
| Loose | Minimize intervention and tolerate more local speed variation |
| Balanced | Balance continuity, reading comfort, and edit size; the workflow default |
| Smooth-first | Reshape cues more actively for steadier display speed |

Changes are persisted immediately. Each field and profile can be reset. A custom profile is copied from
one factory template and resets to that template's baseline.

The default primary reading side is the translation. Optional settings can audit the source, optimize
both sides, run constrained local LLM repair, write QA reports, or associate media for ForcedAligner
timing evidence. ForcedAligner is optional and requires no separately supplied VAD file.

## Format policy

SRT is the canonical format between content modules. Importing ASS/VTT intentionally discards style,
positioning, effects, and other advanced presentation data. Export or video synthesis applies the
current VideoCaptioner subtitle style when an ASS result is needed.

See [Subtitle Postprocessing](/en/guide/subtitle-postprocessing) for the user workflow and run
`videocaptioner postprocess --help` for command-level overrides.
