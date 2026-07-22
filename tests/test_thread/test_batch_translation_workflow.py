from dataclasses import replace

from videocaptioner.core.entities import (
    BatchTaskStatus,
    BatchTaskType,
    FullProcessTask,
    SubtitleConfig,
    TranslatorServiceEnum,
)
from videocaptioner.core.translate.enhanced.models import (
    TermConfirmationMode,
    TranslationAuditMode,
    TranslationExecutionMode,
)
from videocaptioner.core.translate.types import TranslationMode
from videocaptioner.ui.task_factory import TaskFactory
from videocaptioner.ui.thread.batch_process_thread import BatchProcessThread, BatchTask
from videocaptioner.ui.thread.subtitle_pipeline_thread import SubtitlePipelineThread


def _manual_config() -> SubtitleConfig:
    return SubtitleConfig(
        translator_service=TranslatorServiceEnum.BING,
        translation_mode=TranslationMode.NON_LLM,
        main_translation_prompt="frozen prompt",
        term_confirmation_mode=TermConfirmationMode.MANUAL,
        translation_audit_mode=TranslationAuditMode.REVIEW_AND_CONFIRM,
        translation_execution_mode=TranslationExecutionMode.GUI_STANDALONE,
    )


def test_batch_task_forces_unattended_translation_policy(tmp_path, qapp):
    source = tmp_path / "source.srt"
    config = _manual_config()
    batch_task = BatchTask(
        str(source),
        BatchTaskType.SUBTITLE,
        subtitle_config=config,
    )

    assert batch_task.subtitle_config is not None
    assert batch_task.subtitle_config is not config
    assert batch_task.subtitle_config.term_confirmation_mode is TermConfirmationMode.AUTOMATIC
    assert (
        batch_task.subtitle_config.translation_audit_mode
        is TranslationAuditMode.AUTO_APPLY_REVIEW
    )
    assert (
        batch_task.subtitle_config.translation_execution_mode
        is TranslationExecutionMode.BATCH
    )


def test_subtitle_task_reuses_detached_config_snapshot(tmp_path):
    snapshot = _manual_config()
    task = TaskFactory.create_subtitle_task(
        str(tmp_path / "source.srt"),
        config_snapshot=snapshot,
    )

    assert task.subtitle_config == snapshot
    assert task.subtitle_config is not snapshot
    snapshot.main_translation_prompt = "changed after submission"
    assert task.subtitle_config.main_translation_prompt == "frozen prompt"


def test_cancelled_batch_item_is_not_overwritten_by_late_finished(tmp_path, qapp):
    thread = BatchProcessThread()
    cancelled = BatchTask(str(tmp_path / "a.wav"), BatchTaskType.TRANSCRIBE)
    next_item = BatchTask(str(tmp_path / "b.wav"), BatchTaskType.TRANSCRIBE)
    thread.current_tasks = {
        cancelled.file_path: cancelled,
        next_item.file_path: next_item,
    }
    thread.task_queue.put(cancelled)
    thread.task_queue.put(next_item)

    thread.stop_task(cancelled.file_path)
    thread._on_finished_wrapper(cancelled)

    assert cancelled.status is BatchTaskStatus.CANCELLED
    assert list(thread.task_queue.queue) == [next_item]


def test_failed_item_does_not_prevent_dispatching_next_item(tmp_path, qapp, monkeypatch):
    thread = BatchProcessThread()
    first = BatchTask(str(tmp_path / "a.wav"), BatchTaskType.TRANSCRIBE)
    second = BatchTask(str(tmp_path / "b.wav"), BatchTaskType.TRANSCRIBE)
    calls = []

    def handle(task):
        calls.append(task.file_path)
        if task is first:
            raise RuntimeError("first failed")

    monkeypatch.setattr(thread, "_handle_transcribe_task", handle)
    thread._process_task(first)
    thread._process_task(second)

    assert first.status is BatchTaskStatus.FAILED
    assert second.status is BatchTaskStatus.RUNNING
    assert calls == [first.file_path, second.file_path]


def test_unattended_pipeline_forces_automatic_terms(tmp_path, qapp):
    config = _manual_config()
    full_task = FullProcessTask(
        file_path=str(tmp_path / "video.mp4"),
        workflow_base_name="video",
        subtitle_config=config,
    )

    SubtitlePipelineThread(full_task)

    assert full_task.subtitle_config is not config
    assert full_task.subtitle_config is not None
    assert full_task.subtitle_config.term_confirmation_mode is TermConfirmationMode.AUTOMATIC
    assert (
        full_task.subtitle_config.translation_audit_mode
        is TranslationAuditMode.AUTO_APPLY_REVIEW
    )
    assert (
        full_task.subtitle_config.translation_execution_mode
        is TranslationExecutionMode.BATCH
    )


def test_full_process_factory_captures_batch_translation_snapshot(tmp_path, monkeypatch):
    config = _manual_config()
    monkeypatch.setattr(
        TaskFactory,
        "create_subtitle_task",
        lambda *args, **kwargs: type("Task", (), {"subtitle_config": replace(config)})(),
    )

    task = TaskFactory.create_full_process_task(str(tmp_path / "video.mp4"))

    assert task.subtitle_config is not None
    assert task.subtitle_config.translation_execution_mode is TranslationExecutionMode.BATCH
    assert task.subtitle_config.term_confirmation_mode is TermConfirmationMode.AUTOMATIC
