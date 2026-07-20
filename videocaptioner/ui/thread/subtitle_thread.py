import os
import threading
from pathlib import Path
from typing import List, Sequence

from PyQt5.QtCore import QThread, pyqtSignal

from videocaptioner.core.asr.asr_data import ASRData
from videocaptioner.core.entities import (
    SubtitleConfig,
    SubtitleLayoutEnum,
    SubtitleProcessData,
    SubtitleTask,
    TranslatorServiceEnum,
)
from videocaptioner.core.llm.check_llm import check_llm_connection
from videocaptioner.core.llm.context import (
    clear_task_context,
    generate_task_id,
    set_task_context,
    update_stage,
)
from videocaptioner.core.optimize.optimize import SubtitleOptimizer
from videocaptioner.core.split.split import SubtitleSplitter
from videocaptioner.core.subtitle import clone_subtitle_data
from videocaptioner.core.translate.enhanced import (
    CancellationToken,
    EnhancedTranslationConfig,
    run_enhanced_translation,
)
from videocaptioner.core.translate.enhanced.models import (
    TermCandidate,
    TermConfirmationMode,
    TranslationAuditMode,
    TranslationExecutionMode,
    TranslationRoleSnapshot,
)
from videocaptioner.core.translate.factory import TranslatorFactory
from videocaptioner.core.translate.types import TranslationMode, TranslatorType
from videocaptioner.core.utils.logger import setup_logger
from videocaptioner.core.utils.stage_summary import (
    build_optimize_stage_summary,
    build_split_stage_summary,
    build_translate_stage_summary,
)
from videocaptioner.ui.common.log_bridge import publish_stage_summary
from videocaptioner.ui.task_factory import TaskFactory

SERVICE_TO_TYPE = {
    TranslatorServiceEnum.OPENAI: TranslatorType.OPENAI,
    TranslatorServiceEnum.GOOGLE: TranslatorType.GOOGLE,
    TranslatorServiceEnum.BING: TranslatorType.BING,
    TranslatorServiceEnum.DEEPLX: TranslatorType.DEEPLX,
}

logger = setup_logger("subtitle_optimization_thread")


def create_translator_from_config(
    config: SubtitleConfig,
    custom_prompt: str = "",
    callback=None,
):
    """根据 SubtitleConfig 创建翻译器"""
    translator_service = config.translator_service
    if translator_service not in SERVICE_TO_TYPE:
        raise ValueError(f"不支持的翻译服务: {translator_service}")

    if translator_service == TranslatorServiceEnum.DEEPLX:
        os.environ["DEEPLX_ENDPOINT"] = config.deeplx_endpoint or ""

    return TranslatorFactory.create_translator(
        translator_type=SERVICE_TO_TYPE[translator_service],
        thread_num=(
            config.main_llm_profile.max_concurrency
            if config.main_llm_profile is not None
            and config.effective_translation_mode()
            in {TranslationMode.SINGLE_LLM.value, TranslationMode.ENHANCED_LLM.value}
            else config.thread_num
        ),
        batch_num=config.batch_size,
        target_language=config.target_language,
        model=config.llm_model or "",
        custom_prompt=custom_prompt,
        is_reflect=(
            config.need_reflect
            if config.effective_translation_mode() == TranslationMode.SINGLE_LLM.value
            else False
        ),
        update_callback=callback,
        profile=(
            config.main_llm_profile
            if config.effective_translation_mode()
            in {TranslationMode.SINGLE_LLM.value, TranslationMode.ENHANCED_LLM.value}
            else None
        ),
    )


class SubtitleThread(QThread):
    finished = pyqtSignal(str, str)
    progress = pyqtSignal(int, str)
    update = pyqtSignal(dict)
    update_all = pyqtSignal(dict)
    error = pyqtSignal(str)
    cancelled = pyqtSignal()
    term_confirmation_required = pyqtSignal(object)
    audit_ready = pyqtSignal(object)

    def __init__(self, task: SubtitleTask):
        super().__init__()
        self.task: SubtitleTask = task
        self.subtitle_length = 0
        self.finished_subtitle_length = 0
        self.custom_prompt_text = ""
        self.optimizer = None
        self.translator = None
        self.cancellation = CancellationToken()
        self._term_condition = threading.Condition()
        self._confirmed_terms: Sequence[TermCandidate] | None = None

    def set_custom_prompt_text(self, text: str):
        self.custom_prompt_text = text

    def _setup_llm_config(self) -> SubtitleConfig:
        """验证 LLM 配置并设置环境变量，返回 SubtitleConfig"""
        config = self.task.subtitle_config
        if not config:
            raise Exception(self.tr("LLM API 未配置, 请检查LLM配置"))
        base_url = config.utility_llm_base_url or config.base_url
        api_key = config.utility_llm_api_key or config.api_key
        model = config.utility_llm_model or config.llm_model
        if base_url and api_key and model:
            success, message = check_llm_connection(
                base_url,
                api_key,
                model,
            )
            if not success:
                raise Exception(f"{self.tr('LLM API 测试失败: ')}{message or ''}")
            os.environ["OPENAI_BASE_URL"] = base_url
            os.environ["OPENAI_API_KEY"] = api_key
            return config
        raise Exception(self.tr("LLM API 未配置, 请检查LLM配置"))

    @staticmethod
    def _enum(value, enum_type):
        return value if isinstance(value, enum_type) else enum_type(str(value))

    def submit_term_confirmation(self, candidates: Sequence[TermCandidate]) -> None:
        """Resume a manual enhanced task with the user's current selections."""

        with self._term_condition:
            self._confirmed_terms = tuple(candidates)
            self._term_condition.notify_all()

    def _confirm_terms(self, candidates: tuple[TermCandidate, ...]) -> Sequence[TermCandidate]:
        with self._term_condition:
            self._confirmed_terms = None
            self.term_confirmation_required.emit(candidates)
            while self._confirmed_terms is None and not self.cancellation.cancelled:
                self._term_condition.wait(timeout=0.2)
            if self.cancellation.cancelled:
                raise InterruptedError("term confirmation cancelled")
            assert self._confirmed_terms is not None
            return self._confirmed_terms

    def _run_enhanced_translation(
        self, asr_data: ASRData, subtitle_config: SubtitleConfig
    ) -> ASRData:
        if subtitle_config.main_llm_profile is None or subtitle_config.review_llm_profile is None:
            missing = ", ".join(subtitle_config.missing_translation_roles())
            raise ValueError(f"增强型 LLM 翻译缺少模型角色: {missing}")
        if subtitle_config.target_language is None:
            raise ValueError("目标语言未配置")
        config = EnhancedTranslationConfig(
            main_role=TranslationRoleSnapshot(
                "main",
                subtitle_config.main_llm_profile,
                subtitle_config.main_translation_prompt,
            ),
            review_role=TranslationRoleSnapshot(
                "review",
                subtitle_config.review_llm_profile,
                subtitle_config.review_translation_prompt,
            ),
            source_language="auto",
            target_language=subtitle_config.target_language.value,
            batch_size=subtitle_config.enhanced_batch_size,
            term_context_radius=subtitle_config.term_context_radius,
            boundary_context_radius=subtitle_config.boundary_context_radius,
            term_confirmation=self._enum(
                subtitle_config.term_confirmation_mode, TermConfirmationMode
            ),
            audit_mode=self._enum(
                subtitle_config.translation_audit_mode, TranslationAuditMode
            ),
            execution_mode=self._enum(
                subtitle_config.translation_execution_mode, TranslationExecutionMode
            ),
        )
        output_path = Path(self.task.output_path or self.task.subtitle_path)
        run = run_enhanced_translation(
            asr_data,
            config,
            output_dir=output_path.parent,
            base_name=self.task.workflow_base_name or output_path.stem,
            imported_glossary_path=subtitle_config.imported_glossary_path,
            cancellation=self.cancellation,
            progress=lambda value, message: self.progress.emit(value, self.tr(message)),
            confirm_terms=(
                self._confirm_terms
                if config.term_confirmation is TermConfirmationMode.MANUAL
                else None
            ),
        )
        self.task.glossary_path = str(run.artifacts.glossary_path)
        self.task.translation_audit_report_path = str(run.artifacts.audit_report_path)
        self.task.translation_audit_report = run.result.audit_report
        if config.execution_mode is TranslationExecutionMode.GUI_STANDALONE:
            self.audit_ready.emit(run.result.audit_report)
        return run.subtitle_data

    def run(self):
        # 设置任务上下文
        task_file = (
            Path(self.task.video_path) if self.task.video_path else Path(self.task.subtitle_path)
        )
        set_task_context(
            task_id=self.task.task_id,
            file_name=task_file.name,
            stage="subtitle",
        )

        try:
            logger.info(f"\n{self.task.subtitle_config.print_config()}")

            # 字幕文件路径检查、对断句字幕路径进行定义
            subtitle_path = self.task.subtitle_path
            assert subtitle_path is not None, self.tr("字幕文件路径为空")

            subtitle_config = self.task.subtitle_config
            assert subtitle_config is not None, self.tr("字幕配置为空")
            if self.task.input_data is not None:
                asr_data = clone_subtitle_data(self.task.input_data)
            elif self.task.editor_data_json is not None:
                asr_data = ASRData.from_json(self.task.editor_data_json)
            else:
                asr_data = ASRData.from_subtitle_file(
                    subtitle_path,
                    layout=subtitle_config.subtitle_layout,
                )

            # 普通 cue 级字幕仍按原流程生成估算词级时间戳，供上游断句器重组。
            if subtitle_config.need_split and not asr_data.is_word_timestamp():
                asr_data.split_to_word_segments()
                self.update_all.emit(asr_data.to_json())

            # 验证 LLM 配置
            if self.need_legacy_llm(subtitle_config, asr_data):
                self.progress.emit(2, self.tr("开始验证 LLM 配置..."))
                subtitle_config = self._setup_llm_config()

            # 字词级字幕按用户选择执行语义断句或本地快速合并。
            if asr_data.is_word_timestamp():
                update_stage("split")
                use_llm_split = subtitle_config.need_split
                split_message = (
                    self.tr("字幕断句...") if use_llm_split else self.tr("快速合并字幕...")
                )
                self.progress.emit(5, split_message)
                logger.info("正在%s", "LLM字幕断句" if use_llm_split else "快速合并字幕")
                splitter = SubtitleSplitter(
                    thread_num=subtitle_config.thread_num,
                    model=subtitle_config.utility_llm_model or subtitle_config.llm_model,
                    max_word_count_cjk=subtitle_config.max_word_count_cjk,
                    max_word_count_english=subtitle_config.max_word_count_english,
                    use_llm=use_llm_split,
                    progress_callback=lambda completed, total: self.progress.emit(
                        5 + int(completed / max(total, 1) * 10),
                        self.tr("字幕断句 {0}/{1}").format(completed, total),
                    ),
                )
                self.splitter = splitter
                try:
                    asr_data = splitter.split_subtitle(asr_data)
                finally:
                    splitter.stop()
                    self.splitter = None
                self.update_all.emit(asr_data.to_json())
                publish_stage_summary(
                    build_split_stage_summary(
                        len(asr_data.segments),
                        use_llm=use_llm_split,
                        fallback_count=splitter.rule_fallback_segments,
                    )
                )

            # 3. 优化字幕
            context_info = f'The subtitles below are from a file named "{task_file}". Use this context to improve accuracy if needed.\n'
            optimization_prompt = context_info + (subtitle_config.custom_prompt_text or "") + "\n"
            self.subtitle_length = len(asr_data.segments)

            if subtitle_config.need_optimize:
                update_stage("optimize")
                self.progress.emit(0, self.tr("优化字幕..."))
                logger.info("正在优化字幕...")
                self.finished_subtitle_length = 0
                utility_model = subtitle_config.utility_llm_model or subtitle_config.llm_model
                if not utility_model:
                    raise Exception(self.tr("LLM 模型未配置"))
                optimizer = SubtitleOptimizer(
                    thread_num=subtitle_config.thread_num,
                    batch_num=subtitle_config.batch_size,
                    model=utility_model,
                    custom_prompt=optimization_prompt or "",
                    update_callback=self.callback,
                )
                asr_data = optimizer.optimize_subtitle(asr_data)
                self.update_all.emit(asr_data.to_json())
                publish_stage_summary(
                    build_optimize_stage_summary(
                        len(asr_data.segments),
                        failed_batches=optimizer.failed_batches,
                        maxed_batches=optimizer.maxed_batches,
                    )
                )

            # 4. 翻译字幕
            if subtitle_config.need_translate:
                update_stage("translate")
                self.progress.emit(0, self.tr("翻译字幕..."))
                logger.info("正在翻译字幕...")
                self.finished_subtitle_length = 0

                if not subtitle_config.target_language:
                    raise Exception(self.tr("目标语言未配置"))

                if (
                    subtitle_config.effective_translation_mode()
                    == TranslationMode.ENHANCED_LLM.value
                ):
                    asr_data = self._run_enhanced_translation(asr_data, subtitle_config)
                    translator = None
                else:
                    main_prompt = context_info + subtitle_config.main_translation_prompt + "\n"
                    translator = create_translator_from_config(
                        subtitle_config, main_prompt, self.callback
                    )
                    self.translator = translator
                    try:
                        asr_data = translator.translate_subtitle(asr_data)
                    finally:
                        translator.stop()
                        self.translator = None
                self.update_all.emit(asr_data.to_json())
                publish_stage_summary(
                    build_translate_stage_summary(
                        len(asr_data.segments),
                        failed_count=translator.failed_count if translator else 0,
                    )
                )

            # 5. 发布内存快照，并强制保存当前阶段唯一的规范 SRT。
            self.task.result_data = clone_subtitle_data(asr_data)
            canonical, exported, warning = TaskFactory.save_stage_subtitle(
                asr_data,
                self.task.output_path or "",
                layout=subtitle_config.subtitle_layout or SubtitleLayoutEnum.ONLY_TRANSLATE,
                export_policy=(self.task.export_policy if self.task.need_next_task else None),
            )
            self.task.output_path = canonical
            if exported:
                logger.info("初版字幕自动导出到 %s", exported)
            if warning:
                logger.warning("初版字幕自动导出失败，继续 workflow: %s", warning)
            logger.info("初版字幕保存到 %s", canonical)

            completed_message = self.tr("优化完成")
            self.progress.emit(100, completed_message)
            logger.info(completed_message)
            self.finished.emit(self.task.video_path, self.task.output_path)

        except InterruptedError:
            logger.info("字幕处理已取消")
            self.cancelled.emit()
            self.progress.emit(100, self.tr("已取消"))
        except Exception as e:
            logger.exception(f"字幕处理失败: {str(e)}")
            self.error.emit(str(e))
            self.progress.emit(100, self.tr("字幕处理失败"))
        finally:
            clear_task_context()

    def need_legacy_llm(self, subtitle_config: SubtitleConfig, asr_data: ASRData):
        return (
            subtitle_config.need_optimize
            or (subtitle_config.need_split and asr_data.is_word_timestamp())
            or (
                subtitle_config.need_translate
                and subtitle_config.effective_translation_mode()
                == TranslationMode.SINGLE_LLM.value
                and subtitle_config.main_llm_profile is None
            )
        )

    # Backward-compatible public helper retained for existing callers/tests.
    def need_llm(self, subtitle_config: SubtitleConfig, asr_data: ASRData):
        return self.need_legacy_llm(subtitle_config, asr_data)

    def callback(self, result: List[SubtitleProcessData]):
        self.finished_subtitle_length += len(result)
        # 简单计算当前进度（0-100%）
        progress = min(
            int((self.finished_subtitle_length / max(self.subtitle_length, 1)) * 100), 100
        )
        self.progress.emit(progress, self.tr("{0}% 处理字幕").format(progress))
        # 转换为字典格式供UI使用
        result_dict = {
            str(data.index): data.translated_text or data.optimized_text or data.original_text
            for data in result
        }
        self.update.emit(result_dict)

    def stop(self):
        """停止所有处理"""
        try:
            self.cancellation.cancel()
            self.requestInterruption()
            with self._term_condition:
                self._term_condition.notify_all()
            # 先停止优化器
            if hasattr(self, "splitter") and self.splitter:
                try:
                    self.splitter.stop()
                except Exception as e:
                    logger.error(f"停止断句器时出错：{str(e)}")

            if hasattr(self, "optimizer") and self.optimizer:
                try:
                    self.optimizer.stop()  # type: ignore
                except Exception as e:
                    logger.error(f"停止优化器时出错：{str(e)}")

            if self.translator is not None:
                try:
                    self.translator.stop()
                except Exception as e:
                    logger.error(f"停止翻译器时出错：{str(e)}")

            # 等待最多3秒
            if not self.wait(3000):
                logger.warning("线程仍有在途请求，将在请求自然结束后丢弃结果")

            # 发送进度信号
            self.progress.emit(100, self.tr("已终止"))

        except Exception as e:
            logger.error(f"停止线程时出错：{str(e)}")
            self.progress.emit(100, self.tr("终止时发生错误"))


class RetranslateThread(QThread):
    """重新翻译选中行的轻量线程"""

    finished = pyqtSignal(dict)  # {key: translated_text}
    progress = pyqtSignal(int, str)  # (百分比, 状态描述)
    error = pyqtSignal(str)

    def __init__(self, selected_data: dict, subtitle_config: SubtitleConfig, file_name: str = ""):
        """
        selected_data: model._data 中选中的条目，键为行号字符串
        subtitle_config: 当前任务配置
        file_name: 用于日志上下文的文件名
        """
        super().__init__()
        self.selected_data = selected_data
        self.subtitle_config = subtitle_config
        self.file_name = file_name
        self.total = len(selected_data)
        self.done = 0

    def _callback(self, result: List[SubtitleProcessData]):
        self.done += len(result)
        pct = min(int(self.done / self.total * 100), 100)
        self.progress.emit(pct, self.tr("{0}% 翻译中").format(pct))

    def run(self):
        set_task_context(
            task_id=generate_task_id(),
            file_name=self.file_name,
            stage="translate",
        )
        try:
            config = self.subtitle_config
            if not config.target_language:
                raise Exception("目标语言未配置")

            # 设置 LLM 环境变量（LLM 翻译需要）
            if config.translator_service == TranslatorServiceEnum.OPENAI:
                if config.main_llm_profile is not None:
                    pass
                elif not (config.base_url and config.api_key and config.llm_model):
                    raise Exception("LLM API 未配置，请检查 LLM 配置")
                else:
                    os.environ["OPENAI_BASE_URL"] = config.base_url
                    os.environ["OPENAI_API_KEY"] = config.api_key

            # 构建仅含选中行的 ASRData
            asr_data = ASRData.from_json(self.selected_data)

            # 创建翻译器并翻译
            translator = create_translator_from_config(
                config,
                custom_prompt=config.main_translation_prompt,
                callback=self._callback,
            )
            asr_data = translator.translate_subtitle(asr_data)

            # 构建 {原始行号: translated_text} 映射
            keys = list(self.selected_data.keys())
            result = {keys[i]: seg.translated_text for i, seg in enumerate(asr_data.segments)}
            self.finished.emit(result)

        except Exception as e:
            logger.exception(f"重新翻译失败: {e}")
            self.error.emit(str(e))
        finally:
            clear_task_context()
