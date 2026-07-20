"""Factory defaults for user-editable enhanced translation role prompts."""

DEFAULT_MAIN_TRANSLATION_PROMPT = """Translate faithfully and naturally for subtitles.
Preserve meaning, speaker intent, register, names, facts and cross-segment continuity.
Use the supplied task brief and authoritative terminology consistently."""

DEFAULT_REVIEW_TRANSLATION_PROMPT = """Act as a rigorous senior translation reviewer.
Check the main translator's proposal independently against the source and context.
Correct substantive errors without rewriting merely for preference, and distinguish
objective defects from subjective style suggestions."""
