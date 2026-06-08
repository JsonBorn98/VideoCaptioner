# coding:utf-8
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from PyQt5.QtCore import QLocale
from PyQt5.QtGui import QColor
from qfluentwidgets import Theme

from videocaptioner.config import WORK_PATH
from videocaptioner.core.application.app_config import (
    layout_from_cli,
    quality_from_cli,
    render_mode_from_cli,
    target_language_from_code,
    transcribe_model_from_cli,
    transcribe_output_format_from_cli,
    translator_from_cli,
)
from videocaptioner.core.application.config_store import (
    build_config,
    get,
    save_many,
)
from videocaptioner.core.dubbing import available_dubbing_presets
from videocaptioner.core.entities import (
    LANGUAGES,
    FasterWhisperModelEnum,
    LLMServiceEnum,
    SubtitleLayoutEnum,
    SubtitleRenderModeEnum,
    TranscribeLanguageEnum,
    TranscribeModelEnum,
    TranscribeOutputFormatEnum,
    TranslatorServiceEnum,
    VadMethodEnum,
    VideoQualityEnum,
    WhisperModelEnum,
)
from videocaptioner.core.translate.types import BING_LANG_MAP, TargetLanguage
from videocaptioner.core.utils.platform_utils import get_available_transcribe_models
from videocaptioner.ui.common.settings_state import (
    BoolValidator,
    ChoiceSettingField,
    ChoiceValidator,
    EnumSettingSerializer,
    FolderValidator,
    RangeSettingField,
    RangeValidator,
    SettingField,
    SettingSerializer,
    SettingsState,
)

DEFAULT_THEME_COLOR = "#ff28f08b"


class Language(Enum):
    """软件语言"""

    CHINESE_SIMPLIFIED = QLocale(QLocale.Chinese, QLocale.China)
    CHINESE_TRADITIONAL = QLocale(QLocale.Chinese, QLocale.HongKong)
    ENGLISH = QLocale(QLocale.English)
    AUTO = QLocale()


class LanguageSerializer(SettingSerializer):
    """Language serializer"""

    def serialize(self, language):
        return language.value.name() if language != Language.AUTO else "Auto"

    def deserialize(self, value: str):
        return Language(QLocale(value)) if value != "Auto" else Language.AUTO


class PlatformAwareTranscribeModelValidator(ChoiceValidator):
    """平台相关的转录模型验证器，在 macOS 上自动过滤掉 FasterWhisper"""

    def __init__(self):
        # 不调用父类的 __init__，因为我们要自定义 options
        self._options = get_available_transcribe_models()

    @property
    def options(self):
        return self._options

    def validate(self, value):
        return value in self._options

    def correct(self, value):
        return value if self.validate(value) else self._options[0]


class Config(SettingsState):
    """应用配置"""

    # ------------------- UI 外观配置 -------------------
    themeMode = ChoiceSettingField(
        "QFluentWidgets",
        "ThemeMode",
        Theme.DARK,
        ChoiceValidator(Theme),
        EnumSettingSerializer(Theme),
    )
    themeColor = SettingField("QFluentWidgets", "ThemeColor", QColor(DEFAULT_THEME_COLOR))

    # LLM配置
    llm_service = ChoiceSettingField(
        "LLM",
        "LLMService",
        LLMServiceEnum.OPENAI,
        ChoiceValidator(LLMServiceEnum),
        EnumSettingSerializer(LLMServiceEnum),
    )

    openai_model = SettingField("LLM", "OpenAI_Model", "gpt-4o-mini")
    openai_model_options = SettingField("LLM", "OpenAI_ModelOptions", [])
    openai_api_key = SettingField("LLM", "OpenAI_API_Key", "")
    openai_api_base = SettingField("LLM", "OpenAI_API_Base", "https://api.openai.com/v1")

    silicon_cloud_model = SettingField("LLM", "SiliconCloud_Model", "gpt-4o-mini")
    silicon_cloud_model_options = SettingField("LLM", "SiliconCloud_ModelOptions", [])
    silicon_cloud_api_key = SettingField("LLM", "SiliconCloud_API_Key", "")
    silicon_cloud_api_base = SettingField(
        "LLM", "SiliconCloud_API_Base", "https://api.siliconflow.cn/v1"
    )

    deepseek_model = SettingField("LLM", "DeepSeek_Model", "deepseek-chat")
    deepseek_model_options = SettingField("LLM", "DeepSeek_ModelOptions", [])
    deepseek_api_key = SettingField("LLM", "DeepSeek_API_Key", "")
    deepseek_api_base = SettingField("LLM", "DeepSeek_API_Base", "https://api.deepseek.com/v1")

    ollama_model = SettingField("LLM", "Ollama_Model", "llama2")
    ollama_model_options = SettingField("LLM", "Ollama_ModelOptions", [])
    ollama_api_key = SettingField("LLM", "Ollama_API_Key", "ollama")
    ollama_api_base = SettingField("LLM", "Ollama_API_Base", "http://localhost:11434/v1")

    lm_studio_model = SettingField("LLM", "LmStudio_Model", "qwen2.5:7b")
    lm_studio_model_options = SettingField("LLM", "LmStudio_ModelOptions", [])
    lm_studio_api_key = SettingField("LLM", "LmStudio_API_Key", "lmstudio")
    lm_studio_api_base = SettingField("LLM", "LmStudio_API_Base", "http://localhost:1234/v1")

    gemini_model = SettingField("LLM", "Gemini_Model", "gemini-pro")
    gemini_model_options = SettingField("LLM", "Gemini_ModelOptions", [])
    gemini_api_key = SettingField("LLM", "Gemini_API_Key", "")
    gemini_api_base = SettingField(
        "LLM",
        "Gemini_API_Base",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
    )

    chatglm_model = SettingField("LLM", "ChatGLM_Model", "glm-4")
    chatglm_model_options = SettingField("LLM", "ChatGLM_ModelOptions", [])
    chatglm_api_key = SettingField("LLM", "ChatGLM_API_Key", "")
    chatglm_api_base = SettingField("LLM", "ChatGLM_API_Base", "https://open.bigmodel.cn/api/paas/v4")

    # ------------------- 翻译配置 -------------------
    translator_service = ChoiceSettingField(
        "Translate",
        "TranslatorServiceEnum",
        TranslatorServiceEnum.BING,
        ChoiceValidator(TranslatorServiceEnum),
        EnumSettingSerializer(TranslatorServiceEnum),
    )
    need_reflect_translate = SettingField("Translate", "NeedReflectTranslate", False, BoolValidator())
    deeplx_endpoint = SettingField("Translate", "DeeplxEndpoint", "")
    batch_size = RangeSettingField("Translate", "BatchSize", 10, RangeValidator(5, 50))
    thread_num = RangeSettingField("Translate", "ThreadNum", 10, RangeValidator(1, 50))

    # ------------------- 转录配置 -------------------
    transcribe_model = ChoiceSettingField(
        "Transcribe",
        "TranscribeModel",
        TranscribeModelEnum.BIJIAN,
        PlatformAwareTranscribeModelValidator(),
        EnumSettingSerializer(TranscribeModelEnum),
    )
    transcribe_output_format = ChoiceSettingField(
        "Transcribe",
        "OutputFormat",
        TranscribeOutputFormatEnum.SRT,
        ChoiceValidator(TranscribeOutputFormatEnum),
        EnumSettingSerializer(TranscribeOutputFormatEnum),
    )
    transcribe_language = ChoiceSettingField(
        "Transcribe",
        "TranscribeLanguage",
        TranscribeLanguageEnum.AUTO,
        ChoiceValidator(TranscribeLanguageEnum),
        EnumSettingSerializer(TranscribeLanguageEnum),
    )

    # ------------------- Whisper Cpp 配置 -------------------
    whisper_model = ChoiceSettingField(
        "Whisper",
        "WhisperModel",
        WhisperModelEnum.TINY,
        ChoiceValidator(WhisperModelEnum),
        EnumSettingSerializer(WhisperModelEnum),
    )

    # ------------------- Faster Whisper 配置 -------------------
    faster_whisper_program = SettingField(
        "FasterWhisper",
        "Program",
        "faster-whisper-xxl.exe",
    )
    faster_whisper_model = ChoiceSettingField(
        "FasterWhisper",
        "Model",
        FasterWhisperModelEnum.TINY,
        ChoiceValidator(FasterWhisperModelEnum),
        EnumSettingSerializer(FasterWhisperModelEnum),
    )
    faster_whisper_model_dir = SettingField("FasterWhisper", "ModelDir", "")
    faster_whisper_device = ChoiceSettingField(
        "FasterWhisper", "Device", "auto", ChoiceValidator(["auto", "cuda", "cpu"])
    )
    # VAD 参数
    faster_whisper_vad_filter = SettingField("FasterWhisper", "VadFilter", True, BoolValidator())
    faster_whisper_vad_threshold = RangeSettingField(
        "FasterWhisper", "VadThreshold", 0.4, RangeValidator(0, 1)
    )
    faster_whisper_vad_method = ChoiceSettingField(
        "FasterWhisper",
        "VadMethod",
        VadMethodEnum.SILERO_V4,
        ChoiceValidator(VadMethodEnum),
        EnumSettingSerializer(VadMethodEnum),
    )
    # 人声提取
    faster_whisper_ff_mdx_kim2 = SettingField("FasterWhisper", "FfMdxKim2", False, BoolValidator())
    # 文本处理参数
    faster_whisper_one_word = SettingField("FasterWhisper", "OneWord", True, BoolValidator())
    # 提示词
    faster_whisper_prompt = SettingField("FasterWhisper", "Prompt", "")

    # ------------------- Whisper API 配置 -------------------
    whisper_api_base = SettingField("WhisperAPI", "WhisperApiBase", "")
    whisper_api_key = SettingField("WhisperAPI", "WhisperApiKey", "")
    whisper_api_model = ChoiceSettingField("WhisperAPI", "WhisperApiModel", "")
    whisper_api_prompt = SettingField("WhisperAPI", "WhisperApiPrompt", "")

    # ------------------- 百炼 Fun-ASR 配置 -------------------
    fun_asr_api_base = SettingField("FunASR", "FunAsrApiBase", "https://dashscope.aliyuncs.com")
    fun_asr_api_key = SettingField("FunASR", "FunAsrApiKey", "")
    fun_asr_model = ChoiceSettingField("FunASR", "FunAsrModel", "fun-asr")

    # ------------------- 字幕配置 -------------------
    need_optimize = SettingField("Subtitle", "NeedOptimize", False, BoolValidator())
    need_translate = SettingField("Subtitle", "NeedTranslate", False, BoolValidator())
    need_split = SettingField("Subtitle", "NeedSplit", False, BoolValidator())
    target_language = ChoiceSettingField(
        "Subtitle",
        "TargetLanguage",
        TargetLanguage.SIMPLIFIED_CHINESE,
        ChoiceValidator(TargetLanguage),
        EnumSettingSerializer(TargetLanguage),
    )
    max_word_count_cjk = SettingField("Subtitle", "MaxWordCountCJK", 28, RangeValidator(8, 100))
    max_word_count_english = SettingField(
        "Subtitle", "MaxWordCountEnglish", 20, RangeValidator(8, 100)
    )
    custom_prompt_text = SettingField("Subtitle", "CustomPromptText", "")

    # ------------------- 字幕合成配置 -------------------
    soft_subtitle = SettingField("Video", "SoftSubtitle", False, BoolValidator())
    need_video = SettingField("Video", "NeedVideo", True, BoolValidator())
    video_quality = ChoiceSettingField(
        "Video",
        "VideoQuality",
        VideoQualityEnum.MEDIUM,
        ChoiceValidator(VideoQualityEnum),
        EnumSettingSerializer(VideoQualityEnum),
    )
    use_subtitle_style = SettingField("Video", "UseSubtitleStyle", False, BoolValidator())

    # ------------------- 配音配置 -------------------
    dubbing_enabled = SettingField("Dubbing", "Enabled", False, BoolValidator())
    dubbing_provider = ChoiceSettingField(
        "Dubbing",
        "Provider",
        "edge",
        ChoiceValidator(["edge", "gemini", "siliconflow"]),
    )
    dubbing_preset = ChoiceSettingField(
        "Dubbing",
        "Preset",
        "edge-cn-female",
        ChoiceValidator(available_dubbing_presets()),
    )
    dubbing_voice = SettingField("Dubbing", "Voice", "zh-CN-XiaoxiaoNeural")
    dubbing_text_track = ChoiceSettingField(
        "Dubbing",
        "TextTrack",
        "auto",
        ChoiceValidator(["auto", "first", "second"]),
    )
    dubbing_timing = ChoiceSettingField(
        "Dubbing",
        "Timing",
        "balanced",
        ChoiceValidator(["natural", "balanced", "strict"]),
    )
    dubbing_audio_mode = ChoiceSettingField(
        "Dubbing",
        "AudioMode",
        "replace",
        ChoiceValidator(["replace", "mix", "duck"]),
    )
    dubbing_api_key = SettingField("Dubbing", "ApiKey", "")
    dubbing_api_base = SettingField("Dubbing", "ApiBase", "")
    dubbing_model = SettingField("Dubbing", "Model", "")
    dubbing_tts_workers = RangeSettingField("Dubbing", "Workers", 5, RangeValidator(1, 20))
    dubbing_clone_audio = SettingField("Dubbing", "CloneAudio", "")
    dubbing_clone_text = SettingField("Dubbing", "CloneText", "")

    # ------------------- 字幕样式配置 -------------------
    subtitle_style_name = SettingField("SubtitleStyle", "StyleName", "default")
    subtitle_layout = ChoiceSettingField(
        "SubtitleStyle",
        "Layout",
        SubtitleLayoutEnum.TRANSLATE_ON_TOP,
        ChoiceValidator(SubtitleLayoutEnum),
        EnumSettingSerializer(SubtitleLayoutEnum),
    )
    subtitle_preview_image = SettingField("SubtitleStyle", "PreviewImage", "")

    # 字幕渲染模式
    subtitle_render_mode = ChoiceSettingField(
        "SubtitleStyle",
        "RenderMode",
        SubtitleRenderModeEnum.ROUNDED_BG,
        ChoiceValidator(SubtitleRenderModeEnum),
        EnumSettingSerializer(SubtitleRenderModeEnum),
    )

    # 圆角背景模式配置
    rounded_bg_font_name = SettingField("RoundedBgStyle", "FontName", "Noto Sans SC")
    rounded_bg_font_size = RangeSettingField(
        "RoundedBgStyle", "FontSize", 52, RangeValidator(16, 120)
    )
    # 背景色：深灰半透明 (R=25, G=25, B=25, A=200)
    rounded_bg_color = SettingField("RoundedBgStyle", "BgColor", "#191919C8")
    rounded_bg_text_color = SettingField("RoundedBgStyle", "TextColor", "#FFFFFF")
    rounded_bg_corner_radius = RangeSettingField(
        "RoundedBgStyle", "CornerRadius", 12, RangeValidator(0, 50)
    )
    rounded_bg_padding_h = RangeSettingField("RoundedBgStyle", "PaddingH", 28, RangeValidator(4, 100))
    rounded_bg_padding_v = RangeSettingField("RoundedBgStyle", "PaddingV", 14, RangeValidator(4, 50))
    rounded_bg_margin_bottom = RangeSettingField(
        "RoundedBgStyle", "MarginBottom", 60, RangeValidator(20, 300)
    )
    rounded_bg_line_spacing = RangeSettingField(
        "RoundedBgStyle", "LineSpacing", 10, RangeValidator(0, 50)
    )
    rounded_bg_letter_spacing = RangeSettingField(
        "RoundedBgStyle", "LetterSpacing", 0, RangeValidator(0, 20)
    )

    # ------------------- 保存配置 -------------------
    work_dir = SettingField("Save", "Work_Dir", WORK_PATH, FolderValidator())

    # ------------------- 软件页面配置 -------------------
    micaEnabled = SettingField("MainWindow", "MicaEnabled", False, BoolValidator())
    dpiScale = ChoiceSettingField(
        "MainWindow",
        "DpiScale",
        "Auto",
        ChoiceValidator([1, 1.25, 1.5, 1.75, 2, "Auto"]),
        restart=True,
    )
    language = ChoiceSettingField(
        "MainWindow",
        "Language",
        Language.AUTO,
        ChoiceValidator(Language),
        LanguageSerializer(),
        restart=True,
    )

    # ------------------- 更新配置 -------------------
    checkUpdateAtStartUp = SettingField("Update", "CheckUpdateAtStartUp", True, BoolValidator())

    # ------------------- 缓存配置 -------------------
    cache_enabled = SettingField("Cache", "CacheEnabled", True, BoolValidator())


@dataclass(frozen=True)
class SharedConfigBinding:
    item: SettingField
    key: str
    to_toml: Callable[[Any], Any] = lambda value: value
    from_toml: Callable[[Any], Any] = lambda value: value


LLM_SERVICE_KEYS = {
    LLMServiceEnum.OPENAI: "openai",
    LLMServiceEnum.SILICON_CLOUD: "silicon_cloud",
    LLMServiceEnum.DEEPSEEK: "deepseek",
    LLMServiceEnum.OLLAMA: "ollama",
    LLMServiceEnum.LM_STUDIO: "lm_studio",
    LLMServiceEnum.GEMINI: "gemini",
    LLMServiceEnum.CHATGLM: "chatglm",
}
KEY_TO_LLM_SERVICE = {value: key for key, value in LLM_SERVICE_KEYS.items()}

TRANSCRIBE_MODEL_KEYS = {
    TranscribeModelEnum.BIJIAN: "bijian",
    TranscribeModelEnum.JIANYING: "jianying",
    TranscribeModelEnum.BAILIAN_FUN_ASR: "fun-asr",
    TranscribeModelEnum.WHISPER_API: "whisper-api",
    TranscribeModelEnum.FASTER_WHISPER: "faster-whisper",
    TranscribeModelEnum.WHISPER_CPP: "whisper-cpp",
}

TRANSLATOR_KEYS = {
    TranslatorServiceEnum.OPENAI: "llm",
    TranslatorServiceEnum.BING: "bing",
    TranslatorServiceEnum.GOOGLE: "google",
    TranslatorServiceEnum.DEEPLX: "deeplx",
}

SUBTITLE_LAYOUT_KEYS = {
    SubtitleLayoutEnum.TRANSLATE_ON_TOP: "target-above",
    SubtitleLayoutEnum.ORIGINAL_ON_TOP: "source-above",
    SubtitleLayoutEnum.ONLY_TRANSLATE: "target-only",
    SubtitleLayoutEnum.ONLY_ORIGINAL: "source-only",
}

RENDER_MODE_KEYS = {
    SubtitleRenderModeEnum.ASS_STYLE: "ass",
    SubtitleRenderModeEnum.ROUNDED_BG: "rounded",
}

VIDEO_QUALITY_KEYS = {
    VideoQualityEnum.ULTRA_HIGH: "ultra",
    VideoQualityEnum.HIGH: "high",
    VideoQualityEnum.MEDIUM: "medium",
    VideoQualityEnum.LOW: "low",
}


def _llm_service_to_key(value: Any) -> str:
    return LLM_SERVICE_KEYS.get(value, "openai")


def _llm_service_from_key(value: Any) -> LLMServiceEnum:
    return KEY_TO_LLM_SERVICE.get(str(value or "openai").lower(), LLMServiceEnum.OPENAI)


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _transcribe_model_to_key(value: Any) -> str:
    return TRANSCRIBE_MODEL_KEYS.get(value, "bijian")


def _transcribe_language_to_key(value: Any) -> str:
    label = _enum_value(value)
    return LANGUAGES.get(label, "") or "auto"


def _transcribe_language_from_key(value: Any) -> TranscribeLanguageEnum:
    raw = str(value or "auto").lower()
    if raw in {"", "auto"}:
        return TranscribeLanguageEnum.AUTO

    for language in TranscribeLanguageEnum:
        if LANGUAGES.get(language.value, "").lower() == raw:
            return language
        if language.value.lower() == raw or language.name.lower() == raw:
            return language
    return TranscribeLanguageEnum.AUTO


def _output_format_to_key(value: Any) -> str:
    return str(_enum_value(value) or "srt").lower()


def _target_language_to_key(value: Any) -> str:
    return BING_LANG_MAP.get(value, "zh-Hans")


def _translator_to_key(value: Any) -> str:
    return TRANSLATOR_KEYS.get(value, "bing")


def _subtitle_layout_to_key(value: Any) -> str:
    return SUBTITLE_LAYOUT_KEYS.get(value, "target-above")


def _render_mode_to_key(value: Any) -> str:
    return RENDER_MODE_KEYS.get(value, "rounded")


def _video_quality_to_key(value: Any) -> str:
    return VIDEO_QUALITY_KEYS.get(value, "medium")


def _theme_from_toml(value: Any) -> Theme:
    try:
        return Theme(str(value or "Dark"))
    except ValueError:
        return Theme.DARK


def _theme_to_toml(value: Any) -> str:
    return _enum_value(value) or "Dark"


def _theme_color_from_toml(value: Any) -> QColor:
    color = QColor(str(value or "#ff28f08b"))
    return color if color.isValid() else QColor("#ff28f08b")


def _theme_color_to_toml(value: Any) -> str:
    if isinstance(value, QColor):
        return value.name(QColor.HexRgb)
    color = QColor(str(value))
    return color.name(QColor.HexRgb) if color.isValid() else "#28f08b"


def _language_from_toml(value: Any) -> Language:
    try:
        return LanguageSerializer().deserialize(str(value or "Auto"))
    except Exception:
        return Language.AUTO


def _language_to_toml(value: Any) -> str:
    return LanguageSerializer().serialize(value)


def _model_options_from_toml(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return _clean_model_options(value)


def _model_options_to_toml(value: Any) -> list[str]:
    return _clean_model_options(value)


def _clean_model_options(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    options: list[str] = []
    seen: set[str] = set()
    for item in value:
        model = str(item or "").strip()
        if not model or model in seen:
            continue
        seen.add(model)
        options.append(model)
    return options


def _bindings() -> list[SharedConfigBinding]:
    return [
        SharedConfigBinding(cfg.themeMode, "ui.theme_mode", _theme_to_toml, _theme_from_toml),
        SharedConfigBinding(
            cfg.themeColor, "ui.theme_color", _theme_color_to_toml, _theme_color_from_toml
        ),
        SharedConfigBinding(cfg.dpiScale, "ui.dpi_scale"),
        SharedConfigBinding(cfg.language, "ui.language", _language_to_toml, _language_from_toml),
        SharedConfigBinding(cfg.micaEnabled, "ui.mica_enabled"),
        SharedConfigBinding(cfg.checkUpdateAtStartUp, "ui.check_update_at_startup"),
        SharedConfigBinding(cfg.subtitle_preview_image, "ui.subtitle_preview_image"),
        SharedConfigBinding(cfg.work_dir, "app.work_dir"),
        SharedConfigBinding(cfg.cache_enabled, "app.cache_enabled"),
        SharedConfigBinding(
            cfg.llm_service, "llm.service", _llm_service_to_key, _llm_service_from_key
        ),
        SharedConfigBinding(cfg.openai_api_key, "llm.providers.openai.api_key"),
        SharedConfigBinding(cfg.openai_api_base, "llm.providers.openai.api_base"),
        SharedConfigBinding(cfg.openai_model, "llm.providers.openai.model"),
        SharedConfigBinding(
            cfg.openai_model_options,
            "llm.providers.openai.model_options",
            _model_options_to_toml,
            _model_options_from_toml,
        ),
        SharedConfigBinding(cfg.silicon_cloud_api_key, "llm.providers.silicon_cloud.api_key"),
        SharedConfigBinding(cfg.silicon_cloud_api_base, "llm.providers.silicon_cloud.api_base"),
        SharedConfigBinding(cfg.silicon_cloud_model, "llm.providers.silicon_cloud.model"),
        SharedConfigBinding(
            cfg.silicon_cloud_model_options,
            "llm.providers.silicon_cloud.model_options",
            _model_options_to_toml,
            _model_options_from_toml,
        ),
        SharedConfigBinding(cfg.deepseek_api_key, "llm.providers.deepseek.api_key"),
        SharedConfigBinding(cfg.deepseek_api_base, "llm.providers.deepseek.api_base"),
        SharedConfigBinding(cfg.deepseek_model, "llm.providers.deepseek.model"),
        SharedConfigBinding(
            cfg.deepseek_model_options,
            "llm.providers.deepseek.model_options",
            _model_options_to_toml,
            _model_options_from_toml,
        ),
        SharedConfigBinding(cfg.ollama_api_key, "llm.providers.ollama.api_key"),
        SharedConfigBinding(cfg.ollama_api_base, "llm.providers.ollama.api_base"),
        SharedConfigBinding(cfg.ollama_model, "llm.providers.ollama.model"),
        SharedConfigBinding(
            cfg.ollama_model_options,
            "llm.providers.ollama.model_options",
            _model_options_to_toml,
            _model_options_from_toml,
        ),
        SharedConfigBinding(cfg.lm_studio_api_key, "llm.providers.lm_studio.api_key"),
        SharedConfigBinding(cfg.lm_studio_api_base, "llm.providers.lm_studio.api_base"),
        SharedConfigBinding(cfg.lm_studio_model, "llm.providers.lm_studio.model"),
        SharedConfigBinding(
            cfg.lm_studio_model_options,
            "llm.providers.lm_studio.model_options",
            _model_options_to_toml,
            _model_options_from_toml,
        ),
        SharedConfigBinding(cfg.gemini_api_key, "llm.providers.gemini.api_key"),
        SharedConfigBinding(cfg.gemini_api_base, "llm.providers.gemini.api_base"),
        SharedConfigBinding(cfg.gemini_model, "llm.providers.gemini.model"),
        SharedConfigBinding(
            cfg.gemini_model_options,
            "llm.providers.gemini.model_options",
            _model_options_to_toml,
            _model_options_from_toml,
        ),
        SharedConfigBinding(cfg.chatglm_api_key, "llm.providers.chatglm.api_key"),
        SharedConfigBinding(cfg.chatglm_api_base, "llm.providers.chatglm.api_base"),
        SharedConfigBinding(cfg.chatglm_model, "llm.providers.chatglm.model"),
        SharedConfigBinding(
            cfg.chatglm_model_options,
            "llm.providers.chatglm.model_options",
            _model_options_to_toml,
            _model_options_from_toml,
        ),
        SharedConfigBinding(
            cfg.transcribe_model,
            "transcribe.asr",
            _transcribe_model_to_key,
            transcribe_model_from_cli,
        ),
        SharedConfigBinding(
            cfg.transcribe_output_format,
            "transcribe.output_format",
            _output_format_to_key,
            transcribe_output_format_from_cli,
        ),
        SharedConfigBinding(
            cfg.transcribe_language,
            "transcribe.language",
            _transcribe_language_to_key,
            _transcribe_language_from_key,
        ),
        SharedConfigBinding(
            cfg.whisper_model,
            "transcribe.whisper_cpp.model",
            _enum_value,
            lambda value: WhisperModelEnum(str(value)),
        ),
        SharedConfigBinding(cfg.whisper_api_key, "whisper_api.api_key"),
        SharedConfigBinding(cfg.whisper_api_base, "whisper_api.api_base"),
        SharedConfigBinding(cfg.whisper_api_model, "whisper_api.model"),
        SharedConfigBinding(cfg.whisper_api_prompt, "whisper_api.prompt"),
        SharedConfigBinding(cfg.fun_asr_api_key, "fun_asr.api_key"),
        SharedConfigBinding(cfg.fun_asr_api_base, "fun_asr.api_base"),
        SharedConfigBinding(cfg.fun_asr_model, "fun_asr.model"),
        SharedConfigBinding(cfg.faster_whisper_program, "transcribe.faster_whisper.program"),
        SharedConfigBinding(
            cfg.faster_whisper_model,
            "transcribe.faster_whisper.model",
            _enum_value,
            lambda value: FasterWhisperModelEnum(str(value)),
        ),
        SharedConfigBinding(cfg.faster_whisper_model_dir, "transcribe.faster_whisper.model_dir"),
        SharedConfigBinding(cfg.faster_whisper_device, "transcribe.faster_whisper.device"),
        SharedConfigBinding(cfg.faster_whisper_vad_filter, "transcribe.faster_whisper.vad_filter"),
        SharedConfigBinding(
            cfg.faster_whisper_vad_threshold, "transcribe.faster_whisper.vad_threshold"
        ),
        SharedConfigBinding(
            cfg.faster_whisper_vad_method,
            "transcribe.faster_whisper.vad_method",
            _enum_value,
            lambda value: VadMethodEnum(str(value).replace("-", "_")),
        ),
        SharedConfigBinding(
            cfg.faster_whisper_ff_mdx_kim2, "transcribe.faster_whisper.voice_extraction"
        ),
        SharedConfigBinding(cfg.faster_whisper_one_word, "transcribe.faster_whisper.one_word"),
        SharedConfigBinding(cfg.faster_whisper_prompt, "transcribe.faster_whisper.prompt"),
        SharedConfigBinding(
            cfg.translator_service, "translate.service", _translator_to_key, translator_from_cli
        ),
        SharedConfigBinding(cfg.need_reflect_translate, "translate.reflect"),
        SharedConfigBinding(cfg.deeplx_endpoint, "translate.deeplx_endpoint"),
        SharedConfigBinding(cfg.batch_size, "subtitle.batch_size"),
        SharedConfigBinding(cfg.thread_num, "subtitle.thread_num"),
        SharedConfigBinding(cfg.need_optimize, "subtitle.optimize"),
        SharedConfigBinding(cfg.need_translate, "subtitle.translate"),
        SharedConfigBinding(cfg.need_split, "subtitle.split"),
        SharedConfigBinding(
            cfg.target_language,
            "translate.target_language",
            _target_language_to_key,
            target_language_from_code,
        ),
        SharedConfigBinding(cfg.max_word_count_cjk, "subtitle.max_word_count_cjk"),
        SharedConfigBinding(cfg.max_word_count_english, "subtitle.max_word_count_english"),
        SharedConfigBinding(cfg.custom_prompt_text, "subtitle.custom_prompt"),
        SharedConfigBinding(cfg.soft_subtitle, "synthesize.soft_subtitle"),
        SharedConfigBinding(cfg.need_video, "synthesize.need_video"),
        SharedConfigBinding(
            cfg.video_quality, "synthesize.quality", _video_quality_to_key, quality_from_cli
        ),
        SharedConfigBinding(cfg.use_subtitle_style, "synthesize.use_subtitle_style"),
        SharedConfigBinding(cfg.subtitle_style_name, "synthesize.style"),
        SharedConfigBinding(
            cfg.subtitle_layout, "synthesize.layout", _subtitle_layout_to_key, layout_from_cli
        ),
        SharedConfigBinding(
            cfg.subtitle_render_mode,
            "synthesize.render_mode",
            _render_mode_to_key,
            render_mode_from_cli,
        ),
        SharedConfigBinding(cfg.rounded_bg_font_name, "synthesize.rounded.font_name"),
        SharedConfigBinding(cfg.rounded_bg_font_size, "synthesize.rounded.font_size"),
        SharedConfigBinding(cfg.rounded_bg_color, "synthesize.rounded.bg_color"),
        SharedConfigBinding(cfg.rounded_bg_text_color, "synthesize.rounded.text_color"),
        SharedConfigBinding(cfg.rounded_bg_corner_radius, "synthesize.rounded.corner_radius"),
        SharedConfigBinding(cfg.rounded_bg_padding_h, "synthesize.rounded.padding_h"),
        SharedConfigBinding(cfg.rounded_bg_padding_v, "synthesize.rounded.padding_v"),
        SharedConfigBinding(cfg.rounded_bg_margin_bottom, "synthesize.rounded.margin_bottom"),
        SharedConfigBinding(cfg.rounded_bg_line_spacing, "synthesize.rounded.line_spacing"),
        SharedConfigBinding(cfg.rounded_bg_letter_spacing, "synthesize.rounded.letter_spacing"),
        SharedConfigBinding(cfg.dubbing_enabled, "dubbing.enabled"),
        SharedConfigBinding(cfg.dubbing_provider, "dubbing.provider"),
        SharedConfigBinding(cfg.dubbing_preset, "dubbing.preset"),
        SharedConfigBinding(cfg.dubbing_voice, "dubbing.voice"),
        SharedConfigBinding(cfg.dubbing_text_track, "dubbing.text_track"),
        SharedConfigBinding(cfg.dubbing_timing, "dubbing.timing"),
        SharedConfigBinding(cfg.dubbing_audio_mode, "dubbing.audio_mode"),
        SharedConfigBinding(cfg.dubbing_api_key, "dubbing.api_key"),
        SharedConfigBinding(cfg.dubbing_api_base, "dubbing.api_base"),
        SharedConfigBinding(cfg.dubbing_model, "dubbing.model"),
        SharedConfigBinding(cfg.dubbing_tts_workers, "dubbing.tts_workers"),
    ]


_syncing_shared_config = False


def _load_shared_config_to_state() -> None:
    global _syncing_shared_config
    shared_config = build_config()
    _syncing_shared_config = True
    try:
        active_provider = str(get(shared_config, "llm.service", "openai") or "openai")
        generic_api_key = get(shared_config, "llm.api_key", "")
        for binding in _bindings():
            raw_value = get(shared_config, binding.key, None)
            if (
                raw_value in (None, "")
                and binding.key == f"llm.providers.{active_provider}.api_key"
            ):
                raw_value = generic_api_key
            if raw_value is None:
                continue
            try:
                binding.item.value = binding.from_toml(raw_value)
            except Exception:
                continue
    finally:
        _syncing_shared_config = False


def _collect_shared_config_values() -> dict[str, Any]:
    values = {binding.key: binding.to_toml(binding.item.value) for binding in _bindings()}
    provider_key = _llm_service_to_key(cfg.llm_service.value)
    values["llm.api_key"] = values.get(f"llm.providers.{provider_key}.api_key", "")
    values["llm.api_base"] = values.get(f"llm.providers.{provider_key}.api_base", "")
    values["llm.model"] = values.get(f"llm.providers.{provider_key}.model", "")
    values["dubbing.use_cache"] = values["app.cache_enabled"]
    values["output.format"] = values["transcribe.output_format"]
    values["synthesize.subtitle_mode"] = "soft" if bool(cfg.soft_subtitle.value) else "hard"
    return values


def _save_shared_config() -> None:
    if _syncing_shared_config:
        return
    try:
        save_many(_collect_shared_config_values())
    except OSError:
        return


def _install_shared_config_sync() -> None:
    for binding in _bindings():
        binding.item.valueChanged.connect(lambda _value, _binding=binding: _save_shared_config())


cfg = Config()
cfg.themeMode.value = Theme.DARK
cfg.themeColor.value = QColor(DEFAULT_THEME_COLOR)
_load_shared_config_to_state()
_install_shared_config_sync()
cfg.save = _save_shared_config
