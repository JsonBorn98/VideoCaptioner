import queue
import time
from functools import partial
from pathlib import Path
from typing import Dict, Optional

from PyQt5.QtCore import QThread, pyqtSignal

from videocaptioner.core.entities import (
    BatchTaskStatus,
    BatchTaskType,
    TranscribeTask,
)
from videocaptioner.core.postprocess import PostprocessProfileStore
from videocaptioner.core.utils.logger import setup_logger
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.task_factory import TaskFactory
from videocaptioner.ui.thread.postprocess_thread import PostprocessThread
from videocaptioner.ui.thread.subtitle_thread import SubtitleThread
from videocaptioner.ui.thread.transcript_thread import TranscriptThread
from videocaptioner.ui.thread.video_synthesis_thread import VideoSynthesisThread

logger = setup_logger("batch_process_thread")


class BatchTask:
    def __init__(
        self,
        file_path: str,
        task_type: BatchTaskType,
        *,
        postprocess_enabled: bool | None = None,
    ):
        self.file_path = file_path
        self.task_type = task_type
        self.status = BatchTaskStatus.WAITING
        self.progress = 0
        self.error_message = ""
        self.current_thread: Optional[QThread] = None
        self.export_policy = TaskFactory.create_subtitle_export_policy()
        self.workflow_base_name = Path(file_path).stem
        for prefix in ("【转录字幕】", "【初版字幕】", "【后处理字幕】"):
            self.workflow_base_name = self.workflow_base_name.removeprefix(prefix)
        enabled = cfg.get(cfg.postprocess_enabled) if postprocess_enabled is None else postprocess_enabled
        profile_id = cfg.get(cfg.postprocess_profile)
        self.postprocess_task = TaskFactory.create_postprocess_task(
            file_path,
            file_path if task_type == BatchTaskType.FULL_PROCESS else None,
            need_next_task=task_type == BatchTaskType.FULL_PROCESS,
            enabled=enabled and task_type != BatchTaskType.TRANSCRIBE,
            profile_id=profile_id,
            config_snapshot=PostprocessProfileStore().resolve_config(profile_id),
            workflow_base_name=self.workflow_base_name,
            export_policy=self.export_policy,
        )


class BatchProcessThread(QThread):
    # 信号定义
    task_progress = pyqtSignal(str, int, str)  # file_path, progress, status
    task_error = pyqtSignal(str, str)  # file_path, error_message
    task_completed = pyqtSignal(str)  # file_path

    def __init__(self):
        super().__init__()
        self.task_queue = queue.Queue()
        self.current_tasks: Dict[str, BatchTask] = {}
        self.max_concurrent_tasks = 1
        self.is_running = False
        self.factory = TaskFactory()
        self.threads = []  # 保存所有创建的线程

    def add_task(self, task: BatchTask):
        self.task_queue.put(task)
        self.current_tasks[task.file_path] = task
        if not self.isRunning():
            self.is_running = True
            self.start()

    def run(self):
        while self.is_running:
            # 检查是否有正在运行的任务数量是否达到上限
            running_tasks = sum(
                1
                for task in self.current_tasks.values()
                if task.status == BatchTaskStatus.RUNNING
            )

            if running_tasks < self.max_concurrent_tasks:
                try:
                    # 非阻塞方式获取任务
                    task = self.task_queue.get_nowait()
                    self._process_task(task)
                except queue.Empty:
                    time.sleep(0.1)  # 避免CPU过度使用
            else:
                time.sleep(0.1)

    def _process_task(self, batch_task: BatchTask):
        try:
            batch_task.status = BatchTaskStatus.RUNNING
            self.task_progress.emit(
                batch_task.file_path, 0, str(BatchTaskStatus.RUNNING)
            )

            if batch_task.task_type == BatchTaskType.TRANSCRIBE:
                self._handle_transcribe_task(batch_task)
            elif batch_task.task_type == BatchTaskType.SUBTITLE:
                self._handle_subtitle_task(batch_task)
            elif batch_task.task_type == BatchTaskType.TRANS_SUB:
                self._handle_trans_sub_task(batch_task)
            elif batch_task.task_type == BatchTaskType.FULL_PROCESS:
                self._handle_full_process_task(batch_task)

        except Exception as e:
            logger.exception(f"处理任务失败: {str(e)}")
            batch_task.status = BatchTaskStatus.FAILED
            batch_task.error_message = str(e)
            self.task_error.emit(batch_task.file_path, str(e))

    def _on_progress_wrapper(self, batch_task: BatchTask, progress: int, message: str):
        """进度信号包装器"""
        self.task_progress.emit(batch_task.file_path, progress, message)

    def _on_error_wrapper(self, batch_task: BatchTask, error: str):
        """错误信号包装器"""
        batch_task.status = BatchTaskStatus.FAILED
        batch_task.error_message = error
        self.task_error.emit(batch_task.file_path, error)

    def _on_finished_wrapper(self, batch_task: BatchTask, task=None):
        """完成信号包装器"""
        batch_task.status = BatchTaskStatus.COMPLETED
        batch_task.progress = 100
        self.task_completed.emit(batch_task.file_path)
        if batch_task.current_thread in self.threads:
            self.threads.remove(batch_task.current_thread)

    def _handle_transcribe_task(self, batch_task: BatchTask):
        # self.max_concurrent_tasks = 3
        task = self.factory.create_transcribe_task(batch_task.file_path, need_next_task=True)
        task.export_policy = batch_task.export_policy
        batch_task.workflow_base_name = task.workflow_base_name
        thread = TranscriptThread(task)
        batch_task.current_thread = thread

        # 保存线程引用
        self.threads.append(thread)

        thread.progress.connect(
            lambda value, message: self.task_progress.emit(
                batch_task.file_path, int(value * 0.7), message
            )
        )
        thread.error.connect(  # type: ignore
            partial(self._on_error_wrapper, batch_task)  # type: ignore
        )
        thread.finished.connect(partial(self._on_finished_wrapper, batch_task))

        thread.start()

    def _handle_subtitle_task(self, batch_task: BatchTask):
        logger.info(f"开始处理字幕任务: {batch_task.file_path}")

        task = self.factory.create_subtitle_task(
            batch_task.file_path,
            need_next_task=True,
            workflow_base_name=batch_task.workflow_base_name,
            export_policy=batch_task.export_policy,
        )
        batch_task.workflow_base_name = task.workflow_base_name
        thread = SubtitleThread(task)
        batch_task.current_thread = thread

        # 保存线程引用
        self.threads.append(thread)

        thread.progress.connect(  # type: ignore
            partial(self._on_progress_wrapper, batch_task)  # type: ignore
        )
        thread.error.connect(  # type: ignore
            partial(self._on_error_wrapper, batch_task)  # type: ignore
        )
        thread.finished.connect(partial(self._start_postprocess, batch_task, False, task))

        thread.start()

    def _handle_trans_sub_task(self, batch_task: BatchTask):
        trans_task = self.factory.create_transcribe_task(
            batch_task.file_path, need_next_task=True
        )
        trans_task.export_policy = batch_task.export_policy
        batch_task.workflow_base_name = trans_task.workflow_base_name
        thread = TranscriptThread(trans_task)
        batch_task.current_thread = thread
        self.current_tasks[batch_task.file_path] = batch_task

        # 保存线程引用
        self.threads.append(thread)

        thread.progress.connect(
            partial(self._on_trans_sub_progress_wrapper, batch_task)
        )
        thread.error.connect(partial(self._on_error_wrapper, batch_task))
        thread.finished.connect(
            partial(self._on_trans_sub_finished_wrapper, batch_task)
        )

        thread.start()

    def _on_trans_sub_progress_wrapper(
        self, batch_task: BatchTask, progress: int, message: str
    ):
        """转录+字幕任务进度包装器"""
        progress = progress // 2  # 转录占50%进度
        self.task_progress.emit(batch_task.file_path, progress, message)

    def _on_trans_sub_finished_wrapper(
        self, batch_task: BatchTask, task: TranscribeTask
    ):
        """转录+字幕任务转录完成包装器"""
        if batch_task.current_thread in self.threads:
            self.threads.remove(batch_task.current_thread)

        # 创建字幕任务
        if not task.output_path:
            raise ValueError("Task output_path is None")
        subtitle_task = self.factory.create_subtitle_task(
            task.output_path,
            batch_task.file_path,
            need_next_task=True,
            workflow_base_name=batch_task.workflow_base_name,
            input_data=task.result_data,
            export_policy=batch_task.export_policy,
        )
        batch_task.workflow_base_name = subtitle_task.workflow_base_name
        thread = SubtitleThread(subtitle_task)
        batch_task.current_thread = thread
        self.current_tasks[batch_task.file_path] = batch_task

        # 保存线程引用
        self.threads.append(thread)

        thread.progress.connect(
            partial(self._on_trans_sub_subtitle_progress_wrapper, batch_task)
        )
        thread.error.connect(partial(self._on_error_wrapper, batch_task))
        thread.finished.connect(
            partial(self._start_postprocess, batch_task, False, subtitle_task)
        )

        thread.start()

    def _on_trans_sub_subtitle_progress_wrapper(
        self, batch_task: BatchTask, progress: int, message: str
    ):
        """转录+字幕任务字幕进度包装器"""
        progress = 50 + int(progress * 0.3)
        self.task_progress.emit(batch_task.file_path, progress, message)

    def _start_postprocess(
        self,
        batch_task: BatchTask,
        full_process: bool,
        subtitle_task,
        video_path: str,
        subtitle_path: str,
    ):
        """Run the optional stage; skipped and fallback results still continue."""
        if batch_task.current_thread in self.threads:
            self.threads.remove(batch_task.current_thread)
        task = batch_task.postprocess_task
        task.source_subtitle_path = subtitle_path
        task.initial_subtitle_path = subtitle_path
        task.active_subtitle_path = subtitle_path
        task.input_data = subtitle_task.result_data
        task.workflow_base_name = subtitle_task.workflow_base_name
        task.export_policy = batch_task.export_policy
        task.media_path = video_path or (batch_task.file_path if full_process else None)
        source = Path(subtitle_path)
        task.postprocessed_subtitle_path = str(
            source.with_name(f"【后处理字幕】{subtitle_task.workflow_base_name}.srt")
        )
        thread = PostprocessThread(task)
        batch_task.current_thread = thread
        self.threads.append(thread)
        thread.error.connect(partial(self._on_error_wrapper, batch_task))
        thread.warning.connect(
            lambda message: self.task_progress.emit(
                batch_task.file_path, 74 if full_process else 99, message
            )
        )
        if full_process:
            thread.progress.connect(
                lambda value, message: self.task_progress.emit(
                    batch_task.file_path, 60 + int(value * 0.15), message
                )
            )
            thread.finished.connect(
                partial(self.on_full_process_postprocess_finished, batch_task)
            )
        else:
            thread.progress.connect(
                lambda value, message: self.task_progress.emit(
                    batch_task.file_path, 80 + int(value * 0.2), message
                )
            )
            thread.finished.connect(partial(self._on_finished_wrapper, batch_task))
        thread.start()

    def _handle_full_process_task(self, batch_task: BatchTask):
        # 首先创建转录任务
        trans_task = self.factory.create_transcribe_task(
            batch_task.file_path, need_next_task=True
        )
        trans_task.export_policy = batch_task.export_policy
        batch_task.workflow_base_name = trans_task.workflow_base_name
        thread = TranscriptThread(trans_task)
        batch_task.current_thread = thread

        # 保存线程引用
        self.threads.append(thread)

        thread.progress.connect(partial(self.on_full_process_progress, batch_task))
        thread.error.connect(partial(self._on_error_wrapper, batch_task))
        thread.finished.connect(partial(self.on_full_process_finished, batch_task))

        thread.start()

    def on_full_process_progress(
        self, batch_task: BatchTask, progress: int, message: str
    ):
        """处理全流程任务的转录进度"""
        if batch_task.status == BatchTaskStatus.RUNNING:
            progress_value = int(progress * 0.3)
            self.task_progress.emit(batch_task.file_path, progress_value, message)

    def on_full_process_finished(self, batch_task: BatchTask, task: TranscribeTask):
        """处理转录完成后开始字幕任务"""
        if batch_task.current_thread in self.threads:
            self.threads.remove(batch_task.current_thread)

        # 转录完成后创建字幕任务
        if not task.output_path:
            raise ValueError("Task output_path is None")
        subtitle_task = self.factory.create_subtitle_task(
            task.output_path,
            batch_task.file_path,
            need_next_task=True,
            workflow_base_name=batch_task.workflow_base_name,
            input_data=task.result_data,
            export_policy=batch_task.export_policy,
        )
        batch_task.workflow_base_name = subtitle_task.workflow_base_name
        thread = SubtitleThread(subtitle_task)
        batch_task.current_thread = thread

        # 保存线程引用
        self.threads.append(thread)

        thread.progress.connect(
            partial(self.on_full_process_subtitle_progress, batch_task)
        )
        thread.error.connect(partial(self._on_error_wrapper, batch_task))
        thread.finished.connect(
            partial(self._start_postprocess, batch_task, True, subtitle_task)
        )

        thread.start()

    def on_full_process_subtitle_progress(
        self, batch_task: BatchTask, progress: int, message: str
    ):
        """处理全流程任务中字幕部分的进度"""
        if batch_task.status == BatchTaskStatus.RUNNING:
            progress_value = 30 + int(progress * 0.3)
            self.task_progress.emit(batch_task.file_path, progress_value, message)

    def on_full_process_postprocess_finished(
        self, batch_task: BatchTask, video_path: str, subtitle_path: str
    ):
        """后处理完成或回退后开始视频合成任务。"""
        if batch_task.current_thread in self.threads:
            self.threads.remove(batch_task.current_thread)

        # 字幕完成后创建视频合成任务
        synthesis_task = self.factory.create_synthesis_task(
            video_path,
            subtitle_path,
            input_data=batch_task.postprocess_task.result_data,
        )
        thread = VideoSynthesisThread(synthesis_task)
        batch_task.current_thread = thread

        # 保存线程引用
        self.threads.append(thread)

        thread.progress.connect(
            partial(self.on_full_process_synthesis_progress, batch_task)
        )
        thread.error.connect(partial(self._on_error_wrapper, batch_task))
        thread.finished.connect(partial(self._on_finished_wrapper, batch_task))

        thread.start()

    def on_full_process_synthesis_progress(
        self, batch_task: BatchTask, progress: int, message: str
    ):
        """处理全流程任务中视频合成部分的进度"""
        if batch_task.status == BatchTaskStatus.RUNNING:
            progress_value = 75 + int(progress * 0.25)
            self.task_progress.emit(batch_task.file_path, progress_value, message)

    def stop_task(self, file_path: str):
        if file_path in self.current_tasks:
            task = self.current_tasks[file_path]
            if task.current_thread:
                if hasattr(task.current_thread, "stop"):
                    task.current_thread.stop()  # type: ignore
            del self.current_tasks[file_path]
            # 从队列中移除任务
            with self.task_queue.mutex:
                self.task_queue.queue.clear()

    def stop_all(self):
        self.is_running = False
        # 停止所有线程
        for thread in self.threads:
            if hasattr(thread, "stop"):
                thread.stop()  # type: ignore
            thread.wait()  # 等待线程结束
        self.threads.clear()
        self.current_tasks.clear()
        # 清空任务队列
        with self.task_queue.mutex:
            self.task_queue.queue.clear()
