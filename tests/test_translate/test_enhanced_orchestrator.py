import json
from collections import defaultdict

import pytest

from videocaptioner.core.llm.models import (
    LLMCallError,
    LLMErrorCategory,
    LLMModelProfile,
    LLMResult,
    LLMTransport,
    LLMUsage,
    ProviderDialect,
)
from videocaptioner.core.translate.enhanced.glossary import subtitle_fingerprint
from videocaptioner.core.translate.enhanced.models import (
    AuditIssueDisposition,
    AuthoritativeGlossary,
    EnhancedTranslationConfig,
    EnhancedTranslationError,
    GlossaryEntry,
    GlossarySelectionSource,
    SubtitleCue,
    TermConfirmationMode,
    TranslationAuditMode,
    TranslationRoleSnapshot,
)
from videocaptioner.core.translate.enhanced.orchestrator import (
    EnhancedTranslationOrchestrator,
)


def _profile(profile_id: str, *, concurrency: int = 1) -> LLMModelProfile:
    return LLMModelProfile(
        profile_id=profile_id,
        name=profile_id.title(),
        transport=LLMTransport.OPENAI_COMPATIBLE,
        dialect=ProviderDialect.GENERIC,
        base_url=f"https://{profile_id}.test/v1",
        api_key="secret",
        model=f"{profile_id}-model",
        work_context_tokens=16_384,
        max_concurrency=concurrency,
    )


def _config(
    *,
    audit_mode: TranslationAuditMode = TranslationAuditMode.AUTO_APPLY_REVIEW,
    batch_size: int = 10,
    term_confirmation: TermConfirmationMode = TermConfirmationMode.AUTOMATIC,
) -> EnhancedTranslationConfig:
    return EnhancedTranslationConfig(
        main_role=TranslationRoleSnapshot(
            "main", _profile("main"), "MAIN USER PROMPT"
        ),
        review_role=TranslationRoleSnapshot(
            "review", _profile("review"), "REVIEW USER PROMPT"
        ),
        source_language="English",
        target_language="简体中文",
        batch_size=batch_size,
        audit_mode=audit_mode,
        term_confirmation=term_confirmation,
    )


def _analysis(*, candidates=()):
    return {
        "brief": {
            "outline": "A planetary science discussion",
            "background": "An educational video",
            "themes": ["astronomy"],
            "style_notes": ["concise"],
            "translation_notes": ["Use established astronomical names"],
        },
        "candidates": list(candidates),
    }


def _candidate():
    return {
        "id": "mercury-planet",
        "source_term": "Mercury",
        "sense": "the planet",
        "aliases": ["planet Mercury"],
        "occurrence_ids": [1],
    }


def _translations(*items):
    return {"translations": [{"id": cue_id, "text": text} for cue_id, text in items]}


class ScriptedGateway:
    def __init__(self, **responses):
        self.responses = {stage: list(values) for stage, values in responses.items()}
        self.calls = []
        self.stage_calls = defaultdict(list)

    def complete(self, profile, request, *, cancelled=None):
        assert cancelled is None or not cancelled()
        stage = request.metadata["stage"]
        self.calls.append((profile, request))
        self.stage_calls[stage].append(request)
        if not self.responses.get(stage):
            raise AssertionError(f"No scripted response for stage {stage!r}")
        value = self.responses[stage].pop(0)
        if isinstance(value, BaseException):
            raise value
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return LLMResult(
            text=text,
            usage=LLMUsage(
                input_tokens=10,
                output_tokens=2,
                cache_read_tokens=4,
                cache_write_tokens=1,
            ),
        )

    @property
    def stages(self):
        return [request.metadata["stage"] for _, request in self.calls]

    @property
    def roles(self):
        return [request.metadata["role"] for _, request in self.calls]


def test_automatic_full_chain_uses_directional_term_review_and_three_pass_roles():
    cues = (
        SubtitleCue(1, "Mercury is the closest planet to the Sun."),
        SubtitleCue(2, "It completes an orbit quickly."),
        SubtitleCue(3, "Now compare it with Venus."),
    )
    gateway = ScriptedGateway(
        analysis_window=[_analysis(candidates=[_candidate()])],
        term_proposal=[{"translation": "水银", "reason": "ambiguous word"}],
        term_review=[
            {
                "is_term": True,
                "decision": "uncertain",
                "translation": "",
                "reason": "need more context",
            }
        ],
        term_review_final=[
            {"decision": "correct", "translation": "水星", "reason": "planet context"}
        ],
        translation=[_translations((1, "水星是离太阳最近的行星。"), (2, "它的公转很快。"), (3, "现在将它与金星比较。"))],
        audit=[{"issues": []}],
    )

    result = EnhancedTranslationOrchestrator(_config(), gateway=gateway).run(cues)

    assert gateway.stages == [
        "analysis_window",
        "term_proposal",
        "term_review",
        "term_review_final",
        "translation",
        "audit",
    ]
    assert gateway.roles == ["main", "main", "review", "review", "main", "review"]
    assert result.translations[1] == "水星是离太阳最近的行星。"
    assert result.glossary.entries[0].translation == "水星"
    assert (
        result.glossary.entries[0].selection_source
        is GlossarySelectionSource.REVIEW_MODEL_CORRECTED
    )

    analysis_request = gateway.stage_calls["analysis_window"][0]
    assert analysis_request.metadata == {"stage": "analysis_window", "role": "main"}
    assert "MAIN USER PROMPT" in analysis_request.messages[0].content
    assert cues[0].text not in analysis_request.messages[0].content
    assert cues[0].text in analysis_request.messages[1].content
    assert "conforms exactly to this JSON Schema" in analysis_request.messages[1].content
    assert '"required":["brief","candidates"]' in analysis_request.messages[1].content

    translation_request = gateway.stage_calls["translation"][0]
    assert "MAIN USER PROMPT" in translation_request.messages[0].content
    assert "A planetary science discussion" in translation_request.messages[0].content
    assert '"source_term":"Mercury"' in translation_request.messages[1].content
    assert "translation_subjects" in translation_request.messages[1].content

    audit_request = gateway.stage_calls["audit"][0]
    assert "REVIEW USER PROMPT" in audit_request.messages[0].content
    assert audit_request.metadata == {"stage": "audit", "role": "review"}


def test_final_term_review_three_invalid_responses_falls_back_to_source_text():
    cues = (SubtitleCue(1, "Mercury is visible tonight."),)
    invalid_final = {"decision": "uncertain", "translation": "", "reason": "still unsure"}
    gateway = ScriptedGateway(
        analysis_window=[_analysis(candidates=[_candidate()])],
        term_proposal=[{"translation": "水银", "reason": "literal"}],
        term_review=[
            {
                "is_term": True,
                "decision": "uncertain",
                "translation": "",
                "reason": "unsure",
            }
        ],
        term_review_final=[invalid_final, invalid_final, invalid_final],
        translation=[_translations((1, "今晚可以看到 Mercury。"))],
        audit=[{"issues": []}],
    )

    result = EnhancedTranslationOrchestrator(_config(), gateway=gateway).run(cues)

    entry = result.glossary.entries[0]
    assert len(gateway.stage_calls["term_review_final"]) == 3
    assert [len(request.messages) for request in gateway.stage_calls["term_review_final"]] == [
        2,
        4,
        6,
    ]
    assert entry.translation == "Mercury"
    assert entry.high_risk is True
    assert entry.selection_source is GlossarySelectionSource.SOURCE_FALLBACK
    assert result.audit_report.warnings == (
        "术语校对响应无效，已保留原文：Mercury",
    )


def test_translation_rejects_boundary_id_then_retries_with_same_stable_prefix():
    cues = (
        SubtitleCue(1, "First unique source."),
        SubtitleCue(2, "Second unique source."),
        SubtitleCue(3, "Third unique source."),
    )
    gateway = ScriptedGateway(
        analysis_window=[_analysis()],
        translation=[
            _translations((1, "第一句。"), (2, "不应输出的边界句。")),
            _translations((1, "第一句。")),
            _translations((2, "第二句。")),
            _translations((3, "第三句。")),
        ],
        audit=[{"issues": []}, {"issues": []}, {"issues": []}],
    )

    result = EnhancedTranslationOrchestrator(
        _config(batch_size=1), gateway=gateway
    ).run(cues)

    requests = gateway.stage_calls["translation"]
    assert result.translations == {1: "第一句。", 2: "第二句。", 3: "第三句。"}
    assert len(requests) == 4
    assert requests[0].messages[0].content == requests[1].messages[0].content
    assert len(requests[0].messages) == 2
    assert len(requests[1].messages) == 4
    assert "extra=[2]" in requests[1].messages[-1].content
    assert '"allowed_output_ids":[1]' in requests[0].messages[1].content
    assert '"after":[{"id":2' in requests[0].messages[1].content


@pytest.mark.parametrize(
    ("mode", "expected_text", "expected_disposition"),
    [
        (
            TranslationAuditMode.REVIEW_AND_CONFIRM,
            "错误的版本 2",
            AuditIssueDisposition.USER_REJECTED,
        ),
        (
            TranslationAuditMode.AUTO_APPLY_REVIEW,
            "正确的版本 2",
            AuditIssueDisposition.AUTO_APPLIED,
        ),
    ],
)
def test_audit_manual_review_and_automatic_review_application(
    mode, expected_text, expected_disposition
):
    cues = (SubtitleCue(1, "Version 2"),)
    gateway = ScriptedGateway(
        analysis_window=[_analysis()],
        translation=[_translations((1, "错误的版本 2"))],
        audit=[
            {
                "issues": [
                    {
                        "id": 1,
                        "categories": ["semantic_accuracy"],
                        "message": "The version meaning is wrong.",
                        "suggested_translation": "正确的版本 2",
                    }
                ]
            }
        ],
    )

    confirm_audit = (
        (lambda report: ())
        if mode is TranslationAuditMode.REVIEW_AND_CONFIRM
        else None
    )
    result = EnhancedTranslationOrchestrator(
        _config(audit_mode=mode), gateway=gateway
    ).run(cues, confirm_audit=confirm_audit)

    assert result.translations[1] == expected_text
    assert result.audit_report.issues[0].disposition is expected_disposition


def test_manual_audit_confirmation_applies_only_selected_consolidated_suggestions():
    cues = (SubtitleCue(1, "First"), SubtitleCue(2, "Second"))
    gateway = ScriptedGateway(
        analysis_window=[_analysis()],
        translation=[_translations((1, "旧一"), (2, "旧二"))],
        audit=[
            {
                "issues": [
                    {
                        "id": 1,
                        "categories": ["semantic_accuracy", "target_language_quality"],
                        "message": "第一条需要修改。",
                        "suggested_translation": "新一",
                    },
                    {
                        "id": 2,
                        "categories": ["terminology"],
                        "message": "第二条术语错误。",
                        "suggested_translation": "新二",
                    },
                ]
            }
        ],
    )

    result = EnhancedTranslationOrchestrator(
        _config(audit_mode=TranslationAuditMode.REVIEW_AND_CONFIRM), gateway=gateway
    ).run(cues, confirm_audit=lambda report: (2,))

    assert result.translations == {1: "旧一", 2: "新二"}
    assert result.audit_report.issues[0].categories == (
        "semantic_accuracy",
        "target_language_quality",
    )
    assert result.audit_report.issues[0].disposition is AuditIssueDisposition.USER_REJECTED
    assert result.audit_report.issues[1].disposition is AuditIssueDisposition.USER_APPLIED


def test_audit_retries_duplicate_suggestions_for_the_same_subtitle():
    cues = (SubtitleCue(1, "Source"),)
    duplicate = {
        "id": 1,
        "categories": ["semantic_accuracy"],
        "message": "需要修正。",
        "suggested_translation": "新译文",
    }
    gateway = ScriptedGateway(
        analysis_window=[_analysis()],
        translation=[_translations((1, "旧译文"))],
        audit=[
            {"issues": [duplicate, duplicate]},
            {"issues": [duplicate]},
        ],
    )

    result = EnhancedTranslationOrchestrator(
        _config(audit_mode=TranslationAuditMode.AUTO_APPLY_REVIEW), gateway=gateway
    ).run(cues)

    assert result.translations[1] == "新译文"
    assert len(gateway.stage_calls["audit"]) == 2
    assert len(result.audit_report.issues) == 1


def test_manual_audit_does_not_pause_when_there_are_no_findings():
    cues = (SubtitleCue(1, "Source"),)
    gateway = ScriptedGateway(
        analysis_window=[_analysis()],
        translation=[_translations((1, "译文"))],
        audit=[{"issues": []}],
    )

    result = EnhancedTranslationOrchestrator(
        _config(audit_mode=TranslationAuditMode.REVIEW_AND_CONFIRM), gateway=gateway
    ).run(
        cues,
        confirm_audit=lambda report: (_ for _ in ()).throw(
            AssertionError("empty audit must not request confirmation")
        ),
    )

    assert result.translations == {1: "译文"}
    assert result.audit_report.issues == ()


def test_exact_glossary_still_analyzes_but_skips_all_term_calls():
    cues = (SubtitleCue(1, "Mercury is visible."),)
    glossary = AuthoritativeGlossary(
        source_language="English",
        target_language="简体中文",
        subtitle_fingerprint=subtitle_fingerprint(cues),
        entries=(
            GlossaryEntry(
                "mercury-planet",
                "Mercury",
                "the planet",
                "水星",
                occurrence_ids=(1,),
                selection_source=GlossarySelectionSource.IMPORTED,
            ),
        ),
    )
    gateway = ScriptedGateway(
        analysis_window=[_analysis(candidates=[_candidate()])],
        translation=[_translations((1, "可以看到水星。"))],
        audit=[{"issues": []}],
    )

    result = EnhancedTranslationOrchestrator(_config(), gateway=gateway).run(
        cues, imported_glossary=glossary
    )

    assert gateway.stages == ["analysis_window", "translation", "audit"]
    assert result.glossary is glossary
    assert '"translation":"水星"' in gateway.stage_calls["translation"][0].messages[1].content


def test_required_translation_failure_does_not_run_audit_or_return_partial_result():
    cues = (SubtitleCue(1, "Hello."),)
    failure = LLMCallError(
        "provider unavailable",
        category=LLMErrorCategory.TRANSIENT,
        retryable=True,
        attempts=4,
    )
    gateway = ScriptedGateway(
        analysis_window=[_analysis()],
        translation=[failure],
    )

    with pytest.raises(EnhancedTranslationError) as raised:
        EnhancedTranslationOrchestrator(_config(), gateway=gateway).run(cues)

    assert raised.value.stage == "translation"
    assert raised.value.retryable is True
    assert raised.value.attempts == 4
    assert gateway.stages == ["analysis_window", "translation"]
    assert "audit" not in gateway.stage_calls


def test_manual_confirmation_is_skipped_when_analysis_finds_no_terms():
    cues = (SubtitleCue(1, "Hello."),)
    gateway = ScriptedGateway(
        analysis_window=[_analysis()],
        translation=[_translations((1, "你好。"))],
        audit=[{"issues": []}],
    )
    confirmation_calls = []

    result = EnhancedTranslationOrchestrator(
        _config(term_confirmation=TermConfirmationMode.MANUAL), gateway=gateway
    ).run(cues, confirm_terms=lambda values: confirmation_calls.append(values) or values)

    assert result.translations == {1: "你好。"}
    assert confirmation_calls == []
