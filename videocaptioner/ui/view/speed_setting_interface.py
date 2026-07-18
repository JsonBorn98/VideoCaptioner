"""Dedicated settings surface for the complete subtitle postprocess stage."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QLabel, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    ComboBoxSettingCard,
    ExpandLayout,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    MessageBox,
    MessageBoxBase,
    PushSettingCard,
    ScrollArea,
    SegmentedWidget,
    SettingCardGroup,
    SwitchSettingCard,
    TitleLabel,
    ToolButton,
)
from qfluentwidgets import FluentIcon as FIF

from videocaptioner.core.postprocess.profiles import (
    TEMPLATE_IDS,
    PostprocessProfileStore,
    get_factory_baseline,
)
from videocaptioner.core.speed import (
    SpeedPolicy,
    SpeedPreset,
    resolve_speed_policy,
)
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.components.SpinBoxSettingCard import (
    DoubleSpinBoxSettingCard,
    SliderSpinBoxSettingCard,
)


class _SettingTab(ScrollArea):
    """Scrollable collection of setting-card groups used by one tab."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.scrollWidget = QWidget(self)
        self.scrollWidget.setObjectName("postprocessSettingScrollWidget")
        self.scrollWidget.setAutoFillBackground(False)
        self.expandLayout = ExpandLayout(self.scrollWidget)
        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)  # type: ignore
        self.viewport().setObjectName("postprocessSettingViewport")
        self.viewport().setAutoFillBackground(False)
        self.expandLayout.setSpacing(24)
        self.expandLayout.setContentsMargins(0, 8, 0, 24)
        self.setStyleSheet(
            """
            QScrollArea { border: none; background: transparent; }
            QWidget#postprocessSettingViewport,
            QWidget#postprocessSettingScrollWidget { background: transparent; }
            """
        )

    def addGroup(self, group: SettingCardGroup) -> None:
        self.expandLayout.addWidget(group)


class _ProfileNameDialog(MessageBoxBase):
    """Small focused dialog for creating and renaming a speed profile."""

    def __init__(self, title: str, initial_name: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.titleLabel = BodyLabel(title, self)
        self.nameLineEdit = LineEdit(self)
        self.nameLineEdit.setPlaceholderText(self.tr("输入方案名称"))
        self.nameLineEdit.setClearButtonEnabled(True)
        self.nameLineEdit.setText(initial_name)
        self.nameLineEdit.selectAll()
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.nameLineEdit)
        self.yesButton.setText(self.tr("确定"))
        self.cancelButton.setText(self.tr("取消"))
        self.widget.setMinimumWidth(380)
        self.nameLineEdit.textChanged.connect(
            lambda text: self.yesButton.setEnabled(bool(text.strip()))
        )
        self.yesButton.setEnabled(bool(initial_name.strip()))


class PostprocessSettingInterface(QWidget):
    """Settings for text, timing, speed, semantic repair and reports.

    The page deliberately keeps the built-in :class:`SpeedPolicy` registry as
    the source of algorithm defaults. Selecting or restoring a preset copies a
    snapshot from that registry into qconfig; the UI does not repeat CPS or
    timing constants.
    """

    _POLICY_BINDINGS: tuple[tuple[str, str, Callable[[SpeedPolicy], Any]], ...] = (
        ("speed_comfort_cps_cjk", "阅读硬限", lambda policy: policy.comfort_cps_cjk),
        ("speed_hard_cps_cjk", "阅读硬限", lambda policy: policy.hard_cps_cjk),
        ("speed_comfort_cps_latin", "阅读硬限", lambda policy: policy.comfort_cps_latin),
        ("speed_hard_cps_latin", "阅读硬限", lambda policy: policy.hard_cps_latin),
        (
            "speed_adjacent_p90_target",
            "阅读硬限",
            lambda policy: policy.adjacent_p90_target,
        ),
        (
            "speed_adjacent_emergency_limit",
            "阅读硬限",
            lambda policy: policy.adjacent_emergency_limit,
        ),
        ("speed_whitespace_weight", "阅读权重", lambda policy: policy.whitespace_weight),
        (
            "speed_weak_punctuation_weight",
            "阅读权重",
            lambda policy: policy.weak_punctuation_weight,
        ),
        (
            "speed_strong_punctuation_weight",
            "阅读权重",
            lambda policy: policy.strong_punctuation_weight,
        ),
        (
            "speed_min_duration_ms",
            "时间与结构",
            lambda policy: round(policy.min_duration_seconds * 1000),
        ),
        (
            "speed_max_duration_ms",
            "时间与结构",
            lambda policy: round(policy.max_duration_seconds * 1000),
        ),
        (
            "speed_local_window_radius",
            "时间与结构",
            lambda policy: policy.local_window_radius,
        ),
        ("speed_rhythm_reset_ms", "时间与结构", lambda policy: policy.rhythm_reset_ms),
        (
            "speed_hard_rhythm_reset_ms",
            "时间与结构",
            lambda policy: policy.hard_rhythm_reset_ms,
        ),
        (
            "speed_bidirectional_smoothing",
            "时间与结构",
            lambda policy: policy.bidirectional_smoothing,
        ),
        (
            "speed_low_boundary_shift_ms",
            "媒体增强对齐",
            lambda policy: policy.low_confidence_boundary_shift_ms,
        ),
        (
            "speed_medium_boundary_shift_ms",
            "媒体增强对齐",
            lambda policy: policy.medium_confidence_boundary_shift_ms,
        ),
        (
            "speed_high_boundary_shift_ms",
            "媒体增强对齐",
            lambda policy: policy.high_confidence_boundary_shift_ms,
        ),
    )

    _SPECIAL_POLICY_FIELDS = {
        "speed_min_duration_ms": "min_duration_seconds",
        "speed_max_duration_ms": "max_duration_seconds",
        "speed_low_boundary_shift_ms": "low_confidence_boundary_shift_ms",
        "speed_medium_boundary_shift_ms": "medium_confidence_boundary_shift_ms",
        "speed_high_boundary_shift_ms": "high_confidence_boundary_shift_ms",
    }

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        profile_store: Any | None = None,
    ):
        super().__init__(parent)
        self._applyingPolicy = False
        self._profileStoreError: Exception | None = None
        try:
            self._profileStore = profile_store or PostprocessProfileStore()
        except Exception as exc:
            self._profileStore = None
            self._profileStoreError = exc
        self.setWindowTitle(self.tr("字幕后处理设置"))
        self.setObjectName("postprocessSettingInterface")
        self.resize(1000, 800)

        self.titleLabel = TitleLabel(self.tr("字幕后处理设置"), self)
        self.titleLabel.setObjectName("speedSettingTitle")
        self.subtitleLabel = QLabel(
            self.tr("集中管理文本清理、显示速度、时间轴、语义修复与质量报告。"), self
        )
        self.subtitleLabel.setObjectName("speedSettingSubtitle")
        self.pivot = SegmentedWidget(self)
        self.pivot.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.stackedWidget = QStackedWidget(self)
        self.vBoxLayout = QVBoxLayout(self)

        self._tabs: dict[str, _SettingTab] = {}
        for route_key, title in (
            ("overview", self.tr("方案")),
            ("text", self.tr("文本处理")),
            ("reading", self.tr("阅读目标")),
            ("timing", self.tr("时间与结构")),
            ("semantic", self.tr("语义修复")),
            ("alignment", self.tr("媒体增强对齐")),
            ("report", self.tr("报告存储")),
        ):
            self._addTab(route_key, title)

        self._buildOverviewTab()
        self._buildTextTab()
        self._buildReadingTab()
        self._buildTimingTab()
        self._buildSemanticTab()
        self._buildAlignmentTab()
        self._buildReportTab()
        self._refreshProfileChoices()
        self._connectPolicyPersistence()
        self._onPresetChanged(cfg.get(cfg.postprocess_profile))

        self.vBoxLayout.setContentsMargins(36, 26, 36, 18)
        self.vBoxLayout.setSpacing(8)
        self.vBoxLayout.addWidget(self.titleLabel)
        self.vBoxLayout.addWidget(self.subtitleLabel)
        self.vBoxLayout.addSpacing(10)
        self.vBoxLayout.addWidget(self.pivot, 0, Qt.AlignLeft)  # type: ignore
        self.vBoxLayout.addWidget(self.stackedWidget, 1)
        self.stackedWidget.currentChanged.connect(self._onCurrentTabChanged)
        self.stackedWidget.setCurrentWidget(self._tabs["overview"])
        self.pivot.setCurrentItem("overview")
        cfg.postprocess_profile.valueChanged.connect(self._onPresetChanged)

        self.setStyleSheet(
            """
            PostprocessSettingInterface { background: transparent; }
            QLabel#speedSettingTitle {
                font: 30px 'Microsoft YaHei';
                background: transparent;
            }
            QLabel#speedSettingSubtitle {
                color: rgba(128, 128, 128, 210);
                background: transparent;
            }
            """
        )

    def _addTab(self, routeKey: str, title: str) -> None:
        tab = _SettingTab(self)
        tab.setObjectName(f"speed-{routeKey}")
        self._tabs[routeKey] = tab
        self.stackedWidget.addWidget(tab)
        self.pivot.addItem(
            routeKey=routeKey,
            text=title,
            onClick=lambda _checked=False, widget=tab: self.stackedWidget.setCurrentWidget(widget),
        )

    def _onCurrentTabChanged(self, index: int) -> None:
        widget = self.stackedWidget.widget(index)
        if widget:
            self.pivot.setCurrentItem(widget.objectName().removeprefix("speed-"))

    def _addResetButton(
        self,
        card: Any,
        configItem: Any,
        default: Callable[[], Any],
    ) -> None:
        button = ToolButton(FIF.SYNC, card)
        button.setObjectName("speedResetButton")
        button.setFixedSize(32, 32)
        button.clicked.connect(lambda _checked=False: cfg.set(configItem, default()))
        card.hBoxLayout.addWidget(button, 0, Qt.AlignRight)  # type: ignore
        card.hBoxLayout.addSpacing(8)

        def refresh(value: Any = None) -> None:
            current = cfg.get(configItem) if value is None else value
            modified = current != default()
            button.setEnabled(modified)
            button.setToolTip(
                self.tr("恢复当前参数默认值") if modified else self.tr("当前已是默认值")
            )

        configItem.valueChanged.connect(refresh)
        refresh()

    def _addProfileReset(self, card: Any, config_item: Any, field_name: str) -> None:
        """Add a compact reset icon targeting the profile's factory baseline."""
        button = ToolButton(FIF.SYNC, card)
        button.setObjectName("postprocessResetButton")
        button.setFixedSize(32, 32)
        button.clicked.connect(
            lambda _checked=False: self._resetProfileField(config_item, field_name)
        )
        card.hBoxLayout.addWidget(button, 0, Qt.AlignRight)  # type: ignore[attr-defined]
        card.hBoxLayout.addSpacing(8)

        def refresh(_value: Any = None) -> None:
            try:
                profile = self._requireProfileStore().get(cfg.get(cfg.postprocess_profile))
                baseline = get_factory_baseline(profile.base_template_id)
                modified = cfg.get(config_item) != getattr(baseline, field_name)
            except Exception:
                modified = cfg.get(config_item) != config_item.defaultValue
            button.setEnabled(modified)
            button.setToolTip(
                self.tr("恢复来源模板出厂值") if modified else self.tr("当前已是出厂值")
            )

        config_item.valueChanged.connect(refresh)
        cfg.postprocess_profile.valueChanged.connect(refresh)
        refresh()

    def _resetProfileField(self, config_item: Any, field_name: str) -> None:
        try:
            profile = self._requireProfileStore().reset_field(
                cfg.get(cfg.postprocess_profile), field_name
            )
            value = getattr(profile.config, field_name)
        except Exception as exc:
            self._showProfileError(self.tr("无法恢复参数"), exc)
            return
        self._applyingPolicy = True
        try:
            cfg.set(config_item, value)
        finally:
            self._applyingPolicy = False

    def _policyDefault(self, getter: Callable[[SpeedPolicy], Any]) -> Any:
        return getter(self._resolveCurrentPolicy())

    def _addPolicyReset(
        self, card: Any, configName: str, getter: Callable[[SpeedPolicy], Any]
    ) -> None:
        button = ToolButton(FIF.SYNC, card)
        button.setObjectName("speedResetButton")
        button.setFixedSize(32, 32)
        button.clicked.connect(lambda _checked=False: self._resetPolicyField(configName, getter))
        card.hBoxLayout.addWidget(button, 0, Qt.AlignRight)  # type: ignore
        card.hBoxLayout.addSpacing(8)

        configItem = getattr(cfg, configName)

        def refresh(_: Any = None) -> None:
            try:
                profile = self._requireProfileStore().get(cfg.get(cfg.postprocess_profile))
                baseline = get_factory_baseline(profile.base_template_id)
                policy = resolve_speed_policy(baseline.speed_profile, baseline.speed_overrides)
                modified = cfg.get(configItem) != getter(policy)
            except Exception:
                modified = False
            button.setEnabled(modified)
            button.setToolTip(
                self.tr("移除此参数的方案覆盖") if modified else self.tr("当前已是方案默认值")
            )

        configItem.valueChanged.connect(refresh)
        cfg.postprocess_profile.valueChanged.connect(refresh)
        refresh()

    def _buildOverviewTab(self) -> None:
        tab = self._tabs["overview"]
        group = SettingCardGroup(self.tr("运行方式"), tab.scrollWidget)
        self.activationCard = SwitchSettingCard(
            FIF.PLAY,
            self.tr("完整流程默认执行"),
            self.tr("任务创建页仍可关闭某次完整流程的整个后处理阶段"),
            cfg.postprocess_enabled,
            group,
        )
        self.profileCard = ComboBoxSettingCard(
            cfg.postprocess_profile,
            FIF.SPEED_HIGH,
            self.tr("字幕后处理方案"),
            self.tr("三个模板均可编辑；自定义方案从模板当前值复制"),
            texts=[self.tr("宽松"), self.tr("均衡"), self.tr("平滑优先")],
            parent=group,
        )
        self.modeCard = ComboBoxSettingCard(
            cfg.speed_mode,
            FIF.DOCUMENT,
            self.tr("默认处理方式"),
            self.tr("应用会写入高可信修改；仅分析只生成结果和报告"),
            texts=[self.tr("应用修改"), self.tr("仅分析")],
            parent=group,
        )
        self.primaryCard = ComboBoxSettingCard(
            cfg.speed_primary,
            FIF.FONT,
            self.tr("阅读体验主侧"),
            self.tr("默认优化译文；布局主侧随字幕布局决定；原文适合单语校正"),
            texts=[self.tr("译文"), self.tr("布局主侧"), self.tr("原文")],
            parent=group,
        )
        self.referenceAuditCard = SwitchSettingCard(
            FIF.DOCUMENT,
            self.tr("同时审计参考侧"),
            self.tr("检查另一侧的硬超速，但不默认改写参考文本"),
            cfg.speed_reference_hard_audit,
            group,
        )
        self._addResetButton(
            self.activationCard,
            cfg.postprocess_enabled,
            lambda: cfg.postprocess_enabled.defaultValue,
        )
        group.addSettingCard(self.activationCard)
        group.addSettingCard(self.profileCard)
        for card, item, field in (
            (self.modeCard, cfg.speed_mode, "speed_mode"),
            (self.primaryCard, cfg.speed_primary, "speed_primary"),
            (
                self.referenceAuditCard,
                cfg.speed_reference_hard_audit,
                "speed_reference_audit",
            ),
        ):
            self._addProfileReset(card, item, field)
            group.addSettingCard(card)

        self.createProfileCard = PushSettingCard(
            self.tr("新建"),
            FIF.ADD,
            self.tr("复制当前模板"),
            self.tr("自定义方案必须从宽松、均衡或平滑优先模板复制"),
            group,
        )
        self.createProfileCard.clicked.connect(self._createProfile)
        group.addSettingCard(self.createProfileCard)
        self.renameProfileCard = PushSettingCard(
            self.tr("重命名"),
            FIF.EDIT,
            self.tr("重命名自定义方案"),
            self.tr("只修改显示名称，任务中保存的稳定方案 ID 不变"),
            group,
        )
        self.renameProfileCard.clicked.connect(self._renameProfile)
        group.addSettingCard(self.renameProfileCard)
        self.deleteProfileCard = PushSettingCard(
            self.tr("删除"),
            FIF.DELETE,
            self.tr("删除自定义方案"),
            self.tr("内置方案不可删除；删除当前方案后会切换到均衡"),
            group,
        )
        self.deleteProfileCard.clicked.connect(self._deleteProfile)
        group.addSettingCard(self.deleteProfileCard)
        tab.addGroup(group)

    def _buildTextTab(self) -> None:
        tab = self._tabs["text"]
        group = SettingCardGroup(self.tr("文本清理"), tab.scrollWidget)
        self.trimTrailingPunctCard = SwitchSettingCard(
            FIF.FONT,
            self.tr("清理行尾弱标点"),
            self.tr("删除行尾逗号、句号等弱标点；出厂默认开启"),
            cfg.trim_trailing_punct,
            group,
        )
        self.normalizeQuotesCard = SwitchSettingCard(
            FIF.LABEL,
            self.tr("中文引号规范化"),
            self.tr("将中文引号统一为「」/『』"),
            cfg.need_normalize_quotes,
            group,
        )
        self.removePlaceholdersCard = SwitchSettingCard(
            FIF.DELETE,
            self.tr("占位符清理"),
            self.tr("删除 [Music]、[音乐]、♪ 等非语义占位符行"),
            cfg.need_remove_placeholders,
            group,
        )
        self.optimizeBothSidesCard = SwitchSettingCard(
            FIF.LANGUAGE,
            self.tr("同时优化双语两侧"),
            self.tr("默认只改写译文；开启后文本能力也可分别作用于原文"),
            cfg.postprocess_optimize_both_sides,
            group,
        )
        bindings = (
            (self.trimTrailingPunctCard, cfg.trim_trailing_punct, "trim_trailing_punct"),
            (self.normalizeQuotesCard, cfg.need_normalize_quotes, "normalize_quotes"),
            (self.removePlaceholdersCard, cfg.need_remove_placeholders, "remove_placeholders"),
            (
                self.optimizeBothSidesCard,
                cfg.postprocess_optimize_both_sides,
                "optimize_both_sides",
            ),
        )
        for card, item, field in bindings:
            self._addProfileReset(card, item, field)
            group.addSettingCard(card)
        tab.addGroup(group)

    def _doubleCard(
        self,
        group: SettingCardGroup,
        configName: str,
        title: str,
        content: str,
        getter: Callable[[SpeedPolicy], Any],
        maximum: float,
        decimals: int = 1,
        step: float = 0.1,
    ) -> None:
        card = DoubleSpinBoxSettingCard(
            getattr(cfg, configName),
            FIF.SPEED_HIGH.icon(),
            self.tr(title),
            self.tr(content),
            minimum=0.0,
            maximum=maximum,
            decimals=decimals,
            step=step,
            parent=group,
        )
        self._addPolicyReset(card, configName, getter)
        group.addSettingCard(card)

    def _rangeCard(
        self,
        group: SettingCardGroup,
        configName: str,
        title: str,
        content: str,
        getter: Callable[[SpeedPolicy], Any],
    ) -> None:
        item = getattr(cfg, configName)
        low, high = item.range
        step = 1 if (high - low) <= 50 else 50
        card = SliderSpinBoxSettingCard(
            item,
            FIF.STOP_WATCH,
            self.tr(title),
            self.tr(content),
            minimum=low,
            maximum=high,
            step=step,
            parent=group,
        )
        self._addPolicyReset(card, configName, getter)
        group.addSettingCard(card)

    def _buildReadingTab(self) -> None:
        tab = self._tabs["reading"]
        targetGroup = SettingCardGroup(self.tr("舒适与硬限"), tab.scrollWidget)
        for args in (
            (
                "speed_comfort_cps_cjk",
                "中文舒适 CPS",
                "中文、日文与韩文主侧的舒适阅读目标",
                lambda policy: policy.comfort_cps_cjk,
                40.0,
            ),
            (
                "speed_hard_cps_cjk",
                "中文硬限 CPS",
                "超过该负载时优先尝试调整时间与结构",
                lambda policy: policy.hard_cps_cjk,
                40.0,
            ),
            (
                "speed_comfort_cps_latin",
                "非中日韩舒适 CPS",
                "拉丁字母等非中日韩文字的舒适阅读目标",
                lambda policy: policy.comfort_cps_latin,
                60.0,
            ),
            (
                "speed_hard_cps_latin",
                "非中日韩硬限 CPS",
                "非中日韩文字的硬超速判定值",
                lambda policy: policy.hard_cps_latin,
                60.0,
            ),
            (
                "speed_adjacent_p90_target",
                "相邻跳变 P90 目标",
                "限制大多数相邻字幕的阅读负载倍率落差",
                lambda policy: policy.adjacent_p90_target,
                5.0,
            ),
            (
                "speed_adjacent_emergency_limit",
                "相邻跳变紧急线",
                "单个边界超过该倍率时必须报告",
                lambda policy: policy.adjacent_emergency_limit,
                8.0,
            ),
        ):
            self._doubleCard(targetGroup, *args)
        tab.addGroup(targetGroup)

        weightGroup = SettingCardGroup(self.tr("阅读负载权重"), tab.scrollWidget)
        for args in (
            (
                "speed_whitespace_weight",
                "空白权重",
                "空格和换行计入阅读负载的权重",
                lambda policy: policy.whitespace_weight,
                2.0,
            ),
            (
                "speed_weak_punctuation_weight",
                "弱标点权重",
                "逗号、顿号等短停顿标点的权重",
                lambda policy: policy.weak_punctuation_weight,
                2.0,
            ),
            (
                "speed_strong_punctuation_weight",
                "强标点权重",
                "句号、问号等完整停顿标点的权重",
                lambda policy: policy.strong_punctuation_weight,
                2.0,
            ),
        ):
            self._doubleCard(weightGroup, *args, decimals=2, step=0.05)
        tab.addGroup(weightGroup)

    def _buildTimingTab(self) -> None:
        tab = self._tabs["timing"]
        gapGroup = SettingCardGroup(self.tr("间隙处理"), tab.scrollWidget)
        self.fixGapsCard = SwitchSettingCard(
            FIF.ALIGNMENT,
            self.tr("闭合短间隙"),
            self.tr("消除相邻字幕间短暂闪烁；出厂默认关闭"),
            cfg.need_fix_gaps,
            gapGroup,
        )
        self.maxGapCard = SliderSpinBoxSettingCard(
            cfg.max_gap_ms,
            FIF.STOP_WATCH,
            self.tr("最大闭合间隙 (ms)"),
            self.tr("闪轴闭合与尾部补偿的分界：此值以下闭合，以上补偿"),
            minimum=100,
            maximum=2000,
            step=50,
            parent=gapGroup,
        )
        for card, item, field in (
            (self.fixGapsCard, cfg.need_fix_gaps, "fix_gaps"),
            (self.maxGapCard, cfg.max_gap_ms, "max_gap_ms"),
        ):
            self._addProfileReset(card, item, field)
            gapGroup.addSettingCard(card)
        tab.addGroup(gapGroup)

        compGroup = SettingCardGroup(self.tr("尾部补偿"), tab.scrollWidget)
        self.tailCompensationCard = SwitchSettingCard(
            FIF.ALIGNMENT,
            self.tr("尾部补偿"),
            self.tr("停顿前为上一段结尾按补偿曲线追加显示时长，避免其过快消失；出厂默认关闭"),
            cfg.need_tail_compensation,
            compGroup,
        )
        self.minCompensationCard = SliderSpinBoxSettingCard(
            cfg.min_compensation_ms,
            FIF.STOP_WATCH,
            self.tr("最小补偿 (ms)"),
            self.tr("间隙刚超过最大闭合间隙时给予的补偿；不超过最大闭合间隙"),
            minimum=0,
            maximum=2000,
            step=50,
            parent=compGroup,
        )
        self.maxCompensationGapCard = SliderSpinBoxSettingCard(
            cfg.max_compensation_gap_ms,
            FIF.STOP_WATCH,
            self.tr("最大补偿间隙 (ms)"),
            self.tr("补偿达到上限的间隙；更大的间隙补偿不再增加"),
            minimum=100,
            maximum=10000,
            step=100,
            parent=compGroup,
        )
        self.maxCompensationCard = SliderSpinBoxSettingCard(
            cfg.max_compensation_ms,
            FIF.STOP_WATCH,
            self.tr("最大补偿 (ms)"),
            self.tr("单段结尾可获得的补偿上限"),
            minimum=0,
            maximum=5000,
            step=50,
            parent=compGroup,
        )
        for card, item, field in (
            (self.tailCompensationCard, cfg.need_tail_compensation, "tail_compensation"),
            (self.minCompensationCard, cfg.min_compensation_ms, "min_compensation_ms"),
            (self.maxCompensationGapCard, cfg.max_compensation_gap_ms, "max_compensation_gap_ms"),
            (self.maxCompensationCard, cfg.max_compensation_ms, "max_compensation_ms"),
        ):
            self._addProfileReset(card, item, field)
            compGroup.addSettingCard(card)
        tab.addGroup(compGroup)
        self._connectCompensationClamp()
        self._onCompensationChanged()  # 建立初始联动范围

        durationGroup = SettingCardGroup(self.tr("显示时长"), tab.scrollWidget)
        for args in (
            (
                "speed_min_duration_ms",
                "普通字幕最短时长 (ms)",
                "短于该时长时，仅在安全且有证据时扩展",
                lambda policy: round(policy.min_duration_seconds * 1000),
            ),
            (
                "speed_max_duration_ms",
                "普通字幕最长时长 (ms)",
                "保护字幕可以例外；普通字幕优先保持在此范围内",
                lambda policy: round(policy.max_duration_seconds * 1000),
            ),
            (
                "speed_local_window_radius",
                "局部节奏半径",
                "目标字幕前后参与局部节奏估计的字幕数量",
                lambda policy: policy.local_window_radius,
            ),
            (
                "speed_rhythm_reset_ms",
                "节奏重置间隙 (ms)",
                "无额外证据时，达到该间隙即开始新的节奏区间",
                lambda policy: policy.rhythm_reset_ms,
            ),
            (
                "speed_hard_rhythm_reset_ms",
                "硬重置间隙 (ms)",
                "任何结构调整都不能跨越的长停顿边界",
                lambda policy: policy.hard_rhythm_reset_ms,
            ),
        ):
            self._rangeCard(durationGroup, *args)
        self.bidirectionalCard = SwitchSettingCard(
            FIF.SPEED_HIGH,
            self.tr("双向平滑"),
            self.tr("允许有限缩短过慢字幕；宽松和均衡方案默认只降低快速尖峰"),
            cfg.speed_bidirectional_smoothing,
            durationGroup,
        )
        self._addPolicyReset(
            self.bidirectionalCard,
            "speed_bidirectional_smoothing",
            lambda policy: policy.bidirectional_smoothing,
        )
        durationGroup.addSettingCard(self.bidirectionalCard)
        tab.addGroup(durationGroup)

    def _buildSemanticTab(self) -> None:
        tab = self._tabs["semantic"]
        group = SettingCardGroup(self.tr("语义安全"), tab.scrollWidget)
        self.semanticRepairCard = SwitchSettingCard(
            FIF.ROBOT,
            self.tr("语义修复"),
            self.tr("时间调整仍无法解决时，允许在受限窗口内重写或重分段"),
            cfg.speed_semantic_repair,
            group,
        )
        self.semanticWindowCard = SliderSpinBoxSettingCard(
            cfg.speed_semantic_window,
            FIF.ROBOT,
            self.tr("语义窗口大小"),
            self.tr("单次语义修复可查看的相邻字幕数量，不能跨保护或重置边界"),
            minimum=cfg.speed_semantic_window.range[0],
            maximum=cfg.speed_semantic_window.range[1],
            step=1,
            parent=group,
        )
        self.uncertainReviewCard = SwitchSettingCard(
            FIF.DOCUMENT,
            self.tr("不确定项独立复核"),
            self.tr("实体、数字等确定性检查无法裁决时，再调用 LLM 复核"),
            cfg.speed_llm_uncertain_review,
            group,
        )
        for card, item, field in (
            (self.semanticRepairCard, cfg.speed_semantic_repair, "speed_semantic_repair"),
            (self.semanticWindowCard, cfg.speed_semantic_window, "speed_semantic_window"),
            (
                self.uncertainReviewCard,
                cfg.speed_llm_uncertain_review,
                "speed_llm_uncertain_review",
            ),
        ):
            self._addProfileReset(card, item, field)
            group.addSettingCard(card)
        tab.addGroup(group)

    def _buildAlignmentTab(self) -> None:
        tab = self._tabs["alignment"]
        group = SettingCardGroup(self.tr("媒体增强时间轴"), tab.scrollWidget)
        self.preciseTimingCard = SwitchSettingCard(
            FIF.STOP_WATCH,
            self.tr("对齐时间轴"),
            self.tr(
                "需要先“关联媒体”才能生成对齐时间轴；未关联媒体时本项自动降级为"
                "字幕内部估算时间轴。默认关闭，对齐失败时同样降级。"
            ),
            cfg.speed_precise_timing,
            group,
        )
        self._addProfileReset(
            self.preciseTimingCard,
            cfg.speed_precise_timing,
            "precise_timing",
        )
        group.addSettingCard(self.preciseTimingCard)
        for args in (
            (
                "speed_low_boundary_shift_ms",
                "仅字幕边界移动预算 (ms)",
                "只有字幕时间轴证据时的单边累计移动上限",
                lambda policy: policy.low_confidence_boundary_shift_ms,
            ),
            (
                "speed_medium_boundary_shift_ms",
                "VAD 边界移动预算 (ms)",
                "有语音活动证据、但没有词级锚点时的上限",
                lambda policy: policy.medium_confidence_boundary_shift_ms,
            ),
            (
                "speed_high_boundary_shift_ms",
                "强制对齐移动预算 (ms)",
                "高可信词级锚点允许的单边累计移动上限",
                lambda policy: policy.high_confidence_boundary_shift_ms,
            ),
        ):
            self._rangeCard(group, *args)
        tab.addGroup(group)

    def _buildReportTab(self) -> None:
        tab = self._tabs["report"]
        group = SettingCardGroup(self.tr("报告与时间证据"), tab.scrollWidget)
        self.qaReportCard = SwitchSettingCard(
            FIF.DOCUMENT,
            self.tr("生成速度 QA 报告"),
            self.tr("记录修改前后指标、未解决问题和风险队列"),
            cfg.speed_qa_report,
            group,
        )
        self.sidecarCard = SwitchSettingCard(
            FIF.SAVE,
            self.tr("保存时间证据 sidecar"),
            self.tr("在字幕旁写入带指纹的 .vctiming.json；标准 SRT 保持不变"),
            cfg.speed_save_timing_sidecar,
            group,
        )
        self._addProfileReset(self.qaReportCard, cfg.speed_qa_report, "qa_report")
        group.addSettingCard(self.qaReportCard)
        self._addResetButton(
            self.sidecarCard,
            cfg.speed_save_timing_sidecar,
            lambda: cfg.speed_save_timing_sidecar.defaultValue,
        )
        group.addSettingCard(self.sidecarCard)
        tab.addGroup(group)

    def _profileLabel(self, profile_id: str) -> str:
        if self._profileStore is not None:
            try:
                return self._profileStore.get(profile_id).name
            except Exception:
                pass
        return profile_id

    def _profileIds(self) -> tuple[str, ...]:
        if self._profileStore is None:
            return TEMPLATE_IDS
        return tuple(profile.profile_id for profile in self._profileStore.list())

    def _refreshProfileChoices(self, selected_id: str | None = None) -> None:
        profile_ids = self._profileIds()
        current_id = selected_id or cfg.get(cfg.postprocess_profile)
        if current_id not in profile_ids:
            if self._profileStore is None:
                profile_ids = (*profile_ids, current_id)
            else:
                current_id = SpeedPreset.BALANCED.value
                cfg.set(cfg.postprocess_profile, current_id)

        validator: Any = cfg.postprocess_profile.validator
        if hasattr(validator, "set_options"):
            validator.set_options(profile_ids)
        combo = self.profileCard.comboBox
        combo.blockSignals(True)
        try:
            combo.clear()
            labels = {profile_id: self._profileLabel(profile_id) for profile_id in profile_ids}
            self.profileCard.optionToText = labels
            for profile_id in profile_ids:
                combo.addItem(labels[profile_id], userData=profile_id)
            combo.setCurrentText(labels[current_id])
        finally:
            combo.blockSignals(False)
        self._updateProfileActions()

    def _updateProfileActions(self) -> None:
        profile_id = cfg.get(cfg.postprocess_profile)
        is_builtin = profile_id in TEMPLATE_IDS
        store_ready = self._profileStore is not None
        self.createProfileCard.setEnabled(store_ready and is_builtin)
        self.renameProfileCard.setEnabled(store_ready and not is_builtin)
        self.deleteProfileCard.setEnabled(store_ready and not is_builtin)

    def _showProfileError(self, action: str, error: Exception) -> None:
        InfoBar.error(
            title=self.tr("方案操作失败"),
            content=f"{action}: {error}",
            orient=Qt.Horizontal,  # type: ignore
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=6000,
            parent=self,
        )

    def _requireProfileStore(self) -> PostprocessProfileStore:
        if self._profileStore is None:
            raise RuntimeError(
                self.tr("无法读取方案文件")
                + (f": {self._profileStoreError}" if self._profileStoreError else "")
            )
        return self._profileStore

    def _askProfileName(self, title: str, initial_name: str = "") -> str | None:
        dialog = _ProfileNameDialog(title, initial_name, self)
        if not dialog.exec():
            return None
        return dialog.nameLineEdit.text().strip()

    def _createProfile(self) -> None:
        profile_id = cfg.get(cfg.postprocess_profile)
        if profile_id not in TEMPLATE_IDS:
            return
        name = self._askProfileName(self.tr("新建自定义方案"))
        if not name:
            return
        try:
            profile = self._requireProfileStore().copy_template(profile_id, name)
        except Exception as exc:
            self._showProfileError(self.tr("无法创建方案"), exc)
            return
        self._refreshProfileChoices(profile.profile_id)
        cfg.set(cfg.postprocess_profile, profile.profile_id)

    def _renameProfile(self) -> None:
        profile_id = cfg.get(cfg.postprocess_profile)
        try:
            store = self._requireProfileStore()
            profile = store.get(profile_id)
        except Exception as exc:
            self._showProfileError(self.tr("无法读取当前方案"), exc)
            return
        name = self._askProfileName(self.tr("重命名自定义方案"), profile.name)
        if not name or name == profile.name:
            return
        try:
            store.rename(profile_id, name)
        except Exception as exc:
            self._showProfileError(self.tr("无法重命名方案"), exc)
            return
        self._refreshProfileChoices(profile_id)

    def _deleteProfile(self) -> None:
        profile_id = cfg.get(cfg.postprocess_profile)
        try:
            store = self._requireProfileStore()
            profile = store.get(profile_id)
        except Exception as exc:
            self._showProfileError(self.tr("无法读取当前方案"), exc)
            return
        confirm = MessageBox(
            self.tr("删除自定义方案"),
            self.tr("确定删除“{name}”吗？此操作不会修改已生成字幕。").format(name=profile.name),
            self,
        )
        if not confirm.exec():
            return
        try:
            store.delete(profile_id)
        except Exception as exc:
            self._showProfileError(self.tr("无法删除方案"), exc)
            return
        cfg.set(cfg.postprocess_profile, SpeedPreset.BALANCED.value)
        self._refreshProfileChoices(SpeedPreset.BALANCED.value)

    def _resolveCurrentPolicy(self) -> SpeedPolicy:
        profile = self._requireProfileStore().get(cfg.get(cfg.postprocess_profile))
        return resolve_speed_policy(profile.config.speed_profile, profile.config.speed_overrides)

    @classmethod
    def _policyField(cls, config_name: str) -> str:
        return cls._SPECIAL_POLICY_FIELDS.get(config_name, config_name.removeprefix("speed_"))

    @classmethod
    def _profileValue(cls, config_name: str, value: Any) -> Any:
        if config_name in {"speed_min_duration_ms", "speed_max_duration_ms"}:
            return value / 1000
        return value

    def _connectCompensationClamp(self) -> None:
        """连动钳制尾部补偿四个旋钮，使其永远满足补偿曲线约束（见 docs/adr/0005）。"""
        self._clampingCompensation = False
        for item in (
            cfg.max_gap_ms,
            cfg.min_compensation_ms,
            cfg.max_compensation_gap_ms,
            cfg.max_compensation_ms,
        ):
            item.valueChanged.connect(self._onCompensationChanged)

    def _onCompensationChanged(self, *_: Any) -> None:
        """把四个旋钮规整到合法区（以最大闭合间隙为锚），并收紧各滑块可选范围。

        规整为幂等操作：合法输入原样返回，故不会与持久化/应用流程形成死循环。
        """
        if self._applyingPolicy or getattr(self, "_clampingCompensation", False):
            return
        if not hasattr(self, "minCompensationCard"):
            return
        self._clampingCompensation = True
        try:
            mg = cfg.get(cfg.max_gap_ms)
            mc = cfg.get(cfg.min_compensation_ms)
            mcg = cfg.get(cfg.max_compensation_gap_ms)
            mx = cfg.get(cfg.max_compensation_ms)
            # 规整（顺序与 PostprocessConfig 约束一致；最大闭合间隙为锚）
            mc = max(0, min(mc, mg))
            mcg = max(mcg, mg + 1)
            mx = max(mx, mc)
            mx = min(mx, mc + (mcg - mg))
            # 写回被调整的值（合法，persist 的 set_field 不会报错）
            if mc != cfg.get(cfg.min_compensation_ms):
                cfg.set(cfg.min_compensation_ms, mc)
            if mcg != cfg.get(cfg.max_compensation_gap_ms):
                cfg.set(cfg.max_compensation_gap_ms, mcg)
            if mx != cfg.get(cfg.max_compensation_ms):
                cfg.set(cfg.max_compensation_ms, mx)
            # 收紧各滑块 / 输入框范围，令用户拖不出非法组合
            self.minCompensationCard.setRange(0, min(mg, mx))
            self.maxCompensationGapCard.setRange(mg + max(1, mx - mc), 10000)
            self.maxCompensationCard.setRange(mc, mc + (mcg - mg))
        finally:
            self._clampingCompensation = False

    def _connectPolicyPersistence(self) -> None:
        for config_name, _, _ in self._POLICY_BINDINGS:
            item = getattr(cfg, config_name)
            item.valueChanged.connect(
                lambda value, name=config_name: self._persistPolicyValue(name, value)
            )
        for item, field_name in (
            (cfg.speed_mode, "speed_mode"),
            (cfg.speed_primary, "speed_primary"),
            (cfg.speed_reference_hard_audit, "speed_reference_audit"),
            (cfg.speed_semantic_repair, "speed_semantic_repair"),
            (cfg.speed_semantic_window, "speed_semantic_window"),
            (cfg.speed_llm_uncertain_review, "speed_llm_uncertain_review"),
            (cfg.speed_precise_timing, "precise_timing"),
            (cfg.speed_qa_report, "qa_report"),
            (cfg.trim_trailing_punct, "trim_trailing_punct"),
            (cfg.need_normalize_quotes, "normalize_quotes"),
            (cfg.need_remove_placeholders, "remove_placeholders"),
            (cfg.need_fix_gaps, "fix_gaps"),
            (cfg.max_gap_ms, "max_gap_ms"),
            (cfg.need_tail_compensation, "tail_compensation"),
            (cfg.min_compensation_ms, "min_compensation_ms"),
            (cfg.max_compensation_gap_ms, "max_compensation_gap_ms"),
            (cfg.max_compensation_ms, "max_compensation_ms"),
            (cfg.postprocess_optimize_both_sides, "optimize_both_sides"),
        ):
            item.valueChanged.connect(
                lambda value, field_name=field_name: self._persistProfileValue(
                    field_name, value
                )
            )

    def _persistProfileValue(self, field_name: str, value: Any) -> None:
        if self._applyingPolicy:
            return
        try:
            self._requireProfileStore().set_field(
                cfg.get(cfg.postprocess_profile), field_name, value
            )
        except Exception as exc:
            self._showProfileError(self.tr("无法保存参数"), exc)

    def _persistPolicyValue(self, config_name: str, value: Any) -> None:
        if self._applyingPolicy:
            return
        try:
            store = self._requireProfileStore()
            profile_id = cfg.get(cfg.postprocess_profile)
            profile = store.get(profile_id)
            overrides = dict(profile.config.speed_overrides)
            overrides[self._policyField(config_name)] = self._profileValue(config_name, value)
            store.set_field(profile_id, "speed_overrides", overrides)
        except Exception as exc:
            self._showProfileError(self.tr("无法保存参数"), exc)

    def _resetPolicyField(self, config_name: str, getter: Callable[[SpeedPolicy], Any]) -> None:
        try:
            store = self._requireProfileStore()
            profile = store.get(cfg.get(cfg.postprocess_profile))
            overrides = dict(profile.config.speed_overrides)
            overrides.pop(self._policyField(config_name), None)
            store.set_field(profile.profile_id, "speed_overrides", overrides)
            baseline = get_factory_baseline(profile.base_template_id)
            policy = resolve_speed_policy(baseline.speed_profile, baseline.speed_overrides)
        except Exception as exc:
            self._showProfileError(self.tr("无法恢复参数"), exc)
            return
        self._applyingPolicy = True
        try:
            cfg.set(getattr(cfg, config_name), getter(policy))
        finally:
            self._applyingPolicy = False

    def _onPresetChanged(self, preset: str) -> None:
        try:
            profile = self._requireProfileStore().get(preset)
            policy = resolve_speed_policy(
                profile.config.speed_profile, profile.config.speed_overrides
            )
        except Exception as exc:
            self._showProfileError(self.tr("无法切换方案"), exc)
            return
        self._applyPolicy(policy)
        self._applyProfileConfig(profile.config)
        self._refreshProfileChoices(preset)

    def _applyProfileConfig(self, config: Any) -> None:
        self._applyingPolicy = True
        try:
            for item, value in (
                (cfg.speed_mode, config.speed_mode),
                (cfg.speed_primary, config.speed_primary),
                (cfg.speed_reference_hard_audit, config.speed_reference_audit),
                (cfg.speed_semantic_repair, config.speed_semantic_repair),
                (cfg.speed_semantic_window, config.speed_semantic_window),
                (cfg.speed_llm_uncertain_review, config.speed_llm_uncertain_review),
                (cfg.speed_precise_timing, config.precise_timing),
                (cfg.speed_qa_report, config.qa_report),
                (cfg.trim_trailing_punct, config.trim_trailing_punct),
                (cfg.need_normalize_quotes, config.normalize_quotes),
                (cfg.need_remove_placeholders, config.remove_placeholders),
                (cfg.need_fix_gaps, config.fix_gaps),
                (cfg.max_gap_ms, config.max_gap_ms),
                (cfg.need_tail_compensation, config.tail_compensation),
                (cfg.min_compensation_ms, config.min_compensation_ms),
                (cfg.max_compensation_gap_ms, config.max_compensation_gap_ms),
                (cfg.max_compensation_ms, config.max_compensation_ms),
                (cfg.postprocess_optimize_both_sides, config.optimize_both_sides),
            ):
                cfg.set(item, value)
        finally:
            self._applyingPolicy = False
        self._onCompensationChanged()  # 依新方案刷新联动范围

    def _applyPolicy(self, policy: SpeedPolicy) -> None:
        self._applyingPolicy = True
        try:
            for configName, _, getter in self._POLICY_BINDINGS:
                cfg.set(getattr(cfg, configName), getter(policy))
        finally:
            self._applyingPolicy = False


# Compatibility import for extensions that used the earlier page name.
SpeedSettingInterface = PostprocessSettingInterface
