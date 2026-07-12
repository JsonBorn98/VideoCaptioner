import datetime
import re
from dataclasses import replace
from pathlib import Path
from typing import Optional

from videocaptioner.config import MODEL_PATH
from videocaptioner.core.entities import (
    LANGUAGES,
    FullProcessTask,
    LLMServiceEnum,
    SubtitleConfig,
    SubtitleExportPolicy,
    SubtitleLayoutEnum,
    SubtitleRenderModeEnum,
    SubtitleTask,
    SynthesisConfig,
    SynthesisTask,
    TranscribeConfig,
    TranscribeModelEnum,
    TranscribeTask,
    TranscriptAndSubtitleTask,
)
from videocaptioner.core.postprocess.config import PostprocessConfig
from videocaptioner.core.postprocess.models import PostprocessLayoutMode, PostprocessTask
from videocaptioner.core.postprocess.profiles import PostprocessProfileStore
from videocaptioner.core.subtitle import export_subtitle_atomic, save_canonical_srt
from videocaptioner.ui.common.config import cfg


def _safe_file_stem_from_source(source: str) -> str:
    stem = Path(source).stem or "media"
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" ._")
    return stem or "media"


class TaskFactory:
    """任务工厂类，用于创建各种类型的任务"""

    @staticmethod
    def _load_style_for_render_mode(style_name: str, render_mode: SubtitleRenderModeEnum):
        """Load a style only if it matches the requested render mode."""
        from videocaptioner.core.subtitle.style_manager import StyleMode, load_style

        expected_mode = (
            StyleMode.ROUNDED if render_mode == SubtitleRenderModeEnum.ROUNDED_BG else StyleMode.ASS
        )
        style = load_style(style_name, mode=expected_mode.value)
        if style is not None and style.mode == expected_mode:
            return style

        default_style = load_style("default", mode=expected_mode.value)
        if default_style is not None and default_style.mode == expected_mode:
            return default_style

        return None

    @staticmethod
    def get_ass_style(style_name: str) -> str:
        """获取 ASS 字幕样式内容 (via style_manager, JSON-first with .txt fallback)"""
        style = TaskFactory._load_style_for_render_mode(
            style_name,
            SubtitleRenderModeEnum.ASS_STYLE,
        )
        if style is not None:
            return style.to_ass_string()
        return ""

    @staticmethod
    def get_style_reference(
        style_name: str, render_mode: SubtitleRenderModeEnum
    ) -> tuple[int, int]:
        """获取当前样式的设计基准分辨率。"""
        style = TaskFactory._load_style_for_render_mode(style_name, render_mode)
        if style is not None:
            return style.reference_width, style.reference_height

        return (
            cfg.subtitle_style_reference_width.value,
            cfg.subtitle_style_reference_height.value,
        )

    @staticmethod
    def get_rounded_style(
        reference_width: Optional[int] = None,
        reference_height: Optional[int] = None,
    ) -> dict:
        """获取圆角背景样式配置 (from UI cfg overrides)"""
        return {
            "font_name": cfg.rounded_bg_font_name.value,
            "font_size": cfg.rounded_bg_font_size.value,
            "reference_width": reference_width or cfg.subtitle_style_reference_width.value,
            "reference_height": reference_height or cfg.subtitle_style_reference_height.value,
            "bg_color": cfg.rounded_bg_color.value,
            "text_color": cfg.rounded_bg_text_color.value,
            "corner_radius": cfg.rounded_bg_corner_radius.value,
            "padding_h": cfg.rounded_bg_padding_h.value,
            "padding_v": cfg.rounded_bg_padding_v.value,
            "margin_bottom": cfg.rounded_bg_margin_bottom.value,
            "line_spacing": cfg.rounded_bg_line_spacing.value,
            "letter_spacing": cfg.rounded_bg_letter_spacing.value,
        }

    @staticmethod
    def create_subtitle_export_policy() -> SubtitleExportPolicy:
        """Freeze the one workflow-wide delivery export choice."""
        reference_width, reference_height = TaskFactory.get_style_reference(
            cfg.subtitle_style_name.value,
            SubtitleRenderModeEnum.ASS_STYLE,
        )
        return SubtitleExportPolicy(
            enabled=cfg.get(cfg.workflow_auto_export),
            format=cfg.get(cfg.workflow_export_format),
            layout=cfg.subtitle_layout.value,
            ass_style=TaskFactory.get_ass_style(cfg.subtitle_style_name.value),
            reference_width=reference_width,
            reference_height=reference_height,
        )

    @staticmethod
    def save_stage_subtitle(
        data,
        output_path: str,
        *,
        layout: SubtitleLayoutEnum,
        export_policy: Optional[SubtitleExportPolicy],
    ) -> tuple[str, str | None, str | None]:
        """Save mandatory SRT, then best-effort workflow delivery export."""
        canonical = save_canonical_srt(data, output_path, layout=layout)
        if export_policy is None or not export_policy.enabled:
            return str(canonical), None, None
        try:
            exported = export_subtitle_atomic(
                data,
                canonical.with_suffix(f".{export_policy.format}"),
                export_format=export_policy.format,
                layout=export_policy.layout,
                ass_style=export_policy.ass_style,
                reference_resolution=(
                    export_policy.reference_width,
                    export_policy.reference_height,
                ),
            )
        except Exception as exc:
            return str(canonical), None, str(exc)
        return str(canonical), str(exported), None

    @staticmethod
    def create_transcribe_task(
        file_path: str = "",
        need_next_task: bool = False,
        task_id: Optional[str] = None,
    ) -> TranscribeTask:
        """创建转录任务"""
        # 获取文件名
        file_name = _safe_file_stem_from_source(file_path)
        has_local_file = bool(file_path and Path(file_path).exists())

        # 构建输出路径
        transcribe_model = cfg.transcribe_model.value
        timestamp_models = {
            TranscribeModelEnum.MIMO_ASR_API,
            TranscribeModelEnum.QWEN_LOCAL_ASR,
        }

        if need_next_task:
            need_word_time_stamp = cfg.need_split.value or transcribe_model in timestamp_models
            output_dir = Path(cfg.work_dir.value) / file_name / "subtitle"
        else:
            need_word_time_stamp = transcribe_model in timestamp_models
            output_dir = Path(file_path).parent if has_local_file else Path(cfg.work_dir.value)
        output_path = str(output_dir / f"【转录字幕】{file_name}.srt")

        config = TranscribeConfig(
            transcribe_model=cfg.transcribe_model.value,
            transcribe_language=LANGUAGES[cfg.transcribe_language.value.value],
            need_word_time_stamp=need_word_time_stamp,
            output_format=cfg.transcribe_output_format.value,
            audio_loudnorm=cfg.audio_loudnorm.value,
            # Whisper Cpp 配置
            whisper_model=cfg.whisper_model.value,
            # Whisper API 配置
            whisper_api_key=cfg.whisper_api_key.value,
            whisper_api_base=cfg.whisper_api_base.value,
            whisper_api_model=cfg.whisper_api_model.value,
            whisper_api_prompt=cfg.whisper_api_prompt.value,
            # MiMo ASR API 配置
            mimo_asr_api_key=cfg.mimo_asr_api_key.value,
            mimo_asr_api_base=cfg.mimo_asr_api_base.value,
            mimo_asr_model=cfg.mimo_asr_model.value,
            mimo_asr_timeout=cfg.mimo_asr_timeout.value,
            mimo_asr_concurrency=cfg.mimo_asr_concurrency.value,
            # Qwen ASR / Forced Aligner 配置
            qwen_asr_model=cfg.qwen_asr_model.value,
            qwen_aligner_model=cfg.qwen_aligner_model.value,
            qwen_model_dir=cfg.qwen_model_dir.value,
            qwen_device=cfg.qwen_device.value,
            qwen_dtype=cfg.qwen_dtype.value,
            qwen_max_new_tokens=cfg.qwen_max_new_tokens.value,
            qwen_chunk_overlap_seconds=cfg.qwen_chunk_overlap_seconds.value,
            qwen_compile_aligner=cfg.qwen_compile_aligner.value,
            # Faster Whisper 配置
            faster_whisper_program=cfg.faster_whisper_program.value,
            faster_whisper_model=cfg.faster_whisper_model.value,
            faster_whisper_model_dir=str(MODEL_PATH),
            faster_whisper_device=cfg.faster_whisper_device.value,
            faster_whisper_vad_filter=cfg.faster_whisper_vad_filter.value,
            faster_whisper_vad_threshold=cfg.faster_whisper_vad_threshold.value,
            faster_whisper_vad_method=cfg.faster_whisper_vad_method.value,
            faster_whisper_ff_mdx_kim2=cfg.faster_whisper_ff_mdx_kim2.value,
            faster_whisper_one_word=cfg.faster_whisper_one_word.value,
            faster_whisper_prompt=cfg.faster_whisper_prompt.value,
        )

        task = TranscribeTask(
            queued_at=datetime.datetime.now(),
            file_path=file_path,
            output_path=output_path,
            transcribe_config=config,
            need_next_task=need_next_task,
            workflow_base_name=file_name,
            export_policy=TaskFactory.create_subtitle_export_policy(),
        )
        if task_id:
            task.task_id = task_id
        return task

    @staticmethod
    def create_subtitle_task(
        file_path: str,
        video_path: Optional[str] = None,
        need_next_task: bool = False,
        task_id: Optional[str] = None,
        *,
        workflow_base_name: Optional[str] = None,
        input_data=None,
        export_policy: Optional[SubtitleExportPolicy] = None,
    ) -> SubtitleTask:
        """创建字幕任务"""
        output_name = Path(file_path).stem
        for prefix in ("【原始字幕】", "【下载字幕】", "【初版字幕】", "【后处理字幕】"):
            output_name = output_name.removeprefix(prefix)
        output_name = workflow_base_name or output_name
        translator_suffix = (
            f"-{cfg.translator_service.value.value}" if cfg.need_translate.value else ""
        )
        if translator_suffix and not output_name.endswith(translator_suffix):
            output_name += translator_suffix
        output_path = str(Path(file_path).parent / f"【初版字幕】{output_name}.srt")

        # 根据当前选择的LLM服务获取对应的配置
        current_service = cfg.llm_service.value
        if current_service == LLMServiceEnum.OPENAI:
            base_url = cfg.openai_api_base.value
            api_key = cfg.openai_api_key.value
            llm_model = cfg.openai_model.value
        elif current_service == LLMServiceEnum.SILICON_CLOUD:
            base_url = cfg.silicon_cloud_api_base.value
            api_key = cfg.silicon_cloud_api_key.value
            llm_model = cfg.silicon_cloud_model.value
        elif current_service == LLMServiceEnum.DEEPSEEK:
            base_url = cfg.deepseek_api_base.value
            api_key = cfg.deepseek_api_key.value
            llm_model = cfg.deepseek_model.value
        elif current_service == LLMServiceEnum.OLLAMA:
            base_url = cfg.ollama_api_base.value
            api_key = cfg.ollama_api_key.value
            llm_model = cfg.ollama_model.value
        elif current_service == LLMServiceEnum.LM_STUDIO:
            base_url = cfg.lm_studio_api_base.value
            api_key = cfg.lm_studio_api_key.value
            llm_model = cfg.lm_studio_model.value
        elif current_service == LLMServiceEnum.GEMINI:
            base_url = cfg.gemini_api_base.value
            api_key = cfg.gemini_api_key.value
            llm_model = cfg.gemini_model.value
        elif current_service == LLMServiceEnum.CHATGLM:
            base_url = cfg.chatglm_api_base.value
            api_key = cfg.chatglm_api_key.value
            llm_model = cfg.chatglm_model.value
        else:
            base_url = ""
            api_key = ""
            llm_model = ""

        reference_width, reference_height = TaskFactory.get_style_reference(
            cfg.subtitle_style_name.value,
            SubtitleRenderModeEnum.ASS_STYLE,
        )

        config = SubtitleConfig(
            # 翻译配置
            base_url=base_url,
            api_key=api_key,
            llm_model=llm_model,
            deeplx_endpoint=cfg.deeplx_endpoint.value,
            # 翻译服务
            translator_service=cfg.translator_service.value,
            # 字幕处理
            need_reflect=cfg.need_reflect_translate.value,
            need_translate=cfg.need_translate.value,
            need_optimize=cfg.need_optimize.value,
            thread_num=cfg.thread_num.value,
            batch_size=cfg.batch_size.value,
            # 字幕布局、样式
            subtitle_layout=cfg.subtitle_layout.value,  # Now returns SubtitleLayoutEnum
            subtitle_style=TaskFactory.get_ass_style(cfg.subtitle_style_name.value),
            subtitle_style_reference_width=reference_width,
            subtitle_style_reference_height=reference_height,
            # 字幕分割
            max_word_count_cjk=cfg.max_word_count_cjk.value,
            max_word_count_english=cfg.max_word_count_english.value,
            need_split=cfg.need_split.value,
            # 字幕翻译
            target_language=cfg.target_language.value,
            # 字幕提示
            custom_prompt_text=cfg.custom_prompt_text.value,
        )

        task = SubtitleTask(
            queued_at=datetime.datetime.now(),
            subtitle_path=file_path,
            video_path=video_path,
            output_path=output_path,
            subtitle_config=config,
            need_next_task=need_next_task,
            input_data=input_data,
            workflow_base_name=output_name,
            export_policy=export_policy or TaskFactory.create_subtitle_export_policy(),
        )
        if task_id:
            task.task_id = task_id
        return task

    @staticmethod
    def create_postprocess_task(
        subtitle_path: str,
        video_path: Optional[str] = None,
        *,
        need_next_task: bool = False,
        enabled: Optional[bool] = None,
        layout_mode: PostprocessLayoutMode | str | None = None,
        profile_id: Optional[str] = None,
        config_snapshot: Optional[PostprocessConfig] = None,
        task_id: Optional[str] = None,
        workflow_base_name: Optional[str] = None,
        input_data=None,
        export_policy: Optional[SubtitleExportPolicy] = None,
    ) -> PostprocessTask:
        """Create an immutable-input subtitle postprocess task."""
        profile_item = getattr(cfg, "postprocess_profile", cfg.speed_profile)
        resolved_profile_id = str(profile_id or profile_item.value or "balanced")
        profile_store = PostprocessProfileStore()
        config = config_snapshot or profile_store.resolve_config(resolved_profile_id)

        model_items = {
            LLMServiceEnum.OPENAI: cfg.openai_model,
            LLMServiceEnum.SILICON_CLOUD: cfg.silicon_cloud_model,
            LLMServiceEnum.DEEPSEEK: cfg.deepseek_model,
            LLMServiceEnum.OLLAMA: cfg.ollama_model,
            LLMServiceEnum.LM_STUDIO: cfg.lm_studio_model,
            LLMServiceEnum.GEMINI: cfg.gemini_model,
            LLMServiceEnum.CHATGLM: cfg.chatglm_model,
        }
        model_item = model_items.get(cfg.llm_service.value)
        if config.llm_model is None:
            config = replace(config, llm_model=model_item.value if model_item else None)

        source = Path(subtitle_path)
        clean_name = source.name
        for prefix in ("【初版字幕】", "【后处理字幕】", "【字幕】", "【样式字幕】"):
            clean_name = clean_name.removeprefix(prefix)
        resolved_base_name = workflow_base_name or _safe_file_stem_from_source(clean_name)
        output_path = str(source.with_name(f"【后处理字幕】{resolved_base_name}.srt"))
        enabled_item = getattr(cfg, "postprocess_enabled", None)
        resolved_enabled = (
            enabled
            if enabled is not None
            else enabled_item.value
            if enabled_item is not None
            else True
        )
        if layout_mode is None:
            if need_next_task:
                layout_mode = {
                    SubtitleLayoutEnum.ORIGINAL_ON_TOP: PostprocessLayoutMode.ORIGINAL_ON_TOP,
                    SubtitleLayoutEnum.TRANSLATE_ON_TOP: PostprocessLayoutMode.TRANSLATE_ON_TOP,
                    SubtitleLayoutEnum.ONLY_ORIGINAL: PostprocessLayoutMode.ORIGINAL_ONLY,
                    SubtitleLayoutEnum.ONLY_TRANSLATE: PostprocessLayoutMode.TRANSLATE_ONLY,
                }[cfg.subtitle_layout.value]
            else:
                layout_mode = PostprocessLayoutMode.AUTO

        task = PostprocessTask(
            source_subtitle_path=subtitle_path,
            initial_subtitle_path=subtitle_path,
            postprocessed_subtitle_path=output_path,
            media_path=video_path,
            profile_id=resolved_profile_id,
            config_snapshot=config,
            layout_mode=layout_mode,
            enabled=resolved_enabled,
            need_next_task=need_next_task,
            input_data=input_data,
            workflow_base_name=resolved_base_name,
            export_policy=export_policy or TaskFactory.create_subtitle_export_policy(),
        )
        if task_id:
            task.task_id = task_id
        return task

    @staticmethod
    def create_synthesis_task(
        video_path: str,
        subtitle_path: str,
        need_next_task: bool = False,
        task_id: Optional[str] = None,
        *,
        input_data=None,
    ) -> SynthesisTask:
        """创建视频合成任务"""
        output_path = str(Path(video_path).parent / f"【卡卡】{Path(video_path).stem}.mp4")

        # 只有启用样式时才传入样式配置
        use_style = cfg.use_subtitle_style.value
        reference_width, reference_height = (
            TaskFactory.get_style_reference(
                cfg.subtitle_style_name.value,
                cfg.subtitle_render_mode.value,
            )
            if use_style
            else (
                cfg.subtitle_style_reference_width.value,
                cfg.subtitle_style_reference_height.value,
            )
        )
        # 新引擎编码设置：暂由现有质量档位映射（完整 GUI 控件为后续增量）。
        # 软字幕不消费 encode_settings（走 add_subtitles copy 路），故编码器恒为 x264，
        # 避免 "copy" 值若被误路由到构建器时与 -vf 冲突。
        from videocaptioner.core.synthesis.models import EncodeSettings

        _vq = cfg.video_quality.value
        encode_settings = EncodeSettings(
            video_encoder="x264",
            encode_mode="cq",
            quality=_vq.get_crf(),
            enc_preset=_vq.get_preset(),
            audio_encoder="copy",
            container="mp4",
        )
        config = SynthesisConfig(
            need_video=cfg.need_video.value,
            soft_subtitle=cfg.soft_subtitle.value,
            render_mode=cfg.subtitle_render_mode.value,
            video_quality=cfg.video_quality.value,
            subtitle_layout=cfg.subtitle_layout.value,
            ass_style=TaskFactory.get_ass_style(cfg.subtitle_style_name.value) if use_style else "",
            rounded_style=(
                TaskFactory.get_rounded_style(reference_width, reference_height)
                if use_style
                else None
            ),
            reference_width=reference_width,
            reference_height=reference_height,
            encode_settings=encode_settings,
        )

        task = SynthesisTask(
            queued_at=datetime.datetime.now(),
            video_path=video_path,
            subtitle_path=subtitle_path,
            output_path=output_path,
            synthesis_config=config,
            need_next_task=need_next_task,
            input_data=input_data,
        )
        if task_id:
            task.task_id = task_id
        return task

    @staticmethod
    def create_transcript_and_subtitle_task(
        file_path: str,
        output_path: Optional[str] = None,
        transcribe_config: Optional[TranscribeConfig] = None,
        subtitle_config: Optional[SubtitleConfig] = None,
    ) -> TranscriptAndSubtitleTask:
        """创建转录和字幕任务"""
        if output_path is None:
            output_path = str(Path(file_path).parent / f"{Path(file_path).stem}_processed.srt")

        return TranscriptAndSubtitleTask(
            queued_at=datetime.datetime.now(),
            file_path=file_path,
            output_path=output_path,
            workflow_base_name=_safe_file_stem_from_source(file_path),
            export_policy=TaskFactory.create_subtitle_export_policy(),
        )

    @staticmethod
    def create_full_process_task(
        file_path: str,
        output_path: Optional[str] = None,
        transcribe_config: Optional[TranscribeConfig] = None,
        subtitle_config: Optional[SubtitleConfig] = None,
        synthesis_config: Optional[SynthesisConfig] = None,
    ) -> FullProcessTask:
        """创建完整处理任务（转录+字幕+合成）"""
        if output_path is None:
            output_path = str(
                Path(file_path).parent / f"{Path(file_path).stem}_final{Path(file_path).suffix}"
            )

        workflow_base_name = _safe_file_stem_from_source(file_path)
        export_policy = TaskFactory.create_subtitle_export_policy()
        postprocess_task = TaskFactory.create_postprocess_task(
            file_path,
            file_path,
            need_next_task=True,
            workflow_base_name=workflow_base_name,
            export_policy=export_policy,
        )
        task = FullProcessTask(
            queued_at=datetime.datetime.now(),
            file_path=file_path,
            output_path=output_path,
            workflow_base_name=workflow_base_name,
            export_policy=export_policy,
            transcribe_config=transcribe_config,
            subtitle_config=subtitle_config,
            postprocess_enabled=postprocess_task.enabled,
            postprocess_task=postprocess_task,
            synthesis_config=synthesis_config,
        )
        postprocess_task.task_id = task.task_id
        return task
