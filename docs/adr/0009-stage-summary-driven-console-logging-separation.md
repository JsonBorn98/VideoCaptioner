# 控制台由前端拥有、日志只管文件，二者同源自阶段摘要

各功能阶段（转录、优化、翻译、断句、字幕后处理、速度、配音、合成）在执行时向用户报告什么，此前是每个核心模块**日志级别选择的副作用**：全局只有一个默认 WARNING 阈值的 `ConsoleFilter`（`core/utils/logger.py`），于是把限流重试、校验失败、分块回退当 WARNING 记录的 optimize/translate/split 显得很吵，而把真实工作记在 INFO（仅进 `app.log` 文件）、或干脆不记日志的字幕后处理与 `core/speed` 则近乎静默；同时 CLI 的 `-v/-q` 只改 root logger，核心 logger 因 `propagate=False` + 固定 INFO 而完全不响应，形同虚设。同一条信息可能经日志 stderr、经 `cli/output`、两者都经或都不经，没有单一事实源。

**决策**：面向用户的控制台由前端拥有——CLI 由 `cli/output` 助手、GUI 由 Qt 信号负责；日志框架只承担诊断与写文件（`app.log`）职责。二者都**从各阶段返回的结构化"阶段摘要"对象渲染**（推广字幕后处理已有的 `QualityReport` / warnings 元组模式），使控制台与文件永不分歧，且"控制台显示什么"不再是日志级别的副作用。既有的实时进度回调（optimize/translate 的 `update_callback`、GUI Qt 信号）保留，不引入重量级的统一 ProgressReporter 协议。

配套两项：把常规自愈事件（agent-loop 校验重试、限流 sleep、split LLM 回退）从 WARNING 降到 DEBUG/INFO，让 WARNING 重新"一出现就值得看"，真正失败仍为 ERROR；`-v/-q` 通过一个可变的模块级共享阈值真正生效（默认档=每阶段一行摘要 + 真实 WARN/ERR，`-v`=额外 per-item 细节与 DEBUG，`-q`=只最终结果 + ERROR）。之所以选可变共享阈值而非设 `VIDEOCAPTIONER_CONSOLE_LOG_LEVEL` 环境变量，是因为后者在 logger 创建时即被捕获进闭包，须在任何核心模块 import 前设置，脆弱且受 import 顺序影响。

**否决的替代方案**：一切走日志框架、需要上控制台的就打 `extra={'console': True}` 标记——会把用户 UX 文案重新绑回日志级别，正是本决策要消除的耦合；以及只在文档里声明分工而不改架构——不修复 optimize/translate 仍靠 WARNING 副作用上屏、后处理仍无法确定性报告的现状。

**衍生效果**：字幕后处理不再在成功时静默，而是从摘要渲染一行 per-stage 概要；`媒体增强对齐`（UI/代码曾称"精准对齐/精准时间轴"，见 `CONTEXT.md`）的结果作为摘要字段常驻显示 `applied / degraded-no-media / degraded-failed` 及每窗证据等级计数，`对齐降级`不再只是一闪而过的提示；新增阶段的义务从"选一个合适的日志级别"变为"返回一个摘要对象"。`core/dubbing` 与 `core/speed` 这两个零日志子系统、以及后处理的 `normalize`/`audit`/`runner`，在编排边界按各自摘要补齐 INFO 覆盖。
