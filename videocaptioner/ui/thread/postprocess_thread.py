from pathlib import Path

from PyQt5.QtCore import QThread, pyqtSignal

from videocaptioner.core.asr.asr_data import ASRData
from videocaptioner.core.llm.context import clear_task_context, set_task_context
from videocaptioner.core.postprocess.models import PostprocessTask
from videocaptioner.core.postprocess.report import build_qa_report
from videocaptioner.core.postprocess.runner import run_postprocess_task
from videocaptioner.core.speed.models import CueSnapshot
from videocaptioner.core.utils.logger import setup_logger
from videocaptioner.ui.task_factory import TaskFactory

logger = setup_logger("subtitle_postprocess_thread")


def _detect_source_language(asr_data: ASRData) -> str:
    from langdetect import LangDetectException, detect

    sample = "\n".join(segment.text for segment in asr_data.segments)[:5000]
    if not sample.strip():
        return ""
    try:
        return detect(sample)
    except LangDetectException:
        return ""


def _resolve_timing(task: PostprocessTask, data: ASRData, _layout):
    from videocaptioner.core.speed.alignment import load_or_align_timing

    snapshots = tuple(
        CueSnapshot.from_input(
            index=index,
            start_ms=segment.start_time,
            end_ms=segment.end_time,
            text=segment.text,
            translated_text=segment.translated_text,
        )
        for index, segment in enumerate(data.segments)
    )
    bundle, issues, cache_hit = load_or_align_timing(
        task.source_subtitle_path,
        task.media_path or "",
        snapshots,
        _detect_source_language(data),
    )
    if issues:
        logger.warning("精准时间轴已降级: %s", "; ".join(issues))
        task.warnings.extend(issue for issue in issues if issue not in task.warnings)
    if cache_hit:
        logger.info("复用精准时间轴缓存: %d 个窗口", len(bundle.windows))
    task.timing_bundle = bundle
    return bundle.windows


class PostprocessThread(QThread):
    """Execute the optional postprocess stage without mutating its input subtitle."""

    finished = pyqtSignal(str, str)
    progress = pyqtSignal(int, str)
    warning = pyqtSignal(str)
    error = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, task: PostprocessTask):
        super().__init__()
        self.task = task
        self.result = None

    def stop(self) -> None:
        """Request cooperative cancellation at the next safe stage boundary."""

        self.requestInterruption()

    def _finish_if_cancelled(self) -> bool:
        if not self.isInterruptionRequested():
            return False
        self.task.status = "cancelled"
        self.cancelled.emit()
        return True

    def run(self) -> None:
        set_task_context(
            task_id=self.task.task_id,
            file_name=Path(self.task.source_subtitle_path).name,
            stage="postprocess",
        )
        try:
            if self._finish_if_cancelled():
                return
            if not self.task.enabled:
                self.progress.emit(10, self.tr("已跳过字幕后处理"))
            else:
                self.progress.emit(5, self.tr("正在读取初版字幕"))

            self.result = run_postprocess_task(
                self.task,
                timing_resolver=_resolve_timing,
            )
            if self._finish_if_cancelled():
                return
            for message in self.result.warnings:
                logger.warning(message)
                self.warning.emit(message)

            active_path = self.task.active_subtitle_path or self.task.initial_subtitle_path or ""
            if self.result.used_fallback:
                self.progress.emit(100, self.tr("字幕后处理已回退到初版字幕"))
            else:
                if self.task.postprocessed_subtitle_path and self.task.status == "completed":
                    canonical, exported, warning = TaskFactory.save_stage_subtitle(
                        self.result.output_data,
                        active_path,
                        layout=self.result.layout,
                        export_policy=self.task.export_policy,
                    )
                    active_path = canonical
                    self.task.postprocessed_subtitle_path = canonical
                    self.task.active_subtitle_path = canonical
                    if exported:
                        logger.info("后处理字幕自动导出到 %s", exported)
                    if warning:
                        message = self.tr("后处理字幕自动导出失败，继续 workflow: ") + warning
                        self.task.warnings.append(message)
                        logger.warning(message)
                        self.warning.emit(message)
                self._write_reports(active_path)
                if self._finish_if_cancelled():
                    return
                self.progress.emit(100, self.tr("字幕后处理完成"))
            self.finished.emit(self.task.media_path or "", active_path)
        except Exception as exc:
            logger.exception("字幕后处理阶段失败: %s", exc)
            initial_path = self.task.initial_subtitle_path or self.task.source_subtitle_path
            if initial_path and Path(initial_path).exists():
                message = self.tr("字幕后处理失败，已回退到初版字幕: ") + str(exc)
                self.task.status = "fallback"
                self.task.error = str(exc)
                self.task.active_subtitle_path = initial_path
                self.warning.emit(message)
                self.progress.emit(100, self.tr("字幕后处理已回退到初版字幕"))
                self.finished.emit(self.task.media_path or "", initial_path)
            else:
                self.error.emit(str(exc))
                self.progress.emit(100, self.tr("字幕后处理失败"))
        finally:
            clear_task_context()

    def _write_reports(self, output_path: str) -> None:
        if self.result is None or not output_path:
            return
        config = self.task.config_snapshot
        if config is not None and config.qa_report:
            qa_path = Path(output_path).with_suffix(".qa.md")
            self.result.report.source_path = self.task.source_subtitle_path
            self.result.report.output_path = output_path
            qa_path.write_text(build_qa_report(self.result.report), encoding="utf-8")
        if self.result.report.speed is not None:
            from videocaptioner.core.speed.report import write_changes

            write_changes(Path(output_path).with_suffix(".speed-changes.json"), self.result.report.speed)
        timing_bundle = self.task.timing_bundle
        if config is not None and config.save_timing_sidecar and timing_bundle is not None:
            from videocaptioner.core.speed.timing_archive import (
                timing_sidecar_path,
                write_timing_archive,
            )

            write_timing_archive(timing_sidecar_path(output_path), timing_bundle)


__all__ = ["PostprocessThread"]
