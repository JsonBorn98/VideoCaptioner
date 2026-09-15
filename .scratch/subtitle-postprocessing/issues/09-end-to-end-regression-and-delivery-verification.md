Autopilot: true
Claimed-at: 2026-09-06T16:39:33.714Z
# 09: 完成端到端回归与交付状态验证

**What to build:** 对完整 workflow、独立调用和所有降级状态执行端到端回归，确认观看约束归属、批量修复、资产隔离、局部回退、成功后导出和旧配置删除在真实适配器组合下保持一致，并完成项目质量检查。

**Blocked by:** 01, 02, 03, 04, 05, 06, 07, 08

**Status:** resolved

- [x] 完整 workflow 覆盖成功、局部未解决后继续下游、模块级失败回退和主动停止。
- [x] 独立后处理覆盖资产自动发现、资产缺失备用输入、仅报告和自动修复。
- [x] 覆盖单语、双语、两侧独立显示策略、自动换行和一对多拆分。
- [x] 覆盖首次提交、4 次业务重试、网络级重试、重复候选和区域级回退计数。
- [x] 覆盖过程目录隔离、manifest 验证、成功后导出及所有失败状态不导出。
- [x] 覆盖旧配置残留不迁移且不影响新默认配置。
- [x] 非集成 pytest、Ruff 和 Pyright 通过，测试只依赖安全的临时资产和 fake 依赖。

## 双轴审查结果（2026-09-07，本票 c3032df；全组 e3f90ca...c3032df）

**本票 diff 审查**（工作区改动 → c3032df）：编码规范轴 3 条——`_RepairGateway` 重复定义已修（提升模块级共享）；status 裸字符串比较与跨文件测试 fixture 判为判断题保留。Spec 轴 2 条——D27「重复修复候选 / 区域状态重复」守卫无触发测试已修（test_repair_execution.py 补两测试：伪造扫描破坏「接受即不再报告」不变量使守卫可达）；ass-Nihon.json 样式数值微调判范围蔓延，已排除出本票提交（留在工作区）。

**全组交付审查**（merge-base e3f90ca，13 commits，68 文件 +6966/-481）：编码规范轴无硬违规，5 条坏味均判为判断题保留（workspace `_write_bytes`/`_write_json` 原子写双胞胎、repair 请求消息双胞胎、`RepairFlow.mode` 裸 str、`_CliSnapshotConfig` 伪装 SubtitleConfig、postprocess.py `active_subtitle_path` 同表达式两连写）。Spec 轴 4 条实质发现需人拍板：

1. **容量收缩在生产路径不可达**——`plan_repair_batches` 收缩/unplannable 逻辑完整（planning.py:340-362），但唯一生产调用（repair.py:801）不传 `token_budget`，收缩与容量不足报告只在测试触发。接线修法是按修复角色的 `work_context_tokens` 减输出预留传预算，但公式改变生产请求行为（超大请求从「不收缩」变「收缩 / 报容量不足」）。
2. **显式补充资产无用户入口**——`explicit_assets` 接缝完备（workspace.py 验证+复制），但 CLI 解析器无资产 flag、GUI 仅内部 `collect_upstream_assets`。US 24 要求「用户可以显式补充资产」；加 CLI flag / GUI 控件是产品决策。
3. **每侧显示模式无用户入口**——核心完整（`config.display_mode_for` + viewing 跳过逻辑），但 CLI 无 flag、GUI 绑定表无控件，只能手改 profile JSON。US 6 要求两侧独立选择显示模式；默认两侧 single_line 可用。
4. **`context` 资产只有声明无生产者**——workspace.py:41/56 声明 kind 与 `context.json`，全仓无写入点，独立运行永久报「过程资产缺失…context」且无产物可消除。spec ID 77 列举「上下文」为过程文件、票 08 验收「上下文…写入 workspace」；生产 context.json / 从 `UPSTREAM_ASSET_KINDS` 删除 / 标注未来槽位，是 spec 语义决策。

已裁决偏差（记录非新发现）：US 30 字面「后处理开始时」快照 vs 实现取修复入口快照——票 05 审查已按 D10 意图裁决保留。

**裁决接受（2026-09-15，发现 #4 `context` 资产）**：provenance 排查确认槽位源自 D07「复用权威术语、全文简报」建议——提出时即记录「尚未获得用户单独确认」，实施 spec 误写成过程文件清单事实，票 03 照字面开槽，生产者因决策未确认从未接线（增强翻译 `TranslationContextBrief` 只在内存传递）。用户裁决选删除：`workspace.py` 三常量移除 `context`、`test_workspace_assets.py` 测试常量同步；CONTEXT.md「过程资产目录」词条去「上下文快照」并入 Avoid；设计记录 D21 与 spec 验收决策行/US22 追加日期勘误（D07 源头行保留原状）；缺简报消费需要时重新加回 kind。发现 #1（容量预算）已由 performance 组票 03 接线解决（repair.py:1363 传 `token_budget`）；发现 #2（显式补充资产入口）维持延后裁决，viewing spec 范围外段已记「另行开票」；发现 #3（显示模式入口）已由 viewing 组票 10-12 交付。本票剩余人工项：点验清单 ②③④（CLI 输出模式/停止按钮/日志配对行）。

**人工点验记录（2026-09-15）**：清单 ① GUI 实机摘要/详情已在 2026-09-14 点验（发现并发等待闪烁，commit 65d5da3 修复）。②③④ 本日用户验收：GUI 侧（③ 停止按钮）验收完毕；② CLI 输出模式与 ④ 日志配对行跳过——CLI 面向 agent 且有自动化测试覆盖，用户裁定不以人工点验替代。

**验证**：全量非集成 1445 passed / 5 skipped（1 个 Qt 子进程 0xC0000005 偶发崩溃按 shoal 协议隔离重跑通过）；Ruff 全过；Pyright 0 errors（20 warnings 全在存量文件）。
