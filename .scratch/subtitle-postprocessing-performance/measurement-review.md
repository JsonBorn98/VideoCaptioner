# Ticket 01 基准测量审查（只读检查结论）

审查对象：`scripts/postprocess_benchmark.py`（协调者更新版）、`scripts/postprocess_transport_probe.py`、`tests/test_postprocess/test_performance_baseline.py`、`tests/test_postprocess/test_transport_baseline.py`。
对照标准：`.scratch/subtitle-postprocessing-performance/issues/01-postprocess-performance-baseline.md` 与同目录 `spec.md`（重点：真实测量不编造、防作弊断言、跨进程报告稳定性、资产隔离）。

## 已确认修复（不再列为问题）

- 校对适配器返回「您好」，与未修复段的「你好」区分；`_measure_case` 逐源 segment 断言修复应用与边界上下文未被改写。
- `asset_discovery.asset_path("postprocess_state"/"qa_report")` 交付断言 + `delivered_state["unresolved_viewing_problems"]` 与报告一致断言。
- `queue_seconds` 改为 `None` 并注明由 transport queue probe 实测，不再伪造 0.0。
- `source_fingerprints`（videocaptioner/ + scripts/ 全部 .py）+ `dependency_lock_sha256`。
- 资产隔离本身未发现残留问题：每 repeat 独立 worker 进程、独立 `VIDEOCAPTIONER_APPDATA_PATH`、独立 disk cache 目录、独立过程目录（workspace 锚在 `task/` 内）；`mkdir(exist_ok=False)`、`open("x")`、transport probe `--output` 存在即拒，均拒绝覆盖；warm 预热任务与计量任务分目录（`warmup/` vs `task/`）。

## 具体可改问题（按影响排序）

1. **默认全量运行会在 ui probe 处崩溃，report.json 不落盘。**
   `main()` 默认（非 `--skip-probes`）probe 循环为 `("queue", "network", "backoff", "ui")`（`--probe-worker` choices 同），但 `scripts/postprocess_ui_probe.py` 不存在。全量运行在跑完全部 case×repeat 后于 ui probe ImportError，`subprocess.run(check=True)` 抛异常，`report.json` 永不写入，全部采样作废。ticket01 只要求排队/网络/退避三类探针；UI 属票 07。
   改法：从默认循环和 `--probe-worker` choices 移除 `"ui"`（或先落地该模块再启用）。

2. **headline 指标（`task_wall_seconds`/`process_cpu_seconds`）在 cProfile 开启下测量，带仪器偏差。**
   `_measure_case` 用 `profiler.enable()` 包住整个任务后才取 wall/cpu。profiler 开销与 Python 调用数成正比，而后续优化票（04/05）改的恰是调用数与并发结构——验收矩阵的「本地性能回退门槛」前后对比会被系统性偏置；local-long 7257 段下开销最大。
   改法：每 repeat 增加一次不挂 profiler 的 wall/cpu 计量（或奇偶 repeat 交替），cProfile 只用于 `local_stages` 归因；报告中两者分开并明确标注口径。

3. **任一 worker 崩溃/超时即丢弃全部已采报告。**
   `main()` 对每个 worker `subprocess.run(..., check=True, timeout=180)`：一个 worker 超时或断言失败会终止整个多案例运行，已完成的 reports 不落盘。ticket 要求区分「预期暴露的现状失败」与「基准基础设施自身失败」，当前一次局部失败即毁掉整轮。
   改法：逐 worker 捕获 `CalledProcessError`/`TimeoutExpired`，把失败（含对应 `.log` 路径）记入 artifact 的 `failures` 列表并继续其余 case，最后仍写 `report.json`。

4. **一对多拆分的第二输出片段逃过逐段防作弊断言。**
   `_measure_case` 用 `re.match(r"Item (\d+):", segment.text)` 决定是否断言 translated_text。mixed/partial-failure 中 `index % 12 == 0` 的段被拆成两段，第二段文本为 "The second sentence is here."，无前缀，regex 跳过 → 其译文完全未断言——恰是 ticket 关心的「多主体与一对多输出」侧。另 local-long 的「零模型请求」只在 pytest 断言，CLI 正式运行不自检。
   改法：fixture 把两句话都带 `Item {index}:` 前缀（如 `f"Item {index}: the first sentence. Item {index}: the second sentence is here."`）使两段均可断言；`_measure_case` 内为 local-long 补 `assert not gateway.calls`。

5. **`concurrency.profile_clamp: None` 是硬编码字面量而非计算值。**
   当前 profiles 未设 `max_concurrency`，`None` 恰好语义相符；一旦 profile 加显式夹钳，报告静默失真，且未记录 gateway 实际生效闸值。
   改法：写入 `profiles[role].clamped_concurrency(concurrency)`（每角色 effective gate）；`task_snapshot_field` 占位可保留。

## 主流程处置

- UI 模块现已交付并实跑，默认全矩阵通过，不适用早期不存在判断。
- 采纳仪器偏差发现：headline 不启用 cProfile；另一个隔离缓存/资产的 diagnostic task 收集 local_stages，断言结果指纹一致。旧 profiled headline 样本保留但不作最终验收依据，重新采集整套五次。
- 采纳一对多断言缺口：明确断言第二片段原文与校订译文；CLI local-long 也必须零请求。
- 不采纳『已采报告丢失』：每个成功 worker 已独立写 JSON，失败日志同样保留；整体退出非零且无成功 aggregate 正是基础设施失败，不会丢单项证据。文档补明。
- profile_clamp=None 是此版本固定配方的真实值，未提供可调profile；后续若加保护上限场景需报告各角色effective值，本轮不扩面。

## 观察项（不占 5 席）

- `max_inflight` 在任务路径恒为 1（修复循环串行），并发证据仅来自 transport probe——已有 `queue_note` 如实标注，可接受。
- `cache_hit` 以 `result.duration_ms is None` 判定，依赖 gateway 实现（真实尝试必为 int、缓存命中为 None）；当前正确但属脆弱耦合，后续票触碰 gateway 时注意。
- transport probe 侧（queue/network/backoff）未发现新问题：loopback-only、`use_cache=False` 防缓存冒充传输观测、有界清理（`HOLD_RELEASE_CAP`/join 超时）、拒绝覆盖输出，与 ticket 的探针要求一致。
