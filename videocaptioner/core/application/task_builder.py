"""Build executable task dataclasses from canonical application config."""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Optional

from videocaptioner.core.application.app_config import AppConfig
from videocaptioner.core.entities import (
    DubbingTask,
    DubbingUIConfig,
    FullProcessTask,
    SubtitleConfig,
    SubtitleTask,
    SynthesisConfig,
    SynthesisTask,
    TranscribeConfig,
    TranscribeTask,
    TranscriptAndSubtitleTask,
)
from videocaptioner.core.subtitle.style_manager import load_style


class TaskBuilder:
    """Create core tasks without depending on UI widgets or CLI argument shapes."""

    def __init__(self, app_config: AppConfig):
        self.config = app_config

    def get_ass_style(self, style_name: Optional[str] = None) -> str:
        style = load_style(style_name or self.config.subtitle.style_name)
        return style.to_ass_string() if style is not None else ""

    def get_rounded_style(self) -> dict:
        return self.config.synthesis.rounded_style.to_dict()

    def create_transcribe_config(self, *, need_word_timestamp: bool) -> TranscribeConfig:
        settings = self.config.transcribe
        return TranscribeConfig(
            transcribe_model=settings.model,
            transcribe_language=settings.language,
            need_word_time_stamp=need_word_timestamp,
            output_format=settings.output_format,
            whisper_model=settings.whisper_model,
            whisper_api_key=settings.whisper_api_key,
            whisper_api_base=settings.whisper_api_base,
            whisper_api_model=settings.whisper_api_model,
            whisper_api_prompt=settings.whisper_api_prompt,
            fun_asr_api_key=settings.fun_asr_api_key,
            fun_asr_api_base=settings.fun_asr_api_base,
            fun_asr_model=settings.fun_asr_model,
            faster_whisper_program=settings.faster_whisper_program,
            faster_whisper_model=settings.faster_whisper_model,
            faster_whisper_model_dir=settings.faster_whisper_model_dir,
            faster_whisper_device=settings.faster_whisper_device,
            faster_whisper_vad_filter=settings.faster_whisper_vad_filter,
            faster_whisper_vad_threshold=settings.faster_whisper_vad_threshold,
            faster_whisper_vad_method=settings.faster_whisper_vad_method,
            faster_whisper_ff_mdx_kim2=settings.faster_whisper_ff_mdx_kim2,
            faster_whisper_one_word=settings.faster_whisper_one_word,
            faster_whisper_prompt=settings.faster_whisper_prompt,
        )

    def create_transcribe_task(
        self,
        file_path: str,
        need_next_task: bool = False,
        task_id: Optional[str] = None,
    ) -> TranscribeTask:
        file_name = Path(file_path).stem
        need_word_timestamp = self.config.subtitle.need_split if need_next_task else False
        if need_next_task:
            output_path = str(
                Path(self.config.work_dir)
                / file_name
                / "subtitle"
                / (
                    f"【原始字幕】{file_name}-"
                    f"{self.config.transcribe.model.value}-"
                    f"{self.config.transcribe.language_label}.srt"
                )
            )
        else:
            output_path = str(Path(file_path).parent / f"{file_name}.srt")

        task = TranscribeTask(
            queued_at=datetime.datetime.now(),
            file_path=file_path,
            output_path=output_path,
            transcribe_config=self.create_transcribe_config(
                need_word_timestamp=need_word_timestamp
            ),
            need_next_task=need_next_task,
        )
        if task_id:
            task.task_id = task_id
        return task

    def create_subtitle_config(self) -> SubtitleConfig:
        llm = self.config.llm
        settings = self.config.subtitle
        return SubtitleConfig(
            base_url=llm.api_base,
            api_key=llm.api_key,
            llm_model=llm.model,
            deeplx_endpoint=settings.deeplx_endpoint,
            translator_service=settings.translator_service,
            need_reflect=settings.need_reflect,
            need_translate=settings.need_translate,
            need_optimize=settings.need_optimize,
            thread_num=settings.thread_num,
            batch_size=settings.batch_size,
            subtitle_layout=settings.layout,
            subtitle_style=self.get_ass_style(settings.style_name),
            max_word_count_cjk=settings.max_word_count_cjk,
            max_word_count_english=settings.max_word_count_english,
            need_split=settings.need_split,
            target_language=settings.target_language,
            custom_prompt_text=settings.custom_prompt_text,
        )

    def create_subtitle_task(
        self,
        file_path: str,
        video_path: Optional[str] = None,
        need_next_task: bool = False,
        task_id: Optional[str] = None,
    ) -> SubtitleTask:
        output_name = Path(file_path).stem.replace("【原始字幕】", "").replace("【下载字幕】", "")
        suffix = (
            f"-{self.config.subtitle.translator_service.value}"
            if self.config.subtitle.need_translate
            else ""
        )
        output_path = str(
            Path(file_path).parent
            / (
                f"【样式字幕】{output_name}{suffix}.ass"
                if need_next_task
                else f"【字幕】{output_name}{suffix}.srt"
            )
        )

        task = SubtitleTask(
            queued_at=datetime.datetime.now(),
            subtitle_path=file_path,
            video_path=video_path,
            output_path=output_path,
            subtitle_config=self.create_subtitle_config(),
            need_next_task=need_next_task,
        )
        if task_id:
            task.task_id = task_id
        return task

    def create_synthesis_config(self) -> SynthesisConfig:
        settings = self.config.synthesis
        subtitle = self.config.subtitle
        use_style = settings.use_subtitle_style
        return SynthesisConfig(
            need_video=settings.need_video,
            soft_subtitle=settings.soft_subtitle,
            render_mode=settings.render_mode,
            video_quality=settings.video_quality,
            subtitle_layout=subtitle.layout,
            ass_style=self.get_ass_style(subtitle.style_name) if use_style else "",
            rounded_style=self.get_rounded_style() if use_style else None,
        )

    def create_synthesis_task(
        self,
        video_path: str,
        subtitle_path: str,
        need_next_task: bool = False,
        task_id: Optional[str] = None,
    ) -> SynthesisTask:
        output_path = str(Path(video_path).parent / f"【卡卡】{Path(video_path).stem}.mp4")
        task = SynthesisTask(
            queued_at=datetime.datetime.now(),
            video_path=video_path,
            subtitle_path=subtitle_path,
            output_path=output_path,
            synthesis_config=self.create_synthesis_config(),
            need_next_task=need_next_task,
        )
        if task_id:
            task.task_id = task_id
        return task

    def create_dubbing_ui_config(self) -> DubbingUIConfig:
        settings = self.config.dubbing
        return DubbingUIConfig(
            enabled=settings.enabled,
            preset=settings.preset,
            provider=settings.provider,
            api_key=settings.api_key,
            api_base=settings.api_base,
            model=settings.model,
            voice=settings.voice,
            text_track=settings.text_track,
            timing=settings.timing,
            audio_mode=settings.audio_mode,
            tts_workers=settings.tts_workers,
            use_cache=self.config.cache_enabled,
            clone_audio_path=settings.clone_audio_path if settings.supports_clone else "",
            clone_audio_text=settings.clone_audio_text if settings.supports_clone else "",
        )

    def create_dubbing_task(
        self,
        video_path: str,
        subtitle_path: str,
        output_video_path: Optional[str] = None,
        output_audio_path: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> DubbingTask:
        video = Path(video_path) if video_path else None
        subtitle = Path(subtitle_path)
        if output_video_path is None and video:
            output_video_path = str(video.parent / f"【配音】{video.stem}{video.suffix}")
        if output_audio_path is None:
            base_dir = video.parent if video else subtitle.parent
            output_audio_path = str(base_dir / f"【配音音频】{subtitle.stem}.wav")

        task = DubbingTask(
            queued_at=datetime.datetime.now(),
            video_path=video_path or None,
            subtitle_path=subtitle_path,
            output_audio_path=output_audio_path,
            output_video_path=output_video_path,
            dubbing_config=self.create_dubbing_ui_config(),
        )
        if task_id:
            task.task_id = task_id
        return task

    def create_transcript_and_subtitle_task(
        self,
        file_path: str,
        output_path: Optional[str] = None,
    ) -> TranscriptAndSubtitleTask:
        if output_path is None:
            output_path = str(Path(file_path).parent / f"{Path(file_path).stem}_processed.srt")
        return TranscriptAndSubtitleTask(
            queued_at=datetime.datetime.now(),
            file_path=file_path,
            output_path=output_path,
        )

    def create_full_process_task(
        self,
        file_path: str,
        output_path: Optional[str] = None,
        transcribe_config: Optional[TranscribeConfig] = None,
        subtitle_config: Optional[SubtitleConfig] = None,
        synthesis_config: Optional[SynthesisConfig] = None,
        dubbing_config: Optional[DubbingUIConfig] = None,
    ) -> FullProcessTask:
        if output_path is None:
            output_path = str(
                Path(file_path).parent / f"{Path(file_path).stem}_final{Path(file_path).suffix}"
            )

        return FullProcessTask(
            queued_at=datetime.datetime.now(),
            file_path=file_path,
            output_path=output_path,
            transcribe_config=transcribe_config
            or self.create_transcribe_task(file_path, need_next_task=True).transcribe_config,
            subtitle_config=subtitle_config
            or self.create_subtitle_task(
                str(Path(file_path).with_suffix(".srt")),
                file_path,
                need_next_task=True,
            ).subtitle_config,
            synthesis_config=synthesis_config
            or self.create_synthesis_task(
                file_path,
                str(Path(file_path).with_suffix(".ass")),
            ).synthesis_config,
            dubbing_config=dubbing_config or self.create_dubbing_ui_config(),
        )
