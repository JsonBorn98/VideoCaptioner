import webbrowser

from PyQt5.QtCore import Qt, QThread, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import QFileDialog, QWidget
from qfluentwidgets import (
    ComboBoxSettingCard,
    CustomColorSettingCard,
    ExpandLayout,
    HyperlinkCard,
    InfoBar,
    OptionsSettingCard,
    PrimaryPushSettingCard,
    PushSettingCard,
    ScrollArea,
    SettingCardGroup,
    SwitchSettingCard,
    TitleLabel,
    setTheme,
    setThemeColor,
)
from qfluentwidgets import FluentIcon as FIF

from videocaptioner.config import FEEDBACK_URL, HELP_URL, RELEASE_URL, VERSION
from videocaptioner.core.constant import (
    INFOBAR_DURATION_ERROR,
    INFOBAR_DURATION_SUCCESS,
    INFOBAR_DURATION_WARNING,
)
from videocaptioner.core.entities import (
    LANGUAGES,
    LLMServiceEnum,
    TranscribeModelEnum,
)
from videocaptioner.core.llm import check_whisper_connection
from videocaptioner.core.llm.check_llm import check_llm_connection, get_available_models
from videocaptioner.core.llm.check_mimo_asr import check_mimo_asr_connection
from videocaptioner.core.postprocess import PostprocessProfileStore
from videocaptioner.core.utils.cache import disable_cache, enable_cache
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.common.signal_bus import signalBus
from videocaptioner.ui.components.EditComboBoxSettingCard import EditComboBoxSettingCard
from videocaptioner.ui.components.LineEditSettingCard import LineEditSettingCard
from videocaptioner.ui.components.QwenASRSettingWidget import QwenModelDownloadDialog
from videocaptioner.ui.components.SpinBoxSettingCard import SpinBoxSettingCard
from videocaptioner.ui.components.TranslationSettingWidget import TranslationSettingWidget


class SettingInterface(ScrollArea):
    """设置界面"""

    def __init__(self, parent=None, *, translation_profile_store=None):
        super().__init__(parent=parent)
        self._translationProfileStore = translation_profile_store
        self.setWindowTitle(self.tr("设置"))
        self.scrollWidget = QWidget()
        self.expandLayout = ExpandLayout(self.scrollWidget)
        self.settingLabel = TitleLabel(self.tr("设置"), self)

        # 初始化所有设置组
        self.__initGroups()
        # 初始化所有配置卡片
        self.__initCards()
        # 初始化界面
        self.__initWidget()
        # 初始化布局
        self.__initLayout()
        # 连接信号和槽
        self.__connectSignalToSlot()

    def __initGroups(self):
        """初始化所有设置组"""
        # 转录配置组
        self.transcribeGroup = SettingCardGroup(self.tr("转录配置"), self.scrollWidget)
        # 旧通用 LLM 工具仍服务于断句、校正；翻译角色在独立分页中配置。
        self.llmGroup = SettingCardGroup(
            self.tr("通用 LLM 工具配置"), self.scrollWidget
        )
        # 翻译与优化组
        self.translateGroup = SettingCardGroup(self.tr("翻译与优化"), self.scrollWidget)
        # 字幕后处理组
        self.postprocessGroup = SettingCardGroup(
            self.tr("字幕后处理"), self.scrollWidget
        )
        # 字幕合成配置组
        self.subtitleGroup = SettingCardGroup(
            self.tr("字幕合成配置"), self.scrollWidget
        )
        # 保存配置组
        self.saveGroup = SettingCardGroup(self.tr("保存配置"), self.scrollWidget)
        # 个性化组
        self.personalGroup = SettingCardGroup(self.tr("个性化"), self.scrollWidget)
        # 关于组
        self.aboutGroup = SettingCardGroup(self.tr("关于"), self.scrollWidget)

    def __initCards(self):
        """初始化所有配置卡片"""

        # ASR 服务配置卡片
        self.__createASRServiceCards()

        # LLM配置卡片
        self.__createLLMServiceCards()

        # 三种平行翻译方式的独立配置入口
        self.translationSettingsWidget = TranslationSettingWidget(
            self.scrollWidget,
            profile_store=self._translationProfileStore,
        )

        # 翻译与优化配置卡片
        self.subtitleCorrectCard = SwitchSettingCard(
            FIF.EDIT,
            self.tr("字幕校正"),
            self.tr("字幕处理过程是否对生成的字幕错别字、名词等进行校正"),
            cfg.need_optimize,
            self.translateGroup,
        )
        self.subtitleTranslateCard = SwitchSettingCard(
            FIF.LANGUAGE,
            self.tr("字幕翻译"),
            self.tr("字幕处理过程是否对生成的字幕进行翻译"),
            cfg.need_translate,
            self.translateGroup,
        )
        self.targetLanguageCard = ComboBoxSettingCard(
            cfg.target_language,
            FIF.LANGUAGE,
            self.tr("目标语言"),
            self.tr("选择翻译字幕的目标语言"),
            texts=[lang.value for lang in cfg.target_language.validator.options],  # type: ignore
            parent=self.translateGroup,
        )

        self.speedSettingsCard = HyperlinkCard(
            "",
            self.tr("打开"),
            FIF.SPEED_HIGH,
            self.tr("字幕后处理设置"),
            self.tr("文本、速度、时间轴、语义修复、媒体增强对齐与报告集中管理"),
            self.postprocessGroup,
        )

        # 字幕合成配置卡片
        self.subtitleStyleCard = HyperlinkCard(
            "",
            self.tr("修改"),
            FIF.FONT,
            self.tr("字幕样式"),
            self.tr("选择字幕的样式（颜色、大小、字体等）"),
            self.subtitleGroup,
        )
        self.subtitleLayoutCard = HyperlinkCard(
            "",
            self.tr("修改"),
            FIF.FONT,
            self.tr("字幕布局"),
            self.tr("选择字幕的布局（单语、双语）"),
            self.subtitleGroup,
        )
        self.needVideoCard = SwitchSettingCard(
            FIF.VIDEO,
            self.tr("需要合成视频"),
            self.tr("开启时触发合成视频，关闭时跳过"),
            cfg.need_video,
            self.subtitleGroup,
        )
        self.softSubtitleCard = SwitchSettingCard(
            FIF.FONT,
            self.tr("软字幕"),
            self.tr("开启时字幕可在播放器中关闭或调整，关闭时字幕烧录到视频画面上"),
            cfg.soft_subtitle,
            self.subtitleGroup,
        )
        self.videoQualityCard = ComboBoxSettingCard(
            cfg.video_quality,
            FIF.SPEED_HIGH,
            self.tr("视频合成质量"),
            self.tr("硬字幕视频合成时的质量等级（质量越高文件越大，编码时间越长）"),
            texts=[quality.value for quality in cfg.video_quality.validator.options],  # type: ignore
            parent=self.subtitleGroup,
        )

        # 保存配置卡片
        self.savePathCard = PushSettingCard(
            self.tr("工作文件夹"),
            FIF.SAVE,
            self.tr("工作目录路径"),
            cfg.get(cfg.work_dir),
            self.saveGroup,
        )

        # 个性化配置卡片
        self.cacheEnabledCard = SwitchSettingCard(
            FIF.HISTORY,
            self.tr("启用缓存"),
            self.tr("相同配置下会复用之前的 ASR 和 LLM 结果；关闭缓存后每次重新生成"),
            cfg.cache_enabled,
            self.personalGroup,
        )
        self.themeCard = OptionsSettingCard(
            cfg.themeMode,
            FIF.BRUSH,
            self.tr("应用主题"),
            self.tr("更改应用程序的外观"),
            texts=[self.tr("浅色"), self.tr("深色"), self.tr("使用系统设置")],
            parent=self.personalGroup,
        )
        self.themeColorCard = CustomColorSettingCard(
            cfg.themeColor,
            FIF.PALETTE,
            self.tr("主题颜色"),
            self.tr("更改应用程序的主题颜色"),
            self.personalGroup,
        )
        self.zoomCard = OptionsSettingCard(
            cfg.dpiScale,
            FIF.ZOOM,
            self.tr("界面缩放"),
            self.tr("更改小部件和字体的大小"),
            texts=["100%", "125%", "150%", "175%", "200%", self.tr("使用系统设置")],
            parent=self.personalGroup,
        )
        self.languageCard = ComboBoxSettingCard(
            cfg.language,
            FIF.LANGUAGE,
            self.tr("语言"),
            self.tr("设置您偏好的界面语言"),
            texts=["简体中文", "繁體中文", "English", self.tr("使用系统设置")],
            parent=self.personalGroup,
        )

        # 关于卡片
        self.helpCard = HyperlinkCard(
            HELP_URL,
            self.tr("打开帮助页面"),
            FIF.HELP,
            self.tr("帮助"),
            self.tr("发现新功能并了解有关VideoCaptioner的使用技巧"),
            self.aboutGroup,
        )
        self.feedbackCard = PrimaryPushSettingCard(
            self.tr("提供反馈"),
            FIF.FEEDBACK,
            self.tr("提供反馈"),
            self.tr("提供反馈帮助我们改进VideoCaptioner"),
            self.aboutGroup,
        )
        self.aboutCard = PrimaryPushSettingCard(
            self.tr("查看发布版本"),
            FIF.INFO,
            self.tr("关于"),
            f"VideoCaptioner {VERSION} · GPL-3.0",
            self.aboutGroup,
        )

        # 添加卡片到对应的组
        self.translateGroup.addSettingCard(self.subtitleCorrectCard)
        self.translateGroup.addSettingCard(self.subtitleTranslateCard)
        self.translateGroup.addSettingCard(self.targetLanguageCard)

        self.postprocessGroup.addSettingCard(self.speedSettingsCard)

        self.subtitleGroup.addSettingCard(self.subtitleStyleCard)
        self.subtitleGroup.addSettingCard(self.subtitleLayoutCard)
        self.subtitleGroup.addSettingCard(self.needVideoCard)
        self.subtitleGroup.addSettingCard(self.softSubtitleCard)
        self.subtitleGroup.addSettingCard(self.videoQualityCard)

        self.saveGroup.addSettingCard(self.savePathCard)
        self.saveGroup.addSettingCard(self.cacheEnabledCard)

        self.personalGroup.addSettingCard(self.themeCard)
        self.personalGroup.addSettingCard(self.themeColorCard)
        self.personalGroup.addSettingCard(self.zoomCard)
        self.personalGroup.addSettingCard(self.languageCard)

        self.aboutGroup.addSettingCard(self.helpCard)
        self.aboutGroup.addSettingCard(self.feedbackCard)
        self.aboutGroup.addSettingCard(self.aboutCard)

    def __createLLMServiceCards(self):
        """创建LLM服务相关的配置卡片"""
        # 服务选择卡片
        self.llmServiceCard = ComboBoxSettingCard(
            cfg.llm_service,
            FIF.ROBOT,
            self.tr("LLM 提供商"),
            self.tr("用于字幕断句、字幕校正等通用工具，不决定翻译模式"),
            texts=[service.value for service in cfg.llm_service.validator.options],  # type: ignore
            parent=self.llmGroup,
        )
        self.llmServiceCard.comboBox.setMinimumWidth(150)

        # 定义每个服务的配置
        service_configs = {
            LLMServiceEnum.OPENAI: {
                "prefix": "openai",
                "api_key_cfg": cfg.openai_api_key,
                "api_base_cfg": cfg.openai_api_base,
                "model_cfg": cfg.openai_model,
                "default_base": "https://api.openai.com/v1",
                "default_models": [
                    "gemini-2.5-pro",
                    "gpt-5",
                    "claude-sonnet-4-5-20250929",
                    "gemini-2.5-flash",
                    "claude-haiku-4-5-20251001",
                ],
            },
            LLMServiceEnum.SILICON_CLOUD: {
                "prefix": "silicon_cloud",
                "api_key_cfg": cfg.silicon_cloud_api_key,
                "api_base_cfg": cfg.silicon_cloud_api_base,
                "model_cfg": cfg.silicon_cloud_model,
                "default_base": "https://api.siliconflow.cn/v1",
                "default_models": [
                    "moonshotai/Kimi-K2-Instruct-0905",
                    "deepseek-ai/DeepSeek-V3",
                ],
            },
            LLMServiceEnum.DEEPSEEK: {
                "prefix": "deepseek",
                "api_key_cfg": cfg.deepseek_api_key,
                "api_base_cfg": cfg.deepseek_api_base,
                "model_cfg": cfg.deepseek_model,
                "default_base": "https://api.deepseek.com/v1",
                "default_models": ["deepseek-chat", "deepseek-reasoner"],
            },
            LLMServiceEnum.OLLAMA: {
                "prefix": "ollama",
                "api_key_cfg": cfg.ollama_api_key,
                "api_base_cfg": cfg.ollama_api_base,
                "model_cfg": cfg.ollama_model,
                "default_base": "http://localhost:11434/v1",
                "default_models": ["qwen3:8b"],
            },
            LLMServiceEnum.LM_STUDIO: {
                "prefix": "LM Studio",
                "api_key_cfg": cfg.lm_studio_api_key,
                "api_base_cfg": cfg.lm_studio_api_base,
                "model_cfg": cfg.lm_studio_model,
                "default_base": "http://localhost:1234/v1",
                "default_models": ["qwen3:8b"],
            },
            LLMServiceEnum.GEMINI: {
                "prefix": "gemini",
                "api_key_cfg": cfg.gemini_api_key,
                "api_base_cfg": cfg.gemini_api_base,
                "model_cfg": cfg.gemini_model,
                "default_base": "https://generativelanguage.googleapis.com/v1beta/openai/",
                "default_models": [
                    "gemini-2.5-pro",
                    "gemini-2.5-flash",
                    "gemini-2.0-flash-lite",
                ],
            },
            LLMServiceEnum.CHATGLM: {
                "prefix": "chatglm",
                "api_key_cfg": cfg.chatglm_api_key,
                "api_base_cfg": cfg.chatglm_api_base,
                "model_cfg": cfg.chatglm_model,
                "default_base": "https://open.bigmodel.cn/api/paas/v4",
                "default_models": ["glm-4-plus", "glm-4-air-250414", "glm-4-flash"],
            },
        }

        # 创建服务配置映射
        self.llm_service_configs = {}

        # 为每个服务创建配置卡片
        for service, config in service_configs.items():
            prefix = config["prefix"]

            # 创建API Key卡片
            api_key_card = LineEditSettingCard(
                config["api_key_cfg"],
                FIF.FINGERPRINT,
                self.tr("API Key"),
                self.tr(f"输入您的 {service.value} API Key"),
                "sk-" if service != LLMServiceEnum.OLLAMA else "",
                self.llmGroup,
            )
            setattr(self, f"{prefix}_api_key_card", api_key_card)

            # 创建Base URL卡片
            api_base_card = LineEditSettingCard(
                config["api_base_cfg"],
                FIF.LINK,
                self.tr("Base URL"),
                self.tr(f"输入 {service.value} Base URL"),
                config["default_base"],
                self.llmGroup,
            )
            setattr(self, f"{prefix}_api_base_card", api_base_card)

            # 设置只读状态：只有 OpenAI、Ollama、LM Studio 可以编辑 Base URL
            if service not in [
                LLMServiceEnum.OPENAI,
                LLMServiceEnum.OLLAMA,
                LLMServiceEnum.LM_STUDIO,
            ]:
                api_base_card.lineEdit.setReadOnly(True)

            # 创建模型选择卡片
            model_card = EditComboBoxSettingCard(
                config["model_cfg"],
                FIF.ROBOT,  # type: ignore
                self.tr("模型"),
                self.tr(f"选择 {service.value} 模型"),
                config["default_models"],
                self.llmGroup,
            )
            setattr(self, f"{prefix}_model_card", model_card)

            # 存储服务配置
            cards = [api_key_card, api_base_card, model_card]

            self.llm_service_configs[service] = {
                "cards": cards,
                "api_base": api_base_card,
                "api_key": api_key_card,
                "model": model_card,
            }

        # 创建检查连接卡片
        self.checkLLMConnectionCard = PushSettingCard(
            self.tr("检查连接"),
            FIF.LINK,
            self.tr("检查 LLM 连接"),
            self.tr("点击检查 API 连接是否正常，并获取模型列表"),
            self.llmGroup,
        )

        # 初始化显示状态
        self.__onLLMServiceChanged(self.llmServiceCard.comboBox.currentText())

    def __createASRServiceCards(self):
        """创建 Whisper API 配置卡片"""
        # 转录配置卡片
        self.transcribeModelCard = ComboBoxSettingCard(
            cfg.transcribe_model,
            FIF.MICROPHONE,
            self.tr("转录模型"),
            self.tr("语音转换文字要使用的语音识别服务"),
            texts=[model.value for model in cfg.transcribe_model.validator.options],  # type: ignore
            parent=self.transcribeGroup,
        )
        self.transcribeModelCard.comboBox.setMinimumWidth(150)
        self.audioLoudnormCard = SwitchSettingCard(
            FIF.VOLUME,
            self.tr("音量标准化"),
            self.tr("抽取音频时使用 EBU R128 loudnorm，适合音量忽大忽小的素材"),
            cfg.audio_loudnorm,
            self.transcribeGroup,
        )

        # API Base URL
        self.whisperApiBaseCard = LineEditSettingCard(
            cfg.whisper_api_base,
            FIF.LINK,
            self.tr("Whisper API Base URL"),
            self.tr("输入 Whisper API Base URL"),
            "https://api.openai.com/v1",
            self.transcribeGroup,
        )

        # API Key
        self.whisperApiKeyCard = LineEditSettingCard(
            cfg.whisper_api_key,
            FIF.FINGERPRINT,
            self.tr("Whisper API Key"),
            self.tr("输入 Whisper API Key"),
            "sk-",
            self.transcribeGroup,
        )

        # 模型选择
        self.whisperApiModelCard = EditComboBoxSettingCard(
            cfg.whisper_api_model,
            FIF.ROBOT,  # type: ignore
            self.tr("Whisper 模型"),
            self.tr("选择 Whisper 模型"),
            [
                "whisper-1",
                "whisper-large-v3-turbo",
            ],
            self.transcribeGroup,
        )

        # 测试连接按钮
        self.checkWhisperConnectionCard = PushSettingCard(
            self.tr("测试 Whisper 连接"),
            FIF.CONNECT,
            self.tr("测试 Whisper API 连接"),
            self.tr("点击测试 API 连接是否正常"),
            self.transcribeGroup,
        )

        self.mimoAsrBaseCard = LineEditSettingCard(
            cfg.mimo_asr_api_base,
            FIF.LINK,
            self.tr("MiMo ASR API Base URL"),
            self.tr("输入 MiMo ASR API Base URL"),
            "https://api.xiaomimimo.com/v1",
            self.transcribeGroup,
        )
        self.mimoAsrKeyCard = LineEditSettingCard(
            cfg.mimo_asr_api_key,
            FIF.FINGERPRINT,
            self.tr("MiMo ASR API Key"),
            self.tr("输入 MiMo API Key"),
            "sk-",
            self.transcribeGroup,
        )
        self.mimoAsrModelCard = EditComboBoxSettingCard(
            cfg.mimo_asr_model,
            FIF.ROBOT,  # type: ignore
            self.tr("MiMo ASR 模型"),
            self.tr("选择 MiMo ASR 模型"),
            ["mimo-v2.5-asr"],
            self.transcribeGroup,
        )
        self.mimoAsrTimeoutCard = SpinBoxSettingCard(
            cfg.mimo_asr_timeout,
            FIF.SPEED_HIGH,  # type: ignore
            self.tr("超时时间"),
            self.tr("长音频分块同步等待秒数"),
            30,
            7200,
            self.transcribeGroup,
        )
        self.mimoAsrConcurrencyCard = SpinBoxSettingCard(
            cfg.mimo_asr_concurrency,
            FIF.ALIGNMENT,  # type: ignore
            self.tr("并发请求数"),
            self.tr("同时请求 MiMo API 的分块数；遇到 429 限流请调低（默认 2）"),
            1,
            8,
            self.transcribeGroup,
        )
        self.checkMimoAsrConnectionCard = PushSettingCard(
            self.tr("测试 MiMo ASR 连接"),
            FIF.CONNECT,
            self.tr("测试 MiMo ASR API 连接"),
            self.tr("点击测试 API 连接是否正常"),
            self.transcribeGroup,
        )

        self.qwenAsrModelCard = ComboBoxSettingCard(
            cfg.qwen_asr_model,
            FIF.ROBOT,
            self.tr("Qwen3 ASR 模型"),
            self.tr("选择本地 Qwen3 ASR 模型"),
            ["Qwen/Qwen3-ASR-1.7B", "Qwen/Qwen3-ASR-0.6B"],
            self.transcribeGroup,
        )
        self.qwenAlignerModelCard = ComboBoxSettingCard(
            cfg.qwen_aligner_model,
            FIF.SYNC,
            self.tr("Qwen3 对齐模型"),
            self.tr("选择 Qwen3-ForcedAligner 模型"),
            ["Qwen/Qwen3-ForcedAligner-0.6B"],
            self.transcribeGroup,
        )
        self.qwenModelDirCard = LineEditSettingCard(
            cfg.qwen_model_dir,
            FIF.FOLDER,
            self.tr("Qwen 模型目录"),
            self.tr("本地模型目录；已下载模型会优先从这里加载"),
            "",
            self.transcribeGroup,
        )
        self.qwenDeviceCard = ComboBoxSettingCard(
            cfg.qwen_device,
            FIF.IOT,
            self.tr("Qwen 运行设备"),
            self.tr("auto / cuda:0 / cpu"),
            ["auto", "cuda:0", "cpu"],
            self.transcribeGroup,
        )
        self.qwenDTypeCard = ComboBoxSettingCard(
            cfg.qwen_dtype,
            FIF.SPEED_HIGH,  # type: ignore
            self.tr("Qwen 计算精度"),
            self.tr("auto / bfloat16 / float16 / float32"),
            ["auto", "bfloat16", "float16", "float32"],
            self.transcribeGroup,
        )
        self.qwenMaxTokensCard = SpinBoxSettingCard(
            cfg.qwen_max_new_tokens,
            FIF.CODE,  # type: ignore
            self.tr("Qwen 最大输出 Tokens"),
            self.tr("长音频分块转写时的最大生成长度"),
            64,
            8192,
            self.transcribeGroup,
        )
        self.qwenChunkOverlapCard = SpinBoxSettingCard(
            cfg.qwen_chunk_overlap_seconds,
            FIF.ALIGNMENT,  # type: ignore
            self.tr("分块重叠秒数"),
            self.tr("相邻 5 分钟音频块的重叠时长，减少切分点漏词"),
            0,
            60,
            self.transcribeGroup,
        )
        self.qwenCompileAlignerCard = SwitchSettingCard(
            FIF.SPEED_HIGH,  # type: ignore
            self.tr("实验性编译对齐模型"),
            self.tr("尝试使用 torch.compile 加速 ForcedAligner，失败时自动回退"),
            cfg.qwen_compile_aligner,
            self.transcribeGroup,
        )
        self.manageQwenModelCard = PushSettingCard(
            self.tr("管理 Qwen 模型"),
            FIF.DOWNLOAD,
            self.tr("Qwen 模型管理"),
            self.tr("下载或更新 Qwen3 ASR / ForcedAligner 模型"),
            self.transcribeGroup,
        )

        # 默认隐藏 Whisper API 配置卡片（仅在选择 Whisper API 时显示）
        self.whisperApiBaseCard.setVisible(False)
        self.whisperApiKeyCard.setVisible(False)
        self.whisperApiModelCard.setVisible(False)
        self.checkWhisperConnectionCard.setVisible(False)

        for card in [
            self.mimoAsrBaseCard,
            self.mimoAsrKeyCard,
            self.mimoAsrModelCard,
            self.mimoAsrTimeoutCard,
            self.mimoAsrConcurrencyCard,
            self.checkMimoAsrConnectionCard,
        ]:
            card.setVisible(False)

        for card in [
            self.qwenAsrModelCard,
            self.qwenAlignerModelCard,
            self.qwenModelDirCard,
            self.qwenDeviceCard,
            self.qwenDTypeCard,
            self.qwenMaxTokensCard,
            self.qwenChunkOverlapCard,
            self.qwenCompileAlignerCard,
            self.manageQwenModelCard,
        ]:
            card.setVisible(False)

    def __initWidget(self):
        self.resize(1000, 800)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)  # type: ignore
        self.setViewportMargins(0, 80, 0, 20)
        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        self.setObjectName("settingInterface")

        # 初始化样式表
        self.scrollWidget.setObjectName("scrollWidget")
        # 初始化转录模型配置卡片的显示状态
        self.__onTranscribeModelChanged(self.transcribeModelCard.comboBox.currentText())

        self.setStyleSheet(
            """
            SettingInterface, #scrollWidget {
                background-color: transparent;
            }
            QScrollArea {
                border: none;
                background-color: transparent;
            }
        """
        )

    def __initLayout(self):
        """初始化布局"""
        self.settingLabel.move(36, 30)

        # 添加转录配置卡片
        self.transcribeGroup.addSettingCard(self.transcribeModelCard)
        self.transcribeGroup.addSettingCard(self.audioLoudnormCard)
        # 添加 Whisper API 配置卡片
        self.transcribeGroup.addSettingCard(self.whisperApiBaseCard)
        self.transcribeGroup.addSettingCard(self.whisperApiKeyCard)
        self.transcribeGroup.addSettingCard(self.whisperApiModelCard)
        self.transcribeGroup.addSettingCard(self.checkWhisperConnectionCard)
        self.transcribeGroup.addSettingCard(self.mimoAsrBaseCard)
        self.transcribeGroup.addSettingCard(self.mimoAsrKeyCard)
        self.transcribeGroup.addSettingCard(self.mimoAsrModelCard)
        self.transcribeGroup.addSettingCard(self.mimoAsrTimeoutCard)
        self.transcribeGroup.addSettingCard(self.mimoAsrConcurrencyCard)
        self.transcribeGroup.addSettingCard(self.checkMimoAsrConnectionCard)
        self.transcribeGroup.addSettingCard(self.qwenAsrModelCard)
        self.transcribeGroup.addSettingCard(self.qwenAlignerModelCard)
        self.transcribeGroup.addSettingCard(self.qwenModelDirCard)
        self.transcribeGroup.addSettingCard(self.qwenDeviceCard)
        self.transcribeGroup.addSettingCard(self.qwenDTypeCard)
        self.transcribeGroup.addSettingCard(self.qwenMaxTokensCard)
        self.transcribeGroup.addSettingCard(self.qwenChunkOverlapCard)
        self.transcribeGroup.addSettingCard(self.qwenCompileAlignerCard)
        self.transcribeGroup.addSettingCard(self.manageQwenModelCard)

        # 添加LLM配置卡片
        self.llmGroup.addSettingCard(self.llmServiceCard)
        for config in self.llm_service_configs.values():
            for card in config["cards"]:
                self.llmGroup.addSettingCard(card)
        self.llmGroup.addSettingCard(self.checkLLMConnectionCard)

        # 将所有组添加到布局
        self.expandLayout.setSpacing(28)
        self.expandLayout.setContentsMargins(36, 10, 36, 0)
        self.expandLayout.addWidget(self.transcribeGroup)
        self.expandLayout.addWidget(self.llmGroup)
        self.expandLayout.addWidget(self.translationSettingsWidget)
        self.expandLayout.addWidget(self.translateGroup)
        self.expandLayout.addWidget(self.postprocessGroup)
        self.expandLayout.addWidget(self.subtitleGroup)
        self.expandLayout.addWidget(self.saveGroup)
        self.expandLayout.addWidget(self.personalGroup)
        self.expandLayout.addWidget(self.aboutGroup)

    def __connectSignalToSlot(self):
        """连接信号与槽"""
        cfg.appRestartSig.connect(self.__showRestartTooltip)

        # LLM服务切换
        self.llmServiceCard.comboBox.currentTextChanged.connect(
            self.__onLLMServiceChanged
        )

        # 转录模型切换
        self.transcribeModelCard.comboBox.currentTextChanged.connect(
            self.__onTranscribeModelChanged
        )

        # 检查 LLM 连接
        self.checkLLMConnectionCard.clicked.connect(self.checkLLMConnection)

        # 检查 Whisper 连接
        self.checkWhisperConnectionCard.clicked.connect(self.checkWhisperConnection)
        self.checkMimoAsrConnectionCard.clicked.connect(self.checkMimoAsrConnection)
        self.manageQwenModelCard.clicked.connect(self.showQwenModelManager)

        # 保存路径
        self.savePathCard.clicked.connect(self.__onsavePathCardClicked)

        # 字幕样式修改跳转
        self.subtitleStyleCard.linkButton.clicked.connect(
            lambda: self.window().switchTo(self.window().subtitleStyleInterface)  # type: ignore
        )
        self.subtitleLayoutCard.linkButton.clicked.connect(
            lambda: self.window().switchTo(self.window().subtitleStyleInterface)  # type: ignore
        )
        self.speedSettingsCard.linkButton.clicked.connect(
            lambda: self.window().switchTo(self.window().postprocessSettingInterface)  # type: ignore
        )
        for item in (
            cfg.postprocess_enabled,
            cfg.postprocess_profile,
            cfg.speed_mode,
            cfg.speed_primary,
        ):
            item.valueChanged.connect(self.__updateSpeedSettingsSummary)
        self.__updateSpeedSettingsSummary()

        # 个性化
        self.cacheEnabledCard.checkedChanged.connect(self.__onCacheEnabledChanged)
        self.themeCard.optionChanged.connect(lambda ci: setTheme(cfg.get(ci)))
        self.themeColorCard.colorChanged.connect(setThemeColor)

        # 反馈
        self.feedbackCard.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(FEEDBACK_URL))  # type: ignore
        )

        # 关于
        self.aboutCard.clicked.connect(self.checkUpdate)

        # 全局 signalBus
        self.transcribeModelCard.comboBox.currentTextChanged.connect(
            signalBus.transcription_model_changed
        )
        self.subtitleCorrectCard.checkedChanged.connect(
            signalBus.subtitle_optimization_changed
        )
        self.subtitleTranslateCard.checkedChanged.connect(
            signalBus.subtitle_translation_changed
        )
        self.targetLanguageCard.comboBox.currentTextChanged.connect(
            signalBus.target_language_changed
        )
        self.softSubtitleCard.checkedChanged.connect(signalBus.soft_subtitle_changed)
        self.needVideoCard.checkedChanged.connect(signalBus.need_video_changed)
        self.videoQualityCard.comboBox.currentTextChanged.connect(
            signalBus.video_quality_changed
        )

    def __showRestartTooltip(self):
        """显示重启提示"""
        InfoBar.success(
            self.tr("更新成功"),
            self.tr("配置将在重启后生效"),
            duration=INFOBAR_DURATION_SUCCESS,
            parent=self,
        )

    def __updateSpeedSettingsSummary(self, _value=None):
        profile_id = cfg.get(cfg.postprocess_profile)
        try:
            profile_name = PostprocessProfileStore().get(profile_id).name
        except Exception:
            profile_name = profile_id
        modeNames = {"apply": self.tr("应用"), "analyze": self.tr("仅分析")}
        primaryNames = {
            "translate": self.tr("译文主侧"),
            "layout": self.tr("布局主侧"),
            "original": self.tr("原文主侧"),
        }
        summary = " · ".join(
            (
                self.tr("默认启用") if cfg.get(cfg.postprocess_enabled) else self.tr("默认跳过"),
                profile_name,
                modeNames.get(cfg.get(cfg.speed_mode), ""),
                primaryNames.get(cfg.get(cfg.speed_primary), ""),
            )
        )
        self.speedSettingsCard.setContent(summary)

    def __onsavePathCardClicked(self):
        """处理保存路径卡片点击事件"""
        folder = QFileDialog.getExistingDirectory(self, self.tr("选择文件夹"), "./")
        if not folder or cfg.get(cfg.work_dir) == folder:
            return
        cfg.set(cfg.work_dir, folder)
        self.savePathCard.setContent(folder)

    def __onCacheEnabledChanged(self, is_enabled: bool):
        """处理缓存开关变化"""
        if is_enabled:
            enable_cache()
            InfoBar.success(
                self.tr("缓存已启用"),
                self.tr("ASR、翻译等操作将优先使用缓存"),
                duration=INFOBAR_DURATION_SUCCESS,
                parent=self,
            )
        else:
            disable_cache()
            InfoBar.warning(
                self.tr("缓存已禁用"),
                self.tr("所有操作将重新生成，不使用缓存（建议开启缓存）"),
                duration=INFOBAR_DURATION_WARNING,
                parent=self,
            )

    def checkLLMConnection(self):
        """检查 LLM 连接"""
        # 保存当前滚动位置
        scroll_position = self.verticalScrollBar().value()

        # 获取当前选中的服务
        current_service = LLMServiceEnum(self.llmServiceCard.comboBox.currentText())

        # 获取服务配置
        service_config = self.llm_service_configs.get(current_service)
        if not service_config:
            return

        api_base = (
            service_config["api_base"].lineEdit.text()
            if service_config["api_base"]
            else ""
        )
        api_key = (
            service_config["api_key"].lineEdit.text()
            if service_config["api_key"]
            else ""
        )
        model = (
            service_config["model"].comboBox.currentText()
            if service_config["model"]
            else ""
        )

        # 禁用检查按钮，显示加载状态
        self.checkLLMConnectionCard.button.setEnabled(False)
        self.checkLLMConnectionCard.button.setText(self.tr("正在检查..."))

        # 立即恢复滚动位置（防止按钮状态改变导致的自动滚动）
        self.verticalScrollBar().setValue(scroll_position)

        # 创建并启动线程
        self.connection_thread = LLMConnectionThread(api_base, api_key, model)
        self.connection_thread.finished.connect(self.onConnectionCheckFinished)
        self.connection_thread.error.connect(self.onConnectionCheckError)
        self.connection_thread.start()

    def onConnectionCheckError(self, message):
        """处理连接检查错误事件"""
        self.checkLLMConnectionCard.button.setEnabled(True)
        self.checkLLMConnectionCard.button.setText(self.tr("检查连接"))
        InfoBar.error(
            self.tr("LLM 连接测试错误"),
            message,
            duration=INFOBAR_DURATION_ERROR,
            parent=self,
        )

    def onConnectionCheckFinished(self, is_success, message, models):
        """处理连接检查完成事件"""
        self.checkLLMConnectionCard.button.setEnabled(True)
        self.checkLLMConnectionCard.button.setText(self.tr("检查连接"))

        # 获取当前服务
        current_service = LLMServiceEnum(self.llmServiceCard.comboBox.currentText())

        if models:
            # 更新当前服务的模型列表
            service_config = self.llm_service_configs.get(current_service)
            if service_config and service_config["model"]:
                temp = service_config["model"].comboBox.currentText()
                service_config["model"].setItems(models)
                service_config["model"].comboBox.setCurrentText(temp)

            InfoBar.success(
                self.tr("获取模型列表成功:"),
                self.tr("一共") + str(len(models)) + self.tr("个模型"),
                duration=INFOBAR_DURATION_SUCCESS,
                parent=self,
            )
        if not is_success:
            InfoBar.error(
                self.tr("LLM 连接测试错误"),
                message,
                duration=INFOBAR_DURATION_ERROR,
                parent=self,
            )
        else:
            InfoBar.success(
                self.tr("LLM 连接测试成功"),
                message,
                duration=INFOBAR_DURATION_SUCCESS,
                parent=self,
            )

    def checkUpdate(self):
        webbrowser.open(RELEASE_URL)

    def __onLLMServiceChanged(self, service):
        """处理LLM服务切换事件"""
        current_service = LLMServiceEnum(service)

        # 隐藏所有卡片
        for config in self.llm_service_configs.values():
            for card in config["cards"]:
                card.setVisible(False)

        # 显示选中服务的卡片
        if current_service in self.llm_service_configs:
            for card in self.llm_service_configs[current_service]["cards"]:
                card.setVisible(True)

            # 为OLLAMA和LM_STUDIO设置默认API Key
            service_config = self.llm_service_configs[current_service]
            if current_service == LLMServiceEnum.OLLAMA and service_config["api_key"]:
                # 如果API Key为空，设置默认值"ollama"
                if not service_config["api_key"].lineEdit.text():
                    service_config["api_key"].lineEdit.setText("ollama")
            if (
                current_service == LLMServiceEnum.LM_STUDIO
                and service_config["api_key"]
            ):
                # 如果API Key为空，设置默认值 "lm-studio"
                if not service_config["api_key"].lineEdit.text():
                    service_config["api_key"].lineEdit.setText("lm-studio")

        # 更新布局
        self.llmGroup.adjustSize()
        self.expandLayout.update()

    def __onTranscribeModelChanged(self, model_name):
        """处理转录模型切换事件"""
        # Whisper API 配置卡片
        whisper_api_cards = [
            self.whisperApiBaseCard,
            self.whisperApiKeyCard,
            self.whisperApiModelCard,
            self.checkWhisperConnectionCard,
        ]
        mimo_asr_cards = [
            self.mimoAsrBaseCard,
            self.mimoAsrKeyCard,
            self.mimoAsrModelCard,
            self.mimoAsrTimeoutCard,
            self.mimoAsrConcurrencyCard,
            self.checkMimoAsrConnectionCard,
        ]
        qwen_local_only_cards = [self.qwenAsrModelCard, self.qwenMaxTokensCard]
        qwen_aligner_cards = [
            self.qwenAlignerModelCard,
            self.qwenModelDirCard,
            self.qwenDeviceCard,
            self.qwenDTypeCard,
            self.qwenChunkOverlapCard,
            self.qwenCompileAlignerCard,
            self.manageQwenModelCard,
        ]

        # 根据选择的模型显示/隐藏 Whisper API 配置
        is_whisper_api = model_name == TranscribeModelEnum.WHISPER_API.value
        is_mimo_asr = model_name == TranscribeModelEnum.MIMO_ASR_API.value
        is_qwen_asr = model_name == TranscribeModelEnum.QWEN_LOCAL_ASR.value
        for card in whisper_api_cards:
            card.setVisible(is_whisper_api)
        for card in mimo_asr_cards:
            card.setVisible(is_mimo_asr)
        for card in qwen_local_only_cards:
            card.setVisible(is_qwen_asr)
        for card in qwen_aligner_cards:
            card.setVisible(is_qwen_asr or is_mimo_asr)

        # 更新布局
        self.transcribeGroup.adjustSize()
        self.expandLayout.update()

    def showQwenModelManager(self):
        """显示 Qwen 模型管理对话框"""
        dialog = QwenModelDownloadDialog(self.window())
        dialog.exec_()

    def checkMimoAsrConnection(self):
        """检查 MiMo ASR API 连接"""
        base_url = self.mimoAsrBaseCard.lineEdit.text().strip()
        api_key = self.mimoAsrKeyCard.lineEdit.text().strip()
        model = self.mimoAsrModelCard.comboBox.currentText().strip()

        if not base_url or not api_key or not model:
            InfoBar.warning(
                self.tr("配置不完整"),
                self.tr("请输入 API Base URL、API Key 和模型"),
                duration=INFOBAR_DURATION_ERROR,
                parent=self,
            )
            return

        self.checkMimoAsrConnectionCard.button.setEnabled(False)
        self.checkMimoAsrConnectionCard.button.setText(self.tr("正在测试..."))

        language = LANGUAGES[cfg.transcribe_language.value.value]
        self.mimo_asr_connection_thread = MimoASRConnectionThread(
            base_url=base_url,
            api_key=api_key,
            model=model,
            language=language,
            timeout=self.mimoAsrTimeoutCard.spinBox.value(),
        )
        self.mimo_asr_connection_thread.result_ready.connect(
            self.onMimoAsrConnectionCheckFinished
        )
        self.mimo_asr_connection_thread.error.connect(
            self.onMimoAsrConnectionCheckError
        )
        self.mimo_asr_connection_thread.start()

    def onMimoAsrConnectionCheckFinished(self, success, result):
        """处理 MiMo ASR 连接检查完成事件"""
        self.checkMimoAsrConnectionCard.button.setEnabled(True)
        self.checkMimoAsrConnectionCard.button.setText(
            self.tr("测试 MiMo ASR 连接")
        )

        if success:
            InfoBar.success(
                self.tr("连接成功"),
                self.tr("MiMo ASR API 连接成功！\n转录结果:") + result,
                duration=INFOBAR_DURATION_SUCCESS,
                parent=self,
            )
        else:
            InfoBar.error(
                self.tr("连接失败"),
                self.tr(f"MiMo ASR API 连接失败！\n{result}"),
                duration=INFOBAR_DURATION_ERROR,
                parent=self,
            )

    def onMimoAsrConnectionCheckError(self, message):
        """处理 MiMo ASR 连接检查错误事件"""
        self.checkMimoAsrConnectionCard.button.setEnabled(True)
        self.checkMimoAsrConnectionCard.button.setText(
            self.tr("测试 MiMo ASR 连接")
        )

        InfoBar.error(
            self.tr("测试错误"),
            message,
            duration=INFOBAR_DURATION_ERROR,
            parent=self,
        )

    def checkWhisperConnection(self):
        """检查 Whisper API 连接"""
        # 保存当前滚动位置
        scroll_position = self.verticalScrollBar().value()

        # 获取配置
        base_url = self.whisperApiBaseCard.lineEdit.text().strip()
        api_key = self.whisperApiKeyCard.lineEdit.text().strip()
        model = self.whisperApiModelCard.comboBox.currentText().strip()

        # 验证必填字段
        if not base_url:
            InfoBar.warning(
                self.tr("配置不完整"),
                self.tr("请输入 Whisper API Base URL"),
                duration=INFOBAR_DURATION_ERROR,
                parent=self,
            )
            return

        if not api_key:
            InfoBar.warning(
                self.tr("配置不完整"),
                self.tr("请输入 Whisper API Key"),
                duration=INFOBAR_DURATION_ERROR,
                parent=self,
            )
            return

        if not model:
            InfoBar.warning(
                self.tr("配置不完整"),
                self.tr("请输入 Whisper 模型名称"),
                duration=INFOBAR_DURATION_ERROR,
                parent=self,
            )
            return

        # 禁用按钮，显示加载状态
        self.checkWhisperConnectionCard.button.setEnabled(False)
        self.checkWhisperConnectionCard.button.setText(self.tr("正在测试..."))

        # 立即恢复滚动位置（防止按钮状态改变导致的自动滚动）
        self.verticalScrollBar().setValue(scroll_position)

        # 创建并启动测试线程
        self.whisper_connection_thread = WhisperConnectionThread(
            base_url, api_key, model
        )
        self.whisper_connection_thread.finished.connect(
            self.onWhisperConnectionCheckFinished
        )
        self.whisper_connection_thread.error.connect(self.onWhisperConnectionCheckError)
        self.whisper_connection_thread.start()

    def onWhisperConnectionCheckFinished(self, success, result):
        """处理 Whisper 连接检查完成事件"""
        # 恢复按钮状态
        self.checkWhisperConnectionCard.button.setEnabled(True)
        self.checkWhisperConnectionCard.button.setText(self.tr("测试 Whisper 连接"))

        if success:
            InfoBar.success(
                self.tr("连接成功"),
                self.tr("Whisper API 连接成功！\n转录结果:") + result,
                duration=INFOBAR_DURATION_SUCCESS,
                parent=self,
            )
        else:
            InfoBar.error(
                self.tr("连接失败"),
                self.tr(f"Whisper API 连接失败！\n{result}"),
                duration=INFOBAR_DURATION_ERROR,
                parent=self,
            )

    def onWhisperConnectionCheckError(self, message):
        """处理 Whisper 连接检查错误事件"""
        # 恢复按钮状态
        self.checkWhisperConnectionCard.button.setEnabled(True)
        self.checkWhisperConnectionCard.button.setText(self.tr("测试 Whisper 连接"))

        InfoBar.error(
            self.tr("测试错误"),
            message,
            duration=INFOBAR_DURATION_ERROR,
            parent=self,
        )


class MimoASRConnectionThread(QThread):
    """MiMo ASR API 连接测试线程"""

    result_ready = pyqtSignal(bool, str)
    error = pyqtSignal(str)

    def __init__(self, base_url, api_key, model, language, timeout):
        super().__init__()
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.language = language
        self.timeout = timeout

    def run(self):
        try:
            success, result = check_mimo_asr_connection(
                base_url=self.base_url,
                api_key=self.api_key,
                model=self.model,
                language=self.language,
                timeout=self.timeout,
            )
            self.result_ready.emit(success, result or "")
        except Exception as e:
            self.error.emit(str(e))


class WhisperConnectionThread(QThread):
    """Whisper API 连接测试线程"""

    finished = pyqtSignal(bool, str)
    error = pyqtSignal(str)

    def __init__(self, base_url, api_key, model):
        super().__init__()
        self.base_url = base_url
        self.api_key = api_key
        self.model = model

    def run(self):
        """执行连接测试"""
        try:
            success, result = check_whisper_connection(
                self.base_url, self.api_key, self.model
            )
            self.finished.emit(success, result)
        except Exception as e:
            self.error.emit(str(e))


class LLMConnectionThread(QThread):
    finished = pyqtSignal(bool, str, list)
    error = pyqtSignal(str)

    def __init__(self, api_base, api_key, model):
        super().__init__()
        self.api_base = api_base
        self.api_key = api_key
        self.model = model

    def run(self):
        """检查 LLM 连接并获取模型列表"""
        try:
            is_success, message = check_llm_connection(
                self.api_base, self.api_key, self.model
            )
            models = get_available_models(self.api_base, self.api_key)
            self.finished.emit(is_success, message, models)
        except Exception as e:
            self.error.emit(str(e))
