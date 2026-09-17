"""CLI 恢复摘要打印与恢复决定回调（票 10，ADR-0022）。

无人值守脚本默认继续：命令函数把「无 ``--fresh``」转成打印恢复摘要
并返回「继续」的恢复决定回调、「有 ``--fresh``」转成恒返「从头开始」
的回调，分别传给翻译与后处理两个模块入口。模块入口只在发现匹配
检查点时调用回调，无检查点时两条路径都不打印（spec「入口适配 · CLI」）。
"""

from __future__ import annotations

from typing import Callable, Mapping, Optional

from videocaptioner.cli import output
from videocaptioner.core.recovery import RecoveryDecision, RecoverySummary

# 已完成级别显示名（对齐 core 侧权威标注表与 GUI 恢复提示，同名级别不出现
# 第二个名字）：单位 ``None`` 的键按布尔级别渲染，带单位的键按计数渲染。
_COMPLETED_LABELS_TYPE = Mapping[str, tuple[str, Optional[str]]]

TRANSLATION_COMPLETED_LABELS: _COMPLETED_LABELS_TYPE = {
    "analysis": ("全文分析", None),
    "glossary": ("术语阶段", None),
    "translation_segments": ("字幕段", "个"),
    "audit_batches": ("审计批", "批"),
}

POSTPROCESS_COMPLETED_LABELS: _COMPLETED_LABELS_TYPE = {
    "phase": ("阶段末检查点", None),
    "rounds": ("修复轮次", "轮"),
}


def _render_completed(summary: RecoverySummary, completed_labels: _COMPLETED_LABELS_TYPE) -> str:
    """已完成级别一行渲染：共享实现（票 11 收口），CLI 恒等本地化。"""

    from videocaptioner.core.recovery import render_completed_levels

    return render_completed_levels(summary, completed_labels)


def print_recovery_summary(
    summary: RecoverySummary,
    *,
    completed_labels: _COMPLETED_LABELS_TYPE,
) -> None:
    """把一份恢复摘要打印到标准错误输出（无人值守日志可查）。"""

    output.info("发现恢复检查点：从检查点继续")
    output.info(f"检查点时间：{summary.checkpoint_time or '未知'}")
    output.info(f"已完成：{_render_completed(summary, completed_labels)}")
    output.info("配置漂移：")
    drift = summary.configuration_drift or ("无",)
    for item in drift:
        output.info(f"  {item}")


def make_recovery_decision_callback(
    fresh: bool,
    *,
    completed_labels: _COMPLETED_LABELS_TYPE,
) -> Callable[[RecoverySummary], RecoveryDecision]:
    """把 ``--fresh`` 旗标做成传给模块入口的恢复决定回调。

    无旗标（默认继续）：打印恢复摘要再返回「继续」——回调由模块入口只在
    发现匹配检查点时调用，无检查点即无打印。有旗标：恒返「从头开始」，
    不打印（spec：摘要只在默认继续路径打印）。
    """

    if fresh:
        return lambda _summary: RecoveryDecision.START_FRESH

    def _continue_with_summary(summary: RecoverySummary) -> RecoveryDecision:
        print_recovery_summary(summary, completed_labels=completed_labels)
        return RecoveryDecision.CONTINUE

    return _continue_with_summary


__all__ = [
    "POSTPROCESS_COMPLETED_LABELS",
    "TRANSLATION_COMPLETED_LABELS",
    "make_recovery_decision_callback",
    "print_recovery_summary",
]
