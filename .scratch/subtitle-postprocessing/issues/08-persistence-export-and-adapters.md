Autopilot: true
Claimed-at: 2026-09-06T08:24:24.510Z
# 08: 整合过程持久化、成功后导出和 workflow 适配器

**What to build:** 让 CLI、Qt workflow 和独立 GUI 共享核心任务入口和统一结果状态。过程资产在模块成功前只留在专用目录；整个模块成功后才可按 manifest 导出，局部回退结果仍作为活动字幕交给下游。

**Blocked by:** 03, 05, 07

**Status:** resolved

- [ ] CLI、Qt workflow 和独立 GUI 装配同一核心任务入口，不复制修复、回退或验收逻辑。
- [ ] 完整 workflow 在后处理成功或局部回退后获得正确活动字幕，并按状态决定是否继续下游。
- [ ] 过程报告、检查点、上下文和翻译资产写入 `videocaptioner-workspace`，不散落到普通输出目录。
- [ ] 模块运行中、取消、模块级失败或未完成时不提供过程资产导出。
- [ ] 整个模块成功完成后可以按 manifest 选择并复制过程资产，保留稳定文件名并可复制 manifest。
- [ ] 导出失败只报告导出失败，不改变已完成的核心后处理结果。
- [ ] 用户界面和 CLI 展示未解决问题、回退警告、报告位置和活动字幕状态。

## 双轴审查结果（2026-09-06，commit 01bbd84 → bb07f02/9224016/71f5e8c）

**编码规范轴**：5 条发现，4 条已修（9224016：explicit-assets 收集循环抽 `collect_upstream_assets`、timing sidecar 守卫抽 `write_timing_sidecar_if_applied`、`args.postprocess_result` 删除、三参数泥团捆成 `PostprocessDeliveryContext`），1 条文档性发现已修（71f5e8c：设计记录 + ADR-0020 入库，D 编号引用不再悬空）。

**Spec 轴**：7 条验收逐项核对，6 条无误。1 条部分实现已修：

1. **验收 7「报告位置」**——CLI 原先仅 `--verbose` 显示、Qt 只 `logger.info`。裁决记录：QtLogHandler 在 INFO 级转发 GUI 日志面板（验证过 log_bridge.py:64），Qt 侧 `logger.info` 是真实用户可见通道，保留；CLI 侧改为任何非 quiet 运行都展示位置行（9224016）。

低优先裁决记录：对齐 sidecar 仍写字幕输出旁（非过程报告，代码注释已声明边界）；process.py 门控已从 `ret == EXIT.SUCCESS` 改为接缝字段 `continue_downstream`（9224016）；checkpoint 收集补测试（test_persistence_and_export.py）。

**测试**：tests/test_postprocess/ 16+10+... 全过；全量非集成 1335 passed；UI 组 90 passed（2 个偶发 Qt 子进程崩溃按 shoal 协议隔离重跑通过）。Ruff/Pyright 全绿。
