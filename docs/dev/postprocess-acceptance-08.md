# 字幕后处理性能治理：完整任务联合验收（票 08）

验收时间：2026-09-14。验收对象：01–07 全部落地后的 `master`（revision `0fb18dd`，工作区干净）。
对照基线：`postprocess-v1` / 验收矩阵 `postprocess-acceptance-v1`（见
[`postprocess-performance-baseline.md`](postprocess-performance-baseline.md)），
**门槛未做任何下调**；本次只按冻结矩阵逐项判定。

## 复跑命令（同机同工具链，`uv run --no-sync`，Python 3.12.13 / 20 CPU）

```bash
uv run --no-sync python -m scripts.postprocess_benchmark --output benchmark-output/postprocess-08-cold --repeats 5
uv run --no-sync python -m scripts.postprocess_benchmark --output benchmark-output/postprocess-08-warm --repeats 5 --case independent --cache-state warm --skip-probes
QT_QPA_PLATFORM=offscreen uv run --no-sync pytest -m 'not integration and not llm' -q
```

`benchmark-output/` 受 gitignore 保护，逐次 JSON/log/指纹全部保留在本地；两次运行
`source_fingerprints` 全程一致（脚本自校验），环境与 01 基线相同（openai 2.15.0 /
httpx 0.28.1 / diskcache 5.6.3 / PyQt5 5.15.11 / pytest 9.0.2）。

## 冻结矩阵逐项判定

### 并发改善（cold slow-main，额度 4，完整任务 median ≤ 1.801243725s）

| 场景 | median（min–max），秒 | 冻结门槛 | 判定 |
| --- | --- | --- | --- |
| slow-main / cold | 0.926296（0.908213–1.058174） | ≤ 1.801243725 | **通过**（余量 48.6%） |
| local-long / cold | 1.957388（1.883682–1.987076） | ≤ 2.522504990 | **通过**（本地无回退） |
| independent / cold | 0.573581（0.561969–0.617177） | — | 记录（无独立墙钟门槛） |
| mixed / cold | 0.577778（0.575091–0.585917） | — | 记录 |
| partial-failure / cold | 0.704027（0.675240–0.714913） | — | 记录 |
| independent / warm | 0.295649（0.282444–0.298691） | — | 记录（暖缓存分层，不混算） |

重叠证据（同批 5 次一致）：slow-main 四个主修复批 **peak overlap = 4**，全部主请求
在 0.42–0.58s 窗口内同时在途；主请求区间并集 0.436s vs 主修复请求 duration 串行和
1.640–1.673s（五次逐次 1.640/1.665/1.664/1.645/1.673s）——**请求确有重叠**。
01 基线同场景串行 median 2.401658s → 0.926296s。

噪声说明：slow-main 五次跨度 16.19%（> median 的 15%），但 01 的 15% 规则只约束
local-long 组有效性；local-long 本次跨度 5.3% 组有效。slow-main 最慢样本
1.058174s 仍低于门槛 41%，不构成改判或重采依据；全部原始样本保留在 artifact。

### 主请求数（independent/slow-main/mixed 无故障时主请求 ≤ 4）

三场景每轮均为 **4/4/4** 个逻辑主修复请求（40 主体 / 每批 10 上限），5 次采样一致，
每个必要输入段均有覆盖（independent 40、mixed 50 主输入段全覆盖）。

### 校对批量化（校对逻辑请求 ≤ 8；覆盖 40/40/50 输入段及全部一对多片段）

- independent / slow-main：**4** 次校对请求（10+10+10+10 主体），覆盖 40/40 主体、
  40 输入段；输出校订 `您好` 实际应用。
- mixed：**4** 次校对请求，覆盖 40 主体、50 输入段、**60 个片段**（含 10 个一对多
  拆分增加片段——每请求 14/14/16/16，不只检查第一片段）。
- partial-failure：4 次校对请求，49 个合法输入段、58 片段逐项验收；失败区域
  （初版段 0）业务重试耗尽后局部回退、resolved 49/50、unresolved=1，其余合法校订
  照常交付；主请求 8 逻辑 / 5 adapter 尝试 / 3 缓存命中（同任务内重试命中缓存，
  未当成 8 次真实尝试——与 01 口径一致）。

现状对比：01 基线同 fixture 校对 40 次 → 4 次（**-90%**，门槛 ≥80%）。

### 停止响应（各探针 ≤ 0.30s，5 次采样）

| 探针 | stop median（max），秒 | 冻结门槛 | 判定 | 附注 |
| --- | --- | --- | --- | --- |
| queue（gate=2：2 排队未发 + 2 真实在途） | 0.0621（0.0745） | ≤ 0.30 每请求 | **通过** | 全部先于受控释放完成（completed_without_release）；迟到结果无交付（before/after_cancel 均空） |
| network（SDK 重试） | 0.0607（0.0715） | ≤ 0.30 且无新 HTTP | **通过** | adapter=HTTP=1（SDK max_retries=0，无隐式重试乘积） |
| backoff（gateway 1.2s 退避中取消） | 0.0132（0.0227） | ≤ 0.30 | **通过** | 取消唤醒退避等待，不忽略 Retry-After 提前重发 |
| ui（完整任务在途停止） | 0.0121–0.0131 | ≤ 0.30 | **通过** | 见下 |

01 基线现状 median 分别为 queue 1.36s / network 5.09s / backoff 0.90s / ui 1.20s
——**四项全部从「不满足」翻转为满足**，无一项靠缩短受控延迟达标（服务计划释放
2s / Retry-After 1.2s / UI 请求 1.2s 全部保持原配方）。

### 完整任务停止（ui 探针，≤ 0.30s + 交付门禁）

五次全部：stop→终态信号 0.0121–0.0131s；`task_status=cancelled`；输入不变；
**无后处理字幕交付**；活动字幕回退初版；下游阻断；线程退出；终态信号
`cancelled`；无过程资产交付（qa_report/postprocess_state/speed_changes 全 False）。
hard cap 未超。等待刷新窗口（barrier→stop 毫秒级）未开即关，无等待事件是取消
正确性；「窗口内持续刷新 ≤0.50s」门槛由 `test_progress_diagnostics.py` 的
0.8s 受控延迟探针验证（07 已交付，本次回归通过）。

### 状态刷新与观测开销

- Qt 信号送达 max 0.048–0.617ms（01 冻结门槛 ≤100ms）；事件循环心跳 max 27.8–41.7ms
  （01 冻结门槛 ≤100ms 同量级满足；回归测试硬断言为 ≤600ms，`test_ui_baseline.py`）。
- `progress_max_gap_s` 0.361–0.376s：为「最后进度→终态」口径（含尾部静默），
  非 01 遗漏尾部静默的旧口径；等待期间刷新由 waiting 事件保证（07）。

### 质量与清理（硬门槛，不容忍）

全部场景、全部采样：原文非空白字符顺序不变；初版文件字节不变；QA 报告与过程
状态交付且状态一致；`local-long` 零模型调用；必要校对覆盖不减少（上述主体/片段
数）；partial-failure 局部回退、失败区域不记通过；取消/模块失败阻断下游且不交付。
三个传输探针（queue/network/backoff）`ok=true`，server 线程退出、handler 作答
排空、worker 全部 join；ui 探针以 outcome/gating 字段验收（hard cap 未超、
gating 七项全过）——**无清理失败**。

## 本票补做的集成审计（不汇总各票测试，专查跨票交叉）

| 交叉路径 | 证据 | 结论 |
| --- | --- | --- |
| 乱序完成 × 校对批量化 | `test_review_groups_follow_fixed_batch_order_not_completion_order`（乱序证据 + 相同分组/字幕/计数）+ `test_completion_order_does_not_change_results_under_concurrency` | 相同响应集合乱序完成产生相同字幕与报告 |
| 并发重叠 × 保护上限 | `test_profile_clamp_lowers_window`（闸 2 生效）+ slow-main 峰值 4 = 额度 | 实际在途不超配置/保护上限 |
| 校对绑定主候选版本 × 并发 | `repair.py` `_review_subject_entry` 捕获对象引用 + `test_round_snapshot_merge.py::test_review_reads_round_base_not_neighbor_merge` 邻批不读归并 | 校对视图以轮次快照为底 |
| 停止 × 归并/交付竞态 | `test_stop_accepted_means_no_new_requests_or_late_writeback` + `test_delivery_race_stop_before_commit_blocks_and_after_commit_completes` | 迟到结果无写回、终态竞争规则明确 |
| 入口一致性 | GUI `task_factory` 550 / CLI `_resolve_thread_num` / 编排 `process.py` 共享 owned gateway——三入口同一 `subtitle.thread_num` 旋钮 | 无入口静默回退网关默认 |
| 共享连接取消隔离 | `test_shared_gateway_cancellation_is_isolated_per_task` | 取消不关其他任务连接 |
| 重复启停泄漏 | `test_repeated_start_stop_leaks_no_threads_or_slots` | 无持续泄漏 |
| 原翻译方式保持 | `test_ordinary_llm_flow_never_issues_review_requests`（普通 LLM 不自动加校对）+ `test_standalone_report_only_and_analyze_do_not_call_gateway`（仅报告/分析不发请求）+ `test_monolingual_bilingual_independent_modes_and_one_to_many`（布局/模式矩阵） | 提速不改翻译方式，非 LLM/仅报告不静默发请求 |
| GUI/CLI 呈现一致 | `test_progress_frontends.py::test_gui_page_summary_and_expandable_detail_render` + `test_progress_frontends.py::test_cli_modes_render_from_the_same_event_facts` | 同一事件事实渲染，摘要/详情、安静/普通/详细同源 |

自动化审计未发现需修复的集成缺陷；**实机人工点验发现 1 个呈现层集成缺陷**
（下节），当场修复并补回归。

## 人工点验发现：并发等待呈现闪烁（已修复）

实机点验观测到详情页消息交替闪烁：

```
正在等待高级校对模型返回：已等待 12.0s（在途 3 / 排队 0）
正在等待主修复模型返回：已等待 230.3s（在途 1 / 排队 0）
（两条消息来回覆盖刷新）
```

**机制**：票 04/05 落地后并发成为常态——每个在途请求各起一个 `WaitRefresher`
（主修复 1 + 校对窗口 3，每 0.2s 各自发射 `waiting_event`）。GUI `_latest_event_fields`
是单槽「最新一条」平铺 dict：主修复消息（已等 230.3s）与校对消息（12.0s）
以 ~10 次/秒交替**整体覆盖**。这违反 spec「展开详情展示主修复**与**高级校对」
——两类等待应并列，不是互斥竞争同一显示位。CLI 普通模式同样覆盖（单行进度）。

**修复**（消费端，事件流本身不动——生产者按请求粒度发射是正确的观测口径）：
`diagnostics.merge_waiting_event` / `drop_waiting_role` 按角色分槽
（`waiting_slots[role]`），`render_detail` 先主修复后校对并列渲染；
GUI `postprocess_interface`、CLI 普通模式接分槽口径。槽清理时机：
`round` 事件清全部槽（新一轮 = 上一轮等待全结束）、`batch` 事件清主修复槽
（该批主修复请求已返回）；校对槽由角色自身等待刷新维持。

回归测试（TDD 先红后绿）：
`test_progress_diagnostics.py::test_concurrent_waiting_events_render_both_roles_side_by_side`
（并发双角色同屏，主修复刷新不覆盖校对槽）、
`::test_waiting_slot_clears_when_role_goes_silent`（角色返回后槽清理）；
`test_progress_frontends.py::test_gui_page_summary_and_expandable_detail_render`
更新为并发断言（两角色并列 + batch 后主修复槽清理、校对保留）。
CLI 单行渲染：`主修复等待中：已等待 230.5s（在途 1 / 排队 0）；…；高级校对等待中：已等待 12.0s（在途 3 / 排队 0）`。

**既有 flake 披露（非本修复引入）**：`test_progress_frontends.py::test_runner_owns_single_cancelled_terminal_event`
子进程用 `processEvents` 轮询 10s/15s 预算等信号，机器负载高时偶发超时
（干净 HEAD 复现 2/6 次；`AssertionError: []` 为等待循环超时，非产品取消路径
失败——取消传输级失败被正确处理）。stash 验证与本修复无关；修复方向
（事件驱动等待循环）已记 shoals，本票不冒称修复。

## 全量回归与 lint/类型检查

- `QT_QPA_PLATFORM=offscreen uv run --no-sync pytest -m 'not integration and not llm' -q`：
  **1540 passed、5 skipped、61 deselected、17 warnings**（218.00s）。
  01 时 1472 → 02–07 各票新增 66 + 本票并发分槽修复新增 2；无删除、无跳过增加
  （5 skipped 与 61 deselected 与 01 完全同口径）。
  首跑出现 2 个失败，复跑全套 1540 全绿，两个均为已记录的既有 flake
  （单跑均过、stash 验证与本修复无关）：
  `test_runner_owns_single_cancelled_terminal_event`（等待循环预算超时，
  干净 HEAD 2/6 复现，已记 shoal）、`test_ffmpeg_source_toggle_rebuilds_encoder_menu`
  （Qt offscreen 偶发，票 07 交付已记录同型）。
- `ruff check .`：全过。`pyright`：**0 errors / 20 warnings**（与 01 基线完全一致，
  无新增告警）。
- 后处理专项：`tests/test_postprocess` 249 + 2 = 251 passed；UI `test_postprocess_interface`
  16 passed；CLI postprocess/process gating 38 passed（含 2 skipped 既有口径）。

## 人工点验清单（供 09 复核；本次未执行 GUI 实机点验）

07 票遗留的 4 项人工点验（GUI 实机摘要行/展开详情、CLI `-v` 对比、停止按钮、
`llm_requests.jsonl` 配对）**未由本次自动化替代**——本票全部证据来自 offscreen Qt
与子进程 CLI，不冒充实机人工确认。09 交付票收口时应向用户出示该清单。

## 范围与遗留声明

- **未执行真实模型质量/性能验证**：无真实 provider 请求、未读取 DHH 正文、未改
  用户配置。真实小样本需另行确认样本规模与费用上限（07/08 均未获该授权）。
- **历史约 4.4 小时静默空档仍未归因**：故障注入（transport/backoff/ui 探针）证明
  各类等待现在可观察、可及时停止，但这不构成历史故障已解释或已修复的证明。
  「全部卡死已修复」的说法不成立——修复的是 01 冻结矩阵列出的串行/进度/停止缺口。
- 整片重跑不在默认授权内，本次未重跑 DHH 整片。
- `.scratch/` tracker、`benchmark-output/`、DHH 字幕正文与密钥均保持本机隔离，
  未提交、未外传。
