Autopilot: true
Claimed-at: 2026-09-05T18:45:56.951Z
# 06: 复用翻译执行快照与翻译方式

**What to build:** 让后处理依据任务开始时冻结的翻译执行快照选择修复方式。增强型翻译继续使用主翻译加高级校对，普通 LLM 翻译沿用普通方式，非 LLM 翻译不静默升级；独立任务只使用已验证的过程资产。

**Blocked by:** 03, 05

**Status:** resolved

- [x] 完整 workflow 会冻结翻译方式、角色、相关提示配置和可复用资产身份，并传递给后处理。
- [x] 增强型翻译的修复请求包含主翻译与高级校对流程，且与原任务角色选择一致。
- [x] 普通 LLM 翻译的修复不自动增加高级校对或其他模型角色。
- [x] 非 LLM 翻译的修复不静默发起 LLM 请求；只能执行可用的确定性处理、仅报告或明确降级。
- [x] 独立任务优先读取并验证过程目录资产，缺失时明确提示并接受显式备用资产。
- [x] 翻译方式选择和资产身份进入报告及任务状态，便于核对实际行为。

## Comments

## 双轴审查结果（2026-09-06，commit f3c8581 + 修复轮 477ea53）

**Spec 轴**：6 条验收逐项核对全部 PASS，无范围蔓延。两条非阻塞观察：
1. `non_llm` 快照优先于显式 profile——用户无法显式选降级用 LLM。裁决：符合 D15「不静默升级」的安全读法，「明确降级」仅在缺快照路径可用（显式 `profile` 参数 / 独立任务显式绑定工具角色方案）。
2. 显式备用资产测试只验证 discovery 登记，未断言备用快照实际驱动修复方式选择。裁决：同一 runner 代码路径已由 `test_standalone_task_rebuilds_snapshot_from_workspace_asset` 覆盖，风险低，保留。

**编码规范轴**：无硬违规。6 条坏味判断题，5 条已修（477ea53：flow_mode_label/role_label 单一来源、PostprocessTask.bind_translation_snapshot() 统一四处赋值块并补 home_interface 空值守卫、describe_role 死代码删除、store_profile_resolver 删未用 store 参数、CLI 手工构造改复用 cli_translation_snapshot），1 条裁决保留（flow_mode 裸字符串贯穿四模块——枚举化属轻量 Primitive Obsession，字符串值已是持久化与报告边界，枚举化收益不足，保留）。

**测试**：tests/test_postprocess/test_translation_snapshot.py 11 个测试全过；受影响套件 289 passed；全量非集成 1311 passed。Ruff/Pyright 全绿。修复轮中 CLI 快照契约 bug（@property vs 方法）先红后绿，已记 shoals。
