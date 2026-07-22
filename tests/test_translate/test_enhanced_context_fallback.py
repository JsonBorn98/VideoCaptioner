import json

from videocaptioner.core.llm.models import (
    LLMCallError,
    LLMErrorCategory,
    LLMModelProfile,
    LLMResult,
    LLMTransport,
    ProviderDialect,
)
from videocaptioner.core.translate.enhanced.models import (
    EnhancedTranslationConfig,
    SubtitleCue,
    TranslationRoleSnapshot,
)
from videocaptioner.core.translate.enhanced.orchestrator import (
    EnhancedTranslationOrchestrator,
)


def _profile(profile_id: str) -> LLMModelProfile:
    return LLMModelProfile(
        profile_id=profile_id,
        name=profile_id,
        transport=LLMTransport.OPENAI_COMPATIBLE,
        dialect=ProviderDialect.GENERIC,
        base_url="https://example.test/v1",
        api_key="secret",
        model="model",
        work_context_tokens=65_536,
        max_concurrency=1,
    )


class _FallbackGateway:
    def __init__(self):
        self.analysis_calls = 0
        self.output_limits = []

    def complete(self, profile, request, **_kwargs):
        stage = request.metadata["stage"]
        self.output_limits.append((stage, request.max_output_tokens))
        if stage == "analysis_window":
            self.analysis_calls += 1
            if self.analysis_calls <= 2:
                raise LLMCallError(
                    "maximum context length exceeded",
                    category=LLMErrorCategory.CONTEXT_LIMIT,
                    retryable=False,
                )
            value = {
                "brief": {
                    "outline": "Greeting",
                    "background": "",
                    "themes": [],
                    "style_notes": [],
                    "translation_notes": [],
                },
                "candidates": [],
            }
        elif stage == "translation":
            value = {"translations": [{"id": 1, "text": "你好"}]}
        elif stage == "audit":
            value = {"issues": []}
        else:
            raise AssertionError(stage)
        return LLMResult(text=json.dumps(value, ensure_ascii=False))


def test_provider_context_overflow_replans_at_32k_then_16k_without_mutating_profile():
    main = _profile("main")
    review = _profile("review")
    config = EnhancedTranslationConfig(
        main_role=TranslationRoleSnapshot("main", main),
        review_role=TranslationRoleSnapshot("review", review),
        source_language="English",
        target_language="Chinese",
    )
    gateway = _FallbackGateway()

    result = EnhancedTranslationOrchestrator(config, gateway=gateway).run(
        (SubtitleCue(1, "Hello"),)
    )

    assert result.translations == {1: "你好"}
    assert [limit for stage, limit in gateway.output_limits if stage == "analysis_window"] == [
        8192,
        4096,
        2048,
    ]
    assert main.work_context_tokens == 65_536
    assert len(result.audit_report.warnings) == 2
    assert "65536 token 的工作上下文" in result.audit_report.warnings[0]
    assert "32768 token 的工作上下文" in result.audit_report.warnings[1]
    assert all("保存的模型方案未被修改" in item for item in result.audit_report.warnings)
