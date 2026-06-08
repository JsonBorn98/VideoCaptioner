from typing import Optional

from videocaptioner.core.application import TaskBuilder
from videocaptioner.core.entities import (
    DubbingUIConfig,
    FullProcessTask,
    SubtitleConfig,
    SynthesisConfig,
    TranscribeConfig,
)
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.config_adapter import app_config_from_ui


class TaskFactory:
    """Build UI tasks from the shared application config.

    Task-construction logic lives in core.application.TaskBuilder. This class
    keeps UI call sites small and removes UI settings internals from the executable task layer.
    """

    @staticmethod
    def _builder() -> TaskBuilder:
        return TaskBuilder(app_config_from_ui(cfg))

    @staticmethod
    def get_ass_style(style_name: str) -> str:
        return TaskFactory._builder().get_ass_style(style_name)

    @staticmethod
    def get_rounded_style() -> dict:
        return TaskFactory._builder().get_rounded_style()

    @staticmethod
    def create_transcribe_task(
        file_path: str,
        need_next_task: bool = False,
        task_id: Optional[str] = None,
    ):
        return TaskFactory._builder().create_transcribe_task(
            file_path,
            need_next_task=need_next_task,
            task_id=task_id,
        )

    @staticmethod
    def create_subtitle_task(
        file_path: str,
        video_path: Optional[str] = None,
        need_next_task: bool = False,
        task_id: Optional[str] = None,
    ):
        return TaskFactory._builder().create_subtitle_task(
            file_path,
            video_path=video_path,
            need_next_task=need_next_task,
            task_id=task_id,
        )

    @staticmethod
    def create_synthesis_task(
        video_path: str,
        subtitle_path: str,
        need_next_task: bool = False,
        task_id: Optional[str] = None,
    ):
        return TaskFactory._builder().create_synthesis_task(
            video_path,
            subtitle_path,
            need_next_task=need_next_task,
            task_id=task_id,
        )

    @staticmethod
    def create_dubbing_task(
        video_path: str,
        subtitle_path: str,
        output_video_path: Optional[str] = None,
        output_audio_path: Optional[str] = None,
        task_id: Optional[str] = None,
    ):
        return TaskFactory._builder().create_dubbing_task(
            video_path,
            subtitle_path,
            output_video_path=output_video_path,
            output_audio_path=output_audio_path,
            task_id=task_id,
        )

    @staticmethod
    def create_dubbing_ui_config() -> DubbingUIConfig:
        return TaskFactory._builder().create_dubbing_ui_config()

    @staticmethod
    def create_transcript_and_subtitle_task(file_path: str, output_path: Optional[str] = None):
        return TaskFactory._builder().create_transcript_and_subtitle_task(
            file_path,
            output_path=output_path,
        )

    @staticmethod
    def create_full_process_task(
        file_path: str,
        output_path: Optional[str] = None,
        transcribe_config: Optional[TranscribeConfig] = None,
        subtitle_config: Optional[SubtitleConfig] = None,
        synthesis_config: Optional[SynthesisConfig] = None,
        dubbing_config: Optional[DubbingUIConfig] = None,
    ) -> FullProcessTask:
        return TaskFactory._builder().create_full_process_task(
            file_path,
            output_path=output_path,
            transcribe_config=transcribe_config,
            subtitle_config=subtitle_config,
            synthesis_config=synthesis_config,
            dubbing_config=dubbing_config,
        )
