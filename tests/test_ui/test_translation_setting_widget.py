import os
from importlib import import_module

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication, QWidget
from qfluentwidgets import CaptionLabel, StrongBodyLabel

from videocaptioner.core.llm.models import (
    LLMModelProfile,
    LLMTransport,
    ProviderDialect,
)
from videocaptioner.core.llm.profiles import LLMModelProfileStore
from videocaptioner.core.translate.types import TranslationMode
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.components.TranslationSettingWidget import (
    TranslationSettingWidget,
    _ProfileDialog,
)
from videocaptioner.ui.view.setting_interface import SettingInterface

app = QApplication.instance() or QApplication([])


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


def test_profile_context_probe_keeps_user_work_budget_unchanged():
    parent = QWidget()
    dialog = _ProfileDialog(parent=parent)
    dialog.nameEdit.setText("探查方案")
    dialog.baseUrlEdit.setText("https://example.test/v1")
    dialog.modelEdit.setText("example-model")
    dialog.contextSpin.setValue(64_000)
    emitted = []
    dialog.probeRequested.connect(emitted.append)

    dialog.probeButton.click()

    assert len(emitted) == 1
    assert emitted[0].work_context_tokens == 64_000
    assert dialog.contextSpin.value() == 64_000
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
    assert widget.translationSettingsWidget.height() > 200
    assert isinstance(widget.translationSettingsWidget.titleLabel, StrongBodyLabel)
    assert isinstance(widget.translationSettingsWidget.subtitleLabel, CaptionLabel)
    enhanced_group = widget.translationSettingsWidget.enhancedMainProfileCard.parentWidget()
    assert enhanced_group.titleLabel.text() == "模型、术语与审计"
    widget.close()
