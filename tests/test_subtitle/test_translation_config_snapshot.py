from dataclasses import replace

import pytest

from videocaptioner.core.entities import SubtitleConfig, TranslatorServiceEnum
from videocaptioner.core.llm.models import (
    LLMModelProfile,
    LLMTransport,
    ProviderDialect,
)
from videocaptioner.core.llm.profiles import LLMModelProfileStore
from videocaptioner.core.translate.enhanced.models import (
    TermConfirmationMode,
    TranslationAuditMode,
    TranslationExecutionMode,
)
from videocaptioner.core.translate.types import TranslationMode
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.task_factory import TaskFactory


def _profile(profile_id: str, model: str) -> LLMModelProfile:
    return LLMModelProfile(
        profile_id=profile_id,
        name=f"{profile_id} profile",
        transport=LLMTransport.OPENAI_COMPATIBLE,
        dialect=ProviderDialect.GENERIC,
        base_url=f"https://{profile_id}.example/v1",
        api_key=f"{profile_id}-key",
        model=model,
        work_context_tokens=65_536,
        max_concurrency=3,
    )


@pytest.mark.parametrize(
    ("main", "review", "single_available", "enhanced_available", "missing"),
    [
        (None, None, False, False, ("main", "review")),
        (_profile("main", "main-v1"), None, True, False, ("review",)),
        (None, _profile("review", "review-v1"), False, False, ("main",)),
        (
            _profile("main", "main-v1"),
            _profile("review", "review-v1"),
            True,
            True,
            (),
        ),
    ],
)
def test_translation_mode_model_availability_matrix(
    main,
    review,
    single_available,
    enhanced_available,
    missing,
):
    config = SubtitleConfig(
        translator_service=TranslatorServiceEnum.BING,
        main_llm_profile=main,
        review_llm_profile=review,
    )

    assert config.is_translation_mode_available(TranslationMode.NON_LLM)
    assert (
        config.is_translation_mode_available(TranslationMode.SINGLE_LLM)
        is single_available
    )
    assert (
        config.is_translation_mode_available(TranslationMode.ENHANCED_LLM)
        is enhanced_available
    )
    assert config.missing_translation_roles(TranslationMode.ENHANCED_LLM) == missing


def test_same_profile_can_fill_both_enhanced_roles():
    shared = _profile("shared", "shared-v1")
    config = SubtitleConfig(
        main_llm_profile=shared,
        review_llm_profile=shared,
    )

    assert config.is_translation_mode_available(TranslationMode.ENHANCED_LLM)


def test_single_llm_task_reuses_frozen_main_profile_in_legacy_fields(tmp_path):
    store = LLMModelProfileStore(tmp_path / "profiles.json")
    main = store.save(_profile("main", "main-v1"))
    old_mode = cfg.translation_mode.value
    old_main_id = cfg.main_llm_profile_id.value
    old_review_id = cfg.review_llm_profile_id.value
    try:
        cfg.set(cfg.translation_mode, TranslationMode.SINGLE_LLM)
        cfg.set(cfg.main_llm_profile_id, main.profile_id)
        cfg.set(cfg.review_llm_profile_id, "")

        task = TaskFactory.create_subtitle_task(
            str(tmp_path / "source.srt"),
            profile_store=store,
        )
        config = task.subtitle_config
        assert config is not None
        assert config.translator_service is TranslatorServiceEnum.OPENAI
        assert config.is_translation_mode_available()
        assert config.main_llm_profile == main
        assert config.review_llm_profile is None
        assert (config.base_url, config.api_key, config.llm_model) == (
            main.base_url,
            main.api_key,
            main.model,
        )
    finally:
        cfg.set(cfg.translation_mode, old_mode)
        cfg.set(cfg.main_llm_profile_id, old_main_id)
        cfg.set(cfg.review_llm_profile_id, old_review_id)


def test_task_factory_freezes_role_profiles_and_role_settings(tmp_path):
    store = LLMModelProfileStore(tmp_path / "profiles.json")
    main = store.save(_profile("main", "main-v1"))
    review = store.save(_profile("review", "review-v1"))
    items = {
        cfg.translation_mode: cfg.translation_mode.value,
        cfg.main_llm_profile_id: cfg.main_llm_profile_id.value,
        cfg.review_llm_profile_id: cfg.review_llm_profile_id.value,
        cfg.main_translation_prompt: cfg.main_translation_prompt.value,
        cfg.review_translation_prompt: cfg.review_translation_prompt.value,
        cfg.enhanced_batch_size: cfg.enhanced_batch_size.value,
        cfg.term_context_radius: cfg.term_context_radius.value,
    }
    try:
        cfg.set(cfg.translation_mode, TranslationMode.ENHANCED_LLM)
        cfg.set(cfg.main_llm_profile_id, main.profile_id)
        cfg.set(cfg.review_llm_profile_id, review.profile_id)
        cfg.set(cfg.main_translation_prompt, "main role prompt")
        cfg.set(cfg.review_translation_prompt, "review role prompt")
        cfg.set(cfg.enhanced_batch_size, 17)
        cfg.set(cfg.term_context_radius, 8)

        task = TaskFactory.create_subtitle_task(
            str(tmp_path / "source.srt"),
            profile_store=store,
            translation_execution_mode=TranslationExecutionMode.GUI_WORKFLOW,
            term_confirmation_mode=TermConfirmationMode.MANUAL,
            translation_audit_mode=TranslationAuditMode.REVIEW_AND_CONFIRM,
            imported_glossary_path=str(tmp_path / "terms.vcglossary.json"),
        )
        config = task.subtitle_config
        assert config is not None

        store.save(replace(main, model="main-v2"))
        store.save(replace(review, model="review-v2"))

        assert config.translation_mode is TranslationMode.ENHANCED_LLM
        assert config.main_llm_profile is not None
        assert config.review_llm_profile is not None
        assert config.main_llm_profile.model == "main-v1"
        assert config.review_llm_profile.model == "review-v1"
        assert config.llm_model == "main-v1"
        assert config.base_url == main.base_url
        assert config.api_key == main.api_key
        assert config.main_translation_prompt == "main role prompt"
        assert config.review_translation_prompt == "review role prompt"
        assert config.enhanced_batch_size == 17
        assert config.term_context_radius == 8
        assert config.term_confirmation_mode is TermConfirmationMode.MANUAL
        assert config.translation_audit_mode is TranslationAuditMode.REVIEW_AND_CONFIRM
        assert config.translation_execution_mode is TranslationExecutionMode.GUI_WORKFLOW
        assert config.imported_glossary_path == str(tmp_path / "terms.vcglossary.json")
    finally:
        for item, value in items.items():
            cfg.set(item, value)


@pytest.mark.parametrize(
    "execution_mode",
    [TranslationExecutionMode.CLI, TranslationExecutionMode.BATCH],
)
def test_noninteractive_task_forces_automatic_terms_and_review_application(
    tmp_path,
    execution_mode,
):
    old_term = cfg.term_confirmation_mode.value
    old_audit = cfg.translation_audit_mode.value
    try:
        cfg.set(cfg.term_confirmation_mode, TermConfirmationMode.MANUAL)
        cfg.set(cfg.translation_audit_mode, TranslationAuditMode.REVIEW_AND_CONFIRM)

        task = TaskFactory.create_subtitle_task(
            str(tmp_path / "source.srt"),
            profile_store=LLMModelProfileStore(tmp_path / "profiles.json"),
            translation_execution_mode=execution_mode,
        )
        config = task.subtitle_config
        assert config is not None
        assert config.term_confirmation_mode is TermConfirmationMode.AUTOMATIC
        assert config.translation_audit_mode is TranslationAuditMode.AUTO_APPLY_REVIEW
    finally:
        cfg.set(cfg.term_confirmation_mode, old_term)
        cfg.set(cfg.translation_audit_mode, old_audit)
