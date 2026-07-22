from videocaptioner.core.translate.enhanced.audit import (
    apply_review_fixes,
    local_audit_issues,
    protected_tokens,
    validate_suggested_translation,
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
    assert all("Translation" not in issue.message for issue in issues)


def test_local_audit_ignores_punctuation_after_numbers():
    cues = (
        SubtitleCue(1, "as early as 1980."),
        SubtitleCue(2, "bought them in 1997,"),
        SubtitleCue(3, "begin at level 1,"),
    )

    issues = local_audit_issues(
        cues,
        {1: "早在1980年。", 2: "于1997年收购，", 3: "从1级开始，"},
    )

    assert issues == ()
    assert protected_tokens(cues[0].text) == ("1980",)
    assert protected_tokens(cues[1].text) == ("1997",)
    assert protected_tokens(cues[2].text) == ("1",)


def test_numeric_audit_compares_values_instead_of_substrings():
    cues = (
        SubtitleCue(1, "The price was 1,000.50 dollars."),
        SubtitleCue(2, "begin at level 1"),
    )

    issues = local_audit_issues(cues, {1: "价格是1000.5美元。", 2: "从10级开始"})

    assert len(issues) == 1
    assert issues[0].cue_id == 2
    assert issues[0].category == "protected_token_missing"
    assert issues[0].message.endswith("1")


def test_suggestion_validation_accepts_equivalent_numeric_formatting():
    assert validate_suggested_translation("In 1980.", "在1980年。") == (True, "")
    assert validate_suggested_translation("Cost: 1,000.50", "价格：1000.5") == (True, "")
    assert validate_suggested_translation("Progress: 42%", "进度：42％") == (True, "")
    assert validate_suggested_translation(
        "Two lines", "第一行\n第二行", current_translation="原有单行"
    ) == (False, "建议译文改变了字幕换行结构")
    assert validate_suggested_translation(
        "Visit https://example.com.", "请访问 https://example.com。"
    ) == (True, "")


def test_review_fix_requires_protected_tokens():
    issue = TranslationAuditIssue(
        cue_id=1,
        category="number",
        message="number mismatch",
        original_text="Version 2",
        translated_text="版本三",
        suggested_translation="版本二",
    )

    translations, resolved = apply_review_fixes({1: "版本三"}, (issue,))

    assert translations[1] == "版本三"
    assert resolved[0].disposition is AuditIssueDisposition.FIX_VALIDATION_FAILED


def test_review_fix_applies_language_quality_suggestion_without_objective_gate():
    issue = TranslationAuditIssue(
        cue_id=1,
        category="target_language_quality",
        message="表达生硬。",
        original_text="The result is clear.",
        translated_text="结果是清楚的。",
        suggested_translation="结果很明确。",
    )

    translations, resolved = apply_review_fixes({1: issue.translated_text}, (issue,))

    assert translations[1] == "结果很明确。"
    assert resolved[0].disposition is AuditIssueDisposition.AUTO_APPLIED


def test_review_fix_rejects_authoritative_term_regression():
    term = GlossaryEntry(
        entry_id="agent",
        source_term="Agent",
        sense="software",
        translation="智能体",
        selection_source=GlossarySelectionSource.REVIEW_MODEL_CORRECTED,
    )
    issue = TranslationAuditIssue(
        cue_id=1,
        category="terminology",
        message="术语需要调整。",
        original_text="The Agent responds.",
        translated_text="智能体作出响应。",
        suggested_translation="代理作出响应。",
    )

    translations, resolved = apply_review_fixes(
        {1: issue.translated_text}, (issue,), authoritative_terms=(term,)
    )

    assert translations[1] == issue.translated_text
    assert resolved[0].disposition is AuditIssueDisposition.FIX_VALIDATION_FAILED
    assert validate_suggested_translation(
        "She said hello.", "她打了招呼。", authoritative_terms=(term,)
    ) == (True, "")


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
    assert "高级校对修正" in markdown
    assert "review_model_corrected" not in markdown

    path = save_audit_markdown(tmp_path / "audit.md", report)
    assert path.read_text(encoding="utf-8") == markdown


def test_markdown_records_review_suggestion_final_text_and_user_disposition():
    issue = TranslationAuditIssue(
        cue_id=7,
        category="semantic_accuracy",
        categories=("semantic_accuracy", "target_language_quality"),
        message="语义和表达均需修正。",
        original_text="Source",
        translated_text="旧译文",
        suggested_translation="校对译文",
        disposition=AuditIssueDisposition.USER_APPLIED,
    )

    markdown = render_audit_markdown(TranslationAuditReport(issues=(issue,)))

    assert "字幕 7 · 语义准确性、目标语言质量" in markdown
    assert "处理结果：已由用户采纳" in markdown
    assert "当前译文：旧译文" in markdown
    assert "建议译文：校对译文" in markdown
    assert "最终译文：校对译文" in markdown
