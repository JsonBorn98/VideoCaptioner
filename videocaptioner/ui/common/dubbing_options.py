from dataclasses import dataclass

from PyQt5.QtCore import QT_TRANSLATE_NOOP


@dataclass(frozen=True)
class DubbingProviderOption:
    key: str
    title: str
    description: str
    needs_api_key: bool
    supports_clone: bool
    default_base: str
    models: tuple[str, ...]


@dataclass(frozen=True)
class DubbingVoiceOption:
    preset: str
    title: str
    description: str
    tags: tuple[str, ...] = ()


DUBBING_PROVIDERS: tuple[DubbingProviderOption, ...] = (
    DubbingProviderOption(
        key="edge",
        title="Edge 免费配音",
        description="免 API Key，适合默认快速生成中文或英文配音。",
        needs_api_key=False,
        supports_clone=False,
        default_base="",
        models=("edge-tts",),
    ),
    DubbingProviderOption(
        key="gemini",
        title="Gemini TTS",
        description="Google Gemini 语音模型，适合英文自然表达。",
        needs_api_key=True,
        supports_clone=False,
        default_base="https://generativelanguage.googleapis.com/v1beta",
        models=("gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"),
    ),
    DubbingProviderOption(
        key="siliconflow",
        title="SiliconFlow CosyVoice",
        description="CosyVoice 中文表现稳定，并支持参考音频克隆。",
        needs_api_key=True,
        supports_clone=True,
        default_base="https://api.siliconflow.cn/v1",
        models=("FunAudioLLM/CosyVoice2-0.5B",),
    ),
)


DUBBING_VOICES: dict[str, tuple[DubbingVoiceOption, ...]] = {
    "edge": (
        DubbingVoiceOption("edge-cn-female", "晓晓", "清晰自然的普通话女声", ("中文", "女声", "免费")),
        DubbingVoiceOption("edge-cn-male", "云希", "年轻自然的普通话男声", ("中文", "男声", "免费")),
        DubbingVoiceOption("edge-cn-xiaoyi", "晓伊", "温和明亮的普通话女声", ("中文", "女声", "免费")),
        DubbingVoiceOption("edge-cn-yunjian", "云健", "更适合演讲和旁白的男声", ("中文", "男声", "免费")),
        DubbingVoiceOption("edge-cn-yunyang", "云扬", "播报感更强的普通话男声", ("中文", "男声", "免费")),
        DubbingVoiceOption("edge-hk-hiugaai", "曉佳", "粤语女声", ("粤语", "女声", "免费")),
        DubbingVoiceOption("edge-hk-wanlung", "雲龍", "粤语男声", ("粤语", "男声", "免费")),
        DubbingVoiceOption("edge-tw-hsiaoyu", "曉臾", "台湾国语女声", ("中文", "女声", "免费")),
        DubbingVoiceOption("edge-tw-yunjhe", "雲哲", "台湾国语男声", ("中文", "男声", "免费")),
        DubbingVoiceOption("edge-en-female", "Jenny", "美式英语女声", ("英文", "女声", "免费")),
        DubbingVoiceOption("edge-en-male", "Guy", "美式英语男声", ("英文", "男声", "免费")),
        DubbingVoiceOption("edge-en-ava", "Ava", "清爽自然的美式英语女声", ("英文", "女声", "免费")),
        DubbingVoiceOption("edge-en-andrew", "Andrew", "清晰稳重的美式英语男声", ("英文", "男声", "免费")),
        DubbingVoiceOption("edge-en-emma", "Emma", "柔和自然的美式英语女声", ("英文", "女声", "免费")),
        DubbingVoiceOption("edge-en-brian", "Brian", "自然稳健的美式英语男声", ("英文", "男声", "免费")),
        DubbingVoiceOption("edge-en-libby", "Libby", "英式英语女声", ("英文", "女声", "免费")),
        DubbingVoiceOption("edge-en-ryan", "Ryan", "英式英语男声", ("英文", "男声", "免费")),
    ),
    "gemini": (
        DubbingVoiceOption("gemini-en-friendly", "Achird", "友好自然的英文表达", ("英文", "推荐", "需 Key")),
        DubbingVoiceOption("gemini-en-neutral", "Kore", "清晰稳定的自然英文", ("英文", "推荐", "需 Key")),
        DubbingVoiceOption("gemini-en-upbeat", "Puck", "更有能量的英文表达", ("英文", "推荐", "需 Key")),
        DubbingVoiceOption("gemini-zephyr", "Zephyr", "明亮清爽的英文声音", ("英文", "Bright", "需 Key")),
        DubbingVoiceOption("gemini-aoede", "Aoede", "明亮自然的英文声音", ("英文", "需 Key")),
        DubbingVoiceOption("gemini-autonoe", "Autonoe", "均衡清晰的英文声音", ("英文", "需 Key")),
        DubbingVoiceOption("gemini-callirrhoe", "Callirrhoe", "轻松自然的英文声音", ("英文", "Easy-going", "需 Key")),
        DubbingVoiceOption("gemini-charon", "Charon", "更沉稳的英文声音", ("英文", "需 Key")),
        DubbingVoiceOption("gemini-despina", "Despina", "平滑自然的英文声音", ("英文", "Smooth", "需 Key")),
        DubbingVoiceOption("gemini-enceladus", "Enceladus", "气声感更明显的英文声音", ("英文", "Breathy", "需 Key")),
        DubbingVoiceOption("gemini-erinome", "Erinome", "清晰直给的英文声音", ("英文", "Clear", "需 Key")),
        DubbingVoiceOption("gemini-fenrir", "Fenrir", "低沉有力的英文声音", ("英文", "需 Key")),
        DubbingVoiceOption("gemini-gacrux", "Gacrux", "成熟稳重的英文声音", ("英文", "Mature", "需 Key")),
        DubbingVoiceOption("gemini-iapetus", "Iapetus", "清澈稳定的英文声音", ("英文", "Clear", "需 Key")),
        DubbingVoiceOption("gemini-laomedeia", "Laomedeia", "轻快活泼的英文声音", ("英文", "Upbeat", "需 Key")),
        DubbingVoiceOption("gemini-leda", "Leda", "轻快明亮的英文声音", ("英文", "需 Key")),
        DubbingVoiceOption("gemini-orus", "Orus", "旁白感更强的英文声音", ("英文", "需 Key")),
        DubbingVoiceOption("gemini-pulcherrima", "Pulcherrima", "前置感更强的英文声音", ("英文", "Forward", "需 Key")),
        DubbingVoiceOption("gemini-rasalgethi", "Rasalgethi", "信息感更强的英文声音", ("英文", "Informative", "需 Key")),
        DubbingVoiceOption("gemini-sadachbia", "Sadachbia", "生动轻快的英文声音", ("英文", "Lively", "需 Key")),
        DubbingVoiceOption("gemini-sadaltager", "Sadaltager", "知识型表达的英文声音", ("英文", "Knowledgeable", "需 Key")),
        DubbingVoiceOption("gemini-schedar", "Schedar", "平稳均衡的英文声音", ("英文", "Even", "需 Key")),
        DubbingVoiceOption("gemini-sulafat", "Sulafat", "温暖自然的英文声音", ("英文", "Warm", "需 Key")),
        DubbingVoiceOption("gemini-umbriel", "Umbriel", "轻松自然的英文声音", ("英文", "Easy-going", "需 Key")),
        DubbingVoiceOption("gemini-vindemiatrix", "Vindemiatrix", "温和柔顺的英文声音", ("英文", "Gentle", "需 Key")),
        DubbingVoiceOption("gemini-zubenelgenubi", "Zubenelgenubi", "休闲自然的英文声音", ("英文", "Casual", "需 Key")),
        DubbingVoiceOption("gemini-achernar", "Achernar", "柔和的英文声音", ("英文", "Soft", "需 Key")),
        DubbingVoiceOption("gemini-algenib", "Algenib", "颗粒感更强的英文声音", ("英文", "Gravelly", "需 Key")),
        DubbingVoiceOption("gemini-algieba", "Algieba", "平滑的英文声音", ("英文", "Smooth", "需 Key")),
        DubbingVoiceOption("gemini-alnilam", "Alnilam", "坚定清晰的英文声音", ("英文", "Firm", "需 Key")),
    ),
    "siliconflow": (
        DubbingVoiceOption("siliconflow-cn-female", "Anna", "自然中文女声，可配合参考音频克隆", ("中文", "女声", "克隆")),
        DubbingVoiceOption("siliconflow-cn-male", "Alex", "自然中文男声，可配合参考音频克隆", ("中文", "男声", "克隆")),
        DubbingVoiceOption("siliconflow-cn-deep-male", "Benjamin", "沉稳低沉的中文男声，可克隆", ("中文", "男声", "克隆")),
        DubbingVoiceOption("siliconflow-cn-charles", "Charles", "磁性中文男声，可克隆", ("中文", "男声", "克隆")),
        DubbingVoiceOption("siliconflow-cn-david", "David", "欢快中文男声，可克隆", ("中文", "男声", "克隆")),
        DubbingVoiceOption("siliconflow-cn-bella", "Bella", "热情中文女声，可克隆", ("中文", "女声", "克隆")),
        DubbingVoiceOption("siliconflow-cn-claire", "Claire", "温柔中文女声，可克隆", ("中文", "女声", "克隆")),
        DubbingVoiceOption("siliconflow-cn-diana", "Diana", "欢快中文女声，可克隆", ("中文", "女声", "克隆")),
    ),
}


def get_provider_option(provider: str) -> DubbingProviderOption:
    for option in DUBBING_PROVIDERS:
        if option.key == provider:
            return option
    return DUBBING_PROVIDERS[0]


def get_provider_titles() -> list[str]:
    return [option.title for option in DUBBING_PROVIDERS]


def get_provider_key_by_title(title: str) -> str:
    for option in DUBBING_PROVIDERS:
        if option.title == title or option.key == title:
            return option.key
    return DUBBING_PROVIDERS[0].key


def is_provider_default_base(value: str) -> bool:
    return value in {"", *(option.default_base for option in DUBBING_PROVIDERS)}


def get_provider_voices(provider: str) -> tuple[DubbingVoiceOption, ...]:
    return DUBBING_VOICES.get(provider, DUBBING_VOICES["edge"])


def get_voice_title(preset: str) -> str:
    for voices in DUBBING_VOICES.values():
        for voice in voices:
            if voice.preset == preset:
                return voice.title
    return preset


def _translation_catalog() -> tuple[str, ...]:
    return (
        QT_TRANSLATE_NOOP("DubbingInterface", "Edge 免费配音"),
        QT_TRANSLATE_NOOP("DubbingInterface", "免 API Key，适合默认快速生成中文或英文配音。"),
        QT_TRANSLATE_NOOP("DubbingInterface", "Gemini TTS"),
        QT_TRANSLATE_NOOP("DubbingInterface", "Google Gemini 语音模型，适合英文自然表达。"),
        QT_TRANSLATE_NOOP("DubbingInterface", "SiliconFlow CosyVoice"),
        QT_TRANSLATE_NOOP("DubbingInterface", "CosyVoice 中文表现稳定，并支持参考音频克隆。"),
        QT_TRANSLATE_NOOP("DubbingInterface", "晓晓"),
        QT_TRANSLATE_NOOP("DubbingInterface", "清晰自然的普通话女声"),
        QT_TRANSLATE_NOOP("DubbingInterface", "云希"),
        QT_TRANSLATE_NOOP("DubbingInterface", "年轻自然的普通话男声"),
        QT_TRANSLATE_NOOP("DubbingInterface", "晓伊"),
        QT_TRANSLATE_NOOP("DubbingInterface", "温和明亮的普通话女声"),
        QT_TRANSLATE_NOOP("DubbingInterface", "云健"),
        QT_TRANSLATE_NOOP("DubbingInterface", "更适合演讲和旁白的男声"),
        QT_TRANSLATE_NOOP("DubbingInterface", "云扬"),
        QT_TRANSLATE_NOOP("DubbingInterface", "播报感更强的普通话男声"),
        QT_TRANSLATE_NOOP("DubbingInterface", "曉佳"),
        QT_TRANSLATE_NOOP("DubbingInterface", "粤语女声"),
        QT_TRANSLATE_NOOP("DubbingInterface", "雲龍"),
        QT_TRANSLATE_NOOP("DubbingInterface", "粤语男声"),
        QT_TRANSLATE_NOOP("DubbingInterface", "曉臾"),
        QT_TRANSLATE_NOOP("DubbingInterface", "台湾国语女声"),
        QT_TRANSLATE_NOOP("DubbingInterface", "雲哲"),
        QT_TRANSLATE_NOOP("DubbingInterface", "台湾国语男声"),
        QT_TRANSLATE_NOOP("DubbingInterface", "美式英语女声"),
        QT_TRANSLATE_NOOP("DubbingInterface", "美式英语男声"),
        QT_TRANSLATE_NOOP("DubbingInterface", "清爽自然的美式英语女声"),
        QT_TRANSLATE_NOOP("DubbingInterface", "清晰稳重的美式英语男声"),
        QT_TRANSLATE_NOOP("DubbingInterface", "柔和自然的美式英语女声"),
        QT_TRANSLATE_NOOP("DubbingInterface", "自然稳健的美式英语男声"),
        QT_TRANSLATE_NOOP("DubbingInterface", "英式英语女声"),
        QT_TRANSLATE_NOOP("DubbingInterface", "英式英语男声"),
        QT_TRANSLATE_NOOP("DubbingInterface", "友好自然的英文表达"),
        QT_TRANSLATE_NOOP("DubbingInterface", "清晰稳定的自然英文"),
        QT_TRANSLATE_NOOP("DubbingInterface", "更有能量的英文表达"),
        QT_TRANSLATE_NOOP("DubbingInterface", "明亮清爽的英文声音"),
        QT_TRANSLATE_NOOP("DubbingInterface", "明亮自然的英文声音"),
        QT_TRANSLATE_NOOP("DubbingInterface", "均衡清晰的英文声音"),
        QT_TRANSLATE_NOOP("DubbingInterface", "轻松自然的英文声音"),
        QT_TRANSLATE_NOOP("DubbingInterface", "更沉稳的英文声音"),
        QT_TRANSLATE_NOOP("DubbingInterface", "平滑自然的英文声音"),
        QT_TRANSLATE_NOOP("DubbingInterface", "气声感更明显的英文声音"),
        QT_TRANSLATE_NOOP("DubbingInterface", "清晰直给的英文声音"),
        QT_TRANSLATE_NOOP("DubbingInterface", "低沉有力的英文声音"),
        QT_TRANSLATE_NOOP("DubbingInterface", "成熟稳重的英文声音"),
        QT_TRANSLATE_NOOP("DubbingInterface", "清澈稳定的英文声音"),
        QT_TRANSLATE_NOOP("DubbingInterface", "轻快活泼的英文声音"),
        QT_TRANSLATE_NOOP("DubbingInterface", "轻快明亮的英文声音"),
        QT_TRANSLATE_NOOP("DubbingInterface", "旁白感更强的英文声音"),
        QT_TRANSLATE_NOOP("DubbingInterface", "前置感更强的英文声音"),
        QT_TRANSLATE_NOOP("DubbingInterface", "信息感更强的英文声音"),
        QT_TRANSLATE_NOOP("DubbingInterface", "生动轻快的英文声音"),
        QT_TRANSLATE_NOOP("DubbingInterface", "知识型表达的英文声音"),
        QT_TRANSLATE_NOOP("DubbingInterface", "平稳均衡的英文声音"),
        QT_TRANSLATE_NOOP("DubbingInterface", "温暖自然的英文声音"),
        QT_TRANSLATE_NOOP("DubbingInterface", "温和柔顺的英文声音"),
        QT_TRANSLATE_NOOP("DubbingInterface", "休闲自然的英文声音"),
        QT_TRANSLATE_NOOP("DubbingInterface", "柔和的英文声音"),
        QT_TRANSLATE_NOOP("DubbingInterface", "颗粒感更强的英文声音"),
        QT_TRANSLATE_NOOP("DubbingInterface", "平滑的英文声音"),
        QT_TRANSLATE_NOOP("DubbingInterface", "坚定清晰的英文声音"),
        QT_TRANSLATE_NOOP("DubbingInterface", "自然中文女声，可配合参考音频克隆"),
        QT_TRANSLATE_NOOP("DubbingInterface", "自然中文男声，可配合参考音频克隆"),
        QT_TRANSLATE_NOOP("DubbingInterface", "沉稳低沉的中文男声，可克隆"),
        QT_TRANSLATE_NOOP("DubbingInterface", "磁性中文男声，可克隆"),
        QT_TRANSLATE_NOOP("DubbingInterface", "欢快中文男声，可克隆"),
        QT_TRANSLATE_NOOP("DubbingInterface", "热情中文女声，可克隆"),
        QT_TRANSLATE_NOOP("DubbingInterface", "温柔中文女声，可克隆"),
        QT_TRANSLATE_NOOP("DubbingInterface", "欢快中文女声，可克隆"),
    )
