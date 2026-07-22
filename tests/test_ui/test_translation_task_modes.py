import os
import subprocess
import sys


def _run_qt_script(script: str) -> None:
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_translation_mode_cards_share_role_bindings_and_report_missing(tmp_path):
    profile_path = repr(str(tmp_path / "profiles.json"))
    _run_qt_script(
        f"""
from PyQt5.QtWidgets import QApplication
from qfluentwidgets import CardWidget, ComboBox, SimpleCardWidget, SpinBox, SwitchButton
from videocaptioner.core.llm.models import LLMModelProfile, LLMTransport, ProviderDialect
from videocaptioner.core.llm.profiles import LLMModelProfileStore
from videocaptioner.core.translate.enhanced.models import TranslationAuditMode
from videocaptioner.core.translate.types import TranslationMode
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.components.TranslationModeSelector import TranslationModeSelector

app = QApplication([])
old = (cfg.translation_mode.value, cfg.main_llm_profile_id.value, cfg.review_llm_profile_id.value)
store = LLMModelProfileStore({profile_path})
store.save(LLMModelProfile(
    profile_id='main', name='Main profile', transport=LLMTransport.OPENAI_COMPATIBLE,
    dialect=ProviderDialect.GENERIC, base_url='https://example.test/v1', api_key='x',
    model='main-model'))
try:
    cfg.set(cfg.main_llm_profile_id, '')
    cfg.set(cfg.review_llm_profile_id, '')
    widget = TranslationModeSelector(profile_store=store)
    assert len(widget.cards) == 3
    assert all('推荐' not in card.text() for card in widget.cards.values())
    assert all(isinstance(card, CardWidget) for card in widget.cards.values())
    assert isinstance(widget.enhanced_llm_panel, SimpleCardWidget)
    assert isinstance(widget.main_profile_combo, ComboBox)
    assert isinstance(widget.enhanced_batch_spin, SpinBox)
    assert isinstance(widget.reflect_checkbox, SwitchButton)
    assert widget.audit_mode_combo.itemText(0) == '审计并人工确认'
    assert widget.audit_mode_combo.itemText(1) == '自动采纳校对修正'
    assert cfg.translation_audit_mode.defaultValue is TranslationAuditMode.AUTO_APPLY_REVIEW

    widget.cards[TranslationMode.ENHANCED_LLM].click()
    app.processEvents()
    assert cfg.translation_mode.value is TranslationMode.ENHANCED_LLM
    assert widget.options_stack.currentWidget() is widget.enhanced_llm_panel
    assert not widget.is_selected_mode_available
    assert '主翻译模型' in widget.cards[TranslationMode.ENHANCED_LLM].text()
    assert '高级校对模型' in widget.cards[TranslationMode.ENHANCED_LLM].text()

    widget.enhanced_main_profile_combo.setCurrentIndex(
        widget.enhanced_main_profile_combo.findData('main'))
    app.processEvents()
    assert cfg.main_llm_profile_id.value == 'main'
    assert widget.main_profile_combo.currentData() == 'main'
    assert not widget.is_selected_mode_available

    widget.review_profile_combo.setCurrentIndex(widget.review_profile_combo.findData('main'))
    app.processEvents()
    assert cfg.review_llm_profile_id.value == 'main'
    assert widget.is_selected_mode_available
    assert '已就绪' not in widget.cards[TranslationMode.ENHANCED_LLM].text()

    widget.cards[TranslationMode.SINGLE_LLM].click()
    assert widget.options_stack.currentWidget() is widget.single_llm_panel
    assert widget.reflect_checkbox.isVisible() == widget.single_llm_panel.isVisible()
    assert not widget.enhanced_llm_panel.isVisible()
    widget.close()
finally:
    cfg.set(cfg.translation_mode, old[0])
    cfg.set(cfg.main_llm_profile_id, old[1])
    cfg.set(cfg.review_llm_profile_id, old[2])
"""
    )


def test_non_llm_mode_requires_a_traditional_translation_service():
    _run_qt_script(
        """
from PyQt5.QtWidgets import QApplication
from videocaptioner.core.entities import TranslatorServiceEnum
from videocaptioner.core.translate.types import TranslationMode
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.components.TranslationModeSelector import TranslationModeSelector

app = QApplication([])
old_service = cfg.translator_service.value
old_mode = cfg.translation_mode.value
try:
    cfg.set(cfg.translator_service, TranslatorServiceEnum.OPENAI)
    widget = TranslationModeSelector()
    widget.set_mode(TranslationMode.NON_LLM)
    assert not widget.is_selected_mode_available
    assert widget.missing_configuration(TranslationMode.NON_LLM) == ('非 LLM 翻译服务',)

    cfg.set(cfg.translator_service, TranslatorServiceEnum.GOOGLE)
    assert widget.is_selected_mode_available
    widget.close()
finally:
    cfg.set(cfg.translator_service, old_service)
    cfg.set(cfg.translation_mode, old_mode)
"""
    )


def test_glossary_review_accepts_empty_custom_value_and_merges_contexts():
    _run_qt_script(
        """
from PyQt5.QtWidgets import QApplication
from qfluentwidgets import ListWidget, PlainTextEdit, PrimaryPushButton, RadioButton
from videocaptioner.core.translate.enhanced.models import (
    GlossarySelectionSource, TermCandidate, TermReviewDecision)
from videocaptioner.ui.components.GlossaryReviewPage import GlossaryReviewPage

app = QApplication([])
page = GlossaryReviewPage()
assert isinstance(page.term_list, ListWidget)
assert isinstance(page.context_view, PlainTextEdit)
assert isinstance(page.review_radio, RadioButton)
assert isinstance(page.continue_button, PrimaryPushButton)
candidates = (
    TermCandidate(candidate_id='one', source_term='Agent', sense='software',
        aliases=('agent',), occurrence_ids=(1,), representative_context_ids=(1,),
        main_translation='代理',
        review_translation='智能体', review_decision=TermReviewDecision.CORRECT,
        final_translation='智能体'),
    TermCandidate(candidate_id='two', source_term='AI agent', sense='software',
        occurrence_ids=(3,), representative_context_ids=(3,), main_translation='AI 代理',
        review_translation='AI 智能体',
        final_translation='AI 智能体'),
)
page.set_candidates(candidates, {1: '#1 context', 3: '#3 context'})
page.term_list.setCurrentRow(0)
page.term_list.item(0).setSelected(True)
page.term_list.item(1).setSelected(True)
page.merge_selected()
assert len(page.candidates) == 1
merged = page.candidates[0]
assert merged.occurrence_ids == (1, 3)
assert merged.representative_context_ids == (1, 3)
assert 'AI agent' in merged.aliases
assert '#1 context' in page.context_view.toPlainText()
assert '#3 context' in page.context_view.toPlainText()

captured = []
page.confirmed.connect(captured.append)
page.custom_radio.setChecked(True)
page.custom_edit.setText('')
page.continue_button.click()
assert len(captured) == 1
assert captured[0][0].final_translation == ''
assert captured[0][0].selection_source is GlossarySelectionSource.USER_CUSTOM
page.close()
"""
    )


def test_subtitle_page_connects_term_and_audit_confirmation(tmp_path):
    subtitle = tmp_path / "source.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nAgent context\n", encoding="utf-8"
    )
    subtitle_path = repr(str(subtitle))
    _run_qt_script(
        f"""
from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import QApplication, QAbstractItemView, QPushButton
from qfluentwidgets import PlainTextEdit, PushButton, TableWidget
from videocaptioner.core.translate.enhanced.models import (
    AuditIssueDisposition, TermCandidate, TranslationAuditIssue, TranslationAuditReport)
from videocaptioner.ui.common.config import cfg
import videocaptioner.ui.view.subtitle_interface as subtitle_view

class FakeThread(QObject):
    finished = pyqtSignal(str, str)
    progress = pyqtSignal(int, str)
    update = pyqtSignal(dict)
    update_all = pyqtSignal(dict)
    error = pyqtSignal(str)
    term_confirmation_required = pyqtSignal(object)
    audit_confirmation_required = pyqtSignal(object)
    audit_ready = pyqtSignal(object)
    def __init__(self, task):
        super().__init__()
        self.task = task
        self.submitted = None
        self.submitted_audit = None
    def set_custom_prompt_text(self, text): self.prompt = text
    def start(self):
        self.term_confirmation_required.emit((TermCandidate(
            candidate_id='one', source_term='Agent', sense='software', occurrence_ids=(1,),
            main_translation='代理', review_translation='智能体', final_translation='智能体'),))
    def submit_term_confirmation(self, candidates): self.submitted = candidates
    def submit_audit_confirmation(self, accepted_ids): self.submitted_audit = accepted_ids
    def stop(self): pass
    def isRunning(self): return False

app = QApplication([])
subtitle_view.SubtitleThread = FakeThread
old_need_translate = cfg.need_translate.value
try:
    # This test exercises page wiring with a fake thread, not configuration
    # availability; bypass whichever persisted model profile state the host has.
    cfg.set(cfg.need_translate, False)
    widget = subtitle_view.SubtitleInterface()
    widget.load_subtitle_file({subtitle_path})
    widget.start_subtitle_optimization(need_create_task=True)
    assert widget.workspace_stack.currentWidget() is widget.glossary_review_page
    assert 'Agent context' in widget.glossary_review_page.context_view.toPlainText()
    widget.glossary_review_page.continue_button.click()
    assert widget.subtitle_optimization_thread.submitted is not None
    assert widget.workspace_stack.currentWidget() is widget.translation_workspace

    report = TranslationAuditReport(issues=(TranslationAuditIssue(
        cue_id=1, category='semantic_accuracy', message='check', original_text='Agent context',
        translated_text='代理语境', suggested_translation='智能体语境'),))
    widget.subtitle_optimization_thread.audit_confirmation_required.emit(report)
    assert widget.workspace_stack.currentWidget() is widget.translation_audit_page
    assert widget.translation_audit_page.issue_table.editTriggers() == QAbstractItemView.NoEditTriggers
    assert isinstance(widget.translation_audit_page.issue_table, TableWidget)
    assert isinstance(widget.translation_audit_page.details_view, PlainTextEdit)
    assert isinstance(widget.translation_audit_page.close_button, PushButton)
    assert widget.translation_audit_page.issue_table.item(0, 1).text() == '语义准确性'
    assert widget.translation_audit_page.details_card.isHidden()
    assert 'Agent context' in widget.translation_audit_page.comparison_view.toPlainText()
    assert '智能体语境' in widget.translation_audit_page.comparison_view.toPlainText()
    assert not widget.translation_audit_page.apply_button.isHidden()
    widget.translation_audit_page.apply_button.click()
    assert widget.translation_audit_page.issue_table.item(0, 5).text() == '将采纳'
    widget.translation_audit_page.finish_button.click()
    assert widget.subtitle_optimization_thread.submitted_audit == (1,)

    final_report = TranslationAuditReport(issues=(TranslationAuditIssue(
        cue_id=1, category='semantic_accuracy', message='check', original_text='Agent context',
        translated_text='代理语境', suggested_translation='智能体语境',
        disposition=AuditIssueDisposition.USER_APPLIED),))
    widget.subtitle_optimization_thread.audit_ready.emit(final_report)
    assert widget.translation_audit_page.issue_table.item(0, 5).text() == '已由用户采纳'
    widget.translation_audit_page.close_button.click()
    assert widget.workspace_stack.currentWidget() is widget.translation_workspace
    widget.close()
finally:
    cfg.set(cfg.need_translate, old_need_translate)
"""
    )


def test_prompt_button_edits_main_translation_prompt_only():
    _run_qt_script(
        """
from PyQt5.QtWidgets import QApplication, QWidget
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.view.subtitle_interface import PromptDialog

app = QApplication([])
old_main = cfg.main_translation_prompt.value
old_optimization = cfg.optimization_prompt_text.value
try:
    cfg.set(cfg.main_translation_prompt, 'main-before')
    parent = QWidget()
    dialog = PromptDialog(parent)
    assert dialog.text_edit.toPlainText() == 'main-before'
    dialog.text_edit.setPlainText('main-after')
    dialog.save_prompt()
    assert cfg.main_translation_prompt.value == 'main-after'
    assert cfg.optimization_prompt_text.value == old_optimization
    dialog.close()
finally:
    cfg.set(cfg.main_translation_prompt, old_main)
"""
    )
