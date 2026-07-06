"""后处理配置 —— 全部新选项的唯一权威默认值。

CLI (`cli/config.py` DEFAULTS)、GUI (`ui/common/config.py` qconfig) 与
`core/entities.SubtitleConfig` 的默认值均应与此处保持一致
(见各文件旁的 ``keep in sync with core/postprocess/config.py`` 注释)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


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
    """闭合间隙的上限（音乐/节奏类内容建议 500）。"""
    min_gap_ms: int = 30
    """闭合间隙的下限（低于此值视为已连续）。"""
    gap_mode: str = "extend"
    """"extend"（技能语义：前段延长到后段开始）| "midpoint"（旧 optimize_timing 语义）。"""

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
            or self.audit_enabled()
        )
