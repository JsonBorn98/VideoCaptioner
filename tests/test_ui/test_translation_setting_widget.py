import json
import os
from importlib import import_module

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QPoint, QRect, Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QWidget
from qfluentwidgets import CaptionLabel, StrongBodyLabel

from videocaptioner.core.llm.check_llm import (
    CapabilityProbeResult,
    ModelProfileProbeResult,
)
from videocaptioner.core.llm.models import (
    LLMModelProfile,
    LLMTransport,
    OpenAIEndpoint,
    ProviderDialect,
    thaw_json_object,
)
from videocaptioner.core.llm.profiles import LLMModelProfileStore
from videocaptioner.core.translate.types import TranslationMode
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.components.TranslationSettingWidget import (
    _REQUEST_OPTION_TEMPLATES,
    TranslationSettingWidget,
    _ProfileDialog,
)
from videocaptioner.ui.view.setting_interface import SettingInterface

app = QApplication.instance() or QApplication([])
app.setQuitOnLastWindowClosed(False)


def _profile(profile_id: str, name: str, model: str) -> LLMModelProfile:
    return LLMModelProfile(
        profile_id=profile_id,
        name=name,
        transport=LLMTransport.OPENAI_COMPATIBLE,
        dialect=ProviderDialect.GENERIC,
        base_url=f"https://{profile_id}.example/v1",
        api_key="secret",
        model=model,
        work_context_tokens=65_536,
        max_concurrency=3,
    )


def test_translation_tabs_are_navigation_only_and_reflect_is_single_llm_only(tmp_path):
    old_mode = cfg.translation_mode.value
    try:
        cfg.set(cfg.translation_mode, TranslationMode.ENHANCED_LLM)
        widget = TranslationSettingWidget(
            profile_store=LLMModelProfileStore(tmp_path / "profiles.json")
        )

        assert tuple(widget.pages) == ("non-llm", "single-llm", "enhanced-llm")
        widget.stackedWidget.setCurrentWidget(widget.pages["single-llm"])
        app.processEvents()
        assert cfg.translation_mode.value is TranslationMode.ENHANCED_LLM
        assert widget.pages["single-llm"].isAncestorOf(widget.reflectCard)
        assert not widget.pages["enhanced-llm"].isAncestorOf(widget.reflectCard)
        widget.close()
    finally:
        cfg.set(cfg.translation_mode, old_mode)


def test_translation_settings_expose_active_page_height_to_expand_layout(tmp_path):
    widget = TranslationSettingWidget(
        profile_store=LLMModelProfileStore(tmp_path / "profiles.json")
    )
    widget.show()
    app.processEvents()

    compact_height = widget.height()
    assert compact_height > widget.pivot.height()
    assert widget.stackedWidget.height() == widget.pages["non-llm"].sizeHint().height()

    widget.stackedWidget.setCurrentWidget(widget.pages["enhanced-llm"])
    app.processEvents()
    assert widget.height() > compact_height
    assert (
        widget.stackedWidget.height()
        == widget.pages["enhanced-llm"].sizeHint().height()
    )
    widget.close()


def test_profile_selectors_show_names_and_share_the_unique_role_binding(tmp_path):
    store = LLMModelProfileStore(tmp_path / "profiles.json")
    first = store.save(_profile("first", "日常翻译", "model-internal-a"))
    second = store.save(_profile("second", "高级翻译", "model-internal-b"))
    old_main = cfg.main_llm_profile_id.value
    try:
        cfg.set(cfg.main_llm_profile_id, first.profile_id)
        widget = TranslationSettingWidget(profile_store=store)
        card = widget.singleMainProfileCard
        labels = [card.comboBox.itemText(i) for i in range(card.comboBox.count())]

        assert "日常翻译" in labels
        assert "高级翻译" in labels
        assert "model-internal-a" not in labels
        assert "model-internal-b" not in labels

        card.comboBox.setCurrentIndex(card.comboBox.findData(second.profile_id))
        app.processEvents()
        assert cfg.main_llm_profile_id.value == second.profile_id
        assert widget.enhancedMainProfileCard.selectedProfileId() == second.profile_id
        assert card.createButton.isVisibleTo(card)
        assert card.editButton.isEnabled()
        assert card.deleteButton.isEnabled()
        widget.close()
    finally:
        cfg.set(cfg.main_llm_profile_id, old_main)


def test_profile_context_probe_keeps_user_work_budget_unchanged(monkeypatch):
    parent = QWidget()
    dialog = _ProfileDialog(parent=parent)
    dialog.nameEdit.setText("探查方案")
    dialog.baseUrlEdit.setText("https://example.test/v1")
    dialog.modelEdit.setText("example-model")
    dialog.contextSpin.setValue(64_000)
    emitted = []
    dialog.probeRequested.connect(emitted.append)
    monkeypatch.setattr(dialog, "_confirmProbeCost", lambda: True)

    dialog.probeButton.click()

    assert len(emitted) == 1
    assert emitted[0].work_context_tokens == 64_000
    assert dialog.contextSpin.value() == 64_000
    dialog.close()
    parent.close()


def test_profile_dialog_lists_and_maps_all_interface_formats():
    parent = QWidget()
    dialog = _ProfileDialog(parent=parent)
    dialog.nameEdit.setText("接口映射")
    dialog.baseUrlEdit.setText("https://example.test/v1")
    dialog.modelEdit.setText("model")

    keys = [
        dialog.interfaceCombo.itemData(index)
        for index in range(dialog.interfaceCombo.count())
    ]
    labels = [
        dialog.interfaceCombo.itemText(index)
        for index in range(dialog.interfaceCombo.count())
    ]
    assert keys == [
        "openai-chat",
        "openai-responses",
        "anthropic-messages",
        "gemini",
    ]
    assert labels == [
        "OpenAI · Chat Completions",
        "OpenAI · Responses",
        "Anthropic · Messages",
        "Google · Gemini",
    ]
    assert dialog.interfaceCombo.currentData() == "openai-chat"

    expected = {
        "openai-chat": (
            LLMTransport.OPENAI_COMPATIBLE,
            OpenAIEndpoint.CHAT_COMPLETIONS,
        ),
        "openai-responses": (
            LLMTransport.OPENAI_COMPATIBLE,
            OpenAIEndpoint.RESPONSES,
        ),
        "anthropic-messages": (
            LLMTransport.ANTHROPIC_MESSAGES,
            OpenAIEndpoint.CHAT_COMPLETIONS,
        ),
        "gemini": (LLMTransport.GEMINI, OpenAIEndpoint.CHAT_COMPLETIONS),
    }
    for key, (transport, endpoint) in expected.items():
        dialog.interfaceCombo.setCurrentIndex(dialog.interfaceCombo.findData(key))
        profile = dialog.temporaryProfile()
        assert profile.transport is transport
        assert profile.openai_endpoint is endpoint

    dialog.close()
    parent.close()


def test_profile_dialog_round_trips_responses_options_and_output_cap():
    parent = QWidget()
    profile = LLMModelProfile(
        profile_id="responses-profile",
        name="Responses",
        transport=LLMTransport.OPENAI_COMPATIBLE,
        dialect=ProviderDialect.OPENAI,
        base_url="https://example.test/v1",
        api_key="secret",
        model="gpt-test",
        work_context_tokens=65_536,
        max_concurrency=3,
        openai_endpoint=OpenAIEndpoint.RESPONSES,
        request_options={
            "reasoning": {"effort": "high"},
        },
        max_output_tokens=8192,
    )

    dialog = _ProfileDialog(profile, parent)
    restored = dialog.temporaryProfile()

    assert dialog.interfaceCombo.currentData() == "openai-responses"
    assert "Responses" in dialog.interfaceCombo.currentText()
    assert dialog.outputModeCombo.currentData() == "custom"
    assert restored.openai_endpoint is OpenAIEndpoint.RESPONSES
    assert restored.max_output_tokens == 8192
    assert thaw_json_object(restored.request_options) == {
        "reasoning": {"effort": "high"},
    }
    dialog.close()
    parent.close()


def test_profile_dialog_maps_interface_formats_and_rejects_protected_json():
    parent = QWidget()
    dialog = _ProfileDialog(parent=parent)
    dialog.nameEdit.setText("Native")
    dialog.baseUrlEdit.setText("https://example.test")
    dialog.modelEdit.setText("model")
    dialog.interfaceCombo.setCurrentIndex(
        dialog.interfaceCombo.findData("anthropic-messages")
    )
    app.processEvents()

    native_profile = dialog.temporaryProfile()
    assert native_profile.transport is LLMTransport.ANTHROPIC_MESSAGES
    assert native_profile.openai_endpoint is OpenAIEndpoint.CHAT_COMPLETIONS

    dialog.interfaceCombo.setCurrentIndex(
        dialog.interfaceCombo.findData("openai-responses")
    )
    dialog.requestOptionsEdit.setPlainText('{"input": "replace application input"}')
    with pytest.raises(ValueError, match="request_options.input"):
        dialog.temporaryProfile()
    dialog.close()
    parent.close()


def test_advanced_templates_replace_only_json_and_all_are_locally_valid(monkeypatch):
    parent = QWidget()
    dialog = _ProfileDialog(parent=parent)
    dialog.nameEdit.setText("Template profile")
    dialog.baseUrlEdit.setText("https://example.test/v1")
    dialog.modelEdit.setText("model-before")
    dialog.interfaceCombo.setCurrentIndex(
        dialog.interfaceCombo.findData("openai-responses")
    )
    monkeypatch.setattr(dialog, "_confirmTemplate", lambda _label: True)

    for key, (_label, _description, expected) in _REQUEST_OPTION_TEMPLATES.items():
        dialog.templateCombo.setCurrentIndex(dialog.templateCombo.findData(key))
        dialog._applyTemplate()
        assert dialog.requestOptions() == expected
        assert dialog.modelEdit.text() == "model-before"
        assert dialog.interfaceCombo.currentData() == "openai-responses"
        dialog.temporaryProfile()

    dialog.close()
    parent.close()


def test_advanced_templates_never_add_temperature_options():
    assert all(
        "temperature" not in json.dumps(options)
        for _label, _description, options in _REQUEST_OPTION_TEMPLATES.values()
    )


def test_profile_dialog_advanced_editor_stays_visible_and_accepts_typing():
    parent = QWidget()
    parent.resize(768, 768)
    parent.show()
    dialog = _ProfileDialog(parent=parent)
    dialog.show()
    app.processEvents()

    dialog.advancedButton.click()
    app.processEvents()
    app.processEvents()

    editor = dialog.requestOptionsEdit
    viewport = dialog.formScrollArea.viewport()
    editor_in_viewport = QRect(editor.mapTo(viewport, QPoint()), editor.size())
    editor_in_form = QRect(editor.mapTo(dialog.formWidget, QPoint()), editor.size())
    template_hint_in_form = QRect(
        dialog.templateHint.mapTo(dialog.formWidget, QPoint()),
        dialog.templateHint.size(),
    )
    request_hint_in_form = QRect(
        dialog.requestOptionsHint.mapTo(dialog.formWidget, QPoint()),
        dialog.requestOptionsHint.size(),
    )

    assert editor.height() >= editor.minimumHeight() >= 170
    assert viewport.rect().contains(editor_in_viewport)
    assert template_hint_in_form.bottom() < editor_in_form.top()
    assert editor_in_form.bottom() < request_hint_in_form.top()
    assert editor.isEnabled()
    assert not editor.isReadOnly()
    assert editor.hasFocus()

    hit = dialog.childAt(editor.mapTo(dialog, editor.rect().center()))
    assert hit is not None
    assert hit is editor or editor.isAncestorOf(hit)

    QTest.keyClick(editor, Qt.Key_A, Qt.ControlModifier)
    QTest.keyClicks(editor, '{"reasoning_effort":"high"}')
    assert editor.toPlainText() == '{"reasoning_effort":"high"}'

    dialog.close()
    parent.close()


def test_profile_dialog_resize_keeps_actions_clear_and_hidden_editor_unfocused():
    parent = QWidget()
    parent.resize(1024, 1000)
    parent.show()
    dialog = _ProfileDialog(parent=parent)
    dialog.show()
    dialog.advancedButton.click()
    app.processEvents()
    app.processEvents()

    parent.resize(768, 500)
    app.processEvents()
    app.processEvents()

    scroll_rect = QRect(
        dialog.formScrollArea.mapTo(dialog, QPoint()), dialog.formScrollArea.size()
    )
    button_group_rect = QRect(
        dialog.buttonGroup.mapTo(dialog, QPoint()), dialog.buttonGroup.size()
    )
    assert not scroll_rect.intersects(button_group_rect)

    save_button_center = dialog.yesButton.mapTo(
        dialog, dialog.yesButton.rect().center()
    )
    hit = dialog.childAt(save_button_center)
    assert hit is not None
    assert hit is dialog.yesButton or dialog.yesButton.isAncestorOf(hit)

    dialog.advancedButton.setFocus()
    dialog.advancedButton.click()
    # Queue an editor reveal, then collapse again before its timer is delivered.
    dialog.advancedButton.click()
    dialog.advancedButton.click()
    app.processEvents()

    assert dialog.advancedWidget.isHidden()
    assert not dialog.requestOptionsEdit.hasFocus()

    dialog.close()
    parent.close()


def test_store_true_requires_confirmation_on_every_save_and_cap_warning(monkeypatch):
    parent = QWidget()
    dialog = _ProfileDialog(parent=parent)
    dialog.nameEdit.setText("Stored")
    dialog.baseUrlEdit.setText("https://example.test/v1")
    dialog.modelEdit.setText("model")
    dialog.outputModeCombo.setCurrentIndex(dialog.outputModeCombo.findData("custom"))
    dialog.outputTokensSpin.setValue(512)
    dialog.requestOptionsEdit.setPlainText('{"store": true}')
    confirmations = []
    monkeypatch.setattr(
        dialog,
        "_confirmStore",
        lambda: confirmations.append("confirmed") is None or True,
    )

    assert dialog.validate() is True
    assert dialog.validate() is True
    assert confirmations == ["confirmed", "confirmed"]
    warnings = dialog.profileWarnings(dialog.temporaryProfile())
    assert any("小于 1024" in warning for warning in warnings)
    dialog.close()
    parent.close()


def test_dialog_save_button_does_not_accept_when_store_confirmation_is_rejected(
    monkeypatch,
):
    parent = QWidget()
    dialog = _ProfileDialog(parent=parent)
    dialog.nameEdit.setText("Stored")
    dialog.baseUrlEdit.setText("https://example.test/v1")
    dialog.modelEdit.setText("model")
    dialog.requestOptionsEdit.setPlainText('{"store": true}')
    accepted = []
    dialog.accepted.connect(lambda: accepted.append(True))
    monkeypatch.setattr(dialog, "_confirmStore", lambda: False)

    dialog.yesButton.click()
    app.processEvents()

    assert accepted == []
    dialog.close()
    parent.close()


def test_profile_dialog_displays_independent_probe_results():
    parent = QWidget()
    dialog = _ProfileDialog(parent=parent)
    dialog.showProbeResult(
        ModelProfileProbeResult(
            text=CapabilityProbeResult(True, "OK"),
            structured=CapabilityProbeResult(False, "schema rejected"),
            max_output_tokens=4608,
        )
    )

    text = dialog.probeResultLabel.text()
    assert "文本能力：通过" in text
    assert "结构化输出：失败" in text
    assert "4608" in text
    dialog.close()
    parent.close()


def test_profile_create_edit_delete_actions_update_store_and_role_binding(
    tmp_path,
    monkeypatch,
):
    module = import_module(
        "videocaptioner.ui.components.TranslationSettingWidget"
    )
    store = LLMModelProfileStore(tmp_path / "profiles.json")
    old_main = cfg.main_llm_profile_id.value

    class FakeDialog:
        def __init__(self, profile=None, parent=None):
            del parent
            self.profile = profile

        def exec(self):
            return True

        def values(self):
            return {
                "name": "编辑后方案" if self.profile else "新建方案",
                "transport": LLMTransport.OPENAI_COMPATIBLE,
                "dialect": ProviderDialect.GENERIC,
                "base_url": "https://example.test/v1",
                "api_key": "secret",
                "model": "example-model-v2" if self.profile else "example-model-v1",
                "work_context_tokens": 65_536,
                "max_concurrency": 2,
            }

    class ConfirmDelete:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def exec(self):
            return True

    try:
        widget = TranslationSettingWidget(profile_store=store)
        monkeypatch.setattr(module, "_ProfileDialog", FakeDialog)
        monkeypatch.setattr(module, "MessageBox", ConfirmDelete)
        monkeypatch.setattr(widget, "_connectProbe", lambda _dialog: None)
        card = widget.singleMainProfileCard

        widget._createProfile(card)
        created_id = cfg.main_llm_profile_id.value
        assert store.get(created_id).name == "新建方案"

        widget._editProfile(card)
        assert store.get(created_id).name == "编辑后方案"
        assert store.get(created_id).model == "example-model-v2"

        widget._deleteProfile(card)
        assert store.list() == ()
        assert cfg.main_llm_profile_id.value == ""
        widget.close()
    finally:
        cfg.set(cfg.main_llm_profile_id, old_main)


def test_setting_interface_embeds_translation_widget_and_relabels_legacy_llm(tmp_path):
    widget = SettingInterface(
        translation_profile_store=LLMModelProfileStore(tmp_path / "profiles.json")
    )

    assert widget.translationSettingsWidget is not None
    assert widget.llmGroup.titleLabel.text() == "通用 LLM 工具配置"
    assert widget.translationSettingsWidget.stackedWidget.count() == 3
    assert widget.llmContentLoggingCard is not None
    assert widget.translationSettingsWidget.height() > 200
    assert isinstance(widget.translationSettingsWidget.titleLabel, StrongBodyLabel)
    assert isinstance(widget.translationSettingsWidget.subtitleLabel, CaptionLabel)
    enhanced_group = widget.translationSettingsWidget.enhancedMainProfileCard.parentWidget()
    assert enhanced_group.titleLabel.text() == "模型、术语与审计"
    widget.close()
