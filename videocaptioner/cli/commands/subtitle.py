"""subtitle command — optimize and/or translate subtitle files."""

import os
from argparse import Namespace
from pathlib import Path

from videocaptioner.cli import exit_codes as EXIT
from videocaptioner.cli import output
from videocaptioner.cli.config import get

# BCP 47 → TargetLanguage.value (Chinese label) mapping for internal use
_LANG_MAP = {
    "zh-Hans": "简体中文",
    "zh-Hant": "繁体中文",
    "en": "英语",
    "en-US": "英语(美国)",
    "en-GB": "英语(英国)",
    "ja": "日本語",
    "ko": "韩语",
    "yue": "粤语",
    "th": "泰语",
    "vi": "越南语",
    "id": "印尼语",
    "ms": "马来语",
    "tl": "菲律宾语",
    "fr": "法语",
    "de": "德语",
    "es": "西班牙语",
    "es-419": "西班牙语(拉丁美洲)",
    "ru": "俄语",
    "pt": "葡萄牙语",
    "pt-BR": "葡萄牙语(巴西)",
    "pt-PT": "葡萄牙语(葡萄牙)",
    "it": "意大利语",
    "nl": "荷兰语",
    "pl": "波兰语",
    "tr": "土耳其语",
    "el": "希腊语",
    "cs": "捷克语",
    "sv": "瑞典语",
    "da": "丹麦语",
    "fi": "芬兰语",
    "nb": "挪威语",
    "hu": "匈牙利语",
    "ro": "罗马尼亚语",
    "bg": "保加利亚语",
    "uk": "乌克兰语",
    "ar": "阿拉伯语",
    "he": "希伯来语",
    "fa": "波斯语",
}


def _display_usage_value(value: object) -> str:
    return "不可用" if value is None else str(value)


def _resolve_target_language(code: str):
    """Resolve a BCP 47 code to a TargetLanguage enum value (case-insensitive)."""
    from videocaptioner.core.translate.types import TargetLanguage

    # Case-insensitive lookup in _LANG_MAP
    code_lower = code.lower()
    label = next((v for k, v in _LANG_MAP.items() if k.lower() == code_lower), None)
    if label:
        for lang in TargetLanguage:
            if lang.value == label:
                return lang

    # Fallback: try direct match against enum values
    for lang in TargetLanguage:
        if lang.value == code or lang.name.lower() == code.lower():
            return lang

    output.error(f"Unknown target language: {code}")
    output.hint(f"Supported codes: {', '.join(_LANG_MAP.keys())}")
    return None


def run(args: Namespace, config: dict) -> int:
    input_path = Path(args.input)
    input_data = getattr(args, "input_data", None)
    if input_data is None and not input_path.exists():
        output.error(f"Input file not found: {input_path}")
        return EXIT.FILE_NOT_FOUND

    from videocaptioner.cli.validators import validate_subtitle_input

    if input_data is None:
        err = validate_subtitle_input(input_path)
        if err is not None:
            return err

    need_optimize = get(config, "subtitle.optimize", True)
    need_translate = get(config, "subtitle.translate", False)
    need_split = get(config, "subtitle.split", True)

    # If user explicitly specified translator or target language, enable translation
    explicitly_wants_translate = (
        getattr(args, "translator", None)
        or getattr(args, "translation_mode", None)
        or getattr(args, "target_language", None)
    )
    explicitly_no_translate = getattr(args, "no_translate", False)
    if explicitly_wants_translate and explicitly_no_translate:
        output.warn(
            "--no-translate conflicts with --translator/--target-language; translation will be skipped"
        )
    elif explicitly_wants_translate:
        need_translate = True
    translator_service = get(config, "translate.service", "bing")
    translation_mode = str(get(config, "translate.mode", "enhanced_llm"))
    valid_modes = {"non_llm", "single_llm", "enhanced_llm"}
    if translation_mode not in valid_modes:
        output.error(f"Unsupported translation mode: {translation_mode}")
        output.hint("Supported modes: non_llm, single_llm, enhanced_llm")
        return EXIT.USAGE_ERROR
    if need_translate and translation_mode == "non_llm" and translator_service == "llm":
        output.error("translate.service=llm is incompatible with non_llm mode")
        output.hint("Choose bing, google, or deeplx, or use enhanced_llm mode")
        return EXIT.USAGE_ERROR
    llm_translation = need_translate and translation_mode != "non_llm"

    # Validate AFTER resolving the actual need_translate / need_optimize state
    needs_llm = (
        need_optimize
        or need_split
        or llm_translation
    )
    if needs_llm:
        from videocaptioner.cli.validators import validate_llm

        if not validate_llm(config):
            return EXIT.USAGE_ERROR
    target_lang_code = get(config, "translate.target_language", "zh-Hans")
    need_reflect = get(config, "translate.reflect", False)
    if need_reflect and translation_mode != "single_llm":
        output.warn("--reflect only works with single_llm mode; ignored")
        need_reflect = False
    if get(config, "translate.glossary_path", "") and translation_mode != "enhanced_llm":
        output.warn("--glossary only works with enhanced_llm mode; ignored")
    if get(config, "translate.review_prompt", "") and translation_mode != "enhanced_llm":
        output.warn("--review-prompt only works with enhanced_llm mode; ignored")

    # Warn on conflicting/ignored options
    if not need_translate and getattr(args, "layout", None):
        output.warn("--layout has no effect without translation (no bilingual output)")
    prompt_arg = getattr(args, "prompt", None)
    prompt_file_arg = getattr(args, "prompt_file", None)
    if (prompt_arg or prompt_file_arg) and not llm_translation:
        output.warn("--prompt/--prompt-file only configures LLM translation")

    thread_num = get(config, "subtitle.thread_num", 4)
    batch_size = get(config, "subtitle.batch_size", 20)

    # Validate numeric ranges
    if thread_num < 1:
        output.error("--thread-num must be at least 1")
        return EXIT.USAGE_ERROR
    if batch_size < 1:
        output.error("--batch-size must be at least 1")
        return EXIT.USAGE_ERROR
    if (
        need_translate
        and translation_mode == "enhanced_llm"
        and int(get(config, "translate.enhanced_batch_size", 10)) < 1
    ):
        output.error("translate.enhanced_batch_size must be at least 1")
        return EXIT.USAGE_ERROR
    layout_str = get(config, "synthesize.layout", "target-above")
    verbose = getattr(args, "verbose", False)
    quiet = getattr(args, "quiet", False)

    # Build output path
    clean_stem = input_path.stem
    for prefix in ("【转录字幕】", "【原始字幕】", "【初版字幕】", "【后处理字幕】"):
        clean_stem = clean_stem.removeprefix(prefix)
    initial_name = f"【初版字幕】{clean_stem}"
    if args.output:
        out = Path(args.output)
        if out.is_dir() or str(args.output).endswith(("/", "\\")):
            out.mkdir(parents=True, exist_ok=True)
            suffix = f"_{target_lang_code}" if need_translate else "_optimized"
            output_path = str(out / f"{initial_name}{suffix}.srt")
        else:
            output_path = str(out.with_suffix(".srt"))
            if out.suffix and out.suffix.lower() != ".srt" and not quiet:
                output.warn(
                    f"Subtitle stages persist canonical SRT; output changed to {output_path}"
                )
    else:
        suffix = f"_{target_lang_code}" if need_translate else "_optimized"
        output_path = str(input_path.with_name(f"{initial_name}{suffix}.srt"))

    # Setup LLM environment
    llm_api_key = get(config, "llm.api_key", "")
    llm_api_base = get(config, "llm.api_base", "")
    llm_model = get(config, "llm.model", "")
    if llm_api_key:
        os.environ["OPENAI_API_KEY"] = llm_api_key
    if llm_api_base:
        os.environ["OPENAI_BASE_URL"] = llm_api_base

    # Load custom prompt (only if LLM features are needed)
    custom_prompt = getattr(args, "prompt", None) or get(config, "translate.main_prompt", "")
    prompt_file = getattr(args, "prompt_file", None)
    if prompt_file and llm_translation:
        p = Path(prompt_file)
        if not p.exists():
            output.error(f"Prompt file not found: {prompt_file}")
            return EXIT.FILE_NOT_FOUND
        custom_prompt = p.read_text(encoding="utf-8")

    if verbose:
        output.info(f"Optimize: {need_optimize}, Translate: {need_translate}")
        if need_translate:
            output.info(
                f"Translation mode: {translation_mode}, service: {translator_service}, "
                f"target: {target_lang_code}"
            )
        if needs_llm and llm_model:
            output.info(f"LLM: {llm_model} @ {llm_api_base}")

    from videocaptioner.cli.validators import resolve_layout

    layout = resolve_layout(layout_str)

    # Load subtitle data
    from videocaptioner.core.subtitle.io import clone_subtitle_data, import_subtitle

    asr_data = (
        clone_subtitle_data(input_data)
        if input_data is not None
        else import_subtitle(input_path, layout_hint=layout).data
    )

    if len(asr_data.segments) == 0 and not quiet:
        output.warn(f"Input file contains 0 subtitle segments: {input_path}")

    progress = None if quiet else output.ProgressLine("Processing subtitles").start()
    _done_count = 0
    _total_count = max(len(asr_data.segments), 1)
    # Level-independent per-stage summaries, rendered at the orchestration
    # boundary just before the final "Done" line (see ADR-0009).
    from videocaptioner.core.utils.stage_summary import (
        StageSummary,
        build_optimize_stage_summary,
        build_split_stage_summary,
        build_translate_stage_summary,
    )

    stage_summaries: list[StageSummary] = []
    enhanced_usages = ()
    enhanced_artifact_paths: tuple[str, str] | None = None

    def callback(result):
        nonlocal _done_count
        if progress:
            _done_count += len(result) if hasattr(result, "__len__") else 1
            pct = min(int(_done_count / _total_count * 100), 95)
            progress.update(pct)

    try:
        # 1. Preserve the original subtitle optimization behavior: ordinary cue-level
        # subtitles are first expanded to estimated word timestamps, then re-segmented.
        if need_split and not asr_data.is_word_timestamp():
            asr_data.split_to_word_segments()

        if asr_data.is_word_timestamp():
            if progress:
                message = "Splitting subtitles..." if need_split else "Merging word subtitles..."
                progress.update(5, message)
            from videocaptioner.core.split.split import SubtitleSplitter

            splitter = SubtitleSplitter(
                thread_num=thread_num,
                model=llm_model,
                max_word_count_cjk=get(config, "subtitle.max_word_count_cjk", 18),
                max_word_count_english=get(config, "subtitle.max_word_count_english", 12),
                use_llm=need_split,
            )
            asr_data = splitter.split_subtitle(asr_data)
            fallbacks = getattr(splitter, "rule_fallback_segments", 0)
            stage_summaries.append(
                build_split_stage_summary(
                    len(asr_data.segments),
                    use_llm=need_split,
                    fallback_count=fallbacks,
                )
            )

        # 2. Optimize
        if need_optimize:
            if progress:
                progress.update(20, "Optimizing subtitles...")
            from videocaptioner.core.optimize.optimize import SubtitleOptimizer

            optimizer = SubtitleOptimizer(
                thread_num=thread_num,
                batch_num=batch_size,
                model=llm_model,
                custom_prompt=get(config, "subtitle.optimization_prompt", ""),
                update_callback=callback,
                extra_rules="",
            )
            asr_data = optimizer.optimize_subtitle(asr_data)
            failed = getattr(optimizer, "failed_batches", 0)
            maxed = getattr(optimizer, "maxed_batches", 0)
            stage_summaries.append(
                build_optimize_stage_summary(
                    len(asr_data.segments),
                    failed_batches=failed,
                    maxed_batches=maxed,
                )
            )

        # 3. Translate
        if need_translate:
            if progress:
                progress.update(60, f"Translating to {target_lang_code}...")

            target_language = _resolve_target_language(target_lang_code)
            if not target_language:
                if progress:
                    progress.finish()  # Clean spinner without duplicate error
                return EXIT.USAGE_ERROR

            if translation_mode == "enhanced_llm":
                from videocaptioner.cli.config import build_legacy_llm_profile
                from videocaptioner.core.translate.enhanced import run_enhanced_translation
                from videocaptioner.core.translate.enhanced.defaults import (
                    DEFAULT_MAIN_TRANSLATION_PROMPT,
                    DEFAULT_REVIEW_TRANSLATION_PROMPT,
                )
                from videocaptioner.core.translate.enhanced.models import (
                    EnhancedTranslationConfig,
                    TranslationAuditMode,
                    TranslationExecutionMode,
                    TranslationRoleSnapshot,
                )

                glossary_path = str(get(config, "translate.glossary_path", "") or "")
                if glossary_path and not Path(glossary_path).is_file():
                    output.error(f"Glossary file not found: {glossary_path}")
                    if progress:
                        progress.finish()
                    return EXIT.FILE_NOT_FOUND
                profile = build_legacy_llm_profile(config)
                enhanced_config = EnhancedTranslationConfig(
                    main_role=TranslationRoleSnapshot(
                        "main", profile, custom_prompt or DEFAULT_MAIN_TRANSLATION_PROMPT
                    ),
                    review_role=TranslationRoleSnapshot(
                        "review",
                        profile,
                        str(get(config, "translate.review_prompt", ""))
                        or DEFAULT_REVIEW_TRANSLATION_PROMPT,
                    ),
                    source_language="auto",
                    target_language=target_language.value,
                    batch_size=int(get(config, "translate.enhanced_batch_size", 10)),
                    term_context_radius=int(get(config, "translate.term_context_radius", 10)),
                    boundary_context_radius=int(
                        get(config, "translate.boundary_context_radius", 3)
                    ),
                    audit_mode=TranslationAuditMode.AUTO_FIX_OBJECTIVE,
                    execution_mode=TranslationExecutionMode.CLI,
                )
                enhanced_run = run_enhanced_translation(
                    asr_data,
                    enhanced_config,
                    output_dir=Path(output_path).parent,
                    base_name=clean_stem,
                    imported_glossary_path=glossary_path or None,
                    progress=(
                        (lambda value, message: progress.update(value, message))
                        if progress
                        else None
                    ),
                )
                asr_data = enhanced_run.subtitle_data
                args.glossary_path = str(enhanced_run.artifacts.glossary_path)
                args.translation_audit_report_path = str(
                    enhanced_run.artifacts.audit_report_path
                )
                args.translation_audit_report = enhanced_run.result.audit_report
                enhanced_usages = tuple(
                    getattr(enhanced_run.result.audit_report, "usages", ())
                )
                enhanced_artifact_paths = (
                    args.glossary_path,
                    args.translation_audit_report_path,
                )
                failed = 0
            else:
                from videocaptioner.core.translate.factory import TranslatorFactory
                from videocaptioner.core.translate.types import TranslatorType

                type_map = {
                    "bing": TranslatorType.BING,
                    "google": TranslatorType.GOOGLE,
                    "deeplx": TranslatorType.DEEPLX,
                }
                if translation_mode == "single_llm":
                    from videocaptioner.cli.config import build_legacy_llm_profile

                    translator_type = TranslatorType.OPENAI
                    profile = build_legacy_llm_profile(config)
                else:
                    translator_type = type_map.get(translator_service)
                    profile = None
                    if translator_type is None:
                        raise ValueError(
                            f"Unsupported non-LLM translation service: {translator_service}"
                        )
                    deeplx_endpoint = str(get(config, "translate.deeplx_endpoint", ""))
                    if translator_service == "deeplx" and deeplx_endpoint:
                        os.environ["DEEPLX_ENDPOINT"] = deeplx_endpoint
                translator = TranslatorFactory.create_translator(
                    translator_type=translator_type,
                    thread_num=thread_num,
                    batch_num=batch_size,
                    target_language=target_language,
                    model=llm_model,
                    custom_prompt=custom_prompt,
                    is_reflect=need_reflect,
                    update_callback=callback,
                    profile=profile,
                )
                asr_data = translator.translate_subtitle(asr_data)
                failed = getattr(translator, "failed_count", 0)
            stage_summaries.append(
                build_translate_stage_summary(
                    len(asr_data.segments),
                    failed_count=failed,
                )
            )

        # 4. Save the initial subtitle. Postprocessing is a separate command/stage.
        from videocaptioner.core.subtitle.io import save_canonical_srt

        output_path = str(save_canonical_srt(asr_data, output_path, layout=layout))
        args.result_data = asr_data

        if progress:
            progress.finish()  # stop spinner + clear line before the clean summary lines
        if not quiet:
            for stage_summary in stage_summaries:
                output.stage(stage_summary)
            for usage in enhanced_usages:
                output.summary(
                    "usage · "
                    f"{usage.role}/{usage.stage} · {usage.calls} calls · "
                    f"in {_display_usage_value(usage.input_tokens)} · "
                    f"out {_display_usage_value(usage.output_tokens)} · "
                    f"cache-read {_display_usage_value(usage.cache_read_tokens)} · "
                    f"cache-write {_display_usage_value(usage.cache_write_tokens)}"
                )
            if enhanced_artifact_paths is not None:
                output.info(f"Glossary: {enhanced_artifact_paths[0]}")
                output.info(f"Translation audit: {enhanced_artifact_paths[1]}")
        if progress:
            n = len(asr_data.segments)
            output.success(f"Done -> {output_path} ({n} segment{'' if n == 1 else 's'})")
        if quiet:
            print(output_path)
        return EXIT.SUCCESS

    except Exception as e:
        if progress:
            progress.fail(output.clean_error(str(e)))
        else:
            output.error(output.clean_error(str(e)))
        if verbose:
            import traceback

            traceback.print_exc()
        return EXIT.RUNTIME_ERROR
