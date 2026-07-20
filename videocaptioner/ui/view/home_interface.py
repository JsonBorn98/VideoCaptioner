from typing import Optional

from PyQt5.QtWidgets import QSizePolicy, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import SegmentedWidget

from videocaptioner.core.llm.context import generate_task_id
from videocaptioner.core.postprocess import PostprocessProfileStore
from videocaptioner.core.translate.enhanced.models import TranslationExecutionMode
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.task_factory import TaskFactory
from videocaptioner.ui.view.postprocess_interface import PostprocessInterface
from videocaptioner.ui.view.subtitle_interface import SubtitleInterface
from videocaptioner.ui.view.task_creation_interface import TaskCreationInterface
from videocaptioner.ui.view.transcription_interface import TranscriptionInterface
from videocaptioner.ui.view.video_synthesis_interface import VideoSynthesisInterface


class HomeInterface(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_task_id: Optional[str] = None  # 当前流程的任务 ID
        self._postprocess_enabled = True
        self._postprocess_profile_id = "balanced"
        self._postprocess_config_snapshot = None
        self._subtitle_config_snapshot = None
        self._workflow_base_name: str | None = None
        self._workflow_export_policy = None
        self._active_subtitle_data = None

        # 设置对象名称和样式
        self.setObjectName("HomeInterface")
        self.setStyleSheet(
            """
            HomeInterface{background: white}
        """
        )

        # 创建分段控件和堆叠控件
        self.pivot = SegmentedWidget(self)
        self.pivot.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)

        self.stackedWidget = QStackedWidget(self)
        self.vBoxLayout = QVBoxLayout(self)

        # 添加子界面
        self.task_creation_interface = TaskCreationInterface(self)
        self.transcription_interface = TranscriptionInterface(self)
        self.subtitle_optimization_interface = SubtitleInterface(self)
        self.postprocess_interface = PostprocessInterface(self)
        self.video_synthesis_interface = VideoSynthesisInterface(self)

        self.addSubInterface(
            self.task_creation_interface, "TaskCreationInterface", self.tr("任务创建")
        )
        self.addSubInterface(
            self.transcription_interface, "TranscriptionInterface", self.tr("语音转录")
        )
        self.addSubInterface(
            self.subtitle_optimization_interface,
            "SubtitleInterface",
            self.tr("字幕优化与翻译"),
        )
        self.addSubInterface(
            self.postprocess_interface,
            "PostprocessInterface",
            self.tr("字幕后处理"),
        )
        self.addSubInterface(
            self.video_synthesis_interface,
            "VideoSynthesisInterface",
            self.tr("字幕视频合成"),
        )

        self.vBoxLayout.addWidget(self.pivot)
        self.vBoxLayout.addWidget(self.stackedWidget)
        self.vBoxLayout.setContentsMargins(30, 10, 30, 30)

        self.stackedWidget.currentChanged.connect(self.onCurrentIndexChanged)
        self.stackedWidget.setCurrentWidget(self.task_creation_interface)
        self.pivot.setCurrentItem("TaskCreationInterface")

        self.task_creation_interface.finished.connect(self.switch_to_transcription)
        self.transcription_interface.finished.connect(
            self.switch_to_subtitle_optimization
        )
        self.subtitle_optimization_interface.finished.connect(
            self.switch_to_postprocess
        )
        self.postprocess_interface.finished.connect(self.switch_to_video_synthesis)

    def switch_to_transcription(self, file_path):
        # 流程开始，生成新的 task_id
        self._current_task_id = generate_task_id()
        self._postprocess_enabled = cfg.get(cfg.postprocess_enabled)
        self._postprocess_profile_id = cfg.get(cfg.postprocess_profile)
        self._postprocess_config_snapshot = PostprocessProfileStore().resolve_config(
            self._postprocess_profile_id
        )

        transcribe_task = TaskFactory.create_transcribe_task(
            file_path, need_next_task=True, task_id=self._current_task_id
        )
        self._workflow_base_name = transcribe_task.workflow_base_name
        self._workflow_export_policy = transcribe_task.export_policy
        self._subtitle_config_snapshot = TaskFactory.create_subtitle_task(
            file_path,
            file_path,
            need_next_task=True,
            workflow_base_name=self._workflow_base_name,
            export_policy=self._workflow_export_policy,
            translation_execution_mode=TranslationExecutionMode.GUI_WORKFLOW,
        ).subtitle_config
        self._active_subtitle_data = None
        self.transcription_interface.set_task(transcribe_task)
        self.transcription_interface.process()
        self.stackedWidget.setCurrentWidget(self.transcription_interface)
        self.pivot.setCurrentItem("TranscriptionInterface")

    def switch_to_subtitle_optimization(self, file_path, video_path):
        # 继续使用同一个 task_id
        transcribe_task = self.transcription_interface.task
        input_data = getattr(transcribe_task, "result_data", None)
        subtitle_task = TaskFactory.create_subtitle_task(
            file_path,
            video_path,
            need_next_task=True,
            task_id=self._current_task_id,
            workflow_base_name=self._workflow_base_name,
            input_data=input_data,
            export_policy=self._workflow_export_policy,
            config_snapshot=self._subtitle_config_snapshot,
            translation_execution_mode=TranslationExecutionMode.GUI_WORKFLOW,
        )
        self._workflow_base_name = subtitle_task.workflow_base_name
        self.subtitle_optimization_interface.set_task(subtitle_task)
        self.subtitle_optimization_interface.process()
        self.stackedWidget.setCurrentWidget(self.subtitle_optimization_interface)
        self.pivot.setCurrentItem("SubtitleInterface")

    def switch_to_postprocess(self, video_path, subtitle_path):
        subtitle_task = self.subtitle_optimization_interface.task
        self._active_subtitle_data = getattr(subtitle_task, "result_data", None)
        if not self._postprocess_enabled:
            self.switch_to_video_synthesis(video_path, subtitle_path)
            return
        # 后处理设置可能在漫长的上游阶段（转录/优化/翻译）期间才改动；在此重新读取所选
        # 方案，确保用户在后处理运行前保存的改动生效，而非沿用 workflow 开始时的快照。
        self._postprocess_profile_id = cfg.get(cfg.postprocess_profile)
        self._postprocess_config_snapshot = PostprocessProfileStore().resolve_config(
            self._postprocess_profile_id
        )
        postprocess_task = TaskFactory.create_postprocess_task(
            subtitle_path,
            video_path,
            need_next_task=True,
            enabled=self._postprocess_enabled,
            profile_id=self._postprocess_profile_id,
            config_snapshot=self._postprocess_config_snapshot,
            task_id=self._current_task_id,
            workflow_base_name=self._workflow_base_name,
            input_data=self._active_subtitle_data,
            export_policy=self._workflow_export_policy,
        )
        self.postprocess_interface.set_task(postprocess_task)
        self.postprocess_interface.process()
        self.stackedWidget.setCurrentWidget(self.postprocess_interface)
        self.pivot.setCurrentItem("PostprocessInterface")

    def switch_to_video_synthesis(self, video_path, subtitle_path):
        postprocess_task = getattr(self.postprocess_interface, "task", None)
        if (
            postprocess_task is not None
            and getattr(postprocess_task, "task_id", None) == self._current_task_id
            and getattr(postprocess_task, "result_data", None) is not None
        ):
            self._active_subtitle_data = postprocess_task.result_data
        # 继续使用同一个 task_id，流程结束后清空
        synthesis_task = TaskFactory.create_synthesis_task(
            video_path,
            subtitle_path,
            need_next_task=True,
            task_id=self._current_task_id,
            input_data=self._active_subtitle_data,
        )
        self._current_task_id = None  # 流程结束
        self._postprocess_config_snapshot = None
        self._subtitle_config_snapshot = None
        self._workflow_base_name = None
        self._workflow_export_policy = None
        self._active_subtitle_data = None
        self.video_synthesis_interface.set_task(synthesis_task)
        self.video_synthesis_interface.process()
        self.stackedWidget.setCurrentWidget(self.video_synthesis_interface)
        self.pivot.setCurrentItem("VideoSynthesisInterface")

    def addSubInterface(self, widget, objectName, text):
        # 添加子界面到堆叠控件和分段控件
        widget.setObjectName(objectName)
        self.stackedWidget.addWidget(widget)
        self.pivot.addItem(
            routeKey=objectName,
            text=text,
            onClick=lambda _checked=False, widget=widget: self.stackedWidget.setCurrentWidget(widget),
        )

    def onCurrentIndexChanged(self, index):
        # 当堆叠控件的当前索引改变时，更新分段控件的当前项
        widget = self.stackedWidget.widget(index)
        if widget:
            self.pivot.setCurrentItem(widget.objectName())

    def closeEvent(self, event):
        # 关闭事件，关闭所有子界面
        self.task_creation_interface.close()
        self.transcription_interface.close()
        self.subtitle_optimization_interface.close()
        self.postprocess_interface.close()
        self.video_synthesis_interface.close()
        super().closeEvent(event)
