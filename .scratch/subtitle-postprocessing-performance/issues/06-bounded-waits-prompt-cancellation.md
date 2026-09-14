Autopilot: true
Claimed-at: 2026-09-14T04:58:42.436Z
# 06 - 有界等待与及时主动停止

**构建什么.** 让用户在后处理排队、等待模型或退避时点击停止，任务可及时结束而不必等模型自然返回；请求等待和重试均有明确期限，迟到响应不写回、不交付部分成果、不触发下游，取消一个任务不影响其他任务的共享连接。

依据P05/P09及本 feature 规格的请求期限与后处理主动停止契约。本票仅依赖01，须能在现有串行修复入口独立成立，并提供兼容的任务/请求级取消能力供并发路径使用；与04/05汇合后的集成在07/08再次验证，不引入隐藏的实施前置依赖。

检查真实适配器、HTTP/SDK、网关、信号量排队和退避的等待/尝试边界。输出规模可合理延长请求窗口，但逻辑请求总期限覆盖排队、网络和退避；Retry-After超过剩余期限时明确失败而非提前重发或无限sleep。停止必须可唤醒等待，不能只在sleep前后检查，不能通过关闭共享client或终止应用实现。

主测试从完整任务入口观察结果/报告/交付与下游门禁，补受控本地服务器验证真实在途取消、SDK重试与资源清理。遵循01停止响应门槛，不发真实provider请求，不新增恢复协议；整任务不设赶时限的低质量交付策略。

**阻塞于.** 01

**状态.** resolved

- [x] 排队获取并发槽、连接/读取等待、在途响应和退避都支持任务级取消；受控服务尚未自然结束时本地停止已完成，并满足01对应响应门槛。
- [x] 逻辑请求具有有限的排队+传输+退避总预算，记录分项耗时；采用真实角色参数和输出规模，不把固定120秒或分阶段HTTP timeout冒充端到端期限。
- [x] 覆盖429、长Retry-After、服务端错误、慢分段响应和SDK隐式重试；不得提前违反Retry-After重发，也不得让多层重试逃出总预算。
- [x] 停止被接受后不发新请求或重试；主修复/校对响应返回、候选归并及交付前复查任务取消/版本，覆盖最后一批、最后一次校对及完成竞态。
- [x] 停止先于交付提交被接受时，不更新后处理结果工作稿、不交付部分后处理字幕、不导出过程资产交付副本、不发正常完成信号、不启动下游；已完成任务不被伪装成取消成功。
- [x] 取消仅作用于当前任务，另一个共享网关任务仍可正常完成；重复启动/停止后线程/任务、连接、并发槽及回调无持续泄漏或后台无限工作。
- [x] 传输重试与业务修复重试保持分离，正常初始提交加最多4次业务修复重试不变；局部失败、修复耗尽回退、模块级失败和停止的结果/下游语义不混淆。
- [x] 完整任务及本地真实传输边界回归通过，既有翻译调用者兼容；UI/CLI停止入口有端到端覆盖，不依赖假网关瞬间取消证明真实网络能力。
- [x] 交付说明本地停止不保证服务端撤销或停止计费，未新增断点恢复；保留初版和原始资产，未请求真实模型。

## Comments

2026-09-14 交付：commit `9386a8d`（实现）+ `42f5d3a`（审查修复），已提交本地 `master`，未 push。双轴审查 7 项实质发现（规范轴 4 + spec 轴 3）当场修完并过一轮修复回审，三连全绿（ruff clean / pyright 0 errors / 全量离线 1526 passed / 5 skipped / 61 deselected）。

- 网关（`core/llm/gateway.py`）：`_CancellableAcquire` 非阻塞 acquire + 30ms 轮询（取消/剩余预算逐轮检查）；`_cancellable_sleep` sleeper 线程跑满 `self._sleep` 注入通道、调用线程 30ms 轮询取消；`deadline_seconds` 总预算（显式覆盖，缺省按尝试结构推导上界：尝试数 × 输出缩放窗口 + 退避 ×1.25 抖动上界，排队由 `_GATE_QUEUE_RESERVE_SECONDS` 30s 附加窗口单独有界）；每次尝试的网络窗口钳到 `min(request window, remaining)`；Retry-After/退避超剩余预算明确 raise 不重发不无限 sleep；排队分项耗时 >50ms 记 INFO 行（实测验证 0.516s 排队被记录）。
- adapter（`core/llm/adapters.py`）：`OpenAICompatibleAdapter.complete(cancelled=)` 每请求独享 `openai.OpenAI` client（`max_retries=0`——SDK 隐式重试乘积消除，票 01 network 探针实测 1 adapter=3 HTTP → 1=1）+ watchdog 取消时只关该请求的 client（在途读 ~1ms 解除，独立验证）；有界 join 5s 兜底，超时即抛合成传输错误不等 worker 收尾。Anthropic/Gemini 接受 kwarg 但 requests.Session 无线程安全 close——在途取消=超时窗口兜底（已写入基线验证范围声明）。
- 修复层（`repair.py`）：`review_stop_gate` 停止被接受后窗口线程不发新校对请求；取消路径 drain futures（应用前重确认取消，迟到校订不写回）+ executor 有界关闭。
- runner（`runner.py`）：`save_canonical_srt` 前交付复查——停止先于提交 → cancelled 阻断（不写结果稿/不交付/活动字幕回退初版/不触发下游）；提交后才置的停止保持 completed（不伪装取消成功）。
- 实测证据：三传输探针 queue 0.072s / network 0.057s（SDK 乘积消除）/ backoff 0.025s 全部 ≤0.30s 冻结门槛；UI 探针 stop→终态 0.011s + 全部交付门禁（无字幕/过程资产交付、初版不变、下游阻断、终态 cancelled）；基准门槛 slow-main median 0.866s ≤1.8012s、local-long 1.769s ≤2.5225s、mixed 主 4≤4 校对 4≤8 覆盖 50/50 unresolved 0 输出 130 段。
- 新增 12 测试（`tests/test_postprocess/test_bounded_waits_cancellation.py`）：排队/在途/退避取消、Retry-After 超预算、总预算钳制、共享网关任务隔离、停止后不发新请求、交付竞态两场景、重复启停无泄漏、传输/业务重试分离、5xx 网关接管、慢分段中途取消。`_HoldService` 增 server_error/slow_chunks 模式补齐票 01 遗留的 5xx/慢分段覆盖。
- 探针适配：`_baseline_gaps` 重写为票 06 后口径（只报真实残留缺口：隐式重试乘积/取消后等满退避/迟到交付）；UI 探针 gap 分类改「取消→终态 >0.30s」；`test_transport_baseline.py`/`test_ui_baseline.py` 现状断言翻转为门槛断言（prompt true / ≤0.30s / http_attempts 1）。
- 遗留声明：Anthropic/Gemini 传输在途取消=超时窗口兜底（尽力语义）；CLI 独立后处理停止边界=进程级 KeyboardInterrupt 130；本地停止不保证服务端撤销/停止计费；未新增断点恢复；历史 4.4h 静默未归因。未发真实 provider 请求；`.scratch/` 保持本机隔离。
