import os
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    ComboBox,
    ComboBoxSettingCard,
    HyperlinkButton,
    HyperlinkCard,
    InfoBar,
    InfoBarPosition,
    MessageBoxBase,
    ProgressBar,
    PushButton,
    SettingCardGroup,
    SingleDirectionScrollArea,
    SubtitleLabel,
)
from qfluentwidgets import FluentIcon as FIF

from videocaptioner.config import MODEL_PATH
from videocaptioner.core.asr.qwen_runtime import (
    QWEN_ALIGNER_MODEL_OPTIONS,
    QWEN_ASR_MODEL_OPTIONS,
)
from videocaptioner.core.constant import INFOBAR_DURATION_ERROR, INFOBAR_DURATION_SUCCESS
from videocaptioner.core.entities import TranscribeLanguageEnum
from videocaptioner.core.utils.platform_utils import open_folder
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.components.LineEditSettingCard import LineEditSettingCard
from videocaptioner.ui.components.SpinBoxSettingCard import SpinBoxSettingCard
from videocaptioner.ui.thread.modelscope_download_thread import ModelscopeDownloadThread

QWEN_DOWNLOAD_MODELS = [
    {
        "label": "Qwen3-ASR-1.7B",
        "repo": "Qwen/Qwen3-ASR-1.7B",
        "path": "Qwen3-ASR-1.7B",
        "kind": "ASR",
    },
    {
        "label": "Qwen3-ASR-0.6B",
        "repo": "Qwen/Qwen3-ASR-0.6B",
        "path": "Qwen3-ASR-0.6B",
        "kind": "ASR",
    },
    {
        "label": "Qwen3-ForcedAligner-0.6B",
        "repo": "Qwen/Qwen3-ForcedAligner-0.6B",
        "path": "Qwen3-ForcedAligner-0.6B",
        "kind": "Aligner",
    },
]


def qwen_model_path(model: dict) -> Path:
    root = Path(cfg.qwen_model_dir.value or str(MODEL_PATH))
    return root / str(model["path"])


def is_qwen_model_downloaded(model: dict) -> bool:
    model_path = qwen_model_path(model)
    return model_path.exists() and any(model_path.iterdir())


class QwenModelDownloadDialog(MessageBoxBase):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.widget.setMinimumWidth(560)
        self.download_thread = None
        self._download_failed = False
        self._setup_ui()

    def _setup_ui(self):
        title_layout = QHBoxLayout()
        title = SubtitleLabel(self.tr("Qwen 模型管理"), self)
        open_folder_btn = HyperlinkButton("", self.tr("打开模型文件夹"), parent=self)
        open_folder_btn.setIcon(FIF.FOLDER)
        open_folder_btn.clicked.connect(
            lambda: open_folder(str(Path(cfg.qwen_model_dir.value or str(MODEL_PATH))))
        )
        title_layout.addWidget(title)
        title_layout.addStretch(1)
        title_layout.addWidget(open_folder_btn)

        self.model_combo = ComboBox(self)
        self.model_combo.setMinimumWidth(420)
        for model in QWEN_DOWNLOAD_MODELS:
            status = "✓" if is_qwen_model_downloaded(model) else " "
            self.model_combo.addItem(
                f"{status} {model['label']} ({model['kind']})",
                userData=model,
            )

        self.status_label = BodyLabel(self.tr("通过 ModelScope 下载到本地模型目录"), self)
        self.progress_bar = ProgressBar(self)
        self.progress_bar.hide()

        self.download_button = PushButton(self.tr("下载 / 更新"), self)
        self.download_button.clicked.connect(self.start_download)

        self.viewLayout.addLayout(title_layout)
        self.viewLayout.addSpacing(8)
        self.viewLayout.addWidget(self.model_combo)
        self.viewLayout.addWidget(self.status_label)
        self.viewLayout.addWidget(self.progress_bar)
        self.viewLayout.addWidget(self.download_button)
        self.yesButton.hide()
        self.cancelButton.setText(self.tr("关闭"))

    def start_download(self):
        model = self.model_combo.currentData()
        if not model:
            return

        save_path = qwen_model_path(model)
        os.makedirs(save_path, exist_ok=True)
        self.download_button.setEnabled(False)
        self._download_failed = False
        self.progress_bar.show()
        self.progress_bar.setValue(0)
        self.status_label.setText(self.tr("开始下载..."))

        self.download_thread = ModelscopeDownloadThread(str(model["repo"]), str(save_path))
        self.download_thread.progress.connect(self._on_progress)
        self.download_thread.error.connect(self._on_error)
        self.download_thread.finished.connect(self._on_finished)
        self.download_thread.start()

    def _on_progress(self, value, message):
        self.progress_bar.setValue(int(value))
        self.status_label.setText(str(message))

    def _on_error(self, error):
        self._download_failed = True
        self.download_button.setEnabled(True)
        InfoBar.error(
            self.tr("下载失败"),
            str(error),
            duration=INFOBAR_DURATION_ERROR,
            position=InfoBarPosition.BOTTOM,
            parent=self.window(),
        )

    def _on_finished(self):
        if self._download_failed:
            return
        self.download_button.setEnabled(True)
        self.progress_bar.setValue(100)
        self.status_label.setText(self.tr("下载完成"))
        InfoBar.success(
            self.tr("下载完成"),
            self.tr("Qwen 模型已下载到本地模型目录"),
            duration=INFOBAR_DURATION_SUCCESS,
            position=InfoBarPosition.BOTTOM,
            parent=self.window(),
        )


class QwenASRSettingWidget(QWidget):
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

        self.setting_group = SettingCardGroup(self.tr("Qwen3 ASR 本地设置"), self)

        self.asr_model_card = ComboBoxSettingCard(
            cfg.qwen_asr_model,
            FIF.ROBOT,
            self.tr("ASR 模型"),
            self.tr("选择本地 Qwen3 ASR 模型"),
            QWEN_ASR_MODEL_OPTIONS,
            self.setting_group,
        )

        self.aligner_model_card = ComboBoxSettingCard(
            cfg.qwen_aligner_model,
            FIF.SYNC,
            self.tr("对齐模型"),
            self.tr("选择 Qwen3-ForcedAligner 模型"),
            QWEN_ALIGNER_MODEL_OPTIONS,
            self.setting_group,
        )

        self.manage_model_card = HyperlinkCard(
            "",
            self.tr("管理模型"),
            FIF.DOWNLOAD,
            self.tr("模型管理"),
            self.tr("下载或更新 Qwen3 ASR / ForcedAligner 模型"),
            self.setting_group,
        )

        self.model_dir_card = LineEditSettingCard(
            cfg.qwen_model_dir,
            FIF.FOLDER,
            self.tr("模型目录"),
            self.tr("本地模型目录；已下载模型会优先从这里加载"),
            str(MODEL_PATH),
            self.setting_group,
        )

        self.language_card = ComboBoxSettingCard(
            cfg.transcribe_language,
            FIF.LANGUAGE,
            self.tr("源语言"),
            self.tr("音视频中说话的语言，默认自动识别"),
            [lang.value for lang in TranscribeLanguageEnum],
            self.setting_group,
        )
        self.language_card.comboBox.setMaxVisibleItems(6)

        self.device_card = ComboBoxSettingCard(
            cfg.qwen_device,
            FIF.IOT,
            self.tr("运行设备"),
            self.tr("auto / cuda:0 / cpu"),
            ["auto", "cuda:0", "cpu"],
            self.setting_group,
        )

        self.dtype_card = ComboBoxSettingCard(
            cfg.qwen_dtype,
            FIF.SPEED_HIGH,  # type: ignore
            self.tr("计算精度"),
            self.tr("auto / bfloat16 / float16 / float32"),
            ["auto", "bfloat16", "float16", "float32"],
            self.setting_group,
        )

        self.max_tokens_card = SpinBoxSettingCard(
            cfg.qwen_max_new_tokens,
            FIF.CODE,  # type: ignore
            self.tr("最大输出 Tokens"),
            self.tr("长音频分块转写时的最大生成长度"),
            64,
            8192,
            self.setting_group,
        )

        self.chunk_overlap_card = SpinBoxSettingCard(
            cfg.qwen_chunk_overlap_seconds,
            FIF.ALIGNMENT,  # type: ignore
            self.tr("分块重叠秒数"),
            self.tr("相邻 5 分钟音频块的重叠时长，减少切分点漏词"),
            0,
            60,
            self.setting_group,
        )

        for card in [
            self.asr_model_card,
            self.aligner_model_card,
            self.model_dir_card,
            self.language_card,
            self.device_card,
            self.dtype_card,
        ]:
            if hasattr(card, "comboBox"):
                card.comboBox.setMinimumWidth(240)
            if hasattr(card, "lineEdit"):
                card.lineEdit.setMinimumWidth(240)

        for card in [
            self.asr_model_card,
            self.aligner_model_card,
            self.manage_model_card,
            self.model_dir_card,
            self.language_card,
            self.device_card,
            self.dtype_card,
            self.max_tokens_card,
            self.chunk_overlap_card,
        ]:
            self.setting_group.addSettingCard(card)

        self.manage_model_card.linkButton.clicked.connect(self._show_model_manager)

        self.containerLayout.addWidget(self.setting_group)
        self.containerLayout.addStretch(1)
        self.scrollArea.setWidget(self.container)
        self.scrollArea.setWidgetResizable(True)
        self.main_layout.addWidget(self.scrollArea)

    def _show_model_manager(self):
        dialog = QwenModelDownloadDialog(self.window())
        dialog.exec_()
