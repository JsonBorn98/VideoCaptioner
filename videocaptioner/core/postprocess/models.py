"""Qt-free task and result contracts for the standalone postprocess stage."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Optional, Protocol

from ..entities import SubtitleLayoutEnum
from ..subtitle.io import canonical_stage_path
from .config import PostprocessConfig

if TYPE_CHECKING:
    from ..asr.asr_data import ASRData
    from ..entities import SubtitleExportPolicy
    from ..speed.timing_evidence import TimingEvidenceBundle
    from .report import QualityReport
    from .translation import TranslationExecutionSnapshot
    from .workspace import ProcessAssetDiscovery


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
    source_language: str = ""
    target_language: str = ""
    translation_method: str = ""
    # 任务开始时冻结的翻译执行快照（票 06，D15）：完整 workflow 由调用方
    # 冻结注入；独立任务由 runner 从验证过的过程资产重建身份快照。
    translation_snapshot: Optional["TranslationExecutionSnapshot"] = field(
        default=None, repr=False
    )
    subtitle_fingerprint: str = ""
    explicit_assets: dict[str, str] = field(default_factory=dict)
    asset_discovery: Optional["ProcessAssetDiscovery"] = field(default=None, repr=False)
    export_policy: "SubtitleExportPolicy | None" = None
    enabled: bool = True
    need_next_task: bool = False
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: Literal[
        "pending",
        "running",
        "completed",
        "fallback",
        "skipped",
        "cancelled",
        "invalid_initial",
    ] = "pending"
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def __post_init__(self) -> None:
        self.layout_mode = PostprocessLayoutMode(self.layout_mode)
        self.initial_subtitle_path = self.initial_subtitle_path or self.source_subtitle_path
        self.active_subtitle_path = self.active_subtitle_path or self.initial_subtitle_path

    def default_output_path(self) -> str:
        source = Path(self.initial_subtitle_path or self.source_subtitle_path)
        return str(canonical_stage_path(source, "后处理字幕"))


class PostprocessAssetAdapter(Protocol):
    """Injected process-asset seam. ``FilesystemAssetStore`` is the default."""

    def discover(self, task: PostprocessTask) -> None:
        """Create or reuse the 过程资产目录 and attach verified assets."""


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
    # 媒体增强对齐 / 对齐时间轴 的可见结果（见 CONTEXT.md，标识符仍为 precise_timing）。
    # precise_timing_outcome: None=未请求；"applied"=已应用对齐时间轴；
    # "degraded_no_media"=对齐降级（未提供关联媒体）；"degraded_failed"=对齐降级（生成失败/空证据）。
    # precise_timing_grades: 仅在 "applied" 时非 None，为按证据等级计数的
    # (等级名, 数量) 序列，等级名取 "HIGH"/"MEDIUM"/"LOW"，按 HIGH→MEDIUM→LOW 排序、只含计数>0 者。
    precise_timing_outcome: Optional[str] = None
    precise_timing_grades: Optional[tuple[tuple[str, int], ...]] = None
    # 下游继续：正常完成/跳过允许下游；无效初版、模块级失败、取消阻断下游。
    continue_downstream: bool = True
