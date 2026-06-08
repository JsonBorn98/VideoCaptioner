#!/usr/bin/env python3
"""Capture VideoCaptioner UI smoke screenshots and exercise key state changes.

This script intentionally avoids MainWindow because qframelesswindow can abort in
headless macOS sessions. It verifies the page widgets directly with the same
shared config and theme setup used by the app entrypoint.
"""

from __future__ import annotations

import math
import os
import struct
import sys
import wave
from pathlib import Path
from typing import Literal

DEFAULT_OUTPUT_DIR = Path("/tmp/vc-ui-verify/ui-smoke")
ThemeName = Literal["dark", "light"]


def _parse_args(argv: list[str]) -> tuple[Path, ThemeName]:
    output_dir = DEFAULT_OUTPUT_DIR
    theme: ThemeName = "dark"
    args = list(argv)
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--theme":
            index += 1
            if index >= len(args) or args[index] not in {"dark", "light"}:
                raise SystemExit("--theme must be 'dark' or 'light'")
            theme = args[index]  # type: ignore[assignment]
        elif arg.startswith("--theme="):
            value = arg.split("=", 1)[1]
            if value not in {"dark", "light"}:
                raise SystemExit("--theme must be 'dark' or 'light'")
            theme = value  # type: ignore[assignment]
        elif arg.startswith("-"):
            raise SystemExit(f"unknown option: {arg}")
        else:
            output_dir = Path(arg)
        index += 1
    return output_dir, theme


def _prepare_environment(output_dir: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if "VIDEOCAPTIONER_CONFIG_FILE" not in os.environ:
        config_path = output_dir / "ui-smoke-config.toml"
        if config_path.exists():
            config_path.unlink()
        os.environ["VIDEOCAPTIONER_CONFIG_FILE"] = str(config_path)
    os.environ.setdefault(
        "QT_LOGGING_RULES", "qt.qpa.fonts=false;qt.qpa.fonts.warning=false"
    )


def _write_reference_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        frames = []
        for i in range(1600):
            sample = int(12000 * math.sin(2 * math.pi * 440 * i / 16000))
            frames.append(struct.pack("<h", sample))
        wav_file.writeframes(b"".join(frames))


def _grab(widget, output_dir: Path, name: str, app) -> None:
    _settle_widget(widget, app)
    pixmap = widget.grab()
    if pixmap.isNull():
        raise RuntimeError(f"empty screenshot: {name}")
    pixmap.save(str(output_dir / f"{name}.png"))


def _settle_widget(widget, app) -> None:
    for _ in range(12):
        app.processEvents()
    preview_threads = getattr(widget, "_preview_threads", None)
    if preview_threads is not None:
        for thread in list(preview_threads):
            thread.wait(3000)
        for _ in range(4):
            app.processEvents()


def _make_contact_sheet(
    output_dir: Path, names: list[str], filename: str = "contact-sheet.png"
) -> Path | None:
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None

    thumbs = []
    for name in names:
        image = Image.open(output_dir / f"{name}.png").convert("RGB")
        image.thumbnail((480, 300), Image.LANCZOS)
        canvas = Image.new("RGB", (500, 344), (31, 32, 32))
        canvas.paste(image, (10, 34))
        ImageDraw.Draw(canvas).text((10, 10), f"{name}.png", fill=(245, 247, 246))
        thumbs.append(canvas)

    cols = 2
    rows = (len(thumbs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * 500, rows * 344), (31, 32, 32))
    for index, thumb in enumerate(thumbs):
        sheet.paste(thumb, ((index % cols) * 500, (index // cols) * 344))

    output_path = output_dir / filename
    sheet.save(output_path)
    return output_path


def main() -> int:
    output_dir, theme_name = _parse_args(sys.argv[1:])
    output_dir.mkdir(parents=True, exist_ok=True)
    _prepare_environment(output_dir)

    from PyQt5.QtCore import QTimer
    from PyQt5.QtWidgets import QApplication
    from qfluentwidgets import Theme, setTheme, setThemeColor

    from videocaptioner.ui.common.config import cfg
    from videocaptioner.ui.view.doctor_interface import DoctorInterface
    from videocaptioner.ui.view.dubbing_interface import DubbingInterface
    from videocaptioner.ui.view.home_interface import HomeInterface
    from videocaptioner.ui.view.setting_interface import SettingInterface
    from videocaptioner.ui.view.subtitle_interface import SubtitleInterface
    from videocaptioner.ui.view.subtitle_style_interface import SubtitleStyleInterface
    from videocaptioner.ui.view.task_creation_interface import TaskCreationInterface
    from videocaptioner.ui.view.transcription_interface import TranscriptionInterface
    from videocaptioner.ui.view.video_synthesis_interface import VideoSynthesisInterface

    app = QApplication([])
    cfg.set(cfg.themeMode, Theme.DARK if theme_name == "dark" else Theme.LIGHT, save=False)
    setTheme(cfg.themeMode.value)
    setThemeColor(cfg.themeColor.value)

    screenshot_names: list[str] = []

    pages = [
        ("home", HomeInterface),
        ("task", TaskCreationInterface),
        ("setting", SettingInterface),
        ("dubbing", DubbingInterface),
        ("video-synthesis", VideoSynthesisInterface),
        ("transcription", TranscriptionInterface),
        ("subtitle", SubtitleInterface),
        ("subtitle-style", SubtitleStyleInterface),
        ("doctor", DoctorInterface),
    ]
    for name, cls in pages:
        widget = cls()
        widget.resize(1280, 820)
        widget.show()
        _settle_widget(widget, app)
        _grab(widget, output_dir, name, app)
        screenshot_names.append(name)
        widget.close()

    settings_screenshot_names = _capture_settings_pages(output_dir, app)
    _check_navigation(output_dir, app, screenshot_names)
    _check_task_creation(output_dir, app, screenshot_names)
    _check_settings(app)
    _check_settings_navigation(app)
    _check_transcription(output_dir, app, screenshot_names)
    _check_subtitle(output_dir, app, screenshot_names)
    _check_subtitle_style(output_dir, app, screenshot_names)
    _check_dubbing(output_dir, app, screenshot_names)
    _check_video_synthesis(output_dir, app, screenshot_names)
    compact_screenshot_names = _capture_compact_states(output_dir, app)

    contact_sheet = _make_contact_sheet(output_dir, screenshot_names)
    settings_contact_sheet = _make_contact_sheet(
        output_dir, settings_screenshot_names, "settings-contact-sheet.png"
    )
    compact_contact_sheet = _make_contact_sheet(
        output_dir, compact_screenshot_names, "compact-contact-sheet.png"
    )
    if contact_sheet:
        print(f"contact_sheet={contact_sheet}")
    if settings_contact_sheet:
        print(f"settings_contact_sheet={settings_contact_sheet}")
    if compact_contact_sheet:
        print(f"compact_contact_sheet={compact_contact_sheet}")
    print(f"screenshots={output_dir}")
    print(f"theme={theme_name}")
    print("ui-smoke=ok")
    QTimer.singleShot(0, app.quit)
    app.exec_()
    return 0


def _capture_settings_pages(output_dir: Path, app) -> list[str]:
    from videocaptioner.ui.view.setting_interface import SettingInterface

    widget = SettingInterface()
    widget.resize(1280, 820)
    widget.show()
    names = []
    pages = [
        "transcribe",
        "llm",
        "translate-service",
        "translate",
        "subtitle",
        "dubbing",
        "save",
        "personal",
        "about",
    ]
    for page in pages:
        name = f"setting-page-{page}"
        widget.setCurrentPage(page)
        _grab(widget, output_dir, name, app)
        names.append(name)
    widget.close()
    return names


def _capture_compact_states(output_dir: Path, app) -> list[str]:
    from videocaptioner.core.entities import SubtitleRenderModeEnum
    from videocaptioner.ui.common.config import cfg
    from videocaptioner.ui.view.dubbing_interface import DubbingInterface
    from videocaptioner.ui.view.setting_interface import SettingInterface
    from videocaptioner.ui.view.subtitle_style_interface import SubtitleStyleInterface
    from videocaptioner.ui.view.transcription_interface import TranscriptionInterface
    from videocaptioner.ui.view.video_synthesis_interface import VideoSynthesisInterface

    names: list[str] = []

    dubbing = DubbingInterface()
    dubbing.resize(960, 720)
    dubbing.show()
    dubbing._on_provider_changed("edge")
    _settle_widget(dubbing, app)
    _assert_fits_parent(dubbing.providerPanel, "compact dubbing provider")
    _assert_fits_parent(dubbing.filterPanel, "compact dubbing filter")
    _assert_fits_parent(dubbing.bodyPanel, "compact dubbing body")
    _grab(dubbing, output_dir, "compact-dubbing-edge", app)
    names.append("compact-dubbing-edge")

    reference_wav = output_dir / "compact-reference.wav"
    _write_reference_wav(reference_wav)
    dubbing._on_provider_changed("siliconflow")
    cfg.set(cfg.dubbing_clone_audio, str(reference_wav), save=False)
    dubbing.previewPanel.setAudioPath(str(reference_wav))
    dubbing.previewPanel.setCloneText("这是一段用于克隆测试的参考音频。")
    _settle_widget(dubbing, app)
    assert dubbing.previewPanel.cloneSection.isVisible()
    assert dubbing.bodyPanel.height() >= dubbing.sidePanel.sizeHint().height()
    _assert_fits_parent(dubbing.sidePanel, "compact dubbing side")
    _grab(dubbing, output_dir, "compact-dubbing-clone", app)
    names.append("compact-dubbing-clone")
    dubbing.close()

    synthesis = VideoSynthesisInterface()
    synthesis.resize(960, 720)
    synthesis.show()
    cfg.set(cfg.need_video, True)
    cfg.set(cfg.dubbing_enabled, True)
    synthesis.set_value()
    _settle_widget(synthesis, app)
    assert not synthesis.progress_bar.isVisible()
    _assert_fits_parent(synthesis.config_card, "compact synthesis config")
    _grab(synthesis, output_dir, "compact-video-synthesis", app)
    names.append("compact-video-synthesis")
    synthesis.on_soft_subtitle_action_triggered(True)
    synthesis.on_render_mode_changed(SubtitleRenderModeEnum.ASS_STYLE.value)
    _settle_widget(synthesis, app)
    assert cfg.soft_subtitle.value
    assert not synthesis.render_mode_button.isEnabled()
    _assert_fits_parent(synthesis.config_card, "compact synthesis soft config")
    _grab(synthesis, output_dir, "compact-video-soft-subtitle", app)
    names.append("compact-video-soft-subtitle")
    synthesis.close()

    settings = SettingInterface()
    settings.resize(960, 720)
    settings.show()
    settings.setCurrentPage("dubbing")
    _settle_widget(settings, app)
    assert settings.dubbingProviderRow.isVisible()
    _grab(settings, output_dir, "compact-setting-dubbing", app)
    names.append("compact-setting-dubbing")
    settings.close()

    for name, cls in [
        ("compact-transcription", TranscriptionInterface),
        ("compact-subtitle-style", SubtitleStyleInterface),
    ]:
        widget = cls()
        widget.resize(960, 720)
        widget.show()
        _grab(widget, output_dir, name, app)
        names.append(name)
        widget.close()

    return names


def _assert_fits_parent(widget, label: str, tolerance: int = 4) -> None:
    parent = widget.parentWidget()
    if parent is None:
        return
    if widget.geometry().right() > parent.width() + tolerance:
        raise AssertionError(
            f"{label} overflows parent: right={widget.geometry().right()} parent={parent.width()}"
        )


def _assert_unique_action_texts(menu, label: str) -> list[str]:
    texts = [action.text() for action in menu.actions() if action.text()]
    if not texts:
        raise AssertionError(f"{label} has no visible menu actions")
    duplicates = sorted({text for text in texts if texts.count(text) > 1})
    if duplicates:
        raise AssertionError(f"{label} has duplicate menu actions: {duplicates}")
    return texts


def _check_navigation(output_dir: Path, app, screenshot_names: list[str]) -> None:
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QHBoxLayout, QLabel, QWidget
    from qfluentwidgets import FluentIcon as FIF
    from qfluentwidgets import NavigationInterface
    from qfluentwidgets.components.navigation.navigation_panel import (
        NavigationDisplayMode,
    )

    from videocaptioner.ui.common.theme_tokens import app_palette
    from videocaptioner.ui.view.main_window import (
        NAV_EXPAND_WIDTH,
        NAV_MINIMUM_EXPAND_WIDTH,
        WINDOW_MINIMUM_WIDTH,
    )

    parent = QWidget()
    parent.resize(1050, 800)
    palette = app_palette()
    parent.setStyleSheet(
        f"background:{palette.bg}; QLabel {{ color:{palette.text}; font-size:24px; }}"
    )
    layout = QHBoxLayout(parent)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)

    navigation = NavigationInterface(parent)
    navigation.setExpandWidth(NAV_EXPAND_WIDTH)
    navigation.setMinimumExpandWidth(NAV_MINIMUM_EXPAND_WIDTH)
    for key, icon, text in [
        ("home", FIF.HOME, "主页"),
        ("batch", FIF.VIDEO, "批量处理"),
        ("style", FIF.FONT, "字幕样式"),
        ("dubbing", FIF.VOLUME, "配音"),
        ("doctor", FIF.SEARCH, "诊断"),
    ]:
        navigation.addItem(key, icon, text)
    content = QLabel("内容区域")
    content.setAlignment(Qt.AlignCenter)  # type: ignore[arg-type]
    layout.addWidget(navigation)
    layout.addWidget(content, 1)

    parent.show()
    app.processEvents()
    navigation.expand(False)
    app.processEvents()
    assert 118 <= NAV_EXPAND_WIDTH <= 144
    assert WINDOW_MINIMUM_WIDTH >= 960
    assert navigation.panel.displayMode == NavigationDisplayMode.EXPAND
    assert navigation.panel.width() == NAV_EXPAND_WIDTH
    _grab(parent, output_dir, "navigation-compact", app)
    screenshot_names.append("navigation-compact")

    parent.resize(900, 800)
    app.processEvents()
    navigation.panel.collapse()
    app.processEvents()
    navigation.expand(False)
    app.processEvents()
    assert navigation.panel.displayMode == NavigationDisplayMode.EXPAND
    assert navigation.panel.width() == NAV_EXPAND_WIDTH
    parent.close()


def _check_task_creation(output_dir: Path, app, screenshot_names: list[str]) -> None:
    from videocaptioner.ui.view.task_creation_interface import TaskCreationInterface

    widget = TaskCreationInterface()
    widget.resize(1280, 820)
    widget.show()
    app.processEvents()
    assert widget._start_mode == "browse"
    assert widget.start_button.text() == "选择文件"
    widget.search_input.setText("https://www.bilibili.com/video/BV1xx411c7mD")
    app.processEvents()
    assert widget._start_mode == "process"
    assert widget.start_button.text() == "开始处理"
    _grab(widget, output_dir, "task-url-state", app)
    screenshot_names.append("task-url-state")
    widget.search_input.clear()
    app.processEvents()
    assert widget._start_mode == "browse"
    assert widget.start_button.text() == "选择文件"
    widget.close()


def _check_settings(app) -> None:
    from PyQt5.QtCore import QEvent
    from PyQt5.QtGui import QColor

    import videocaptioner.ui.view.setting_interface as setting_module
    from videocaptioner.core.entities import (
        LLMServiceEnum,
        SubtitleRenderModeEnum,
        TranscribeModelEnum,
        TranslatorServiceEnum,
    )
    from videocaptioner.ui.common.app_icons import AppIcon, custom_icon_path, render_svg_icon
    from videocaptioner.ui.common.config import DEFAULT_THEME_COLOR, cfg
    from videocaptioner.ui.view.setting_interface import SettingInterface

    widget = SettingInterface()
    widget.resize(1280, 820)
    widget.show()
    app.processEvents()
    assert custom_icon_path(AppIcon.ARROW_LEFT).is_file()
    assert not render_svg_icon(AppIcon.ARROW_LEFT, "#28f08b", 17).isNull()
    assert not widget.backButton.icon().isNull()
    assert widget.backButton.iconSize().width() == 17
    assert widget.backButton.property("appIcon") == AppIcon.ARROW_LEFT.value
    app.sendEvent(widget.backButton, QEvent(QEvent.Enter))
    app.processEvents()
    assert widget.backButton.property("appIcon") == AppIcon.ARROW_LEFT.value
    app.sendEvent(widget.backButton, QEvent(QEvent.Leave))
    app.processEvents()

    widget.setCurrentPage("translate-service")
    widget.translatorServiceControl.setCurrentText(TranslatorServiceEnum.OPENAI.value)
    app.processEvents()
    assert cfg.translator_service.value == TranslatorServiceEnum.OPENAI
    assert widget.needReflectTranslateRow.isVisible()
    assert widget.batchSizeRow.isVisible()
    assert widget.threadNumRow.isVisible()
    assert not widget.deeplxEndpointRow.isVisible()

    widget.translatorServiceControl.setCurrentText(TranslatorServiceEnum.DEEPLX.value)
    app.processEvents()
    assert cfg.translator_service.value == TranslatorServiceEnum.DEEPLX
    assert not widget.needReflectTranslateRow.isVisible()
    assert widget.deeplxEndpointRow.isVisible()

    widget.setCurrentPage("subtitle")
    cfg.set(cfg.subtitle_render_mode, SubtitleRenderModeEnum.ASS_STYLE)
    app.processEvents()
    assert cfg.subtitle_render_mode.value == SubtitleRenderModeEnum.ASS_STYLE

    widget.setCurrentPage("llm")
    widget.llmServiceControl.setCurrentText(LLMServiceEnum.SILICON_CLOUD.value)
    app.processEvents()
    assert widget.loadLLMModelsButton.text() == "加载模型"
    assert widget.checkLLMButton.text() == "测试连接"
    widget._save_llm_model_options(
        LLMServiceEnum.SILICON_CLOUD,
        ["  cached-model-a  ", "cached-model-a", "cached-model-b"],
    )
    widget._refresh_llm_rows(LLMServiceEnum.SILICON_CLOUD)
    app.processEvents()
    silicon_model_control = widget.llmProviderControls[LLMServiceEnum.SILICON_CLOUD]["model"]
    assert cfg.silicon_cloud_model_options.value == ["cached-model-a", "cached-model-b"]
    assert silicon_model_control.findText("cached-model-a") >= 0
    assert silicon_model_control.findText("cached-model-b") >= 0
    widget._save_llm_model_options(LLMServiceEnum.OPENAI, ["openai-cached-model"])
    widget._refresh_llm_rows(LLMServiceEnum.OPENAI)
    app.processEvents()
    openai_model_control = widget.llmProviderControls[LLMServiceEnum.OPENAI]["model"]
    assert openai_model_control.findText("openai-cached-model") >= 0
    assert openai_model_control.findText("cached-model-a") < 0
    widget._refresh_llm_rows(LLMServiceEnum.SILICON_CLOUD)
    app.processEvents()
    assert silicon_model_control.findText("cached-model-a") >= 0
    assert silicon_model_control.findText("openai-cached-model") < 0
    _assert_llm_threads_are_split(setting_module)

    widget.setCurrentPage("transcribe")
    widget.transcribeModelControl.setCurrentText(TranscribeModelEnum.WHISPER_API.value.replace(" ✨", ""))
    app.processEvents()
    assert cfg.transcribe_model.value == TranscribeModelEnum.WHISPER_API
    assert widget.whisperApiKeyRow.isVisible()
    assert not widget.whisperCppModelRow.isVisible()

    widget.transcribeModelControl.setCurrentText(TranscribeModelEnum.WHISPER_CPP.value)
    app.processEvents()
    assert cfg.transcribe_model.value == TranscribeModelEnum.WHISPER_CPP
    assert widget.whisperCppModelRow.isVisible()
    assert not widget.whisperApiKeyRow.isVisible()

    widget.setCurrentPage("dubbing")
    widget.dubbingProviderControl.setCurrentText("SiliconFlow CosyVoice")
    app.processEvents()
    assert cfg.dubbing_provider.value == "siliconflow"
    assert widget.dubbingApiKeyRow.isVisible()
    assert widget.dubbingModelRow.isVisible()
    assert widget.dubbingPresetControl.count() > 0
    widget.dubbingApiKeyControl.setText("  sk-smoke-test\n")
    app.processEvents()
    assert cfg.dubbing_api_key.value == "sk-smoke-test"
    assert widget.dubbingApiKeyControl.text() == "sk-smoke-test"

    widget.dubbingProviderControl.setCurrentText("Edge 免费配音")
    app.processEvents()
    assert cfg.dubbing_provider.value == "edge"
    assert not widget.dubbingApiKeyRow.isVisible()

    widget.setCurrentPage("personal")
    cfg.set(cfg.themeColor, QColor("#336699"))
    app.processEvents()
    assert widget.themeColorSwatch.color().name(QColor.HexRgb) == "#336699"
    assert widget.themeColorResetButton.isEnabled()
    widget.themeColorResetButton.click()
    app.processEvents()
    default_theme_color = QColor(DEFAULT_THEME_COLOR).name(QColor.HexRgb).lower()
    assert cfg.themeColor.value.name(QColor.HexRgb).lower() == default_theme_color
    assert widget.themeColorSwatch.color().name(QColor.HexRgb).lower() == default_theme_color
    assert not widget.themeColorResetButton.isEnabled()
    widget.close()


def _assert_llm_threads_are_split(setting_module) -> None:
    original_check = setting_module.check_llm_connection
    original_load = setting_module.get_available_models
    calls: list[str] = []

    def fake_check(api_base: str, api_key: str, model: str):
        calls.append(f"check:{api_base}:{api_key}:{model}")
        return True, "ok"

    def fake_load(api_base: str, api_key: str):
        calls.append(f"load:{api_base}:{api_key}")
        return ["model-a", "model-b"]

    setting_module.check_llm_connection = fake_check
    setting_module.get_available_models = fake_load
    try:
        check_results = []
        check_thread = setting_module.LLMConnectionThread("https://example.test/v1", "sk-test", "model-a")
        check_thread.finished.connect(lambda success, message: check_results.append((success, message)))
        check_thread.run()
        assert check_results == [(True, "ok")]
        assert calls == ["check:https://example.test/v1:sk-test:model-a"]

        calls.clear()
        load_results = []
        load_thread = setting_module.LLMModelLoadThread(
            setting_module.LLMServiceEnum.OPENAI,
            "https://example.test/v1",
            "sk-test",
        )
        load_thread.finished.connect(lambda service, models: load_results.append((service, models)))
        load_thread.run()
        assert load_results == [(setting_module.LLMServiceEnum.OPENAI, ["model-a", "model-b"])]
        assert calls == ["load:https://example.test/v1:sk-test"]
    finally:
        setting_module.check_llm_connection = original_check
        setting_module.get_available_models = original_load


def _check_settings_navigation(app) -> None:
    from PyQt5.QtWidgets import QVBoxLayout, QWidget

    from videocaptioner.ui.view.doctor_interface import DoctorInterface, ItemAction
    from videocaptioner.ui.view.setting_interface import SettingInterface
    from videocaptioner.ui.view.subtitle_interface import SubtitleInterface
    from videocaptioner.ui.view.transcription_interface import TranscriptionInterface

    class SettingsHost(QWidget):
        def __init__(self):
            super().__init__()
            self.opened_pages: list[str] = []
            self.current_interface = None
            self.layout = QVBoxLayout(self)
            self.settingInterface = SettingInterface(self)
            self.layout.addWidget(self.settingInterface)

        def openSettingsPage(self, page_key: str) -> bool:  # noqa: N802
            if not self.settingInterface.setCurrentPage(page_key):
                return False
            self.current_interface = self.settingInterface
            self.opened_pages.append(self.settingInterface.currentPageKey())
            return True

        def switchTo(self, interface):
            self.current_interface = interface

    host = SettingsHost()
    host.resize(1280, 820)
    host.show()
    app.processEvents()

    doctor = DoctorInterface(host)
    for action, page_key in [
        (ItemAction.TRANSCRIBE_SETTINGS, "transcribe"),
        (ItemAction.LLM_SETTINGS, "llm"),
        (ItemAction.TRANSLATE_SETTINGS, "translate-service"),
        (ItemAction.DUBBING_SETTINGS, "dubbing"),
    ]:
        doctor._handle_action(action)
        app.processEvents()
        assert host.opened_pages[-1] == page_key
        assert host.settingInterface.currentPageKey() == page_key
        assert host.current_interface is host.settingInterface

    for alias, page_key in [
        ("asr", "transcribe"),
        ("tts", "dubbing"),
        ("voice", "dubbing"),
        ("models", "llm"),
        ("translation-service", "translate-service"),
        ("video-synthesis", "subtitle"),
    ]:
        assert host.openSettingsPage(alias)
        app.processEvents()
        assert host.opened_pages[-1] == page_key
        assert host.settingInterface.currentPageKey() == page_key

    transcription = TranscriptionInterface(host)
    transcription._show_output_settings()
    app.processEvents()
    assert host.opened_pages[-1] == "transcribe"
    assert host.settingInterface.currentPageKey() == "transcribe"
    transcription.close()

    subtitle = SubtitleInterface(host)
    subtitle.show_subtitle_settings()
    app.processEvents()
    assert host.opened_pages[-1] == "translate"
    assert host.settingInterface.currentPageKey() == "translate"
    subtitle.close()

    doctor.close()
    host.close()


def _check_transcription(output_dir: Path, app, screenshot_names: list[str]) -> None:
    from videocaptioner.core.entities import TranscribeModelEnum
    from videocaptioner.ui.common.config import cfg
    from videocaptioner.ui.view.transcription_interface import TranscriptionInterface

    widget = TranscriptionInterface()
    widget.resize(1280, 820)
    widget.show()
    app.processEvents()

    model_names = _assert_unique_action_texts(widget.model_menu, "transcription model menu")
    selected_text = model_names[0]
    selected_model = next(
        model for model in TranscribeModelEnum if model.value == selected_text
    )
    widget.on_transcription_model_changed(selected_text)
    app.processEvents()
    assert cfg.transcribe_model.value == selected_model
    assert widget.model_button.text() == selected_text

    _grab(widget, output_dir, "transcription-model-state", app)
    screenshot_names.append("transcription-model-state")
    widget.close()


def _check_subtitle(output_dir: Path, app, screenshot_names: list[str]) -> None:
    from videocaptioner.core.entities import SubtitleLayoutEnum
    from videocaptioner.core.translate.types import TargetLanguage
    from videocaptioner.ui.common.config import cfg
    from videocaptioner.ui.view.subtitle_interface import SubtitleInterface

    widget = SubtitleInterface()
    widget.resize(1280, 820)
    widget.show()
    app.processEvents()

    layout_names = _assert_unique_action_texts(widget.layout_menu, "subtitle layout menu")
    language_names = _assert_unique_action_texts(
        widget.target_language_menu, "subtitle target language menu"
    )
    assert {layout.value for layout in SubtitleLayoutEnum}.issubset(set(layout_names))
    assert TargetLanguage.ENGLISH.value in language_names

    widget.on_subtitle_layout_changed(SubtitleLayoutEnum.ONLY_ORIGINAL.value)
    app.processEvents()
    assert cfg.subtitle_layout.value == SubtitleLayoutEnum.ONLY_ORIGINAL
    assert widget.layout_button.text() == SubtitleLayoutEnum.ONLY_ORIGINAL.value

    widget.on_subtitle_translation_changed(True)
    widget.on_target_language_changed(TargetLanguage.ENGLISH.value)
    app.processEvents()
    assert cfg.need_translate.value
    assert cfg.target_language.value == TargetLanguage.ENGLISH
    assert widget.target_language_button.isEnabled()
    assert widget.target_language_button.text() == TargetLanguage.ENGLISH.value

    widget.on_subtitle_translation_changed(False)
    app.processEvents()
    assert not cfg.need_translate.value
    assert not widget.target_language_button.isEnabled()

    _grab(widget, output_dir, "subtitle-menu-state", app)
    screenshot_names.append("subtitle-menu-state")
    widget.close()


def _check_subtitle_style(output_dir: Path, app, screenshot_names: list[str]) -> None:
    from videocaptioner.ui.view.subtitle_style_interface import SubtitleStyleInterface

    widget = SubtitleStyleInterface()
    widget.resize(1280, 820)
    widget.show()
    _settle_widget(widget, app)
    widget.previewTextCard.setCurrentText("短文本")
    _settle_widget(widget, app)
    assert not widget.previewOriginalTextCard.isVisible()
    assert not widget.previewTranslationTextCard.isVisible()
    assert "Elementary" in widget._preview_texts()[0]
    widget.previewTextCard.setCurrentText("自定义")
    _settle_widget(widget, app)
    assert widget.previewOriginalTextCard.isVisible()
    assert widget.previewTranslationTextCard.isVisible()
    widget.previewOriginalTextCard.lineEdit.setText("Custom original preview text")
    widget.previewTranslationTextCard.lineEdit.setText("自定义译文预览文本")
    _settle_widget(widget, app)
    assert widget._preview_texts() == ("Custom original preview text", "自定义译文预览文本")
    _grab(widget, output_dir, "subtitle-style-custom-preview", app)
    screenshot_names.append("subtitle-style-custom-preview")
    widget.resize(1440, 980)
    _settle_widget(widget, app)
    if abs(widget.previewCard.geometry().bottom() - widget.settingsScrollArea.geometry().bottom()) > 2:
        raise AssertionError(
            f"subtitle preview card bottom is not aligned: "
            f"preview={widget.previewCard.geometry().bottom()} "
            f"settings={widget.settingsScrollArea.geometry().bottom()}"
        )
    _grab(widget, output_dir, "subtitle-style-fullscreen-state", app)
    screenshot_names.append("subtitle-style-fullscreen-state")
    widget.close()


def _check_dubbing(output_dir: Path, app, screenshot_names: list[str]) -> None:
    from videocaptioner.ui.common.config import cfg
    from videocaptioner.ui.view.dubbing_interface import DubbingInterface

    reference_wav = output_dir / "reference.wav"
    _write_reference_wav(reference_wav)

    widget = DubbingInterface()
    widget.resize(1280, 820)
    widget.show()
    app.processEvents()
    for provider in ["edge", "gemini", "siliconflow"]:
        widget._on_provider_changed(provider)
        app.processEvents()
        assert cfg.dubbing_provider.value == provider
        assert len(widget.voiceTable.rows) > 0
        assert widget.previewPanel.cloneSection.isVisible() == (provider == "siliconflow")
        for key, card in widget.providerCards.items():
            assert card.badge.isVisible() == (key == provider)

    widget._on_gender_filter("女声")
    app.processEvents()
    assert widget.voiceTable.rows
    assert all("女声" in row.voice.tags for row in widget.voiceTable.rows)

    cfg.set(cfg.dubbing_clone_audio, str(reference_wav), save=False)
    widget.previewPanel.setAudioPath(str(reference_wav))
    widget.previewPanel.setCloneText("这是一段用于克隆测试的参考音频。")
    app.processEvents()
    assert widget.bodyPanel.height() >= widget.sidePanel.sizeHint().height()
    assert widget.previewPanel.playButton.isEnabled()
    assert widget.previewPanel.cloneTextInput.isVisible()
    _grab(widget, output_dir, "dubbing-clone-state", app)
    screenshot_names.append("dubbing-clone-state")

    widget._clear_clone_audio()
    app.processEvents()
    assert not cfg.dubbing_clone_audio.value
    assert not widget.previewPanel.playButton.isEnabled()
    widget.close()


def _check_video_synthesis(output_dir: Path, app, screenshot_names: list[str]) -> None:
    from videocaptioner.core.entities import SubtitleRenderModeEnum, VideoQualityEnum
    from videocaptioner.ui.common.config import cfg
    from videocaptioner.ui.view.video_synthesis_interface import VideoSynthesisInterface

    widget = VideoSynthesisInterface()
    widget.resize(1280, 820)
    widget.show()
    app.processEvents()
    assert not widget.progress_bar.isVisible()
    assert not widget.output_panel.pill.isVisible()
    assert not widget.files_panel.pill.isVisible()
    assert not widget.dubbing_card.pill.isVisible()
    assert not hasattr(widget, "voice_tag")

    subtitle_type_actions = _assert_unique_action_texts(
        widget.subtitle_type_menu, "video subtitle type menu"
    )
    assert subtitle_type_actions == ["硬字幕", "软字幕"]
    assert set(_assert_unique_action_texts(widget.render_mode_menu, "video render mode menu")) == {
        mode.value for mode in SubtitleRenderModeEnum
    }
    assert set(
        _assert_unique_action_texts(widget.video_quality_menu, "video quality menu")
    ) == {quality.value for quality in VideoQualityEnum}

    cfg.set(cfg.need_video, True)
    cfg.set(cfg.dubbing_enabled, True)
    widget.set_value()
    app.processEvents()
    assert not widget.synthesize_button.isEnabled()

    widget.on_output_subtitle_button_clicked(False)
    app.processEvents()
    assert not cfg.need_video.value
    assert cfg.dubbing_enabled.value
    assert not widget.output_subtitle_button.isChecked()
    assert widget.output_dubbing_button.isChecked()

    widget.on_output_subtitle_button_clicked(True)
    widget.on_use_style_action_triggered(True)
    app.processEvents()
    assert cfg.use_subtitle_style.value
    assert not cfg.soft_subtitle.value
    assert widget.render_mode_button.isEnabled()

    widget.on_soft_subtitle_action_triggered(True)
    app.processEvents()
    assert cfg.soft_subtitle.value
    assert not cfg.use_subtitle_style.value
    assert not widget.render_mode_button.isEnabled()
    widget.on_render_mode_changed(SubtitleRenderModeEnum.ASS_STYLE.value)
    app.processEvents()
    assert cfg.subtitle_render_mode.value == SubtitleRenderModeEnum.ASS_STYLE
    _grab(widget, output_dir, "video-soft-subtitle-state", app)
    screenshot_names.append("video-soft-subtitle-state")

    widget.on_video_synthesis_progress(42, "正在合成")
    app.processEvents()
    assert widget.progress_bar.isVisible()
    assert widget.status_label.isVisible()
    assert widget.status_label.text() == "正在合成"
    _grab(widget, output_dir, "video-progress-state", app)
    screenshot_names.append("video-progress-state")
    widget.close()


if __name__ == "__main__":
    raise SystemExit(main())
