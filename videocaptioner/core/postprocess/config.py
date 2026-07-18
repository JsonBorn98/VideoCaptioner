"""后处理配置 —— 全部新选项的唯一权威默认值。

CLI (`cli/config.py` DEFAULTS)、GUI (`ui/common/config.py` qconfig) 与
`core/entities.SubtitleConfig` 的默认值均应与此处保持一致
(见各文件旁的 ``keep in sync with core/postprocess/config.py`` 注释)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Literal, Optional


@dataclass
class PostprocessConfig:
    """规则型后处理 / 审计的统一配置。

    所有开关默认关闭（或保持与现状一致），由用户主动开启。
    ``trim_trailing_punct`` 默认开，用于复刻现有 ``remove_punctuation`` 行为。
    """

    # ---- 文本清理 ----
    remove_placeholders: bool = False
    """删除 [Music]/[音乐]/♪ 等占位符行/段。"""
    extra_placeholder_patterns: List[str] = field(default_factory=list)
    """用户扩展的占位符词表（整行匹配，大小写不敏感）。"""

    normalize_quotes: bool = False
    """中文引号规范化（“”->「」、‘’->『』），并对中文行启用扩展弱尾标点清理。"""
    trim_trailing_punct: bool = True
    """删除行尾弱标点。默认开，复刻旧 remove_punctuation（仅 ，。）行为。"""

    # ---- 时轴 ----
    fix_gaps: bool = False
    """闭合相邻段之间的微小间隙（消除闪烁）。"""
    max_gap_ms: int = 800
    """最大闭合间隙：闪轴闭合的上限，同时是尾部补偿的下界（此值以下闭合，以上补偿）。"""
    min_gap_ms: int = 30
    """闭合间隙的下限（低于此值视为已连续）。"""
    gap_mode: str = "extend"
    """"extend"（技能语义：前段延长到后段开始）| "midpoint"（旧 optimize_timing 语义）。"""

    # ---- 时轴：尾部补偿（单调钳制补偿曲线，见 docs/adr/0005）----
    # 曲线由两拐点定义：下拐点 (max_gap_ms, min_compensation_ms)、
    # 上拐点 (max_compensation_gap_ms, max_compensation_ms)。间隙 <= max_gap_ms 不补偿
    # （归闪轴闭合）；跨过即给 min_compensation_ms，随间隙线性升到 max_compensation_ms 后封顶。
    tail_compensation: bool = False
    """启用尾部补偿：为超过最大闭合间隙的停顿前、上一段的显示结尾追加时长，避免其过快
    消失。补偿量随间隙单调不降并封顶；最小留白 = max_gap_ms - min_compensation_ms。"""
    min_compensation_ms: int = 200
    """最小补偿：间隙刚跨过 max_gap_ms 时给予的补偿时长；须不超过 max_gap_ms。"""
    max_compensation_gap_ms: int = 2000
    """最大补偿间隙：补偿达到上限的间隙；更大的间隙补偿不再增加，只让留白继续张开。"""
    max_compensation_ms: int = 800
    """最大补偿：单段结尾可获得的补偿时长上限。"""

    # ---- 审计 / 报告 ----
    audit_reading_speed: bool = False
    """阅读速度 / 时长异常审计（只读）。qa_report 开启时强制视为 True。"""
    max_cps_cjk: float = 11.0
    max_cps_latin: float = 20.0
    comfort_cps_cjk: float = 9.0
    comfort_cps_latin: float = 16.0
    min_duration_ms: int = 1000
    """感知最短显示时长；低于此值记入舒适警告。"""
    max_duration_ms: int = 7000
    """普通字幕最长显示时长；超出记入长时长异常。"""
    short_text_max_chars: int = 12
    """短文本长显示判定：主行字符数下限。"""
    short_text_max_duration_ms: int = 4000
    """短文本长显示判定：时长上限。"""
    qa_report: bool = False
    """生成 Markdown QA 报告；开启时隐含执行审计。"""

    # ---- LLM 压缩重译（P2）----
    compress_fast_subtitles: bool = False
    """对超硬限的中文行做局部压缩重译（需 LLM）。"""
    llm_model: Optional[str] = None
    """压缩重译使用的 LLM 模型名（由调用方从各自配置注入）。"""

    # ---- 统一字幕速度优化 ----
    speed_optimize: bool = False
    """启用统一结构级速度优化；开启时不再叠加旧速度管线。"""
    speed_mode: Literal["apply", "analyze"] = "apply"
    """apply 写入后处理结果；analyze 使整个后处理阶段只读。"""
    speed_profile: str = "balanced"
    """内置或自定义速度方案 ID。首版接入三种内置方案。"""
    speed_profile_file: Optional[str] = None
    """可选的版本化 profile JSON；用于 CLI/自动化中的便携任务配置。"""
    speed_primary: Literal["translate", "original", "layout"] = "translate"
    """translate / original / layout，默认以译文体验为主。"""
    speed_overrides: dict[str, Any] = field(default_factory=dict)
    """相对所选 profile 的任务级高级覆盖。"""
    speed_reference_audit: bool = False
    speed_semantic_repair: bool = True
    """对确定性阶段仍未解决的硬超速，启用受验证约束的局部 LLM 修复。"""
    speed_semantic_window: int = 5
    """语义修复上下文窗口大小；写回仍以非重叠事务执行。"""
    speed_llm_uncertain_review: bool = True
    """确定性校验不能判定时，调用独立语义复核。"""
    optimize_both_sides: bool = False
    """双语字幕默认只改写译文；开启后允许文本能力分别处理原文与译文。"""
    # 领域术语：媒体增强对齐 / 对齐时间轴（见 CONTEXT.md）。代码标识符保持 precise_timing
    # 不变，UI 显示串已改称"媒体增强对齐 / 对齐时间轴"；"精准对齐 / 精准时间轴"为旧称，勿再沿用。
    precise_timing: bool = False
    """请求使用关联媒体生成对齐时间轴（媒体增强对齐）；runner 本身不隐式加载模型。"""
    save_timing_sidecar: bool = False
    """精准时间证据可用时，请求调用方保存可复用的 sidecar。"""

    def __post_init__(self) -> None:
        bool_fields = (
            "remove_placeholders",
            "normalize_quotes",
            "trim_trailing_punct",
            "fix_gaps",
            "tail_compensation",
            "audit_reading_speed",
            "qa_report",
            "compress_fast_subtitles",
            "speed_optimize",
            "speed_reference_audit",
            "speed_semantic_repair",
            "speed_llm_uncertain_review",
            "optimize_both_sides",
            "precise_timing",
            "save_timing_sidecar",
        )
        for field_name in bool_fields:
            if type(getattr(self, field_name)) is not bool:
                raise ValueError(f"{field_name} must be a boolean")
        int_fields = (
            "max_gap_ms",
            "min_gap_ms",
            "min_compensation_ms",
            "max_compensation_gap_ms",
            "max_compensation_ms",
            "min_duration_ms",
            "max_duration_ms",
            "short_text_max_chars",
            "short_text_max_duration_ms",
            "speed_semantic_window",
        )
        for field_name in int_fields:
            if type(getattr(self, field_name)) is not int:
                raise ValueError(f"{field_name} must be an integer")
        for field_name in (
            "max_cps_cjk",
            "max_cps_latin",
            "comfort_cps_cjk",
            "comfort_cps_latin",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{field_name} must be a positive number")
        if not isinstance(self.extra_placeholder_patterns, list) or not all(
            isinstance(item, str) for item in self.extra_placeholder_patterns
        ):
            raise ValueError("extra_placeholder_patterns must be an array of strings")
        if not isinstance(self.speed_overrides, dict):
            raise ValueError("speed_overrides must be an object")
        if self.speed_mode not in ("apply", "analyze"):
            raise ValueError("speed_mode must be 'apply' or 'analyze'")
        if self.speed_primary not in ("translate", "original", "layout"):
            raise ValueError("speed_primary must be translate, original, or layout")
        if not 1 <= self.speed_semantic_window <= 15:
            raise ValueError("speed_semantic_window must be between 1 and 15")
        if self.gap_mode not in ("extend", "midpoint"):
            raise ValueError("gap_mode must be 'extend' or 'midpoint'")
        if self.min_gap_ms < 0 or self.max_gap_ms < self.min_gap_ms:
            raise ValueError("gap limits must satisfy 0 <= min_gap_ms <= max_gap_ms")
        for field_name in (
            "min_compensation_ms",
            "max_compensation_gap_ms",
            "max_compensation_ms",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must not be negative")
        # 补偿曲线约束（见 docs/adr/0005）：最小补偿 <= 最大闭合间隙 < 最大补偿间隙，
        # 最大补偿 >= 最小补偿，且斜率 <= 1（保证补偿量与留白均随间隙单调、永不重叠）。
        if self.min_compensation_ms > self.max_gap_ms:
            raise ValueError("min_compensation_ms cannot exceed max_gap_ms")
        if self.max_compensation_gap_ms <= self.max_gap_ms:
            raise ValueError("max_compensation_gap_ms must exceed max_gap_ms")
        if self.max_compensation_ms < self.min_compensation_ms:
            raise ValueError("max_compensation_ms cannot be less than min_compensation_ms")
        if (
            self.max_compensation_ms - self.min_compensation_ms
            > self.max_compensation_gap_ms - self.max_gap_ms
        ):
            raise ValueError(
                "compensation ramp is too steep: "
                "max_compensation_ms - min_compensation_ms must not exceed "
                "max_compensation_gap_ms - max_gap_ms (slope <= 1)"
            )
        if self.min_duration_ms <= 0 or self.max_duration_ms < self.min_duration_ms:
            raise ValueError("duration limits must be positive and ordered")
        if self.short_text_max_chars <= 0 or self.short_text_max_duration_ms <= 0:
            raise ValueError("short-text limits must be positive")
        if self.comfort_cps_cjk > self.max_cps_cjk:
            raise ValueError("comfort_cps_cjk cannot exceed max_cps_cjk")
        if self.comfort_cps_latin > self.max_cps_latin:
            raise ValueError("comfort_cps_latin cannot exceed max_cps_latin")

    def audit_enabled(self) -> bool:
        """是否需要执行审计（审计开关 / QA 报告 / 压缩重译任一开启）。"""
        return self.audit_reading_speed or self.qa_report or self.compress_fast_subtitles

    def any_enabled(self) -> bool:
        """是否有任何非默认后处理需要执行（用于快速短路）。"""
        return (
            self.remove_placeholders
            or self.normalize_quotes
            or self.trim_trailing_punct
            or self.fix_gaps
            or self.tail_compensation
            or self.audit_enabled()
            or self.speed_optimize
        )
