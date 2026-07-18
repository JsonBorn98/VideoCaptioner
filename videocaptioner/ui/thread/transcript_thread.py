import datetime
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from PyQt5.QtCore import QThread, pyqtSignal

from videocaptioner.core.asr import transcribe
from videocaptioner.core.asr.qwen_local_asr import close_qwen_worker_pool
from videocaptioner.core.asr.qwen_runtime import clear_qwen_model_cache
from videocaptioner.core.entities import (
    SubtitleLayoutEnum,
    TranscribeModelEnum,
    TranscribeOutputFormatEnum,
    TranscribeTask,
)
from videocaptioner.core.subtitle import (
    clone_subtitle_data,
    export_subtitle_atomic,
    import_subtitle,
    save_canonical_srt,
)
from videocaptioner.core.utils.logger import setup_logger
from videocaptioner.core.utils.stage_summary import StageSummary
from videocaptioner.core.utils.video_utils import video2audio
from videocaptioner.ui.common.log_bridge import publish_stage_summary
from videocaptioner.ui.task_factory import TaskFactory

logger = setup_logger("transcript_thread")


class TranscriptionCancelled(Exception):
    """Raised when the user cancels an active transcription task."""


class TranscriptThread(QThread):
    finished = pyqtSignal(TranscribeTask)
    progress = pyqtSignal(int, str)
    error = pyqtSignal(str)
    canceled = pyqtSignal()

    def __init__(self, task: TranscribeTask):
        super().__init__()
        self.task = task
        self._cancel_requested = False
        self._cancel_emitted = False
        self._temp_audio_path: str | None = None
        self._temp_workspace_path: str | None = None
        self._step_timings: list[tuple[str, float, str]] = []

    def run(self):
        task_started = time.perf_counter()
        status = "success"
        try:
            self.task.started_at = datetime.datetime.now()
            logger.info(
                "转录任务开始: file=%s, output=%s, model=%s",
                self.task.file_path,
                self.task.output_path,
                (
                    self.task.transcribe_config.transcribe_model
                    if self.task.transcribe_config
                    else None
                ),
            )
            if self.task.transcribe_config:
                logger.info(
                    f"\n{self.task.transcribe_config.print_config()}",
                    extra={"console": True},
                )
            else:
                logger.info("转录配置为空，等待验证阶段返回错误")

            self._raise_if_cancelled()
            with self._timed_step("validate_task"):
                self._validate_task()

            # 检查是否已下载字幕文件
            with self._timed_step("check_downloaded_subtitle"):
                subtitle_already_downloaded = self._check_downloaded_subtitle()
            if subtitle_already_downloaded:
                status = "skipped_existing_subtitle"
                return

            self._raise_if_cancelled()
            self._perform_transcription()

        except TranscriptionCancelled:
            status = "canceled"
            logger.info("转录任务已取消")
            self._emit_canceled()
        except Exception as e:
            status = "failed"
            logger.exception("转录过程中发生错误: %s", str(e))
            self.error.emit(str(e))
            self.progress.emit(100, self.tr("转录失败"))
        finally:
            with self._timed_step("cleanup_runtime_cache"):
                self._cleanup_runtime_cache()
            self._log_timing_summary(status, time.perf_counter() - task_started)

    def cancel(self, force: bool = False):
        """Request cancellation; force terminates the worker if it cannot stop itself."""
        self._cancel_requested = True
        self.requestInterruption()
        if force and self.isRunning():
            if self._uses_native_qwen_runtime():
                logger.warning("跳过强制终止 Qwen 转录线程，等待隔离 worker 退出")
                return
            logger.warning("强制终止转录线程")
            self.terminate()
            self.wait(1000)
            self._cleanup_temp_audio()
            self._cleanup_temp_workspace()
            self._cleanup_runtime_cache()
            self._emit_canceled()

    def _uses_native_qwen_runtime(self) -> bool:
        config = self.task.transcribe_config
        if not config or not config.transcribe_model:
            return False
        return config.transcribe_model in {
            TranscribeModelEnum.QWEN_LOCAL_ASR,
            TranscribeModelEnum.MIMO_ASR_API,
        }

    def _raise_if_cancelled(self):
        if self._cancel_requested or self.isInterruptionRequested():
            raise TranscriptionCancelled()

    def _emit_canceled(self):
        if self._cancel_emitted:
            return
        self._cancel_emitted = True
        self.progress.emit(100, self.tr("已停止"))
        self.canceled.emit()

    @contextmanager
    def _timed_step(
        self,
        name: str,
        *,
        console_start: str | None = None,
        console_success: str | None = None,
        console_failure: str | None = None,
    ) -> Iterator[None]:
        started = time.perf_counter()
        logger.info("转录步骤开始: %s", name)
        if console_start:
            logger.info(console_start, extra={"console": True})
        try:
            yield
        except BaseException:
            elapsed = time.perf_counter() - started
            self._step_timings.append((name, elapsed, "failed"))
            logger.info("转录步骤失败: %s，用时 %.2fs", name, elapsed)
            if console_failure:
                logger.info(
                    "%s，用时 %.2fs",
                    console_failure,
                    elapsed,
                    extra={"console": True},
                )
            raise
        else:
            elapsed = time.perf_counter() - started
            self._step_timings.append((name, elapsed, "success"))
            logger.info("转录步骤完成: %s，用时 %.2fs", name, elapsed)
            if console_success:
                logger.info(
                    "%s，用时 %.2fs",
                    console_success,
                    elapsed,
                    extra={"console": True},
                )

    def _log_timing_summary(self, status: str, total_elapsed: float):
        summary = ", ".join(
            f"{name}={elapsed:.2f}s/{step_status}"
            for name, elapsed, step_status in self._step_timings
        )
        logger.info(
            "转录任务耗时汇总: status=%s, total=%.2fs, steps=[%s]",
            status,
            total_elapsed,
            summary,
        )
        status_text = {
            "success": "转录任务完成",
            "failed": "转录任务失败",
            "canceled": "转录任务已取消",
            "skipped_existing_subtitle": "转录任务跳过",
        }.get(status, f"转录任务结束({status})")
        logger.info(
            "%s，总用时 %.2fs",
            status_text,
            total_elapsed,
            extra={"console": True},
        )

    def _cleanup_runtime_cache(self):
        config = self.task.transcribe_config
        if not config or not config.transcribe_model:
            return
        if config.transcribe_model == TranscribeModelEnum.QWEN_LOCAL_ASR:
            logger.info("关闭 Qwen Local 隔离 worker")
            close_qwen_worker_pool()
            return
        if config.transcribe_model in {
            TranscribeModelEnum.MIMO_ASR_API,
        }:
            logger.info("关闭 MiMo/Qwen 对齐隔离 worker")
            close_qwen_worker_pool()
            clear_qwen_model_cache()

    def _cleanup_temp_audio(self):
        if self._temp_audio_path:
            Path(self._temp_audio_path).unlink(missing_ok=True)
            self._temp_audio_path = None

    def _cleanup_temp_workspace(self):
        if self._temp_workspace_path:
            shutil.rmtree(self._temp_workspace_path, ignore_errors=True)
            self._temp_workspace_path = None

    def _validate_task(self):
        """验证任务配置"""
        if not self.task.transcribe_config:
            raise ValueError(self.tr("转录配置为空"))

        if not self.task.output_path:
            raise ValueError(self.tr("输出路径为空"))

        if not self.task.file_path:
            raise ValueError(self.tr("文件路径为空"))

        video_path = Path(self.task.file_path)
        if not video_path.exists():
            logger.error(f"视频文件不存在：{video_path}")
            raise ValueError(self.tr("视频文件不存在"))

    def _check_downloaded_subtitle(self) -> bool:
        """检查是否存在下载的字幕文件"""
        if not (self.task.need_next_task and self.task.file_path):
            return False

        subtitle_dir = Path(self.task.file_path).parent / "subtitle"
        if not subtitle_dir.exists():
            return False

        downloaded_subtitles = list(subtitle_dir.glob("【下载字幕】*"))
        if not downloaded_subtitles:
            return False

        subtitle_file = downloaded_subtitles[0]
        imported = import_subtitle(subtitle_file)
        self.task.result_data = clone_subtitle_data(imported.data)
        canonical, _exported, warning = TaskFactory.save_stage_subtitle(
            imported.data,
            self.task.output_path or "",
            layout=imported.layout,
            export_policy=self.task.export_policy if self.task.need_next_task else None,
        )
        self.task.output_path = canonical
        if warning:
            logger.warning("转录字幕自动导出失败，继续 workflow: %s", warning)
        logger.info(f"字幕文件已下载，跳过转录。找到下载的字幕文件：{subtitle_file}")
        self.progress.emit(100, self.tr("字幕已下载"))
        publish_stage_summary(
            StageSummary(
                "transcribe",
                [("段", len(imported.data.segments))],
                status="downloaded",
            )
        )
        self.finished.emit(self.task)
        return True

    def _perform_transcription(self):
        """执行转录流程"""
        assert self.task.file_path is not None
        assert self.task.transcribe_config is not None
        assert self.task.output_path is not None

        video_path = Path(self.task.file_path)

        self.progress.emit(5, self.tr("转换音频中"))

        with self._timed_step("create_temp_workspace"):
            temp_workspace_path = tempfile.mkdtemp(
                prefix=".videocaptioner-",
                dir=str(video_path.parent),
            )
            self._temp_workspace_path = temp_workspace_path
            temp_audio_path = str(Path(temp_workspace_path) / "source.wav")
            self._temp_audio_path = temp_audio_path
            logger.info(
                "转录临时工作目录: workspace=%s, audio=%s",
                temp_workspace_path,
                temp_audio_path,
            )
        previous_runtime_temp_dir = self.task.transcribe_config.runtime_temp_dir
        self.task.transcribe_config.runtime_temp_dir = temp_workspace_path

        try:
            # 转换音频文件
            # 获取选中的音轨索引（如果有）
            with self._timed_step(
                "convert_video_to_audio",
                console_start="开始转换音频",
                console_success="音频转换完成",
                console_failure="音频转换失败",
            ):
                audio_track_index = self.task.selected_audio_track_index
                logger.info(
                    "开始转换音频: input=%s, output=%s, audio_track_index=%s, loudnorm=%s",
                    video_path,
                    temp_audio_path,
                    audio_track_index,
                    self.task.transcribe_config.audio_loudnorm,
                )
                is_success = video2audio(
                    str(video_path),
                    output=temp_audio_path,
                    audio_track_index=audio_track_index,
                    loudnorm=self.task.transcribe_config.audio_loudnorm,
                )
            self._raise_if_cancelled()
            if not is_success:
                logger.error("音频转换失败")
                raise RuntimeError(self.tr("音频转换失败"))

            self.progress.emit(20, self.tr("语音转录中"))

            # 进行转录
            with self._timed_step(
                "run_asr_transcription",
                console_start="开始语音转录",
                console_success="语音转录完成",
                console_failure="语音转录失败",
            ):
                asr_data = transcribe(
                    temp_audio_path,
                    self.task.transcribe_config,
                    callback=self.progress_callback,
                )
                logger.info("语音转录返回: segments=%s", len(asr_data.segments))
            self._raise_if_cancelled()
            with self._timed_step("save_subtitle_outputs"):
                self._save_asr_data(asr_data)

            self.progress.emit(100, self.tr("转录完成"))
            publish_stage_summary(
                StageSummary("transcribe", [("段", len(asr_data.segments))])
            )
            self.finished.emit(self.task)
        finally:
            self.task.transcribe_config.runtime_temp_dir = previous_runtime_temp_dir
            with self._timed_step("cleanup_temp_files"):
                self._cleanup_temp_audio()
                self._cleanup_temp_workspace()

    def _save_asr_data(self, asr_data):
        assert self.task.transcribe_config is not None
        assert self.task.output_path is not None

        self.task.result_data = clone_subtitle_data(asr_data)
        output_path = Path(self.task.output_path)
        if self.task.need_next_task:
            canonical, _exported, warning = TaskFactory.save_stage_subtitle(
                asr_data,
                str(output_path),
                layout=(
                    self.task.export_policy.layout
                    if self.task.export_policy
                    else SubtitleLayoutEnum.ONLY_ORIGINAL
                ),
                export_policy=self.task.export_policy,
            )
            self.task.output_path = canonical
            if warning:
                logger.warning("转录字幕自动导出失败，继续 workflow: %s", warning)
            return

        # Standalone transcription keeps its format choices, but canonical SRT
        # is always written first and cannot be disabled.
        save_canonical_srt(asr_data, output_path)
        output_format_enum = (
            self.task.transcribe_config.output_format or TranscribeOutputFormatEnum.SRT
        )
        base_path = output_path.with_suffix("")

        if output_format_enum == TranscribeOutputFormatEnum.ALL:
            formats_to_export = [
                fmt.value.lower()
                for fmt in TranscribeOutputFormatEnum
                if fmt != TranscribeOutputFormatEnum.ALL
            ]
        else:
            formats_to_export = [output_format_enum.value.lower()]

        formats_to_export = list(set(formats_to_export))

        for fmt in formats_to_export:
            self._raise_if_cancelled()
            save_path = f"{base_path}.{fmt}"
            if fmt == "srt":
                continue
            policy = self.task.export_policy
            export_subtitle_atomic(
                asr_data,
                save_path,
                export_format=fmt,
                layout=(policy.layout if policy else SubtitleLayoutEnum.ONLY_ORIGINAL),
                ass_style=(policy.ass_style if policy else None),
                reference_resolution=(
                    (policy.reference_width, policy.reference_height)
                    if policy
                    else (1280, 720)
                ),
            )
            logger.info("%s 字幕文件已保存到: %s", fmt.upper(), save_path)

    def progress_callback(self, value, message):
        self._raise_if_cancelled()
        progress = min(20 + (value * 0.8), 100)
        self.progress.emit(int(progress), message)
