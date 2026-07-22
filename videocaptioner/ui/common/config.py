# coding:utf-8
from enum import Enum

from PyQt5.QtCore import QLocale
from PyQt5.QtGui import QColor
from qfluentwidgets import (
    BoolValidator,
    ConfigItem,
    ConfigSerializer,
    ConfigValidator,
    EnumSerializer,
    FolderValidator,
    OptionsConfigItem,
    OptionsValidator,
    QConfig,
    RangeConfigItem,
    RangeValidator,
    Theme,
    qconfig,
)

from videocaptioner.config import MODEL_PATH, SETTINGS_PATH, WORK_PATH
from videocaptioner.core.entities import (
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
from videocaptioner.core.postprocess.config import PostprocessConfig
from videocaptioner.core.speed import available_speed_presets, get_speed_policy
from videocaptioner.core.translate.enhanced.defaults import (
    DEFAULT_MAIN_TRANSLATION_PROMPT,
    DEFAULT_REVIEW_TRANSLATION_PROMPT,
)
from videocaptioner.core.translate.enhanced.models import (
    TermConfirmationMode,
    TranslationAuditMode,
)
from videocaptioner.core.translate.types import TargetLanguage, TranslationMode
from videocaptioner.core.utils.platform_utils import get_available_transcribe_models
from videocaptioner.ui.common.translation_migration import migrate_legacy_translation_settings

_BALANCED_SPEED_POLICY = get_speed_policy()
_POSTPROCESS_DEFAULTS = PostprocessConfig()


class Language(Enum):
    """软件语言"""

    CHINESE_SIMPLIFIED = QLocale(QLocale.Chinese, QLocale.China)
    CHINESE_TRADITIONAL = QLocale(QLocale.Chinese, QLocale.HongKong)
    ENGLISH = QLocale(QLocale.English)
    AUTO = QLocale()


class LanguageSerializer(ConfigSerializer):
    """Language serializer"""

    def serialize(self, language):
        return language.value.name() if language != Language.AUTO else "Auto"

    def deserialize(self, value: str):
        return Language(QLocale(value)) if value != "Auto" else Language.AUTO


class PlatformAwareTranscribeModelValidator(OptionsValidator):
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


class SpeedProfileValidator(ConfigValidator):
    """Keep persisted custom IDs valid while exposing dynamic combo-box options."""

    def __init__(self):
        self.options = list(available_speed_presets())

    def validate(self, value):
        return isinstance(value, str) and bool(value) and len(value) <= 64

    def correct(self, value):
        return value if self.validate(value) else "balanced"

    def set_options(self, options):
        self.options = list(dict.fromkeys(options))


class Config(QConfig):
    """应用配置"""

    # LLM配置
    llm_service = OptionsConfigItem(
        "LLM",
        "LLMService",
        LLMServiceEnum.OPENAI,
        OptionsValidator(LLMServiceEnum),
        EnumSerializer(LLMServiceEnum),
    )

    openai_model = ConfigItem("LLM", "OpenAI_Model", "gpt-4o-mini")
    openai_api_key = ConfigItem("LLM", "OpenAI_API_Key", "")
    openai_api_base = ConfigItem("LLM", "OpenAI_API_Base", "https://api.openai.com/v1")

    silicon_cloud_model = ConfigItem("LLM", "SiliconCloud_Model", "gpt-4o-mini")
    silicon_cloud_api_key = ConfigItem("LLM", "SiliconCloud_API_Key", "")
    silicon_cloud_api_base = ConfigItem(
        "LLM", "SiliconCloud_API_Base", "https://api.siliconflow.cn/v1"
    )

    deepseek_model = ConfigItem("LLM", "DeepSeek_Model", "deepseek-chat")
    deepseek_api_key = ConfigItem("LLM", "DeepSeek_API_Key", "")
    deepseek_api_base = ConfigItem(
        "LLM", "DeepSeek_API_Base", "https://api.deepseek.com/v1"
    )

    ollama_model = ConfigItem("LLM", "Ollama_Model", "llama2")
    ollama_api_key = ConfigItem("LLM", "Ollama_API_Key", "ollama")
    ollama_api_base = ConfigItem("LLM", "Ollama_API_Base", "http://localhost:11434/v1")

    lm_studio_model = ConfigItem("LLM", "LmStudio_Model", "qwen2.5:7b")
    lm_studio_api_key = ConfigItem("LLM", "LmStudio_API_Key", "lmstudio")
    lm_studio_api_base = ConfigItem(
        "LLM", "LmStudio_API_Base", "http://localhost:1234/v1"
    )

    gemini_model = ConfigItem("LLM", "Gemini_Model", "gemini-pro")
    gemini_api_key = ConfigItem("LLM", "Gemini_API_Key", "")
    gemini_api_base = ConfigItem(
        "LLM",
        "Gemini_API_Base",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
    )

    chatglm_model = ConfigItem("LLM", "ChatGLM_Model", "glm-4")
    chatglm_api_key = ConfigItem("LLM", "ChatGLM_API_Key", "")
    chatglm_api_base = ConfigItem(
        "LLM", "ChatGLM_API_Base", "https://open.bigmodel.cn/api/paas/v4"
    )

    # ------------------- 翻译配置 -------------------
    translator_service = OptionsConfigItem(
        "Translate",
        "TranslatorServiceEnum",
        TranslatorServiceEnum.BING,
        OptionsValidator(TranslatorServiceEnum),
        EnumSerializer(TranslatorServiceEnum),
    )
    translation_mode = OptionsConfigItem(
        "Translate",
        "TranslationMode",
        TranslationMode.ENHANCED_LLM,
        OptionsValidator(TranslationMode),
        EnumSerializer(TranslationMode),
    )
    main_llm_profile_id = ConfigItem("Translate", "MainLLMProfileId", "")
    review_llm_profile_id = ConfigItem("Translate", "ReviewLLMProfileId", "")
    main_translation_prompt = ConfigItem(
        "Translate", "MainTranslationPrompt", DEFAULT_MAIN_TRANSLATION_PROMPT
    )
    review_translation_prompt = ConfigItem(
        "Translate", "ReviewTranslationPrompt", DEFAULT_REVIEW_TRANSLATION_PROMPT
    )
    translation_migration_version = ConfigItem(
        "Translate", "TranslationMigrationVersion", 0
    )
    need_reflect_translate = ConfigItem(
        "Translate", "NeedReflectTranslate", False, BoolValidator()
    )
    deeplx_endpoint = ConfigItem("Translate", "DeeplxEndpoint", "")
    batch_size = RangeConfigItem("Translate", "BatchSize", 10, RangeValidator(5, 50))
    enhanced_batch_size = RangeConfigItem(
        "Translate", "EnhancedBatchSize", 10, RangeValidator(1, 50)
    )
    term_context_radius = RangeConfigItem(
        "Translate", "TermContextRadius", 10, RangeValidator(0, 50)
    )
    term_confirmation_mode = OptionsConfigItem(
        "Translate",
        "TermConfirmationMode",
        TermConfirmationMode.AUTOMATIC,
        OptionsValidator(TermConfirmationMode),
        EnumSerializer(TermConfirmationMode),
    )
    translation_audit_mode = OptionsConfigItem(
        "Translate",
        "TranslationAuditMode",
        TranslationAuditMode.AUTO_APPLY_REVIEW,
        OptionsValidator(TranslationAuditMode),
        EnumSerializer(TranslationAuditMode),
    )
    thread_num = RangeConfigItem("Translate", "ThreadNum", 10, RangeValidator(1, 50))

    # ------------------- 转录配置 -------------------
    transcribe_model = OptionsConfigItem(
        "Transcribe",
        "TranscribeModel",
        TranscribeModelEnum.BIJIAN,
        PlatformAwareTranscribeModelValidator(),
        EnumSerializer(TranscribeModelEnum),
    )
    transcribe_output_format = OptionsConfigItem(
        "Transcribe",
        "OutputFormat",
        TranscribeOutputFormatEnum.SRT,
        OptionsValidator(TranscribeOutputFormatEnum),
        EnumSerializer(TranscribeOutputFormatEnum),
    )
    transcribe_language = OptionsConfigItem(
        "Transcribe",
        "TranscribeLanguage",
        TranscribeLanguageEnum.AUTO,
        OptionsValidator(TranscribeLanguageEnum),
        EnumSerializer(TranscribeLanguageEnum),
    )
    audio_loudnorm = ConfigItem("Transcribe", "AudioLoudnorm", False, BoolValidator())

    # ------------------- Whisper Cpp 配置 -------------------
    whisper_model = OptionsConfigItem(
        "Whisper",
        "WhisperModel",
        WhisperModelEnum.TINY,
        OptionsValidator(WhisperModelEnum),
        EnumSerializer(WhisperModelEnum),
    )

    # ------------------- Faster Whisper 配置 -------------------
    faster_whisper_program = ConfigItem(
        "FasterWhisper",
        "Program",
        "faster-whisper-xxl.exe",
    )
    faster_whisper_model = OptionsConfigItem(
        "FasterWhisper",
        "Model",
        FasterWhisperModelEnum.TINY,
        OptionsValidator(FasterWhisperModelEnum),
        EnumSerializer(FasterWhisperModelEnum),
    )
    faster_whisper_model_dir = ConfigItem("FasterWhisper", "ModelDir", "")
    faster_whisper_device = OptionsConfigItem(
        "FasterWhisper", "Device", "cuda", OptionsValidator(["cuda", "cpu"])
    )
    # VAD 参数
    faster_whisper_vad_filter = ConfigItem(
        "FasterWhisper", "VadFilter", True, BoolValidator()
    )
    faster_whisper_vad_threshold = RangeConfigItem(
        "FasterWhisper", "VadThreshold", 0.4, RangeValidator(0, 1)
    )
    faster_whisper_vad_method = OptionsConfigItem(
        "FasterWhisper",
        "VadMethod",
        VadMethodEnum.SILERO_V4,
        OptionsValidator(VadMethodEnum),
        EnumSerializer(VadMethodEnum),
    )
    # 人声提取
    faster_whisper_ff_mdx_kim2 = ConfigItem(
        "FasterWhisper", "FfMdxKim2", False, BoolValidator()
    )
    # 文本处理参数
    faster_whisper_one_word = ConfigItem(
        "FasterWhisper", "OneWord", True, BoolValidator()
    )
    # 提示词
    faster_whisper_prompt = ConfigItem("FasterWhisper", "Prompt", "")

    # ------------------- Whisper API 配置 -------------------
    whisper_api_base = ConfigItem("WhisperAPI", "WhisperApiBase", "")
    whisper_api_key = ConfigItem("WhisperAPI", "WhisperApiKey", "")
    whisper_api_model = OptionsConfigItem("WhisperAPI", "WhisperApiModel", "")
    whisper_api_prompt = ConfigItem("WhisperAPI", "WhisperApiPrompt", "")

    # ------------------- MiMo ASR API 配置 -------------------
    mimo_asr_api_base = ConfigItem(
        "MiMoASR", "ApiBase", "https://api.xiaomimimo.com/v1"
    )
    mimo_asr_api_key = ConfigItem("MiMoASR", "ApiKey", "")
    mimo_asr_model = ConfigItem("MiMoASR", "Model", "mimo-v2.5-asr")
    mimo_asr_timeout = RangeConfigItem(
        "MiMoASR", "Timeout", 600, RangeValidator(30, 7200)
    )
    mimo_asr_concurrency = RangeConfigItem(
        "MiMoASR", "Concurrency", 2, RangeValidator(1, 8)
    )

    # ------------------- Qwen3 ASR / Forced Aligner 配置 -------------------
    qwen_asr_model = OptionsConfigItem(
        "QwenASR",
        "AsrModel",
        "Qwen/Qwen3-ASR-1.7B",
        OptionsValidator(["Qwen/Qwen3-ASR-1.7B", "Qwen/Qwen3-ASR-0.6B"]),
    )
    qwen_aligner_model = OptionsConfigItem(
        "QwenASR",
        "AlignerModel",
        "Qwen/Qwen3-ForcedAligner-0.6B",
        OptionsValidator(["Qwen/Qwen3-ForcedAligner-0.6B"]),
    )
    qwen_model_dir = ConfigItem(
        "QwenASR", "ModelDir", str(MODEL_PATH), FolderValidator()
    )
    qwen_device = OptionsConfigItem(
        "QwenASR",
        "Device",
        "auto",
        OptionsValidator(["auto", "cuda:0", "cpu"]),
    )
    qwen_dtype = OptionsConfigItem(
        "QwenASR",
        "DType",
        "auto",
        OptionsValidator(["auto", "bfloat16", "float16", "float32"]),
    )
    qwen_max_new_tokens = RangeConfigItem(
        "QwenASR", "MaxNewTokens", 2048, RangeValidator(64, 8192)
    )
    qwen_chunk_overlap_seconds = RangeConfigItem(
        "QwenASR", "ChunkOverlapSeconds", 10, RangeValidator(0, 60)
    )
    qwen_compile_aligner = ConfigItem(
        "QwenASR", "CompileAligner", False, BoolValidator()
    )

    # ------------------- 字幕配置 -------------------
    need_optimize = ConfigItem("Subtitle", "NeedOptimize", False, BoolValidator())
    need_translate = ConfigItem("Subtitle", "NeedTranslate", False, BoolValidator())
    need_split = ConfigItem("Subtitle", "NeedSplit", False, BoolValidator())
    target_language = OptionsConfigItem(
        "Subtitle",
        "TargetLanguage",
        TargetLanguage.SIMPLIFIED_CHINESE,
        OptionsValidator(TargetLanguage),
        EnumSerializer(TargetLanguage),
    )
    max_word_count_cjk = ConfigItem(
        "Subtitle", "MaxWordCountCJK", 28, RangeValidator(8, 100)
    )
    max_word_count_english = ConfigItem(
        "Subtitle", "MaxWordCountEnglish", 20, RangeValidator(8, 100)
    )
    custom_prompt_text = ConfigItem("Subtitle", "CustomPromptText", "")
    optimization_prompt_text = ConfigItem("Subtitle", "OptimizationPromptText", "")

    # 规则型后处理 / 审计
    # keep in sync with core/postprocess/config.py (PostprocessConfig)
    need_remove_placeholders = ConfigItem(
        "Subtitle", "NeedRemovePlaceholders", False, BoolValidator()
    )
    need_normalize_quotes = ConfigItem(
        "Subtitle", "NeedNormalizeQuotes", False, BoolValidator()
    )
    trim_trailing_punct = ConfigItem(
        "Subtitle", "TrimTrailingPunct", True, BoolValidator()
    )
    need_fix_gaps = ConfigItem("Subtitle", "NeedFixGaps", False, BoolValidator())
    max_gap_ms = RangeConfigItem("Subtitle", "MaxGapMs", 800, RangeValidator(100, 2000))
    need_tail_compensation = ConfigItem(
        "Subtitle", "NeedTailCompensation", False, BoolValidator()
    )
    min_compensation_ms = RangeConfigItem(
        "Subtitle", "MinCompensationMs", 200, RangeValidator(0, 2000)
    )
    max_compensation_gap_ms = RangeConfigItem(
        "Subtitle", "MaxCompensationGapMs", 2000, RangeValidator(100, 10000)
    )
    max_compensation_ms = RangeConfigItem(
        "Subtitle", "MaxCompensationMs", 800, RangeValidator(0, 5000)
    )
    need_audit_speed = ConfigItem(
        "Subtitle", "NeedAuditSpeed", False, BoolValidator()
    )
    max_cps_cjk = RangeConfigItem("Subtitle", "MaxCpsCjk", 11, RangeValidator(5, 30))
    max_cps_latin = RangeConfigItem(
        "Subtitle", "MaxCpsLatin", 20, RangeValidator(8, 40)
    )
    need_compress_fast = ConfigItem(
        "Subtitle", "NeedCompressFast", False, BoolValidator()
    )
    need_qa_report = ConfigItem("Subtitle", "NeedQaReport", False, BoolValidator())

    # ------------------- 独立字幕后处理阶段 -------------------
    # 完整 workflow 默认执行；任务创建与批量编排可关闭整个阶段。
    postprocess_enabled = ConfigItem(
        "Postprocess", "Enabled", True, BoolValidator()
    )
    # 完整方案 ID。模板和自定义方案由 PostprocessProfileStore 校验。
    postprocess_profile = OptionsConfigItem(
        "Postprocess", "Profile", "balanced", SpeedProfileValidator()
    )
    postprocess_optimize_both_sides = ConfigItem(
        "Postprocess", "OptimizeBothSides", False, BoolValidator()
    )
    workflow_auto_export = ConfigItem(
        "SubtitleDelivery", "AutoExport", False, BoolValidator()
    )
    workflow_export_format = OptionsConfigItem(
        "SubtitleDelivery",
        "ExportFormat",
        "ass",
        OptionsValidator(["ass", "vtt"]),
    )

    # ------------------- 字幕速度优化 -------------------
    # SpeedPolicy 是算法参数的唯一权威默认值；这里仅声明 qconfig 持久化映射。
    speed_activation = OptionsConfigItem(
        "SubtitleSpeed",
        "Activation",
        "auto",
        OptionsValidator(["auto", "on", "off"]),
    )
    speed_profile = OptionsConfigItem(
        "SubtitleSpeed",
        "Profile",
        _POSTPROCESS_DEFAULTS.speed_profile,
        SpeedProfileValidator(),
    )
    speed_mode = OptionsConfigItem(
        "SubtitleSpeed",
        "Mode",
        _POSTPROCESS_DEFAULTS.speed_mode,
        OptionsValidator(["apply", "analyze"]),
    )
    speed_primary = OptionsConfigItem(
        "SubtitleSpeed",
        "PrimarySide",
        _POSTPROCESS_DEFAULTS.speed_primary,
        OptionsValidator(["translate", "layout", "original"]),
    )
    # 领域术语：媒体增强对齐 / 对齐时间轴（见 CONTEXT.md）。配置组/键与字段名保持
    # "SubtitleSpeed"/"PreciseTiming"/precise_timing 不变，仅 UI 显示串改用领域词。
    speed_precise_timing = ConfigItem(
        "SubtitleSpeed", "PreciseTiming", False, BoolValidator()
    )
    speed_reference_hard_audit = ConfigItem(
        "SubtitleSpeed", "ReferenceHardAudit", False, BoolValidator()
    )

    speed_comfort_cps_cjk = ConfigItem(
        "SubtitleSpeed",
        "ComfortCpsCjk",
        _BALANCED_SPEED_POLICY.comfort_cps_cjk,
        RangeValidator(1.0, 40.0),
    )
    speed_hard_cps_cjk = ConfigItem(
        "SubtitleSpeed",
        "HardCpsCjk",
        _BALANCED_SPEED_POLICY.hard_cps_cjk,
        RangeValidator(1.0, 40.0),
    )
    speed_comfort_cps_latin = ConfigItem(
        "SubtitleSpeed",
        "ComfortCpsLatin",
        _BALANCED_SPEED_POLICY.comfort_cps_latin,
        RangeValidator(1.0, 60.0),
    )
    speed_hard_cps_latin = ConfigItem(
        "SubtitleSpeed",
        "HardCpsLatin",
        _BALANCED_SPEED_POLICY.hard_cps_latin,
        RangeValidator(1.0, 60.0),
    )
    speed_adjacent_p90_target = ConfigItem(
        "SubtitleSpeed",
        "AdjacentP90Target",
        _BALANCED_SPEED_POLICY.adjacent_p90_target,
        RangeValidator(1.0, 5.0),
    )
    speed_adjacent_emergency_limit = ConfigItem(
        "SubtitleSpeed",
        "AdjacentEmergencyLimit",
        _BALANCED_SPEED_POLICY.adjacent_emergency_limit,
        RangeValidator(1.0, 8.0),
    )
    speed_whitespace_weight = ConfigItem(
        "SubtitleSpeed",
        "WhitespaceWeight",
        _BALANCED_SPEED_POLICY.whitespace_weight,
        RangeValidator(0.0, 2.0),
    )
    speed_weak_punctuation_weight = ConfigItem(
        "SubtitleSpeed",
        "WeakPunctuationWeight",
        _BALANCED_SPEED_POLICY.weak_punctuation_weight,
        RangeValidator(0.0, 2.0),
    )
    speed_strong_punctuation_weight = ConfigItem(
        "SubtitleSpeed",
        "StrongPunctuationWeight",
        _BALANCED_SPEED_POLICY.strong_punctuation_weight,
        RangeValidator(0.0, 2.0),
    )
    speed_min_duration_ms = RangeConfigItem(
        "SubtitleSpeed",
        "MinDurationMs",
        round(_BALANCED_SPEED_POLICY.min_duration_seconds * 1000),
        RangeValidator(500, 5000),
    )
    speed_max_duration_ms = RangeConfigItem(
        "SubtitleSpeed",
        "MaxDurationMs",
        round(_BALANCED_SPEED_POLICY.max_duration_seconds * 1000),
        RangeValidator(1000, 12000),
    )
    speed_local_window_radius = RangeConfigItem(
        "SubtitleSpeed",
        "LocalWindowRadius",
        _BALANCED_SPEED_POLICY.local_window_radius,
        RangeValidator(1, 12),
    )
    speed_rhythm_reset_ms = RangeConfigItem(
        "SubtitleSpeed",
        "RhythmResetMs",
        _BALANCED_SPEED_POLICY.rhythm_reset_ms,
        RangeValidator(100, 3000),
    )
    speed_hard_rhythm_reset_ms = RangeConfigItem(
        "SubtitleSpeed",
        "HardRhythmResetMs",
        _BALANCED_SPEED_POLICY.hard_rhythm_reset_ms,
        RangeValidator(200, 6000),
    )
    speed_low_boundary_shift_ms = RangeConfigItem(
        "SubtitleSpeed",
        "LowBoundaryShiftMs",
        _BALANCED_SPEED_POLICY.low_confidence_boundary_shift_ms,
        RangeValidator(0, 2000),
    )
    speed_medium_boundary_shift_ms = RangeConfigItem(
        "SubtitleSpeed",
        "MediumBoundaryShiftMs",
        _BALANCED_SPEED_POLICY.medium_confidence_boundary_shift_ms,
        RangeValidator(0, 3000),
    )
    speed_high_boundary_shift_ms = RangeConfigItem(
        "SubtitleSpeed",
        "HighBoundaryShiftMs",
        _BALANCED_SPEED_POLICY.high_confidence_boundary_shift_ms,
        RangeValidator(0, 5000),
    )
    speed_bidirectional_smoothing = ConfigItem(
        "SubtitleSpeed",
        "BidirectionalSmoothing",
        _BALANCED_SPEED_POLICY.bidirectional_smoothing,
        BoolValidator(),
    )
    speed_semantic_repair = ConfigItem(
        "SubtitleSpeed",
        "SemanticRepair",
        _POSTPROCESS_DEFAULTS.speed_semantic_repair,
        BoolValidator(),
    )
    speed_semantic_window = RangeConfigItem(
        "SubtitleSpeed", "SemanticWindow", 5, RangeValidator(1, 15)
    )
    speed_llm_uncertain_review = ConfigItem(
        "SubtitleSpeed", "LlmUncertainReview", True, BoolValidator()
    )
    speed_qa_report = ConfigItem(
        "SubtitleSpeed", "QaReport", _POSTPROCESS_DEFAULTS.qa_report, BoolValidator()
    )
    speed_save_timing_sidecar = ConfigItem(
        "SubtitleSpeed", "SaveTimingSidecar", False, BoolValidator()
    )

    # ------------------- 字幕合成配置 -------------------
    soft_subtitle = ConfigItem("Video", "SoftSubtitle", False, BoolValidator())
    need_video = ConfigItem("Video", "NeedVideo", True, BoolValidator())
    video_quality = OptionsConfigItem(
        "Video",
        "VideoQuality",
        VideoQualityEnum.MEDIUM,
        OptionsValidator(VideoQualityEnum),
        EnumSerializer(VideoQualityEnum),
    )
    use_subtitle_style = ConfigItem("Video", "UseSubtitleStyle", False, BoolValidator())

    # ------------------- 视频编码配置（新引擎） -------------------
    video_encoder = ConfigItem("Video", "VideoEncoder", "x264")
    encode_mode = OptionsConfigItem(
        "Video", "EncodeMode", "cq", OptionsValidator(["cq", "abr"])
    )
    encode_cq = RangeConfigItem("Video", "EncodeCq", 23, RangeValidator(0, 63))
    encode_bitrate_kbps = RangeConfigItem(
        "Video", "EncodeBitrateKbps", 4000, RangeValidator(100, 200000)
    )
    # ffmpeg 核心来源：默认（内置，不可变）/ 自定义（用户 BIN_PATH，git 忽略）
    ffmpeg_source = OptionsConfigItem(
        "Video", "FfmpegSource", "default", OptionsValidator(["default", "custom"])
    )
    # 自定义 ffmpeg 参数（追加在构建命令末段；命令预览未识别 token 也归入此处）
    extra_args = ConfigItem("Video", "ExtraArgs", "")

    # ------------------- 编码器选项（预设/微调/配置/级别/快速解码） -------------------
    # 空字符串 = 自动/默认（编码器默认值，None）
    enc_preset = ConfigItem("Video", "EncPreset", "")
    enc_tune = ConfigItem("Video", "EncTune", "")
    enc_profile = ConfigItem("Video", "EncProfile", "")
    enc_level = ConfigItem("Video", "EncLevel", "")
    fast_decode = ConfigItem("Video", "FastDecode", False, BoolValidator())

    # ------------------- 分辨率与帧率 -------------------
    # 0 = 与源相同（不放大）
    target_height = RangeConfigItem("Video", "TargetHeight", 0, RangeValidator(0, 4320))
    # 空字符串 = 与源相同
    out_fps = ConfigItem("Video", "OutFps", "")
    vfr = ConfigItem("Video", "Vfr", True, BoolValidator())

    # ------------------- 音频 -------------------
    audio_encoder = ConfigItem("Video", "AudioEncoder", "copy")
    audio_bitrate_kbps = RangeConfigItem(
        "Video", "AudioBitrateKbps", 192, RangeValidator(32, 1024)
    )

    # ------------------- 其他 · 高级 -------------------
    container = OptionsConfigItem("Video", "Container", "mp4", OptionsValidator(["mp4", "mkv"]))
    faststart = ConfigItem("Video", "Faststart", True, BoolValidator())
    keep_metadata = ConfigItem("Video", "KeepMetadata", True, BoolValidator())
    start_zero = ConfigItem("Video", "StartZero", True, BoolValidator())

    # ------------------- 字幕样式配置 -------------------
    subtitle_style_name = ConfigItem("SubtitleStyle", "StyleName", "default")
    subtitle_layout = OptionsConfigItem(
        "SubtitleStyle",
        "Layout",
        SubtitleLayoutEnum.TRANSLATE_ON_TOP,
        OptionsValidator(SubtitleLayoutEnum),
        EnumSerializer(SubtitleLayoutEnum),
    )
    subtitle_preview_image = ConfigItem("SubtitleStyle", "PreviewImage", "")
    subtitle_style_reference_width = RangeConfigItem(
        "SubtitleStyle", "ReferenceWidth", 1280, RangeValidator(320, 7680)
    )
    subtitle_style_reference_height = RangeConfigItem(
        "SubtitleStyle", "ReferenceHeight", 720, RangeValidator(180, 4320)
    )

    # 字幕渲染模式
    subtitle_render_mode = OptionsConfigItem(
        "SubtitleStyle",
        "RenderMode",
        SubtitleRenderModeEnum.ROUNDED_BG,
        OptionsValidator(SubtitleRenderModeEnum),
        EnumSerializer(SubtitleRenderModeEnum),
    )

    # 圆角背景模式配置
    rounded_bg_font_name = ConfigItem("RoundedBgStyle", "FontName", "Noto Sans SC")
    rounded_bg_font_size = RangeConfigItem(
        "RoundedBgStyle", "FontSize", 52, RangeValidator(16, 120)
    )
    # 背景色：深灰半透明 (R=25, G=25, B=25, A=200)
    rounded_bg_color = ConfigItem("RoundedBgStyle", "BgColor", "#191919C8")
    rounded_bg_text_color = ConfigItem("RoundedBgStyle", "TextColor", "#FFFFFF")
    rounded_bg_corner_radius = RangeConfigItem(
        "RoundedBgStyle", "CornerRadius", 12, RangeValidator(0, 50)
    )
    rounded_bg_padding_h = RangeConfigItem(
        "RoundedBgStyle", "PaddingH", 28, RangeValidator(4, 100)
    )
    rounded_bg_padding_v = RangeConfigItem(
        "RoundedBgStyle", "PaddingV", 14, RangeValidator(4, 50)
    )
    rounded_bg_margin_bottom = RangeConfigItem(
        "RoundedBgStyle", "MarginBottom", 60, RangeValidator(20, 300)
    )
    rounded_bg_line_spacing = RangeConfigItem(
        "RoundedBgStyle", "LineSpacing", 10, RangeValidator(0, 50)
    )
    rounded_bg_letter_spacing = RangeConfigItem(
        "RoundedBgStyle", "LetterSpacing", 0, RangeValidator(0, 20)
    )

    # ------------------- 保存配置 -------------------
    work_dir = ConfigItem("Save", "Work_Dir", WORK_PATH, FolderValidator())

    # ------------------- 软件页面配置 -------------------
    micaEnabled = ConfigItem("MainWindow", "MicaEnabled", False, BoolValidator())
    dpiScale = OptionsConfigItem(
        "MainWindow",
        "DpiScale",
        "Auto",
        OptionsValidator([1, 1.25, 1.5, 1.75, 2, "Auto"]),
        restart=True,
    )
    language = OptionsConfigItem(
        "MainWindow",
        "Language",
        Language.AUTO,
        OptionsValidator(Language),
        LanguageSerializer(),
        restart=True,
    )

    # ------------------- 更新配置 -------------------
    checkUpdateAtStartUp = ConfigItem(
        "Update", "CheckUpdateAtStartUp", True, BoolValidator()
    )

    # ------------------- 缓存配置 -------------------
    cache_enabled = ConfigItem("Cache", "CacheEnabled", True, BoolValidator())


cfg = Config()
cfg.themeMode.value = Theme.DARK
cfg.themeColor.value = QColor("#ff28f08b")
migrate_legacy_translation_settings(SETTINGS_PATH)
qconfig.load(SETTINGS_PATH, cfg)
