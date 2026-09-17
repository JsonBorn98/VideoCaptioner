"""模块无关的恢复提示对话框（票 08，供票 09 后处理复用）。

只依赖 ``core.recovery`` 的模块中性类型：``RecoverySummary`` 的
``completed`` 键集合与漂移项语义由各模块自己定义，调用方以
``completed_labels`` 提供显示名与单位，对话框不掺任何翻译或后处理
专有知识（ADR-0022：提示只在当前模块页面内呈现）。
"""

from __future__ import annotations

from typing import Mapping, Optional

from PyQt5.QtWidgets import QWidget
from qfluentwidgets import BodyLabel, MessageBoxBase, StrongBodyLabel

from videocaptioner.core.recovery import RecoverySummary


class RecoveryPromptDialog(MessageBoxBase):
    """「从检查点继续 / 从头开始」的恢复决定对话框。

    ``completed_labels`` 把 ``summary.completed`` 的每个键映射到
    ``(显示名, 单位)``：单位为 ``None`` 的键按「已完成」渲染（布尔级别），
    带单位的键按「{显示名} {数值} {单位}」渲染（批次数）。未标注的键
    以键名 + 计数兜底显示——新增恢复级别不会在提示里静默消失。
    """

    def __init__(
        self,
        summary: RecoverySummary,
        *,
        completed_labels: Mapping[str, tuple[str, Optional[str]]],
        title: Optional[str] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.summary = summary
        self._completed_labels = dict(completed_labels)
        self._title_label = StrongBodyLabel(title or self.tr("发现恢复检查点"), self)
        self._time_label = BodyLabel(self)
        self._completed_label = BodyLabel(self)
        self._drift_header = BodyLabel(self.tr("配置漂移"), self)
        # 无漂移显示「无」：漂移行列表以同一兜底值渲染（单一来源）。
        drift_items = summary.configuration_drift or (self.tr("无"),)
        self._drift_labels = tuple(BodyLabel(self) for _ in drift_items)

        self._time_label.setText(
            self.tr("检查点时间：{time}").format(
                time=summary.checkpoint_time or self.tr("未知")
            )
        )
        self._completed_label.setText(
            self.tr("已完成：{completed}").format(completed=self._render_completed())
        )
        for label, item in zip(self._drift_labels, drift_items):
            label.setText(item)
            label.setWordWrap(True)

        self.viewLayout.addWidget(self._title_label)
        self.viewLayout.addWidget(self._time_label)
        self.viewLayout.addWidget(self._completed_label)
        self.viewLayout.addWidget(self._drift_header)
        for label in self._drift_labels:
            self.viewLayout.addWidget(label)
        self.viewLayout.setSpacing(10)

        self.yesButton.setText(self.tr("从检查点继续"))
        self.cancelButton.setText(self.tr("从头开始"))
        self.yesButton.setFocus()

    def _render_completed(self) -> str:
        """已完成级别一行渲染：共享实现（票 11 收口），``tr`` 做本地化。"""

        from videocaptioner.core.recovery import render_completed_levels

        return render_completed_levels(
            self.summary, self._completed_labels, translate=self.tr
        )
