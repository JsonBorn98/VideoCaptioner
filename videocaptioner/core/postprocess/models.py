"""Qt-free task and result contracts for the standalone postprocess stage."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ..entities import SubtitleLayoutEnum
from ..subtitle.io import canonical_stage_path
from .config import PostprocessConfig

if TYPE_CHECKING:
    from ..asr.asr_data import ASRData
    from ..entities import SubtitleExportPolicy
    from ..speed.timing_evidence import TimingEvidenceBundle
    from .report import QualityReport


class PostprocessLayoutMode(str, Enum):
    """How an independently supplied subtitle should be interpreted."""

    AUTO = "auto"
    SINGLE = "single"
    ORIGINAL_ONLY = "original_only"
    TRANSLATE_ONLY = "translate_only"
    ORIGINAL_ON_TOP = "original_on_top"
    TRANSLATE_ON_TOP = "translate_on_top"


@dataclass
class PostprocessTask:
    """One isolated postprocess job.

    ``initial_subtitle_path`` is the immutable hand-off artifact.  The runner
    always writes a different ``postprocessed_subtitle_path`` and selects one
    of them as ``active_subtitle_path`` for downstream stages.
    """

    source_subtitle_path: str
    profile_id: str = "balanced"
    layout_mode: PostprocessLayoutMode | str = PostprocessLayoutMode.AUTO
    media_path: str | None = None
    postprocessed_subtitle_path: str | None = None
    initial_subtitle_path: str | None = None
    active_subtitle_path: str | None = None
    config_snapshot: PostprocessConfig | None = None
    timing_bundle: "TimingEvidenceBundle | None" = field(default=None, repr=False)
    input_data: "ASRData | None" = field(default=None, repr=False)
    result_data: "ASRData | None" = field(default=None, repr=False)
    workflow_base_name: str = ""
    export_policy: "SubtitleExportPolicy | None" = None
    enabled: bool = True
    need_next_task: bool = False
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: Literal["pending", "running", "completed", "fallback", "skipped", "cancelled"] = (
        "pending"
    )
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def __post_init__(self) -> None:
        self.layout_mode = PostprocessLayoutMode(self.layout_mode)
        self.initial_subtitle_path = self.initial_subtitle_path or self.source_subtitle_path
        self.active_subtitle_path = self.active_subtitle_path or self.initial_subtitle_path

    def default_output_path(self) -> str:
        source = Path(self.initial_subtitle_path or self.source_subtitle_path)
        return str(canonical_stage_path(source, "后处理字幕"))


@dataclass(frozen=True)
class PostprocessResult:
    """Completed stage result, including fallback and layout evidence."""

    task: PostprocessTask
    input_data: "ASRData"
    output_data: "ASRData"
    report: "QualityReport"
    layout: SubtitleLayoutEnum
    layout_confidence: float
    warnings: tuple[str, ...] = ()
    succeeded: bool = True
    used_fallback: bool = False
