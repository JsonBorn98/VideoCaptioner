Autopilot: true
Claimed-at: 2026-09-11T07:52:53.668Z
# 03 - 接通主修复容量规划

**构建什么.** 让完整后处理任务真正按模型容量发送主修复批次：每批计算完整请求输入和输出预留，容量不足时先减少修复主体数量，再缩减边界上下文；零上下文单主体仍无法容纳时明确报告，而不是反复截断或静默丢内容。该行为在现有执行方式下即可端到端验证，不依赖并发或02的内部重构。

依据本 feature 规格的容量规划部分、P04、ADR-0019及ADR-0020。覆盖主/译文本、提示词、问题、术语等已验证资产、反馈、上下文与结构化输出开销；一对多输出须预留原译对应和协议开销。模型角色取真实翻译执行快照或已验证角色方案，不因借用网关而切到工具角色。

保留主体数、字幕段数、问题数各自口径；默认主体计数仅为内部起点，不新增重复用户旋钮，也不把上游字幕段计数无解释地当主体数。高级校对组批由05承接，本票提供可复用的容量计算边界，但不预先重构全部翻译规划。以01基准证明容量安全与本地开销，不改变用户模型参数，不发真实请求。

**阻塞于.** 01

**状态.** resolved

- [x] 从完整任务入口捕获实际主修复请求，证明容量校验覆盖完整序列化载荷和输出预留，而不是仅对主体裸文本或固定数量做检查。
- [x] 使用角色真实工作上下文与请求输出上限，覆盖一对多输出及结构化协议开销；缺少可用容量信息时沿用有依据的保守规划并明确记录，不能把未知容量当无限。
- [x] 容量不够时先减少主体再缩减边界上下文，保持主体完整；单主体零上下文仍不足时不发送超预算请求、不截断原文，报告容量不足及正确问题身份。
- [x] 覆盖混合主体大小、长提示词/术语/反馈、两个显示侧、多问题同主体、超大单主体以及配置临界值，实际发送的请求均遵守规划契约。
- [x] 输出截断、响应结构错误和局部缺项分别处理；不把截断结果当完整候选应用，缩批及失败恢复保持有限尝试，不增加不计数的递归调用。
- [x] 普通LLM、增强型及仅报告修复模式保持原有角色、校对要求、内容保护、局部失败和下游门禁；不得自动提高用户请求输出上限或减少必要质量检查。
- [x] 通过01对应容量与本地性能基准，并记录请求数、估计token、主体/问题/字幕段计数及各收缩原因；不声称已有真实模型吞吐证据。

## Comments

2026-09-11 交付：commit `d54d06f`（实现）+ `453e9fe`（审查修复，已提交本地 `master`，未 push）。双轴审查 4 项实质发现当场修完并过一轮修复回审（84 测试 / ruff / pyright 0 三连）。

- 实现：`plan_repair_batches` 新增 `batch_input_estimator`/`output_reserve_estimator` 接缝（对齐上游 `plan_translation_batches` 先例）；`repair.py` 接入真实预算 `repair_profile.work_context_tokens`——每批估算完整序列化请求 + 比例式输出预留（1.5×主体载荷 + 每主体 256 协议开销，`work_context_tokens-1` 与 `max_output_tokens` 双钳制不抬升）；超预算先减主体→再缩上下文→零上下文单主体进 unplannable 明确报告不截断。
- 审查修复：收缩归因移到规划器（`RepairBatch.subjects_shrunk`/`context_shrunk` 收缩路径置位，执行侧按批聚合——修「用问题段数当主体数 + 未收缩批计入收缩数」口径错误）；估算逐字复用 `_request_messages`（单一来源，删 `include_system_prompt` 与重复用户消息包装）；`planned_subjects`/`planned_problems` 与段数分列；测试按载荷形状切片主修复请求（修 `[:requests]` 错位与 `or True` 死守卫）+ 补长 feedback/结构错误路径。
- 测试：新增 `tests/test_postprocess/test_repair_capacity_planning.py` 13 测试（混合主体大小/长提示词/长 feedback/两显示侧（auto_wrap 上下文收缩）/多问题同主体/超大单主体 unplannable/配置临界值 16384/输出截断/结构错误/局部缺项/普通 LLM/仅报告/max_output_tokens 钳制）。
- 回归：后处理目录 204 passed；全量离线 `QT_QPA_PLATFORM=offscreen uv run --no-sync pytest -m 'not integration and not llm'` → 1492 passed / 5 skipped / 61 deselected；2 个 `tests/test_ui` 失败为已知 Qt offscreen 抖动（字体目录 0xC0000005），单独复跑通过，与本票无关。ruff 通过；pyright 0 errors。
- 01 冻结基准复跑（gitignored `benchmark-output/ticket03-*`）：local-long median 2.152s（修复后复测 2.06s）≤ 门槛 2.5225s，跨度 13.3%/1.6% < 15% 噪声规则；independent/mixed/partial-failure/slow-main 请求数、覆盖、输出段数、回退与冻结基线一致，无容量收缩副作用（65536 工作上下文下 fixture 天然放得下——正确行为，×0.75 并发改善门槛属票 04/05）。观测字段经 benchmark `vars()` 序列化自动进 01 产物。
- 边界确认：并发调度/翻译规划重构/高级校对组批（属票 05）/新用户旋钮均未触碰；ADR-0021 轮次快照语义未破坏（规划与执行同轮同用轮初冻结副本，估算与实际请求逐字一致已探针验证 1372==1372）。未发真实模型请求，不声称真实模型吞吐证据。
- 审查遗留（判断题，不阻塞）：测试桩 `_RecordingGateway` 跨 `test_round_snapshot_merge.py`/`test_repair_capacity_planning.py` 复制漂移（共享助手模块留给后续票统一）；`planning.py` `context_radius`/`boundary_context_radius` 双名同概念；`RepairSummary` 持续膨胀趋势。`.scratch/` 保持本机隔离，未提交或外传。
