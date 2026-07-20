import datetime
from dataclasses import replace
from pathlib import Path

from PyQt5.QtCore import QThread, pyqtSignal

from videocaptioner.core.entities import (
    FullProcessTask,
)
from videocaptioner.core.translate.enhanced.models import (
    TermConfirmationMode,
    TranslationAuditMode,
    TranslationExecutionMode,
)
from videocaptioner.core.utils.logger import setup_logger
from videocaptioner.ui.task_factory import TaskFactory

from .postprocess_thread import PostprocessThread
from .subtitle_thread import SubtitleThread
from .transcript_thread import TranscriptThread
from .video_synthesis_thread import VideoSynthesisThread

logger = setup_logger("subtitle_pipeline_thread")


class SubtitlePipelineThread(QThread):
    """字幕处理全流程线程，包含:
    1. 转录生成字幕
    2. 字幕优化/翻译
    3. 字幕后处理（可选，失败回退初版）
    4. 视频合成
    """

    progress = pyqtSignal(int, str)  # 进度值, 进度描述
    finished = pyqtSignal(FullProcessTask)
    error = pyqtSignal(str)

    def __init__(self, task: FullProcessTask):
        super().__init__()
        self.task = task
        self.has_error = False
        if task.subtitle_config is None:
            task.subtitle_config = TaskFactory.create_subtitle_task(
                task.file_path or "",
                task.file_path,
                need_next_task=True,
                workflow_base_name=task.workflow_base_name,
                export_policy=task.export_policy,
                translation_execution_mode=TranslationExecutionMode.BATCH,
            ).subtitle_config
        else:
            task.subtitle_config = replace(
                task.subtitle_config,
                translation_execution_mode=TranslationExecutionMode.BATCH,
                term_confirmation_mode=TermConfirmationMode.AUTOMATIC,
                translation_audit_mode=TranslationAuditMode.AUTO_FIX_OBJECTIVE,
            )

    def run(self):
        try:

            def handle_error(error_msg):
                logger.error("pipeline 发生错误: %s", error_msg)
                self.has_error = True
                self.error.emit(error_msg)

            # 在任务开始时冻结当前后处理方案，运行途中设置变化不影响本任务。
            postprocess_task = self.task.postprocess_task or TaskFactory.create_postprocess_task(
                self.task.file_path or "",
                self.task.file_path,
                need_next_task=True,
                enabled=self.task.postprocess_enabled,
                task_id=self.task.task_id,
            )
            postprocess_task.enabled = self.task.postprocess_enabled
            self.task.postprocess_task = postprocess_task

            # 1. 转录生成字幕
            self.task.started_at = datetime.datetime.now()
            logger.info(f"\n{self.task.transcribe_config.print_config()}")
            logger.info(f"\n{self.task.subtitle_config.print_config()}")
            if self.task.synthesis_config:
                logger.info(f"\n{self.task.synthesis_config.print_config()}")
            self.progress.emit(0, self.tr("开始转录"))

            # 创建转录任务。阶段 SRT 会落盘，但下游直接消费 result_data。
            transcribe_task = TaskFactory.create_transcribe_task(
                self.task.file_path or "",
                need_next_task=True,
                task_id=self.task.task_id,
            )
            transcribe_task.transcribe_config = self.task.transcribe_config
            transcribe_task.workflow_base_name = self.task.workflow_base_name
            transcribe_task.export_policy = self.task.export_policy
            transcript_thread = TranscriptThread(transcribe_task)
            transcript_thread.progress.connect(
                lambda value, msg: self.progress.emit(int(value * 0.3), msg)
            )
            transcript_thread.error.connect(handle_error)
            transcript_thread.run()

            if self.has_error:
                logger.info("转录过程中发生错误，终止流程")
                return

            # 2. 字幕优化/翻译
            # self.task.status = Task.Status.OPTIMIZING
            self.progress.emit(30, self.tr("开始优化字幕"))

            subtitle_task = TaskFactory.create_subtitle_task(
                transcribe_task.output_path or "",
                self.task.file_path,
                need_next_task=True,
                task_id=self.task.task_id,
                workflow_base_name=self.task.workflow_base_name,
                input_data=transcribe_task.result_data,
                export_policy=self.task.export_policy,
                config_snapshot=self.task.subtitle_config,
                translation_execution_mode=TranslationExecutionMode.BATCH,
            )
            optimization_thread = SubtitleThread(subtitle_task)
            optimization_thread.progress.connect(
                lambda value, msg: self.progress.emit(int(30 + value * 0.3), msg)
            )
            optimization_thread.error.connect(handle_error)
            optimization_thread.run()

            if self.has_error:
                logger.info("字幕优化过程中发生错误，终止流程")
                return

            # 3. 独立字幕后处理。任何可恢复失败都由线程回退到初版字幕。
            self.progress.emit(60, self.tr("开始字幕后处理"))
            self.task.workflow_base_name = subtitle_task.workflow_base_name
            postprocess_task.source_subtitle_path = subtitle_task.output_path or ""
            postprocess_task.initial_subtitle_path = subtitle_task.output_path
            postprocess_task.active_subtitle_path = subtitle_task.output_path
            postprocess_task.input_data = subtitle_task.result_data
            postprocess_task.workflow_base_name = subtitle_task.workflow_base_name
            postprocess_task.export_policy = self.task.export_policy
            initial = Path(subtitle_task.output_path or "subtitle.srt")
            postprocess_task.postprocessed_subtitle_path = str(
                initial.with_name(f"【后处理字幕】{subtitle_task.workflow_base_name}.srt")
            )
            postprocess_thread = PostprocessThread(postprocess_task)
            postprocess_thread.progress.connect(
                lambda value, msg: self.progress.emit(int(60 + value * 0.15), msg)
            )
            postprocess_thread.error.connect(handle_error)
            postprocess_thread.run()

            if self.has_error:
                logger.info("字幕后处理输入无效，终止流程")
                return

            # 4. 视频合成
            # self.task.status = Task.Status.GENERATING
            self.progress.emit(75, self.tr("开始合成视频"))

            # 创建合成任务
            active_data = postprocess_task.result_data or subtitle_task.result_data
            self.task.result_data = active_data
            synthesis_task = TaskFactory.create_synthesis_task(
                self.task.file_path or "",
                postprocess_task.active_subtitle_path or subtitle_task.output_path or "",
                task_id=self.task.task_id,
                input_data=active_data,
            )
            synthesis_task.output_path = self.task.output_path
            synthesis_task.synthesis_config = self.task.synthesis_config
            synthesis_thread = VideoSynthesisThread(synthesis_task)
            synthesis_thread.progress.connect(
                lambda value, msg: self.progress.emit(int(75 + value * 0.25), msg)
            )
            synthesis_thread.error.connect(handle_error)
            synthesis_thread.run()

            if self.has_error:
                logger.info("视频合成过程中发生错误，终止流程")
                return

            # self.task.status = FullProcessTask.Status.COMPLETED  # type: ignore
            logger.info("处理完成")
            self.progress.emit(100, self.tr("处理完成"))
            self.finished.emit(self.task)

        except Exception as e:
            # self.task.status = FullProcessTask.Status.FAILED  # type: ignore
            logger.exception("处理失败: %s", str(e))
            self.error.emit(str(e))
