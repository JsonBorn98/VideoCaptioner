import json
import threading
import time
from collections import defaultdict

import pytest

import videocaptioner.core.translate.enhanced.orchestrator as orchestrator_module
from videocaptioner.core.llm.adapters import (
    DEFAULT_TIMEOUT_SECONDS,
    request_timeout_seconds,
)
from videocaptioner.core.llm.models import (
    LLMCallError,
    LLMErrorCategory,
    LLMModelProfile,
    LLMResult,
    LLMTransport,
    LLMUsage,
    ProviderDialect,
)
from videocaptioner.core.translate.enhanced.glossary import subtitle_fingerprint
from videocaptioner.core.translate.enhanced.models import (
    AnalysisWindow,
    AuditIssueDisposition,
    AuthoritativeGlossary,
    CancellationToken,
    EnhancedTranslationConfig,
    EnhancedTranslationError,
    GlossaryEntry,
    GlossarySelectionSource,
    SubtitleCue,
    TermConfirmationMode,
    TranslationAuditMode,
    TranslationRoleSnapshot,
)
from videocaptioner.core.translate.enhanced.orchestrator import (
    EnhancedTranslationOrchestrator,
)
from videocaptioner.core.translate.enhanced.token_planner import (
    TokenBudgetExceeded,
    estimate_cues_tokens,
)


def _profile(
    profile_id: str,
    *,
    concurrency: int | None = 1,
    work_context_tokens: int = 16_384,
    max_output_tokens: int | None = None,
    transport: LLMTransport = LLMTransport.OPENAI_COMPATIBLE,
    request_options=None,
) -> LLMModelProfile:
    dialect = (
        ProviderDialect.ANTHROPIC
        if transport is LLMTransport.ANTHROPIC_MESSAGES
        else ProviderDialect.GENERIC
    )
    return LLMModelProfile(
        profile_id=profile_id,
        name=profile_id.title(),
        transport=transport,
        dialect=dialect,
        base_url=f"https://{profile_id}.test/v1",
        api_key="secret",
        model=f"{profile_id}-model",
        work_context_tokens=work_context_tokens,
        max_concurrency=concurrency,
        max_output_tokens=max_output_tokens,
        request_options=request_options or {},
    )


def _config(
    *,
    audit_mode: TranslationAuditMode = TranslationAuditMode.AUTO_APPLY_REVIEW,
    batch_size: int = 10,
    max_concurrency: int = 10,
    term_confirmation: TermConfirmationMode = TermConfirmationMode.AUTOMATIC,
    main_profile: LLMModelProfile | None = None,
    review_profile: LLMModelProfile | None = None,
    source_language: str = "English",
    target_language: str = "简体中文",
) -> EnhancedTranslationConfig:
    return EnhancedTranslationConfig(
        main_role=TranslationRoleSnapshot(
            "main", main_profile or _profile("main"), "MAIN USER PROMPT"
        ),
        review_role=TranslationRoleSnapshot(
            "review", review_profile or _profile("review"), "REVIEW USER PROMPT"
        ),
        source_language=source_language,
        target_language=target_language,
        batch_size=batch_size,
        max_concurrency=max_concurrency,
        audit_mode=audit_mode,
        term_confirmation=term_confirmation,
    )


def _unclamped_config(
    *,
    batch_size: int = 1,
    max_concurrency: int = 2,
    term_confirmation: TermConfirmationMode = TermConfirmationMode.AUTOMATIC,
) -> EnhancedTranslationConfig:
    return _config(
        batch_size=batch_size,
        max_concurrency=max_concurrency,
        main_profile=_profile("main", concurrency=None),
        review_profile=_profile("review", concurrency=None),
        term_confirmation=term_confirmation,
    )


def _analysis(*, candidates=()):
    return {
        "brief": {
            "outline": "A planetary science discussion",
            "background": "An educational video",
            "themes": ["astronomy"],
            "style_notes": ["concise"],
            "translation_notes": ["Use established astronomical names"],
        },
        "candidates": list(candidates),
    }


def _candidate():
    return {
        "id": "mercury-planet",
        "source_term": "Mercury",
        "sense": "the planet",
        "aliases": ["planet Mercury"],
        "occurrence_ids": [1],
    }


def _translations(*items):
    return {"translations": [{"id": cue_id, "text": text} for cue_id, text in items]}


def _output_limit_error(*, attempts: int = 1):
    return LLMCallError(
        "generated output exhausted the configured limit",
        category=LLMErrorCategory.INVALID_RESPONSE,
        retryable=True,
        attempts=attempts,
        finish_reason="length",
    )


class ScriptedGateway:
    def __init__(self, **responses):
        self.responses = {stage: list(values) for stage, values in responses.items()}
        self.calls = []
        self.stage_calls = defaultdict(list)

    def complete(self, profile, request, *, cancelled=None):
        assert cancelled is None or not cancelled()
        stage = request.metadata["stage"]
        self.calls.append((profile, request))
        self.stage_calls[stage].append(request)
        if not self.responses.get(stage):
            raise AssertionError(f"No scripted response for stage {stage!r}")
        value = self.responses[stage].pop(0)
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, LLMResult):
            return value
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return LLMResult(
            text=text,
            usage=LLMUsage(
                input_tokens=10,
                output_tokens=2,
                cache_read_tokens=4,
                cache_write_tokens=1,
            ),
        )

    @property
    def stages(self):
        return [request.metadata["stage"] for _, request in self.calls]

    @property
    def roles(self):
        return [request.metadata["role"] for _, request in self.calls]


def _dynamic_payload(request) -> dict:
    content = request.messages[1].content
    start = content.find("<DYNAMIC_SUBTITLES>")
    end = content.find("</DYNAMIC_SUBTITLES>")
    assert start != -1 and end != -1
    return json.loads(content[start + len("<DYNAMIC_SUBTITLES>") : end].strip())


def _translation_subject_ids(request) -> tuple[int, ...]:
    return tuple(item["id"] for item in _dynamic_payload(request)["translation_subjects"])


def _analysis_cue_ids(request) -> tuple[int, ...]:
    payload = _dynamic_payload(request)
    return tuple(item["id"] for item in payload)


def _audit_subject_ids(request) -> tuple[int, ...]:
    return tuple(item["id"] for item in _dynamic_payload(request)["audit_subjects"])


def _term_candidate_id(request) -> str:
    return str(_dynamic_payload(request)["candidate"]["id"])


def _one_cue_analysis_windows(cues, **kwargs):
    return tuple(AnalysisWindow(cues=(cue,), estimated_input_tokens=1) for cue in cues)


_TERM_STAGES = frozenset({"term_proposal", "term_review", "term_review_final"})


class ConcurrentScriptedGateway:
    """Thread-safe scripted gateway that records in-flight batch shape."""

    def __init__(
        self,
        *,
        delays: dict | None = None,
        translation_overrides: dict[int, object] | None = None,
        default_delay: float = 0.05,
        delayed_stages: frozenset[str] | None = None,
        cancel_stage: str = "translation",
        analysis_candidates=(),
    ) -> None:
        self._lock = threading.Lock()
        self.calls = []
        self.stage_calls = defaultdict(list)
        self.delays = delays or {}
        self.translation_overrides = translation_overrides or {}
        self.default_delay = default_delay
        self.delayed_stages = (
            set(delayed_stages) if delayed_stages is not None else {"translation"}
        )
        self.cancel_stage = cancel_stage
        self.analysis_candidates = list(analysis_candidates)
        self._in_flight: dict[str, int] = defaultdict(int)
        self.max_in_flight: dict[str, int] = defaultdict(int)
        self.started: dict[int, float] = {}
        self.finished: dict[int, float] = {}
        self.stage_started: dict[str, dict] = defaultdict(dict)
        self.stage_finished: dict[str, dict] = defaultdict(dict)
        self.completed_subject_ids: list[int] = []
        self.abandoned_subject_ids: list[int] = []
        self.completed_keys: dict[str, list] = defaultdict(list)
        self.abandoned_keys: dict[str, list] = defaultdict(list)
        self.ready_to_cancel = threading.Event()
        self._first_translation_done = False
        self._first_cancel_stage_done = False

    def _stable_id(self, stage, request):
        if stage == "translation":
            subject_ids = _translation_subject_ids(request)
            return subject_ids[0] if subject_ids else 0
        if stage == "analysis_window":
            cue_ids = _analysis_cue_ids(request)
            return cue_ids[0] if cue_ids else 0
        if stage == "audit":
            subject_ids = _audit_subject_ids(request)
            return subject_ids[0] if subject_ids else 0
        if stage in _TERM_STAGES:
            return _term_candidate_id(request)
        return stage

    def complete(self, profile, request, *, cancelled=None):
        stage = request.metadata["stage"]
        subject_ids = (
            _translation_subject_ids(request) if stage == "translation" else ()
        )
        first_id = subject_ids[0] if subject_ids else 0
        stable_id = self._stable_id(stage, request)
        delay = (
            self.delays.get(stable_id, self.default_delay)
            if stage in self.delayed_stages
            else 0.0
        )
        with self._lock:
            self.calls.append((profile, request))
            self.stage_calls[stage].append(request)
            self._in_flight[stage] += 1
            self.max_in_flight[stage] = max(
                self.max_in_flight[stage], self._in_flight[stage]
            )
            if stage in _TERM_STAGES:
                term_in_flight = sum(self._in_flight[name] for name in _TERM_STAGES)
                self.max_in_flight["term"] = max(
                    self.max_in_flight["term"], term_in_flight
                )
            now = time.perf_counter()
            self.stage_started[stage][stable_id] = now
            if stage == "translation":
                self.started[first_id] = now
            if (
                stage == self.cancel_stage
                and self._first_cancel_stage_done
                and self._in_flight[stage] >= 2
            ):
                self.ready_to_cancel.set()
        abandoned = False
        try:
            deadline = time.perf_counter() + delay
            while time.perf_counter() < deadline:
                if cancelled is not None and cancelled():
                    abandoned = True
                    raise InterruptedError("LLM request cancelled")
                time.sleep(min(0.01, max(0.0, deadline - time.perf_counter())))
            if cancelled is not None and cancelled():
                abandoned = True
                raise InterruptedError("LLM request cancelled")
            if stage in {"analysis_window", "analysis_summary"}:
                value = _analysis(candidates=self.analysis_candidates)
            elif stage == "audit":
                value = {"issues": []}
            elif stage == "term_proposal":
                source = _dynamic_payload(request)["candidate"]["source_term"]
                value = {"translation": f"{source}译", "reason": "ok"}
            elif stage == "term_review":
                value = {
                    "is_term": True,
                    "decision": "accept",
                    "translation": "",
                    "reason": "ok",
                }
            elif stage == "term_review_final":
                value = {"decision": "accept", "translation": "", "reason": "ok"}
            elif stage == "translation":
                if first_id in self.translation_overrides:
                    value = self.translation_overrides[first_id]
                    if isinstance(value, BaseException):
                        raise value
                else:
                    value = _translations(
                        *((cue_id, f"译文 {cue_id}") for cue_id in subject_ids)
                    )
            else:
                raise AssertionError(f"unexpected stage {stage!r}")
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            return LLMResult(
                text=text,
                usage=LLMUsage(
                    input_tokens=10,
                    output_tokens=2,
                    cache_read_tokens=4,
                    cache_write_tokens=1,
                ),
            )
        finally:
            with self._lock:
                self._in_flight[stage] -= 1
                finished_at = time.perf_counter()
                self.stage_finished[stage][stable_id] = finished_at
                if abandoned:
                    self.abandoned_keys[stage].append(stable_id)
                else:
                    self.completed_keys[stage].append(stable_id)
                if stage == "translation":
                    self.finished[first_id] = finished_at
                    if abandoned:
                        self.abandoned_subject_ids.extend(subject_ids)
                    elif not isinstance(
                        self.translation_overrides.get(first_id), BaseException
                    ):
                        self.completed_subject_ids.extend(subject_ids)
                        self._first_translation_done = True
                if stage == self.cancel_stage and not abandoned:
                    self._first_cancel_stage_done = True

    @property
    def stages(self):
        return [request.metadata["stage"] for _, request in self.calls]


def test_enhanced_planners_and_requests_use_each_role_output_cap(monkeypatch):
    main_profile = _profile(
        "main", work_context_tokens=65_536, max_output_tokens=4_000
    )
    review_profile = _profile(
        "review", work_context_tokens=32_768, max_output_tokens=2_000
    )
    analysis_plans = []
    translation_plans = []
    real_analysis_planner = orchestrator_module.plan_analysis_windows
    real_translation_planner = orchestrator_module.plan_translation_batches

    def track_analysis_planner(*args, **kwargs):
        analysis_plans.append(
            (kwargs["working_context_tokens"], kwargs["output_reserve_tokens"])
        )
        return real_analysis_planner(*args, **kwargs)

    def track_translation_planner(*args, **kwargs):
        cues = args[0]
        estimator = kwargs.get("output_reserve_estimator")
        reserve = (
            estimator(cues)
            if estimator is not None
            else kwargs.get("output_reserve_tokens")
        )
        translation_plans.append((kwargs["working_context_tokens"], reserve))
        return real_translation_planner(*args, **kwargs)

    monkeypatch.setattr(orchestrator_module, "plan_analysis_windows", track_analysis_planner)
    monkeypatch.setattr(
        orchestrator_module, "plan_translation_batches", track_translation_planner
    )
    gateway = ScriptedGateway(
        analysis_window=[_analysis()],
        translation=[_translations((1, "译文"))],
        audit=[{"issues": []}],
    )

    EnhancedTranslationOrchestrator(
        _config(main_profile=main_profile, review_profile=review_profile), gateway=gateway
    ).run((SubtitleCue(1, "Source"),))

    subject_tokens = estimate_cues_tokens((SubtitleCue(1, "Source"),))
    translation_reserve = min(int(subject_tokens * 1.2) + 256, 4_000)
    audit_reserve = min(int(subject_tokens * 1.3) + 256, 2_000)
    assert analysis_plans == [(65_536, 4_000)]
    assert translation_plans == [(65_536, translation_reserve), (32_768, audit_reserve)]
    assert gateway.stage_calls["analysis_window"][0].max_output_tokens == 4_000
    assert gateway.stage_calls["translation"][0].max_output_tokens == 4_000
    assert gateway.stage_calls["audit"][0].max_output_tokens == 2_000


def test_anthropic_manual_thinking_conflict_is_rejected_before_any_stage():
    review_profile = _profile(
        "review",
        transport=LLMTransport.ANTHROPIC_MESSAGES,
        request_options={"thinking": {"type": "enabled", "budget_tokens": 4096}},
    )
    gateway = ScriptedGateway()

    with pytest.raises(EnhancedTranslationError) as raised:
        EnhancedTranslationOrchestrator(
            _config(review_profile=review_profile), gateway=gateway
        )

    assert raised.value.stage == "profile_validation"
    assert raised.value.category == "configuration"
    assert "高级校对模型方案无法用于增强翻译" in str(raised.value)
    assert "force tool_choice" in str(raised.value)
    assert gateway.calls == []


def test_context_fallback_auto_recalculates_output_reserve_for_lower_context(monkeypatch):
    main_profile = _profile("main", work_context_tokens=65_536)
    observed_plans = []
    real_analysis_planner = orchestrator_module.plan_analysis_windows

    def track_analysis_planner(*args, **kwargs):
        observed_plans.append(
            (kwargs["working_context_tokens"], kwargs["output_reserve_tokens"])
        )
        return real_analysis_planner(*args, **kwargs)

    monkeypatch.setattr(orchestrator_module, "plan_analysis_windows", track_analysis_planner)
    context_limit = LLMCallError(
        "context window exceeded",
        category=LLMErrorCategory.CONTEXT_LIMIT,
        retryable=False,
    )
    gateway = ScriptedGateway(
        analysis_window=[context_limit, _analysis()],
        translation=[_translations((1, "译文"))],
        audit=[{"issues": []}],
    )

    EnhancedTranslationOrchestrator(
        _config(main_profile=main_profile), gateway=gateway
    ).run((SubtitleCue(1, "Source"),))

    assert observed_plans == [(65_536, 8_192), (32_768, 4_096)]
    request_caps = [
        request.max_output_tokens for request in gateway.stage_calls["analysis_window"]
    ]
    assert request_caps[0] == 32_768
    assert 1_024 <= request_caps[1] < 32_768


def test_context_fallback_never_reduces_below_explicit_output_cap(monkeypatch):
    main_profile = _profile(
        "main", work_context_tokens=65_536, max_output_tokens=20_000
    )
    observed_plans = []
    real_analysis_planner = orchestrator_module.plan_analysis_windows

    def track_analysis_planner(*args, **kwargs):
        observed_plans.append(
            (kwargs["working_context_tokens"], kwargs["output_reserve_tokens"])
        )
        return real_analysis_planner(*args, **kwargs)

    monkeypatch.setattr(orchestrator_module, "plan_analysis_windows", track_analysis_planner)
    context_limit = LLMCallError(
        "context window exceeded",
        category=LLMErrorCategory.CONTEXT_LIMIT,
        retryable=False,
    )
    gateway = ScriptedGateway(analysis_window=[context_limit, context_limit])

    with pytest.raises(EnhancedTranslationError) as raised:
        EnhancedTranslationOrchestrator(
            _config(main_profile=main_profile), gateway=gateway
        ).run((SubtitleCue(1, "Source"),))

    assert raised.value.stage == "analysis_window"
    assert raised.value.category == LLMErrorCategory.CONTEXT_LIMIT.value
    assert "max_output_tokens=20000 leaves no safe runtime context fallback" in str(
        raised.value
    )
    assert observed_plans == [(65_536, 8_192), (32_768, 4_096)]
    assert len(gateway.stage_calls["analysis_window"]) == 2
    assert all(
        request.max_output_tokens == 20_000
        for request in gateway.stage_calls["analysis_window"]
    )


def test_256k_api_cap_is_not_used_as_the_planner_output_reserve():
    role = TranslationRoleSnapshot(
        "main",
        _profile(
            "main",
            work_context_tokens=300_000,
            max_output_tokens=256_000,
        ),
    )

    assert (
        EnhancedTranslationOrchestrator._planning_output_reserve(
            role, 300_000, stage="translation"
        )
        == 8_192
    )
    assert (
        EnhancedTranslationOrchestrator._planning_output_reserve(
            role, 300_000, stage="audit"
        )
        == 4_096
    )
    assert EnhancedTranslationOrchestrator._request_output_caps(
        role,
        300_000,
        1_000,
        stage="translation",
    ) == (256_000,)


def test_planning_output_reserve_scales_with_subject_input_not_fixed_ceiling():
    role = TranslationRoleSnapshot(
        "main",
        _profile("main", work_context_tokens=65_536),
    )
    small = EnhancedTranslationOrchestrator._planning_output_reserve(
        role, 65_536, stage="translation", subject_input_tokens=100
    )
    large = EnhancedTranslationOrchestrator._planning_output_reserve(
        role, 65_536, stage="translation", subject_input_tokens=10_000
    )
    audit = EnhancedTranslationOrchestrator._planning_output_reserve(
        role, 65_536, stage="audit", subject_input_tokens=10_000
    )

    assert small == int(100 * 1.2) + 256
    assert large == int(10_000 * 1.2) + 256
    assert large > 8_192
    assert audit == int(10_000 * 1.3) + 256
    assert audit > large


def test_small_translation_batch_keeps_near_baseline_timeout():
    cues = (SubtitleCue(1, "Source"),)
    gateway = ScriptedGateway(
        analysis_window=[_analysis()],
        translation=[_translations((1, "译文"))],
        audit=[{"issues": []}],
    )
    main_profile = _profile("main", work_context_tokens=65_536)
    review_profile = _profile("review", work_context_tokens=65_536)

    EnhancedTranslationOrchestrator(
        _config(main_profile=main_profile, review_profile=review_profile),
        gateway=gateway,
    ).run(cues)

    request = gateway.stage_calls["translation"][0]
    reserve = EnhancedTranslationOrchestrator._planning_output_reserve(
        TranslationRoleSnapshot("main", main_profile),
        65_536,
        stage="translation",
        subject_input_tokens=estimate_cues_tokens(cues),
    )
    assert request.timeout == request_timeout_seconds(reserve)
    assert request.timeout < DEFAULT_TIMEOUT_SECONDS + 30
    assert request.max_output_tokens == 32_768


def test_main_output_limit_raises_cap_and_reduces_reasoning_together():
    main_profile = _profile(
        "main",
        work_context_tokens=300_000,
        request_options={"reasoning_effort": "high", "metadata": {"mode": "translate"}},
    )
    gateway = ScriptedGateway(
        analysis_window=[_analysis()],
        translation=[_output_limit_error(), _translations((1, "译文"))],
        audit=[{"issues": []}],
    )

    result = EnhancedTranslationOrchestrator(
        _config(main_profile=main_profile), gateway=gateway
    ).run((SubtitleCue(1, "Source"),))

    requests = gateway.stage_calls["translation"]
    assert result.translations == {1: "译文"}
    assert [request.max_output_tokens for request in requests] == [32_768, 65_536]
    assert requests[0].request_options_override is None
    assert requests[1].request_options_override["reasoning_effort"] == "low"
    assert requests[1].request_options_override["metadata"]["mode"] == "translate"
    assert main_profile.request_options["reasoning_effort"] == "high"


def test_review_output_limit_reduces_reasoning_before_raising_cap():
    review_profile = _profile(
        "review",
        work_context_tokens=300_000,
        request_options={"reasoning": {"effort": "xhigh"}},
    )
    gateway = ScriptedGateway(
        analysis_window=[_analysis()],
        translation=[_translations((1, "译文"))],
        audit=[_output_limit_error(), _output_limit_error(), {"issues": []}],
    )

    result = EnhancedTranslationOrchestrator(
        _config(review_profile=review_profile), gateway=gateway
    ).run((SubtitleCue(1, "Source"),))

    requests = gateway.stage_calls["audit"]
    assert result.translations == {1: "译文"}
    assert [request.max_output_tokens for request in requests] == [
        32_768,
        32_768,
        65_536,
    ]
    assert requests[0].request_options_override is None
    assert requests[1].request_options_override["reasoning"]["effort"] == "low"
    assert requests[2].request_options_override["reasoning"]["effort"] == "low"
    assert review_profile.request_options["reasoning"]["effort"] == "xhigh"


def test_adaptive_reasoning_reduces_known_numeric_thinking_budgets():
    role = TranslationRoleSnapshot(
        "main",
        _profile(
            "main",
            request_options={
                "generationConfig": {
                    "thinkingConfig": {"thinkingBudget": 20_000},
                    "topP": 0.9,
                },
                "extra_body": {"thinking_budget": 12_000},
            },
        ),
    )

    lowered = orchestrator_module._lower_reasoning_options(role, 32_768)

    assert lowered["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 4_096
    assert lowered["generationConfig"]["topP"] == 0.9
    assert lowered["extra_body"]["thinking_budget"] == 4_096
    assert role.profile.request_options["extra_body"]["thinking_budget"] == 12_000


def test_automatic_full_chain_uses_directional_term_review_and_three_pass_roles():
    cues = (
        SubtitleCue(1, "Mercury is the closest planet to the Sun."),
        SubtitleCue(2, "It completes an orbit quickly."),
        SubtitleCue(3, "Now compare it with Venus."),
    )
    gateway = ScriptedGateway(
        analysis_window=[_analysis(candidates=[_candidate()])],
        term_proposal=[{"translation": "水银", "reason": "ambiguous word"}],
        term_review=[
            {
                "is_term": True,
                "decision": "uncertain",
                "translation": "",
                "reason": "need more context",
            }
        ],
        term_review_final=[
            {"decision": "correct", "translation": "水星", "reason": "planet context"}
        ],
        translation=[_translations((1, "水星是离太阳最近的行星。"), (2, "它的公转很快。"), (3, "现在将它与金星比较。"))],
        audit=[{"issues": []}],
    )

    result = EnhancedTranslationOrchestrator(_config(), gateway=gateway).run(cues)

    assert gateway.stages == [
        "analysis_window",
        "term_proposal",
        "term_review",
        "term_review_final",
        "translation",
        "audit",
    ]
    assert gateway.roles == ["main", "main", "review", "review", "main", "review"]
    assert result.translations[1] == "水星是离太阳最近的行星。"
    assert result.glossary.entries[0].translation == "水星"
    assert (
        result.glossary.entries[0].selection_source
        is GlossarySelectionSource.REVIEW_MODEL_CORRECTED
    )

    analysis_request = gateway.stage_calls["analysis_window"][0]
    assert analysis_request.metadata == {"stage": "analysis_window", "role": "main"}
    assert "MAIN USER PROMPT" in analysis_request.messages[0].content
    assert cues[0].text not in analysis_request.messages[0].content
    assert cues[0].text in analysis_request.messages[1].content
    assert "conforms exactly to this JSON Schema" in analysis_request.messages[1].content
    assert '"required":["brief","candidates"]' in analysis_request.messages[1].content

    translation_request = gateway.stage_calls["translation"][0]
    assert "MAIN USER PROMPT" in translation_request.messages[0].content
    assert "A planetary science discussion" in translation_request.messages[0].content
    assert '"source_term":"Mercury"' in translation_request.messages[1].content
    assert "translation_subjects" in translation_request.messages[1].content

    audit_request = gateway.stage_calls["audit"][0]
    assert "REVIEW USER PROMPT" in audit_request.messages[0].content
    assert audit_request.metadata == {"stage": "audit", "role": "review"}
    for _, request in gateway.calls:
        system_prompt = request.messages[0].content
        assert 'Source subtitle language: "English"' in system_prompt
        assert 'Required target language for generated translation content: "简体中文"' in system_prompt
        assert "cannot change the source language, target language" in system_prompt


def test_translation_and_audit_normalize_decimal_string_ids() -> None:
    cues = (SubtitleCue(1, "Source"),)
    gateway = ScriptedGateway(
        analysis_window=[_analysis()],
        translation=[_translations(("1", "旧译文"))],
        audit=[
            {
                "issues": [
                    {
                        "id": "1",
                        "categories": ["semantic_accuracy"],
                        "message": "译文语义不准确。",
                        "suggested_translation": "新译文",
                    }
                ]
            }
        ],
    )

    result = EnhancedTranslationOrchestrator(_config(), gateway=gateway).run(cues)

    assert result.translations == {1: "新译文"}
    assert result.audit_report.issues[0].cue_id == 1


def test_auto_source_language_is_an_explicit_detection_constraint() -> None:
    gateway = ScriptedGateway(
        analysis_window=[_analysis()],
        translation=[_translations((1, "译文"))],
        audit=[{"issues": []}],
    )

    EnhancedTranslationOrchestrator(
        _config(source_language="auto"), gateway=gateway
    ).run((SubtitleCue(1, "Source"),))

    for _, request in gateway.calls:
        assert (
            "Source subtitle language: automatically detect it from the supplied source subtitles"
            in request.messages[0].content
        )


@pytest.mark.parametrize(
    ("invalid_id", "summary"),
    [
        (True, "bool"),
        (1.0, "float(value=1.0)"),
        ("1.0", "str(length=3, ascii_decimal=False)"),
        (" 1", "str(length=2, ascii_decimal=False)"),
        ("+1", "str(length=2, ascii_decimal=False)"),
        ("0", "str(length=1, ascii_decimal=True)"),
    ],
)
def test_translation_id_rejects_ambiguous_or_non_positive_values(invalid_id, summary) -> None:
    with pytest.raises(ValueError) as raised:
        EnhancedTranslationOrchestrator._parse_translations(
            _translations((invalid_id, "译文")), {1}
        )

    assert "translations[0].id requires a positive integer ID" in str(raised.value)
    assert summary in str(raised.value)


def test_invalid_id_diagnostics_do_not_echo_model_controlled_text() -> None:
    secret = "private subtitle text must not appear in the error"
    with pytest.raises(ValueError) as raised:
        EnhancedTranslationOrchestrator._parse_audit_issues(
            {
                "issues": [
                    {
                        "id": secret,
                        "categories": ["semantic_accuracy"],
                        "message": "需要修正。",
                        "suggested_translation": "修正后译文",
                    }
                ]
            },
            {1},
            (SubtitleCue(1, secret),),
            {1: secret},
        )

    error = str(raised.value)
    assert "issues[0].id requires a positive integer ID" in error
    assert "str(length=" in error
    assert secret not in error


def test_final_term_review_three_invalid_responses_falls_back_to_source_text():
    cues = (SubtitleCue(1, "Mercury is visible tonight."),)
    invalid_final = {"decision": "uncertain", "translation": "", "reason": "still unsure"}
    gateway = ScriptedGateway(
        analysis_window=[_analysis(candidates=[_candidate()])],
        term_proposal=[{"translation": "水银", "reason": "literal"}],
        term_review=[
            {
                "is_term": True,
                "decision": "uncertain",
                "translation": "",
                "reason": "unsure",
            }
        ],
        term_review_final=[invalid_final, invalid_final, invalid_final],
        translation=[_translations((1, "今晚可以看到 Mercury。"))],
        audit=[{"issues": []}],
    )

    result = EnhancedTranslationOrchestrator(_config(), gateway=gateway).run(cues)

    entry = result.glossary.entries[0]
    assert len(gateway.stage_calls["term_review_final"]) == 3
    assert [len(request.messages) for request in gateway.stage_calls["term_review_final"]] == [
        2,
        4,
        6,
    ]
    assert entry.translation == "Mercury"
    assert entry.high_risk is True
    assert entry.selection_source is GlossarySelectionSource.SOURCE_FALLBACK
    assert result.audit_report.warnings == (
        "术语校对响应无效，已保留原文：Mercury",
    )


def test_translation_rejects_boundary_id_then_retries_with_same_stable_prefix():
    cues = (
        SubtitleCue(1, "First unique source."),
        SubtitleCue(2, "Second unique source."),
        SubtitleCue(3, "Third unique source."),
    )
    gateway = ScriptedGateway(
        analysis_window=[_analysis()],
        translation=[
            _translations((1, "第一句。"), (2, "不应输出的边界句。")),
            _translations((1, "第一句。")),
            _translations((2, "第二句。")),
            _translations((3, "第三句。")),
        ],
        audit=[{"issues": []}, {"issues": []}, {"issues": []}],
    )

    result = EnhancedTranslationOrchestrator(
        _config(batch_size=1), gateway=gateway
    ).run(cues)

    requests = gateway.stage_calls["translation"]
    assert result.translations == {1: "第一句。", 2: "第二句。", 3: "第三句。"}
    assert len(requests) == 4
    assert requests[0].messages[0].content == requests[1].messages[0].content
    assert len(requests[0].messages) == 2
    assert len(requests[1].messages) == 4
    assert "extra=[2]" in requests[1].messages[-1].content
    assert '"allowed_output_ids":[1]' in requests[0].messages[1].content
    assert '"after":[{"id":2' in requests[0].messages[1].content


@pytest.mark.parametrize(
    ("mode", "expected_text", "expected_disposition"),
    [
        (
            TranslationAuditMode.REVIEW_AND_CONFIRM,
            "错误的版本 2",
            AuditIssueDisposition.USER_REJECTED,
        ),
        (
            TranslationAuditMode.AUTO_APPLY_REVIEW,
            "正确的版本 2",
            AuditIssueDisposition.AUTO_APPLIED,
        ),
    ],
)
def test_audit_manual_review_and_automatic_review_application(
    mode, expected_text, expected_disposition
):
    cues = (SubtitleCue(1, "Version 2"),)
    gateway = ScriptedGateway(
        analysis_window=[_analysis()],
        translation=[_translations((1, "错误的版本 2"))],
        audit=[
            {
                "issues": [
                    {
                        "id": 1,
                        "categories": ["semantic_accuracy"],
                        "message": "The version meaning is wrong.",
                        "suggested_translation": "正确的版本 2",
                    }
                ]
            }
        ],
    )

    confirm_audit = (
        (lambda report: ())
        if mode is TranslationAuditMode.REVIEW_AND_CONFIRM
        else None
    )
    result = EnhancedTranslationOrchestrator(
        _config(audit_mode=mode), gateway=gateway
    ).run(cues, confirm_audit=confirm_audit)

    assert result.translations[1] == expected_text
    assert result.audit_report.issues[0].disposition is expected_disposition


def test_manual_audit_confirmation_applies_only_selected_consolidated_suggestions():
    cues = (SubtitleCue(1, "First"), SubtitleCue(2, "Second"))
    gateway = ScriptedGateway(
        analysis_window=[_analysis()],
        translation=[_translations((1, "旧一"), (2, "旧二"))],
        audit=[
            {
                "issues": [
                    {
                        "id": 1,
                        "categories": ["semantic_accuracy", "target_language_quality"],
                        "message": "第一条需要修改。",
                        "suggested_translation": "新一",
                    },
                    {
                        "id": 2,
                        "categories": ["terminology"],
                        "message": "第二条术语错误。",
                        "suggested_translation": "新二",
                    },
                ]
            }
        ],
    )

    result = EnhancedTranslationOrchestrator(
        _config(audit_mode=TranslationAuditMode.REVIEW_AND_CONFIRM), gateway=gateway
    ).run(cues, confirm_audit=lambda report: (2,))

    assert result.translations == {1: "旧一", 2: "新二"}
    assert result.audit_report.issues[0].categories == (
        "semantic_accuracy",
        "target_language_quality",
    )
    assert result.audit_report.issues[0].disposition is AuditIssueDisposition.USER_REJECTED
    assert result.audit_report.issues[1].disposition is AuditIssueDisposition.USER_APPLIED


def test_audit_retries_duplicate_suggestions_for_the_same_subtitle():
    cues = (SubtitleCue(1, "Source"),)
    duplicate = {
        "id": 1,
        "categories": ["semantic_accuracy"],
        "message": "需要修正。",
        "suggested_translation": "新译文",
    }
    gateway = ScriptedGateway(
        analysis_window=[_analysis()],
        translation=[_translations((1, "旧译文"))],
        audit=[
            {"issues": [duplicate, duplicate]},
            {"issues": [duplicate]},
        ],
    )

    result = EnhancedTranslationOrchestrator(
        _config(audit_mode=TranslationAuditMode.AUTO_APPLY_REVIEW), gateway=gateway
    ).run(cues)

    assert result.translations[1] == "新译文"
    assert len(gateway.stage_calls["audit"]) == 2
    assert len(result.audit_report.issues) == 1


def test_invalid_audit_shape_degrades_after_retries_and_keeps_local_audit(
    monkeypatch,
):
    secret = "private model explanation must never enter diagnostics"
    invalid = {"summary": secret, "items": [{"value": 1}, {"value": 2}]}
    gateway = ScriptedGateway(
        analysis_window=[_analysis()],
        translation=[_translations((1, "Source"))],
        audit=[invalid, invalid, invalid],
    )
    logged: list[str] = []

    def capture_warning(message, *args):
        logged.append(message % args if args else message)

    monkeypatch.setattr(orchestrator_module.logger, "warning", capture_warning)

    result = EnhancedTranslationOrchestrator(_config(), gateway=gateway).run(
        (SubtitleCue(1, "Source"),)
    )

    assert result.translations == {1: "Source"}
    assert len(gateway.stage_calls["audit"]) == 3
    assert result.audit_report.issues[0].category == "source_copied"
    assert result.audit_report.warnings == (
        "高级校对对字幕 1 的响应结构无效，已跳过该批次模型审校；"
        "译文和本地审计结果已保留。",
    )
    for request in gateway.stage_calls["audit"][1:]:
        feedback = request.messages[-1].content
        assert "response_shape=object(" in feedback
        assert "summary" in feedback
        assert "items" in feedback
        assert "array_lengths=[items:2]" in feedback
        assert secret not in feedback
    assert secret not in "\n".join(logged)
    assert any("已跳过该批次模型审校" in message for message in logged)


def test_audit_configuration_error_is_not_degraded():
    failure = LLMCallError(
        "invalid provider configuration",
        category=LLMErrorCategory.CONFIGURATION,
        retryable=False,
    )
    gateway = ScriptedGateway(
        analysis_window=[_analysis()],
        translation=[_translations((1, "译文"))],
        audit=[failure],
    )

    with pytest.raises(EnhancedTranslationError) as raised:
        EnhancedTranslationOrchestrator(_config(), gateway=gateway).run(
            (SubtitleCue(1, "Source"),)
        )

    assert raised.value.stage == "audit"
    assert raised.value.category == LLMErrorCategory.CONFIGURATION.value
    assert len(gateway.stage_calls["audit"]) == 1


def test_provider_invalid_audit_response_degrades_and_keeps_translation():
    failure = LLMCallError(
        "provider returned empty content",
        category=LLMErrorCategory.INVALID_RESPONSE,
        retryable=False,
    )
    gateway = ScriptedGateway(
        analysis_window=[_analysis()],
        translation=[_translations((1, "译文"))],
        audit=[failure],
    )

    result = EnhancedTranslationOrchestrator(_config(), gateway=gateway).run(
        (SubtitleCue(1, "Source"),)
    )

    assert result.translations == {1: "译文"}
    assert result.audit_report.issues == ()
    assert result.audit_report.warnings == (
        "高级校对对字幕 1 的响应结构无效，已跳过该批次模型审校；"
        "译文和本地审计结果已保留。",
    )


def test_audit_planning_budget_error_uses_structured_failure(monkeypatch):
    real_planner = orchestrator_module.plan_translation_batches

    def fail_audit_planning(*args, **kwargs):
        if kwargs.get("batch_input_estimator") is not None:
            raise TokenBudgetExceeded("audit prompt cannot fit")
        return real_planner(*args, **kwargs)

    monkeypatch.setattr(
        orchestrator_module, "plan_translation_batches", fail_audit_planning
    )
    gateway = ScriptedGateway(
        analysis_window=[_analysis()],
        translation=[_translations((1, "译文"))],
    )

    with pytest.raises(EnhancedTranslationError, match="audit prompt cannot fit") as raised:
        EnhancedTranslationOrchestrator(_config(), gateway=gateway).run(
            (SubtitleCue(1, "Source"),)
        )

    assert raised.value.stage == "audit_planning"
    assert raised.value.category == "context_budget"
    assert raised.value.retryable is False


def test_manual_audit_does_not_pause_when_there_are_no_findings():
    cues = (SubtitleCue(1, "Source"),)
    gateway = ScriptedGateway(
        analysis_window=[_analysis()],
        translation=[_translations((1, "译文"))],
        audit=[{"issues": []}],
    )

    result = EnhancedTranslationOrchestrator(
        _config(audit_mode=TranslationAuditMode.REVIEW_AND_CONFIRM), gateway=gateway
    ).run(
        cues,
        confirm_audit=lambda report: (_ for _ in ()).throw(
            AssertionError("empty audit must not request confirmation")
        ),
    )

    assert result.translations == {1: "译文"}
    assert result.audit_report.issues == ()


def test_exact_glossary_still_analyzes_but_skips_all_term_calls():
    cues = (SubtitleCue(1, "Mercury is visible."),)
    glossary = AuthoritativeGlossary(
        source_language="English",
        target_language="简体中文",
        subtitle_fingerprint=subtitle_fingerprint(cues),
        entries=(
            GlossaryEntry(
                "mercury-planet",
                "Mercury",
                "the planet",
                "水星",
                occurrence_ids=(1,),
                selection_source=GlossarySelectionSource.IMPORTED,
            ),
        ),
    )
    gateway = ScriptedGateway(
        analysis_window=[_analysis(candidates=[_candidate()])],
        translation=[_translations((1, "可以看到水星。"))],
        audit=[{"issues": []}],
    )

    result = EnhancedTranslationOrchestrator(_config(), gateway=gateway).run(
        cues, imported_glossary=glossary
    )

    assert gateway.stages == ["analysis_window", "translation", "audit"]
    assert result.glossary is glossary
    assert '"translation":"水星"' in gateway.stage_calls["translation"][0].messages[1].content


def test_translation_batch_splits_after_all_automatic_output_caps_are_exhausted():
    cues = tuple(SubtitleCue(cue_id, f"Source {cue_id}") for cue_id in range(1, 5))
    main_profile = _profile("main", work_context_tokens=300_000)
    gateway = ScriptedGateway(
        analysis_window=[_analysis()],
        translation=[
            _output_limit_error(),
            _output_limit_error(),
            _output_limit_error(),
            _translations((1, "译文 1"), (2, "译文 2")),
            _translations((3, "译文 3"), (4, "译文 4")),
        ],
        audit=[{"issues": []}],
    )

    result = EnhancedTranslationOrchestrator(
        _config(batch_size=4, main_profile=main_profile), gateway=gateway
    ).run(cues)

    assert result.translations == {
        1: "译文 1",
        2: "译文 2",
        3: "译文 3",
        4: "译文 4",
    }
    assert [
        request.max_output_tokens for request in gateway.stage_calls["translation"]
    ] == [32_768, 65_536, 256_000, 32_768, 32_768]


def test_audit_batch_splits_then_single_output_limit_degrades_safely():
    cues = (SubtitleCue(1, "Source 1"), SubtitleCue(2, "Source 2"))
    review_profile = _profile("review", max_output_tokens=2_048)
    gateway = ScriptedGateway(
        analysis_window=[_analysis()],
        translation=[_translations((1, "译文 1"), (2, "译文 2"))],
        audit=[_output_limit_error(), _output_limit_error(), {"issues": []}],
    )

    result = EnhancedTranslationOrchestrator(
        _config(batch_size=2, review_profile=review_profile), gateway=gateway
    ).run(cues)

    assert result.translations == {1: "译文 1", 2: "译文 2"}
    assert len(gateway.stage_calls["audit"]) == 3
    assert result.audit_report.warnings == (
        "高级校对对字幕 1 的输出额度仍不足，已跳过该字幕模型审校；"
        "译文和本地审计结果已保留。",
    )


def test_required_translation_failure_does_not_run_audit_or_return_partial_result():
    cues = (SubtitleCue(1, "Hello."),)
    failure = LLMCallError(
        "provider unavailable",
        category=LLMErrorCategory.TRANSIENT,
        retryable=True,
        attempts=4,
    )
    gateway = ScriptedGateway(
        analysis_window=[_analysis()],
        translation=[failure],
    )

    with pytest.raises(EnhancedTranslationError) as raised:
        EnhancedTranslationOrchestrator(_config(), gateway=gateway).run(cues)

    assert raised.value.stage == "translation"
    assert raised.value.retryable is True
    assert raised.value.attempts == 4
    assert gateway.stages == ["analysis_window", "translation"]
    assert "audit" not in gateway.stage_calls


def test_manual_confirmation_is_skipped_when_analysis_finds_no_terms():
    cues = (SubtitleCue(1, "Hello."),)
    gateway = ScriptedGateway(
        analysis_window=[_analysis()],
        translation=[_translations((1, "你好。"))],
        audit=[{"issues": []}],
    )
    confirmation_calls = []

    result = EnhancedTranslationOrchestrator(
        _config(term_confirmation=TermConfirmationMode.MANUAL), gateway=gateway
    ).run(cues, confirm_terms=lambda values: confirmation_calls.append(values) or values)

    assert result.translations == {1: "你好。"}
    assert confirmation_calls == []


def _timed_result(payload, duration_ms: int) -> LLMResult:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return LLMResult(
        text=text,
        usage=LLMUsage(
            input_tokens=10,
            output_tokens=2,
            cache_read_tokens=4,
            cache_write_tokens=1,
        ),
        duration_ms=duration_ms,
    )


def test_usage_snapshot_accumulates_wall_clock_by_role_and_stage():
    cues = (SubtitleCue(1, "Source"),)
    gateway = ScriptedGateway(
        analysis_window=[_timed_result(_analysis(), 120)],
        translation=[_timed_result(_translations((1, "译文")), 80)],
        audit=[_timed_result({"issues": []}, 40)],
    )

    result = EnhancedTranslationOrchestrator(_config(), gateway=gateway).run(cues)

    by_key = {(usage.role, usage.stage): usage for usage in result.audit_report.usages}
    assert by_key[("main", "analysis_window")].calls == 1
    assert by_key[("main", "analysis_window")].duration_ms == 120
    assert by_key[("main", "translation")].duration_ms == 80
    assert by_key[("review", "audit")].duration_ms == 40
    assert by_key[("main", "analysis_window")].input_tokens == 10


def test_usage_snapshot_sums_wall_clock_across_stage_calls():
    cues = (SubtitleCue(1, "One"), SubtitleCue(2, "Two"))
    gateway = ScriptedGateway(
        analysis_window=[_timed_result(_analysis(), 10)],
        translation=[
            _timed_result(_translations((1, "一")), 50),
            _timed_result(_translations((2, "二")), 70),
        ],
        audit=[
            _timed_result({"issues": []}, 5),
            _timed_result({"issues": []}, 7),
        ],
    )

    result = EnhancedTranslationOrchestrator(
        _config(batch_size=1), gateway=gateway
    ).run(cues)

    by_key = {(usage.role, usage.stage): usage for usage in result.audit_report.usages}
    assert by_key[("main", "translation")].calls == 2
    assert by_key[("main", "translation")].duration_ms == 120
    assert by_key[("main", "analysis_window")].duration_ms == 10
    assert by_key[("review", "audit")].calls == 2
    assert by_key[("review", "audit")].duration_ms == 12


def test_formal_translation_batches_never_exceed_task_concurrency():
    cues = tuple(SubtitleCue(cue_id, f"Source {cue_id}") for cue_id in range(1, 6))
    gateway = ConcurrentScriptedGateway(default_delay=0.08)

    result = EnhancedTranslationOrchestrator(
        _unclamped_config(batch_size=1, max_concurrency=2), gateway=gateway
    ).run(cues)

    assert result.translations == {cue_id: f"译文 {cue_id}" for cue_id in range(1, 6)}
    assert gateway.max_in_flight["translation"] == 2
    assert sorted(gateway.completed_subject_ids) == [1, 2, 3, 4, 5]


def test_out_of_order_translation_batches_merge_by_subtitle_id():
    cues = tuple(SubtitleCue(cue_id, f"Source {cue_id}") for cue_id in range(1, 4))
    gateway = ConcurrentScriptedGateway(delays={1: 0.25, 2: 0.02, 3: 0.08})

    result = EnhancedTranslationOrchestrator(
        _unclamped_config(batch_size=1, max_concurrency=3), gateway=gateway
    ).run(cues)

    assert result.translations == {1: "译文 1", 2: "译文 2", 3: "译文 3"}
    assert gateway.finished[2] < gateway.finished[1]
    assert gateway.finished[3] < gateway.finished[1]


def test_translation_cancel_abandons_in_flight_and_keeps_completed_batches():
    cues = tuple(SubtitleCue(cue_id, f"Source {cue_id}") for cue_id in range(1, 5))
    token = CancellationToken()
    gateway = ConcurrentScriptedGateway(delays={1: 0.05, 2: 2.0, 3: 2.0, 4: 2.0})
    caught: list[BaseException] = []

    def run() -> None:
        try:
            EnhancedTranslationOrchestrator(
                _unclamped_config(batch_size=1, max_concurrency=2),
                gateway=gateway,
                cancellation=token,
            ).run(cues)
        except BaseException as exc:
            caught.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    assert gateway.ready_to_cancel.wait(timeout=2)
    token.cancel()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert len(caught) == 1
    assert isinstance(caught[0], InterruptedError)
    assert gateway.completed_subject_ids == [1]
    assert set(gateway.abandoned_subject_ids) >= {2, 3}
    assert 4 not in gateway.completed_subject_ids
    assert "audit" not in gateway.stage_calls


def test_translation_skips_warmup_when_batch_count_fits_concurrency():
    cues = (SubtitleCue(1, "Source 1"), SubtitleCue(2, "Source 2"))
    gateway = ConcurrentScriptedGateway(default_delay=0.15)

    result = EnhancedTranslationOrchestrator(
        _unclamped_config(batch_size=1, max_concurrency=2), gateway=gateway
    ).run(cues)

    assert result.translations == {1: "译文 1", 2: "译文 2"}
    assert gateway.max_in_flight["translation"] == 2
    assert gateway.started[2] < gateway.finished[1]
    assert gateway.started[1] < gateway.finished[2]


def test_translation_keeps_serial_warmup_when_batch_count_exceeds_concurrency():
    cues = tuple(SubtitleCue(cue_id, f"Source {cue_id}") for cue_id in range(1, 5))
    gateway = ConcurrentScriptedGateway(default_delay=0.08)

    result = EnhancedTranslationOrchestrator(
        _unclamped_config(batch_size=1, max_concurrency=2), gateway=gateway
    ).run(cues)

    assert result.translations == {cue_id: f"译文 {cue_id}" for cue_id in range(1, 5)}
    assert gateway.finished[1] <= gateway.started[2]
    assert gateway.finished[1] <= gateway.started[3]
    assert gateway.max_in_flight["translation"] == 2


def test_later_translation_batch_failure_fails_task_without_audit():
    cues = (SubtitleCue(1, "Source 1"), SubtitleCue(2, "Source 2"))
    failure = LLMCallError(
        "provider unavailable",
        category=LLMErrorCategory.TRANSIENT,
        retryable=True,
        attempts=4,
    )
    gateway = ConcurrentScriptedGateway(translation_overrides={2: failure}, default_delay=0.02)

    with pytest.raises(EnhancedTranslationError) as raised:
        EnhancedTranslationOrchestrator(
            _unclamped_config(batch_size=1, max_concurrency=2), gateway=gateway
        ).run(cues)

    assert raised.value.stage == "translation"
    assert raised.value.retryable is True
    assert raised.value.attempts == 4
    assert "audit" not in gateway.stage_calls


def _term_candidate(candidate_id: str, source_term: str, occurrence_id: int):
    return {
        "id": candidate_id,
        "source_term": source_term,
        "sense": f"{source_term} sense",
        "aliases": [],
        "occurrence_ids": [occurrence_id],
    }


def test_analysis_windows_never_exceed_task_concurrency(monkeypatch):
    cues = tuple(SubtitleCue(cue_id, f"Source {cue_id}") for cue_id in range(1, 6))
    monkeypatch.setattr(
        orchestrator_module, "plan_analysis_windows", _one_cue_analysis_windows
    )
    gateway = ConcurrentScriptedGateway(
        delayed_stages={"analysis_window"},
        default_delay=0.08,
    )

    result = EnhancedTranslationOrchestrator(
        _unclamped_config(batch_size=1, max_concurrency=2), gateway=gateway
    ).run(cues)

    assert result.translations == {cue_id: f"译文 {cue_id}" for cue_id in range(1, 6)}
    assert gateway.max_in_flight["analysis_window"] == 2
    assert sorted(gateway.completed_keys["analysis_window"]) == [1, 2, 3, 4, 5]


def test_term_candidates_stay_serial_inside_and_bounded_across():
    cues = tuple(SubtitleCue(cue_id, f"Source {cue_id}") for cue_id in range(1, 4))
    candidates = (
        _term_candidate("term-a", "Alpha", 1),
        _term_candidate("term-b", "Beta", 2),
        _term_candidate("term-c", "Gamma", 3),
    )
    gateway = ConcurrentScriptedGateway(
        delayed_stages={"term_proposal", "term_review"},
        default_delay=0.08,
        analysis_candidates=candidates,
    )

    result = EnhancedTranslationOrchestrator(
        _unclamped_config(batch_size=1, max_concurrency=2), gateway=gateway
    ).run(cues)

    assert gateway.max_in_flight["term"] == 2
    assert gateway.max_in_flight["term_proposal"] <= 2
    assert gateway.max_in_flight["term_review"] <= 2
    for candidate_id in ("term-a", "term-b", "term-c"):
        assert (
            gateway.stage_started["term_proposal"][candidate_id]
            < gateway.stage_started["term_review"][candidate_id]
        )
        assert (
            gateway.stage_finished["term_proposal"][candidate_id]
            <= gateway.stage_started["term_review"][candidate_id]
        )
    assert {entry.source_term for entry in result.glossary.entries} == {
        "Alpha",
        "Beta",
        "Gamma",
    }


def test_manual_term_confirmation_waits_until_every_candidate_is_resolved():
    cues = tuple(SubtitleCue(cue_id, f"Source {cue_id}") for cue_id in range(1, 4))
    candidates = (
        _term_candidate("term-a", "Alpha", 1),
        _term_candidate("term-b", "Beta", 2),
        _term_candidate("term-c", "Gamma", 3),
    )
    gateway = ConcurrentScriptedGateway(
        delayed_stages={"term_proposal", "term_review"},
        default_delay=0.05,
        analysis_candidates=candidates,
    )
    confirmation_calls = []

    def confirm_terms(values):
        confirmation_calls.append(tuple(item.candidate_id for item in values))
        assert len(gateway.stage_calls["term_proposal"]) == 3
        assert len(gateway.stage_calls["term_review"]) == 3
        return values

    result = EnhancedTranslationOrchestrator(
        _unclamped_config(
            batch_size=1,
            max_concurrency=2,
            term_confirmation=TermConfirmationMode.MANUAL,
        ),
        gateway=gateway,
    ).run(cues, confirm_terms=confirm_terms)

    assert confirmation_calls == [("term-a", "term-b", "term-c")]
    assert len(result.glossary.entries) == 3
    assert "translation" in gateway.stage_calls
    assert confirmation_calls and gateway.stage_calls["term_review"]


def test_analysis_cancel_abandons_in_flight_and_skips_later_stages(monkeypatch):
    cues = tuple(SubtitleCue(cue_id, f"Source {cue_id}") for cue_id in range(1, 5))
    monkeypatch.setattr(
        orchestrator_module, "plan_analysis_windows", _one_cue_analysis_windows
    )
    token = CancellationToken()
    gateway = ConcurrentScriptedGateway(
        delays={1: 0.05, 2: 2.0, 3: 2.0, 4: 2.0},
        delayed_stages={"analysis_window"},
        cancel_stage="analysis_window",
        default_delay=0.05,
    )
    caught: list[BaseException] = []

    def run() -> None:
        try:
            EnhancedTranslationOrchestrator(
                _unclamped_config(batch_size=1, max_concurrency=2),
                gateway=gateway,
                cancellation=token,
            ).run(cues)
        except BaseException as exc:
            caught.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    assert gateway.ready_to_cancel.wait(timeout=2)
    token.cancel()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert len(caught) == 1
    assert isinstance(caught[0], InterruptedError)
    assert gateway.completed_keys["analysis_window"] == [1]
    assert set(gateway.abandoned_keys["analysis_window"]) >= {2, 3}
    assert 4 not in gateway.completed_keys["analysis_window"]
    assert "term_proposal" not in gateway.stage_calls
    assert "translation" not in gateway.stage_calls
    assert "audit" not in gateway.stage_calls


def test_audit_cancel_abandons_in_flight_and_skips_nothing_already_translated():
    cues = tuple(SubtitleCue(cue_id, f"Source {cue_id}") for cue_id in range(1, 5))
    token = CancellationToken()
    gateway = ConcurrentScriptedGateway(
        delays={1: 0.05, 2: 2.0, 3: 2.0, 4: 2.0},
        delayed_stages={"audit"},
        cancel_stage="audit",
        default_delay=0.05,
    )
    caught: list[BaseException] = []

    def run() -> None:
        try:
            EnhancedTranslationOrchestrator(
                _unclamped_config(batch_size=1, max_concurrency=2),
                gateway=gateway,
                cancellation=token,
            ).run(cues)
        except BaseException as exc:
            caught.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    assert gateway.ready_to_cancel.wait(timeout=2)
    token.cancel()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert len(caught) == 1
    assert isinstance(caught[0], InterruptedError)
    assert gateway.completed_keys["audit"] == [1]
    assert set(gateway.abandoned_keys["audit"]) >= {2, 3}
    assert 4 not in gateway.completed_keys["audit"]
    assert "translation" in gateway.stage_calls


def _stage_values(recorded: list[tuple[int, str]], start: int, end: int) -> list[int]:
    return [value for value, _ in recorded if start <= value <= end]


def test_progress_slides_inside_multi_batch_translation_and_audit():
    cues = (SubtitleCue(1, "Source 1"), SubtitleCue(2, "Source 2"))
    gateway = ScriptedGateway(
        analysis_window=[_analysis()],
        translation=[
            _translations((1, "译文 1")),
            _translations((2, "译文 2")),
        ],
        audit=[{"issues": []}, {"issues": []}],
    )
    recorded: list[tuple[int, str]] = []

    result = EnhancedTranslationOrchestrator(
        _config(batch_size=1),
        gateway=gateway,
        progress=lambda value, message: recorded.append((value, message)),
    ).run(cues)

    values = [value for value, _ in recorded]
    assert values == sorted(values)
    translation_values = _stage_values(recorded, 40, 80)
    audit_values = _stage_values(recorded, 80, 99)
    assert len(set(translation_values)) > 1
    assert len(set(audit_values)) > 1
    assert recorded[0] == (1, "Analyzing complete source subtitles")
    assert recorded[-1] == (100, "Enhanced translation completed")
    assert result.translations == {1: "译文 1", 2: "译文 2"}


def test_progress_slides_inside_multi_window_analysis(monkeypatch):
    cues = (SubtitleCue(1, "Source 1"), SubtitleCue(2, "Source 2"))
    real_planner = orchestrator_module.plan_analysis_windows

    def two_windows(*args, **kwargs):
        windows = real_planner(*args, **kwargs)
        if len(windows) == 1:
            first, second = cues
            return (
                type(windows[0])(cues=(first,), estimated_input_tokens=1),
                type(windows[0])(cues=(second,), estimated_input_tokens=1),
            )
        return windows

    monkeypatch.setattr(orchestrator_module, "plan_analysis_windows", two_windows)
    gateway = ScriptedGateway(
        analysis_window=[_analysis(), _analysis()],
        analysis_summary=[_analysis()],
        translation=[
            _translations((1, "译文 1")),
            _translations((2, "译文 2")),
        ],
        audit=[{"issues": []}, {"issues": []}],
    )
    recorded: list[tuple[int, str]] = []

    EnhancedTranslationOrchestrator(
        _config(batch_size=1),
        gateway=gateway,
        progress=lambda value, message: recorded.append((value, message)),
    ).run(cues)

    values = [value for value, _ in recorded]
    assert values == sorted(values)
    analysis_values = _stage_values(recorded, 1, 20)
    assert len(set(analysis_values)) > 1
    assert any(1 < value < 20 for value in analysis_values)


def test_progress_slides_inside_term_resolution():
    cues = (
        SubtitleCue(1, "Mercury is the closest planet to the Sun."),
        SubtitleCue(2, "Now compare it with Venus."),
    )
    second = {
        "id": "venus-planet",
        "source_term": "Venus",
        "sense": "the planet",
        "aliases": [],
        "occurrence_ids": [2],
    }
    gateway = ScriptedGateway(
        analysis_window=[_analysis(candidates=[_candidate(), second])],
        term_proposal=[
            {"translation": "水星", "reason": "planet"},
            {"translation": "金星", "reason": "planet"},
        ],
        term_review=[
            {
                "is_term": True,
                "decision": "accept",
                "translation": "水星",
                "reason": "ok",
            },
            {
                "is_term": True,
                "decision": "accept",
                "translation": "金星",
                "reason": "ok",
            },
        ],
        translation=[
            _translations((1, "水星是离太阳最近的行星。"), (2, "现在将它与金星比较。"))
        ],
        audit=[{"issues": []}],
    )
    recorded: list[tuple[int, str]] = []

    result = EnhancedTranslationOrchestrator(
        _config(),
        gateway=gateway,
        progress=lambda value, message: recorded.append((value, message)),
    ).run(cues)

    values = [value for value, _ in recorded]
    assert values == sorted(values)
    term_values = _stage_values(recorded, 20, 40)
    assert len(set(term_values)) > 1
    assert any(20 < value < 40 for value in term_values)
    assert {entry.source_term for entry in result.glossary.entries} == {"Mercury", "Venus"}


def test_incremental_on_translations_keeps_first_batch_when_later_batch_fails():
    cues = (SubtitleCue(1, "Source 1"), SubtitleCue(2, "Source 2"))
    failure = LLMCallError(
        "provider unavailable",
        category=LLMErrorCategory.TRANSIENT,
        retryable=True,
        attempts=4,
    )
    gateway = ConcurrentScriptedGateway(
        delays={1: 0.02, 2: 0.2}, translation_overrides={2: failure}
    )
    persisted: list[dict[int, str]] = []

    with pytest.raises(EnhancedTranslationError):
        EnhancedTranslationOrchestrator(
            _unclamped_config(batch_size=1, max_concurrency=2), gateway=gateway
        ).run(cues, on_translations=persisted.append)

    assert persisted
    assert persisted[0] == {1: "译文 1"}
    assert all(2 not in batch for batch in persisted)

