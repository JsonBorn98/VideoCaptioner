from videocaptioner.core.translate.enhanced.models import (
    GlossaryEntry,
    SubtitleCue,
    TranslationBatch,
    TranslationContextBrief,
)
from videocaptioner.core.translate.enhanced.prompt_assembler import (
    assemble_prompt,
    translation_batch_payload,
)


def test_prompt_components_have_fixed_order_and_preserve_user_text() -> None:
    user_prompt = "Keep  double spaces and $variables unchanged."
    assembly = assemble_prompt(
        system_constraints="Return valid JSON.",
        user_role_prompt=user_prompt,
        context_brief=TranslationContextBrief(outline="A product launch"),
        glossary_entries=(GlossaryEntry("id", "Falcon", "project", "猎鹰"),),
        stage_instruction="Translate subjects only.",
        dynamic_subtitles={"translation_subjects": [{"id": 1, "text": "Falcon"}]},
        glossary_version="sha256:glossary",
    )
    prompt = assembly.full_prompt

    tags = (
        "<SYSTEM_CONSTRAINTS>",
        "<USER_ROLE_PROMPT>",
        "<TRANSLATION_CONTEXT_BRIEF>",
        "<RELEVANT_AUTHORITATIVE_TERMS",
        "<STAGE_INSTRUCTION>",
        "<DYNAMIC_SUBTITLES>",
    )
    positions = [prompt.index(tag) for tag in tags]
    assert positions == sorted(positions)
    assert user_prompt in prompt
    assert "Falcon" not in assembly.stable_prefix
    assert "translation_subjects" not in assembly.stable_prefix


def test_translation_batch_payload_separates_context_and_allowed_output_ids() -> None:
    batch = TranslationBatch(
        subjects=(SubtitleCue(2, "translate me"),),
        context_before=(SubtitleCue(1, "previous context"),),
        context_after=(SubtitleCue(3, "next context"),),
    )

    payload = translation_batch_payload(batch)

    assert payload["allowed_output_ids"] == [2]
    assert payload["translation_subjects"] == [{"id": 2, "text": "translate me"}]
    assert payload["boundary_context_read_only"]["before"][0]["id"] == 1
    assert payload["boundary_context_read_only"]["after"][0]["id"] == 3
