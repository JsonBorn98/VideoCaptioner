Autopilot: true
Claimed-at: 2026-09-14T06:30:15.395Z
# 07 - 任务进度摘要与展开详情

**构建什么.** 让独立与编排后处理不再整轮只显示一次文案：用户能从摘要看到阶段、轮次、批次完成、问题验收与耗时，展开查看实际并发、在途请求、排队、期限、重试及限流。长模型调用期间等待时长持续更新，停止可操作，成功/警告/失败/停止都有明确终态。

依据P07与ADR-0009，沿用进度回调、Qt信号及阶段摘要，必要时增加后处理范围内兼容的结构化事件，避免全应用重量级协议和从worker操作控件。CLI保持普通/详细/安静模式，GUI与诊断日志共享事实，不能靠日志级别副作用生成界面状态。

本票是05并发校对与06有界停止的集成点：覆盖正在排队或运行的多角色请求，取消后迟到事件不能让界面回到成功。任务、轮次、批次、主体/问题、角色及尝试在内容日志关闭时仍可关联。请求开始就写元数据，结束/重试/缓存/取消分别记录；不记录密钥或完整内容。

使用01冻结的界面刷新与观测开销门槛；故障注入验证静默等待能区分，但不宣称已经解释历史4.4小时空档。

**阻塞于.** 05, 06

**状态.** resolved

- [x] GUI默认摘要显示当前阶段/轮次、批次进度、已解决/待验收/未解决状态和运行耗时；详情可展开显示主修复与校对、有效并发、在途/排队、等待期限和重试原因。
- [x] 模型返回或批次完成不直接计为问题解决，只有归并验收通过才更新对应状态；重规划、拆分、回退和下一轮分母变化有明确口径，不生成虚假单调百分比。
- [x] 规划、本地处理、请求等待、校对、归并验收、保存及终态都有事件；长请求期间等待时长更新且界面响应停止，符合01刷新门槛，不依赖token streaming。
- [x] 内容日志关闭时，请求开始与后续完成/缓存/重试/取消可按任务、轮次、批次和尝试关联；只有开始无终态的异常退出记录保持未闭合，不伪造成功或确定失败。
- [x] 文件日志区分本地阶段耗时、请求duration累计、排队、退避和任务墙钟，跨重启及静默跨度不混算；不输出密钥、认证信息、完整prompt/响应或推理正文。
- [x] GUI经Qt信号呈现，CLI普通/详细/安静模式兼容，现有简单进度回调继续可用；正常、带警告、失败和停止的终态一致且无重复完成。
- [x] 集成05与06，覆盖多profile并发、校对在途、排队/退避停止、最后响应竞态；取消后进度不复活、不更新工作稿、不触发下游，共享其他任务不受影响。
- [x] 完成offscreen界面、CLI和完整任务回归，观测开销满足01门槛；提交需人工点验清单，明确历史静默空档仍未归因（除非已取得直接证据）。

## Comments

2026-09-14 交付：commit `96cfc5e`（实现）+ `0fb18dd`（审查修复），已提交本地 `master`，未 push。双轴审查 13 项发现（规范轴 7 + spec 轴 6）当场修完并过一轮修复回审，三连全绿（ruff 全过 / pyright 0 errors / 20 既有 warnings 与 01 基线一致 / 全量离线 1538 passed / 5 skipped / 61 deselected）。

- 事件通道（`core/postprocess/diagnostics.py` 新增）：round（分母逐轮显式）/ batch（归并验收通过计数，返回≠解决）/ waiting（0.2s 节流等待刷新，01 冻结 0.50s 门槛 2.5 倍余量）/ retry（传输/业务失败原因）/ terminal（completed/cancelled/failed/report_only）/ stage 六类事件，经可选 `on_event` 回调穿透 `run_postprocess_task`→`execute_viewing_repair`；既有 `progress` 百分比回调并行保留（45% 步恢复），不建全应用重量级协议（ADR-0009）。
- GUI：`PostprocessThread.progress_event` Qt 信号（JSON 字符串载荷，queued 送达；worker 不触控件）；页面常驻摘要行 + 展开/收起详情（`_toggle_detail`）渲染窗口/在途/排队/等待时长/等待期限/已通过/未解决；取消后 `_cancelling` 抑制 waiting/round/batch/retry（进度不复活）。
- CLI：安静全静默、普通模式仅 waiting 刷进度行、详细模式逐行渲染全部事件；三模式同一事件事实。
- 请求日志：`status="started"` 行先落盘（异常退出只有开始无终态=未闭合，不伪造成功）；task_id/round/batch 关联字段进 `_base_entry`（内容日志关闭仍可关联；不含 prompt/响应/密钥）。审查修复后 scope 收窄：仅携带 task_id 的后处理请求多落 started 行，其他调用方保持每次尝试一行。
- 终态去重（审查修复）：取消终态由 runner 统一恰发一次（修复层只上抛）；completed 带 wall_seconds+warnings；failed 带可读 counts；早期取消（修复前/discover）也发终态。
- 修复层细节：`review_index` 协调线程 submit 前分配（默认参数捕获，消除 worker 竞态）；WaitRefresher 收编 `_start_refresher`（on_event=None 零观测开销）；`_request_window_seconds` 复用网关 `request_deadline_hint`（单一缩放口径）。
- 测试：新增 `test_progress_diagnostics.py` 9 测试（口径/分母跨轮/0.8s 受控延迟等待刷新 ≤0.50s/取消流终止/metadata 关联/started-终态配对/渲染助手）+ `test_progress_frontends.py` 4 测试（GUI 信号送达/页面详情/CLI 三模式/runner 恰一次取消终态）；logger/cache 测试更新 started 行口径；UI 探针补 waiting 事件口径（立即停止场景窗口未开即关=取消正确性，非缺口）。
- 回归：后处理+llm+thread+cli 全绿；全量 1538 passed。已知本机 Qt offscreen 偶发崩溃（`test_postprocess_drop_hint_and_settings_title_are_theme_aware`，干净 master 同样复现、单跑/复跑均过）非本票回归。
- 验证范围声明：未发真实 provider 请求；观测开销=0.2s 节流事件（满足 01「不逐 token 刷屏」）；**历史 4.4h 静默空档仍未归因**（故障注入只验证静默等待可区分，不宣称已解释）；立即停止场景无等待刷新是取消正确性，刷新门槛由 0.8s 探针验证。
- 人工点验清单（验收 8）：① GUI 运行一次后处理观察摘要行与「展开详情」切换（在途/排队/等待时长刷新）；② CLI `-v` 逐行事件 vs 默认单行进度 vs `-q` 静默；③ 停止按钮：取消后进度文案不再变化、详情区不复活；④ `AppData/logs/llm_requests.jsonl` 出现 started/success 成对行且带 task_id/round/batch。`.scratch/` 保持本机隔离。
