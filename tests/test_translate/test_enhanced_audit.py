from videocaptioner.core.translate.enhanced.audit import (
    apply_objective_fixes,
    local_audit_issues,
)
from videocaptioner.core.translate.enhanced.models import (
    AuditIssueDisposition,
    GlossaryEntry,
    GlossarySelectionSource,
    SubtitleCue,
    TranslationAuditIssue,
    TranslationAuditReport,
)
from videocaptioner.core.translate.enhanced.report import (
    render_audit_markdown,
    save_audit_markdown,
)


def test_local_audit_checks_source_copy_and_protected_tokens():
    cues = (
        SubtitleCue(1, "Version 2 costs $10 at https://example.com"),
        SubtitleCue(2, "hello"),
    )
    issues = local_audit_issues(cues, {1: "版本售价", 2: "hello"})

    assert {issue.category for issue in issues} == {
        "protected_token_missing",
        "source_copied",
    }


def test_objective_fix_requires_protected_tokens():
    issue = TranslationAuditIssue(
        cue_id=1,
        category="number",
        message="number mismatch",
        original_text="Version 2",
        translated_text="版本三",
        suggested_translation="版本二",
        objective=True,
    )

    translations, resolved = apply_objective_fixes({1: "版本三"}, (issue,))

    assert translations[1] == "版本三"
    assert resolved[0].disposition is AuditIssueDisposition.FIX_VALIDATION_FAILED


def test_markdown_uses_unavailable_for_missing_usage_and_saves_atomically(tmp_path):
    report = TranslationAuditReport(
        authoritative_terms=(
            GlossaryEntry(
                entry_id="agent-software",
                source_term="Agent|AI",
                sense="software\nagent",
                translation="智能体",
                selection_source=GlossarySelectionSource.REVIEW_MODEL_CORRECTED,
            ),
        )
    )
    markdown = render_audit_markdown(report)
    assert "不可用" in markdown
    assert "未发现需要报告" in markdown
    assert "Agent\\|AI" in markdown
    assert "review_model_corrected" in markdown

    path = save_audit_markdown(tmp_path / "audit.md", report)
    assert path.read_text(encoding="utf-8") == markdown
