---
status: accepted
---

# Adopt adaptive structural subtitle speed optimization

Subtitle reading-speed optimization is a layout-aware, policy-driven structural stage rather than a per-cue audit. It defaults to the translated main display side, uses adaptive local reading-load targets and bounded deterministic/LLM repair windows, and may retime, resegment, or redistribute text under graded timing evidence.

For bilingual subtitles, structural and timing changes preserve the mapping of both display sides. Text changes target the translated side by default; the source side remains a protected reference unless the user explicitly enables optimization of both sides. A monolingual subtitle treats its only display side as primary.

Pure-subtitle processing remains the default. Users may optionally associate media and run windowed ForcedAligner enhancement, with conservative fallback, QA-visible unresolved conflicts, preset plus advanced controls, internal evidence caching, and an optional fingerprint-validated timing sidecar. The unified speed pipeline replaces the independent reading-speed audit, fast-compression, and gap-repair public controls.

Translation tasks enable the balanced policy and validated semantic repair by default. Correction-only tasks remain off by default, and media-based ForcedAligner enhancement remains opt-in. All semantically necessary bounded repair windows are processed; efficiency comes from deterministic filtering, deduplication, caching, and finite validation retries rather than user-facing token or window budgets.
