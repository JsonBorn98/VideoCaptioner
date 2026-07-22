"""Pure domain contracts for enhanced LLM subtitle translation."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from threading import Event
from typing import Any, Mapping, Optional

from videocaptioner.core.llm.models import LLMModelProfile


@dataclass(frozen=True)
class SubtitleCue:
    """One stable, numbered source subtitle used by enhanced translation."""

    cue_id: int
    text: str

    def __post_init__(self) -> None:
        if self.cue_id <= 0:
            raise ValueError("cue_id must be a positive integer")


@dataclass(frozen=True)
class TranslationContextBrief:
    """Task-level summary produced by hierarchical whole-subtitle analysis."""

    outline: str = ""
    background: str = ""
    themes: tuple[str, ...] = ()
    style_notes: tuple[str, ...] = ()
    translation_notes: tuple[str, ...] = ()

    def as_prompt_text(self) -> str:
        sections = [
            ("Outline", self.outline),
            ("Background", self.background),
            ("Themes", "\n".join(f"- {item}" for item in self.themes)),
            ("Style", "\n".join(f"- {item}" for item in self.style_notes)),
            (
                "Translation notes",
                "\n".join(f"- {item}" for item in self.translation_notes),
            ),
        ]
        return "\n\n".join(
            f"{heading}:\n{body}" for heading, body in sections if body
        )


class GlossarySelectionSource(str, Enum):
    """Origin of the final authoritative translation for a term sense."""

    MAIN_MODEL = "main_model"
    REVIEW_MODEL_ACCEPTED = "review_model_accepted"
    REVIEW_MODEL_CORRECTED = "review_model_corrected"
    USER_MAIN = "user_main"
    USER_REVIEW = "user_review"
    USER_CUSTOM = "user_custom"
    SOURCE_FALLBACK = "source_fallback"
    IMPORTED = "imported"


@dataclass(frozen=True)
class GlossaryEntry:
    """One term sense and its authoritative translation for the task."""

    entry_id: str
    source_term: str
    sense: str
    translation: str
    aliases: tuple[str, ...] = ()
    occurrence_ids: tuple[int, ...] = ()
    selection_source: GlossarySelectionSource = GlossarySelectionSource.MAIN_MODEL
    high_risk: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        if not self.entry_id.strip():
            raise ValueError("entry_id must not be empty")
        if not self.source_term.strip():
            raise ValueError("source_term must not be empty")
        if any(cue_id <= 0 for cue_id in self.occurrence_ids):
            raise ValueError("occurrence_ids must contain positive integers")


@dataclass(frozen=True)
class AuthoritativeGlossary:
    """Complete persisted glossary for one source/target subtitle task."""

    source_language: str
    target_language: str
    subtitle_fingerprint: str
    entries: tuple[GlossaryEntry, ...] = ()
    schema: str = "videocaptioner.project_glossary"
    version: int = 1

    def __post_init__(self) -> None:
        if not self.source_language.strip() or not self.target_language.strip():
            raise ValueError("source_language and target_language must not be empty")
        if not self.subtitle_fingerprint.startswith("sha256:"):
            raise ValueError("subtitle_fingerprint must be a sha256 fingerprint")
        entry_ids = [entry.entry_id for entry in self.entries]
        if len(entry_ids) != len(set(entry_ids)):
            raise ValueError("glossary entry IDs must be unique")


class GlossaryImportMode(str, Enum):
    """How safely a persisted glossary can be reused for a new task."""

    EXACT = "exact"
    SEED = "seed"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True)
class GlossaryImportResult:
    """Classified glossary import with stale occurrence data removed for seeds."""

    mode: GlossaryImportMode
    glossary: Optional[AuthoritativeGlossary]
    reason: str = ""


@dataclass(frozen=True)
class AnalysisWindow:
    """A token-budgeted, subtitle-boundary-aligned whole-analysis window."""

    cues: tuple[SubtitleCue, ...]
    estimated_input_tokens: int

    @property
    def cue_ids(self) -> tuple[int, ...]:
        return tuple(cue.cue_id for cue in self.cues)


@dataclass(frozen=True)
class TranslationBatch:
    """Translation subjects separated from read-only boundary context."""

    subjects: tuple[SubtitleCue, ...]
    context_before: tuple[SubtitleCue, ...] = ()
    context_after: tuple[SubtitleCue, ...] = ()
    estimated_input_tokens: int = 0

    def __post_init__(self) -> None:
        if not self.subjects:
            raise ValueError("translation batch must contain at least one subject")
        subject_ids = {cue.cue_id for cue in self.subjects}
        context_ids = {
            cue.cue_id for cue in (*self.context_before, *self.context_after)
        }
        if subject_ids & context_ids:
            raise ValueError("translation subjects and boundary context must be disjoint")

    @property
    def subject_ids(self) -> tuple[int, ...]:
        return tuple(cue.cue_id for cue in self.subjects)


@dataclass(frozen=True)
class StageUsage:
    """Provider-reported usage for one role/stage; unavailable means ``None``."""

    role: str
    stage: str
    calls: int = 0
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None


class AuditIssueDisposition(str, Enum):
    REPORTED = "reported"
    AUTO_APPLIED = "auto_fixed"
    USER_APPLIED = "user_applied"
    USER_REJECTED = "user_rejected"
    FIX_VALIDATION_FAILED = "fix_validation_failed"


@dataclass(frozen=True)
class TranslationAuditIssue:
    cue_id: int
    category: str
    message: str
    original_text: str
    translated_text: str
    suggested_translation: str = ""
    categories: tuple[str, ...] = ()
    disposition: AuditIssueDisposition = AuditIssueDisposition.REPORTED

    def __post_init__(self) -> None:
        categories = self.categories or ((self.category,) if self.category else ())
        categories = tuple(dict.fromkeys(value for value in categories if value))
        object.__setattr__(self, "categories", categories)
        if not self.category and categories:
            object.__setattr__(self, "category", categories[0])


@dataclass(frozen=True)
class TranslationAuditReport:
    """Single structured translation audit result used by UI and Markdown."""

    issues: tuple[TranslationAuditIssue, ...] = ()
    authoritative_terms: tuple[GlossaryEntry, ...] = ()
    usages: tuple[StageUsage, ...] = ()
    warnings: tuple[str, ...] = ()


class TermReviewDecision(str, Enum):
    ACCEPT = "accept"
    CORRECT = "correct"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class TermCandidate:
    candidate_id: str
    source_term: str
    sense: str
    aliases: tuple[str, ...] = ()
    occurrence_ids: tuple[int, ...] = ()
    representative_context_ids: tuple[int, ...] = ()
    main_translation: str = ""
    review_translation: str = ""
    review_decision: TermReviewDecision = TermReviewDecision.UNCERTAIN
    final_translation: str = ""
    selection_source: GlossarySelectionSource = GlossarySelectionSource.MAIN_MODEL
    is_term: bool = True
    high_risk: bool = False
    ignored: bool = False

    def __post_init__(self) -> None:
        if not self.candidate_id.strip():
            raise ValueError("candidate_id must not be empty")
        if not self.source_term.strip():
            raise ValueError("source_term must not be empty")
        if any(cue_id <= 0 for cue_id in self.occurrence_ids):
            raise ValueError("occurrence_ids must contain positive integers")
        if any(cue_id <= 0 for cue_id in self.representative_context_ids):
            raise ValueError("representative_context_ids must contain positive integers")

    def to_glossary_entry(self) -> GlossaryEntry:
        return GlossaryEntry(
            entry_id=self.candidate_id,
            source_term=self.source_term,
            sense=self.sense,
            translation=self.final_translation,
            aliases=self.aliases,
            occurrence_ids=self.occurrence_ids,
            selection_source=self.selection_source,
            high_risk=self.high_risk,
        )


class TermConfirmationMode(str, Enum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"


class TranslationAuditMode(str, Enum):
    # Keep the stored values stable so existing user configuration migrates safely.
    REVIEW_AND_CONFIRM = "report_only"
    AUTO_APPLY_REVIEW = "auto_fix_objective"


class TranslationExecutionMode(str, Enum):
    GUI_STANDALONE = "gui_standalone"
    GUI_WORKFLOW = "gui_workflow"
    CLI = "cli"
    BATCH = "batch"


@dataclass(frozen=True)
class TranslationRoleSnapshot:
    role: str
    profile: LLMModelProfile
    user_prompt: str = ""


@dataclass(frozen=True)
class EnhancedTranslationConfig:
    main_role: TranslationRoleSnapshot
    review_role: TranslationRoleSnapshot
    source_language: str
    target_language: str
    batch_size: int = 10
    term_context_radius: int = 10
    boundary_context_radius: int = 3
    term_confirmation: TermConfirmationMode = TermConfirmationMode.AUTOMATIC
    audit_mode: TranslationAuditMode = TranslationAuditMode.AUTO_APPLY_REVIEW
    execution_mode: TranslationExecutionMode = TranslationExecutionMode.GUI_STANDALONE

    def __post_init__(self) -> None:
        if self.main_role.role != "main":
            raise ValueError("main_role.role must be 'main'")
        if self.review_role.role != "review":
            raise ValueError("review_role.role must be 'review'")
        if self.batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if self.term_context_radius < 0 or self.boundary_context_radius < 0:
            raise ValueError("context radii must not be negative")
        if (
            self.execution_mode in {TranslationExecutionMode.CLI, TranslationExecutionMode.BATCH}
            and self.term_confirmation is TermConfirmationMode.MANUAL
        ):
            raise ValueError("CLI and batch translation cannot use manual term confirmation")
        if (
            self.execution_mode is not TranslationExecutionMode.GUI_STANDALONE
            and self.audit_mode is TranslationAuditMode.REVIEW_AND_CONFIRM
        ):
            raise ValueError("only standalone GUI translation can confirm audit suggestions")


@dataclass(frozen=True)
class EnhancedTranslationResult:
    translations: Mapping[int, str]
    brief: TranslationContextBrief
    glossary: AuthoritativeGlossary
    audit_report: TranslationAuditReport


class EnhancedTranslationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stage: str,
        category: str,
        retryable: bool,
        attempts: int = 1,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.category = category
        self.retryable = retryable
        self.attempts = attempts


class CancellationToken:
    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise InterruptedError("enhanced translation cancelled")
