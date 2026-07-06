from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import (
    ComboBoxSettingCard,
    HyperlinkCard,
    InfoBar,
    InfoBarPosition,
    PushSettingCard,
    SettingCardGroup,
    SingleDirectionScrollArea,
    SwitchSettingCard,
)
from qfluentwidgets import FluentIcon as FIF

from videocaptioner.core.constant import INFOBAR_DURATION_ERROR, INFOBAR_DURATION_SUCCESS
from videocaptioner.core.entities import LANGUAGES
from videocaptioner.core.llm.check_mimo_asr import check_mimo_asr_connection
from videocaptioner.core.utils.logger import setup_logger

from ..common.config import cfg
from .EditComboBoxSettingCard import EditComboBoxSettingCard
from .LineEditSettingCard import LineEditSettingCard
from .QwenASRSettingWidget import QwenModelDownloadDialog
from .SpinBoxSettingCard import SpinBoxSettingCard

logger = setup_logger("mimo_asr_settings")


class MimoASRSettingWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_ui()

    def setup_ui(self):
        self.main_layout = QVBoxLayout(self)

        self.scrollArea = SingleDirectionScrollArea(orient=Qt.Vertical, parent=self)  # type: ignore
        self.scrollArea.setStyleSheet("QScrollArea{background: transparent; border: none}")

        self.container = QWidget(self)
        self.container.setStyleSheet("QWidget{background: transparent}")
        self.containerLayout = QVBoxLayout(self.container)

        self.setting_group = SettingCardGroup(self.tr("MiMo ASR API 设置"), self)
        self.aligner_group = SettingCardGroup(self.tr("本地 Qwen3 对齐设置"), self)

        self.base_url_card = LineEditSettingCard(
            cfg.mimo_asr_api_base,
            FIF.LINK,
            self.tr("API Base URL"),
            self.tr("输入 MiMo ASR API Base URL"),
            "https://api.xiaomimimo.com/v1",
            self.setting_group,
        )

        self.api_key_card = LineEditSettingCard(
            cfg.mimo_asr_api_key,
            FIF.FINGERPRINT,
            self.tr("API Key"),
            self.tr("输入 MiMo API Key"),
            "sk-",
            self.setting_group,
        )

        self.model_card = EditComboBoxSettingCard(
            cfg.mimo_asr_model,
            FIF.ROBOT,  # type: ignore
            self.tr("模型"),
            self.tr("选择 MiMo ASR 模型"),
            ["mimo-v2.5-asr"],
            self.setting_group,
        )

        self.timeout_card = SpinBoxSettingCard(
            cfg.mimo_asr_timeout,
            FIF.SPEED_HIGH,  # type: ignore
            self.tr("超时时间"),
            self.tr("长音频分块同步等待秒数"),
            30,
            7200,
            self.setting_group,
        )

        self.concurrency_card = SpinBoxSettingCard(
            cfg.mimo_asr_concurrency,
            FIF.ALIGNMENT,  # type: ignore
            self.tr("并发请求数"),
            self.tr("同时请求 MiMo API 的分块数；遇到 429 限流请调低（默认 2）"),
            1,
            8,
            self.setting_group,
        )

        self.chunk_overlap_card = SpinBoxSettingCard(
            cfg.qwen_chunk_overlap_seconds,
            FIF.ALIGNMENT,  # type: ignore
            self.tr("分块重叠秒数"),
            self.tr("相邻音频块的重叠时长，减少切分点漏词"),
            0,
            60,
            self.setting_group,
        )

        self.check_connection_card = PushSettingCard(
            self.tr("测试连接"),
            FIF.CONNECT,
            self.tr("测试 MiMo ASR API 连接"),
            self.tr("点击测试 API 连接是否正常"),
            self.setting_group,
        )

        self.aligner_model_card = ComboBoxSettingCard(
            cfg.qwen_aligner_model,
            FIF.SYNC,
            self.tr("对齐模型"),
            self.tr("MiMo 只返回纯文本，开启断句时使用该模型生成字/词时间戳"),
            ["Qwen/Qwen3-ForcedAligner-0.6B"],
            self.aligner_group,
        )

        self.aligner_model_dir_card = LineEditSettingCard(
            cfg.qwen_model_dir,
            FIF.FOLDER,
            self.tr("模型目录"),
            self.tr("本地 ForcedAligner 模型目录"),
            "",
            self.aligner_group,
        )

        self.aligner_device_card = ComboBoxSettingCard(
            cfg.qwen_device,
            FIF.IOT,
            self.tr("运行设备"),
            self.tr("auto / cuda:0 / cpu"),
            ["auto", "cuda:0", "cpu"],
            self.aligner_group,
        )

        self.aligner_dtype_card = ComboBoxSettingCard(
            cfg.qwen_dtype,
            FIF.SPEED_HIGH,  # type: ignore
            self.tr("计算精度"),
            self.tr("auto / bfloat16 / float16 / float32"),
            ["auto", "bfloat16", "float16", "float32"],
            self.aligner_group,
        )

        self.manage_model_card = HyperlinkCard(
            "",
            self.tr("管理模型"),
            FIF.DOWNLOAD,
            self.tr("模型管理"),
            self.tr("下载或更新 Qwen3-ForcedAligner"),
            self.aligner_group,
        )

        self.aligner_compile_card = SwitchSettingCard(
            FIF.SPEED_HIGH,  # type: ignore
            self.tr("实验性编译对齐模型"),
            self.tr("尝试使用 torch.compile 加速 ForcedAligner，失败时自动回退"),
            cfg.qwen_compile_aligner,
            self.aligner_group,
        )

        for card in [
            self.base_url_card,
            self.api_key_card,
            self.model_card,
            self.aligner_model_card,
            self.aligner_model_dir_card,
            self.aligner_device_card,
            self.aligner_dtype_card,
            self.aligner_compile_card,
        ]:
            if hasattr(card, "lineEdit"):
                card.lineEdit.setMinimumWidth(240)
            if hasattr(card, "comboBox"):
                card.comboBox.setMinimumWidth(240)

        self.setting_group.addSettingCard(self.base_url_card)
        self.setting_group.addSettingCard(self.api_key_card)
        self.setting_group.addSettingCard(self.model_card)
        self.setting_group.addSettingCard(self.timeout_card)
        self.setting_group.addSettingCard(self.concurrency_card)
        self.setting_group.addSettingCard(self.chunk_overlap_card)
        self.setting_group.addSettingCard(self.check_connection_card)
        self.aligner_group.addSettingCard(self.aligner_model_card)
        self.aligner_group.addSettingCard(self.aligner_model_dir_card)
        self.aligner_group.addSettingCard(self.aligner_device_card)
        self.aligner_group.addSettingCard(self.aligner_dtype_card)
        self.aligner_group.addSettingCard(self.aligner_compile_card)
        self.aligner_group.addSettingCard(self.manage_model_card)

        self.check_connection_card.clicked.connect(self.on_check_connection)
        self.manage_model_card.linkButton.clicked.connect(self._show_model_manager)

        self.containerLayout.addWidget(self.setting_group)
        self.containerLayout.addWidget(self.aligner_group)
        self.containerLayout.addStretch(1)

        self.scrollArea.setWidget(self.container)
        self.scrollArea.setWidgetResizable(True)
        self.main_layout.addWidget(self.scrollArea)

    def on_check_connection(self):
        base_url = self.base_url_card.lineEdit.text().strip()
        api_key = self.api_key_card.lineEdit.text().strip()
        model = self.model_card.comboBox.currentText().strip()

        if not base_url or not api_key or not model:
            InfoBar.warning(
                self.tr("配置不完整"),
                self.tr("请输入 API Base URL、API Key 和模型"),
                duration=INFOBAR_DURATION_ERROR,
                position=InfoBarPosition.TOP,
                parent=self.window(),
            )
            return

        self.check_connection_card.button.setEnabled(False)
        self.check_connection_card.button.setText(self.tr("正在测试..."))

        language = LANGUAGES[cfg.transcribe_language.value.value]
        self.connection_thread = MimoASRConnectionThread(
            base_url=base_url,
            api_key=api_key,
            model=model,
            language=language,
            timeout=self.timeout_card.spinBox.value(),
        )
        self.connection_thread.result_ready.connect(self.on_connection_check_finished)
        self.connection_thread.error.connect(self.on_connection_check_error)
        self.connection_thread.start()

    def _show_model_manager(self):
        dialog = QwenModelDownloadDialog(self.window())
        dialog.exec_()

    def on_connection_check_finished(self, success, result):
        self.check_connection_card.button.setEnabled(True)
        self.check_connection_card.button.setText(self.tr("测试连接"))

        if success:
            InfoBar.success(
                self.tr("连接成功"),
                self.tr("MiMo ASR API 连接成功！") + "\n" + result,
                duration=INFOBAR_DURATION_SUCCESS,
                position=InfoBarPosition.BOTTOM,
                parent=self.window(),
            )
        else:
            InfoBar.error(
                self.tr("连接失败"),
                self.tr(f"MiMo ASR API 连接失败！\n{result}"),
                duration=INFOBAR_DURATION_ERROR,
                position=InfoBarPosition.BOTTOM,
                parent=self.window(),
            )

    def on_connection_check_error(self, message):
        self.check_connection_card.button.setEnabled(True)
        self.check_connection_card.button.setText(self.tr("测试连接"))
        InfoBar.error(
            self.tr("测试错误"),
            message,
            duration=INFOBAR_DURATION_ERROR,
            position=InfoBarPosition.BOTTOM,
            parent=self.window(),
        )


class MimoASRConnectionThread(QThread):
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
            logger.exception("MiMo ASR connection thread failed")
            self.error.emit(str(e))
