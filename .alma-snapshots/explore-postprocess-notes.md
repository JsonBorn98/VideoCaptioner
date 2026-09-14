# 探索笔记：字幕后处理性能治理（tickets 01–09 共享）

- 仓库：`C:\Users\runzhe.li\Software\VideoCaptioner`，分支 `feat/subtitle-postprocessing-performance`（HEAD `f13e8c4`）。
- 日期：2026-09-10。只读探索，未修改代码。
- 所有行号基于当前 HEAD；实现票开工前如已合入新提交，先 grep 函数名复核行号。
- spec: `.scratch/subtitle-postprocessing-performance/spec.md`；tickets: 同目录 `issues/01..09-*.md`。

---

## 1. 完整后处理任务入口 seam（`run_postprocess_task`）

### 1.1 定义与签名

`videocaptioner/core/postprocess/runner.py:277-294`：

```python
def run_postprocess_task(
    task: PostprocessTask,
    *,
    profile_store: PostprocessProfileStore | None = None,
    timing_windows: Iterable["TimingEvidenceWindow"] = (),
    timing_resolver: TimingResolver | None = None,
    gateway: Optional["LLMGateway"] = None,
    assets: PostprocessAssetAdapter | None = None,
    cancelled: Callable[[], bool] | None = None,
    progress: ProgressCallback | None = None,   # Callable[[int, str], None]，runner.py:41
) -> PostprocessResult:
```

关键参数语义：
- `task`：`videocaptioner/core/postprocess/models.py:39-127` `PostprocessTask`。字段含 `source_subtitle_path` / `initial_subtitle_path`（不可变初版） / `postprocessed_subtitle_path` / `active_subtitle_path` / `config_snapshot: PostprocessConfig | None` / `input_data: ASRData | None`（内存初版，优先于文件） / `translation_snapshot` / `explicit_assets` / `status`（pending/running/completed/fallback/skipped/cancelled/invalid_initial，models.py:80-86） / `warnings` / `task_id`（uuid hex）。`bind_translation_snapshot()` 在 models.py:109-120（统一各适配层赋值口径）。
- `gateway`：模型响应替换点 —— 测试传入 fake gateway（duck-type `complete(profile, request, *, cancelled=None) -> LLMResult`）。
- `cancelled`：取消触发点，调用方传 `Callable[[], bool]`；runner 在阶段边界轮询（runner.py:329-332, 489-490, 522-533），修复循环内每次请求前后轮询。
- `progress`：进度收集点，`(int, str)` 百分比+文案。
- `assets`：过程资产 adapter（默认 `FilesystemAssetStore`，runner.py:352）。

返回 `PostprocessResult`（models.py:128-152，frozen dataclass）：`task` / `input_data` / `output_data` / `report: QualityReport` / `layout` / `layout_confidence` / `warnings` / `succeeded` / `used_fallback` / `precise_timing_outcome` / `precise_timing_grades` / `continue_downstream`。

### 1.2 runner 内部阶段序列与现有进度刻度

`run_postprocess_task` 主体（runner.py:296-580）顺序：
1. `task.status = "running"`（:296）；`_load_and_classify`（:302，runner.py:44-102：input_data 克隆或 import_subtitle，layout 置信度）→ progress 10。
2. 初版快照 `original = clone_subtitle_data(input_data)`（:310）；`_validate_output`（runner.py:105-116，非法 → `invalid_initial` 阻断 :313-327）。
3. 取消检查（:329）；未启用 → skipped（:333-340）。
4. 配置冻结（:342-350）：`config_snapshot` 为 None 时从 `profile_store.resolve_config` 解析；然后 `PostprocessConfig(**config_payload(config))` 深拷贝冻结，仅保留 `utility_llm_profile` 对象引用注入。**注意：独立入口的 utility profile 是"工具角色"（被 `_stripped` 剥离 max_output_tokens/request_options，见 §3.2），而修复循环实际用的 main/review profile 来自 translation_snapshot，不经剥离（见 §2.2）。**
5. `adapter.discover(task)`（:354）→ progress 18；翻译快照重建（:368-374，从过程资产 `translation_snapshot` 文件）；`subtitle_fingerprint`（:351，`workspace.fingerprint_subtitle`）。
6. precise timing（:376-412，`degraded_*` 语义）。
7. analyze 模式 dry-run 提前返回（:414-466）。
8. `run_pre_stage`（:469，占位符清理）→ progress 25。
9. **修复执行**（:474-513）：`needs_repair = config.utility_llm_profile is not None and config.any_viewing_single_line()`；`with borrow_utility_gateway(gateway)... as runtime:`（runner.py:480）→ `run_post_stage`（:481-488，规范化/压缩/间隙/速度优化/审计）→ progress 45 → 若有单行限长侧：progress 55「正在修复观看问题」→ `execute_viewing_repair(working, config, report, layout, gateway=runtime, snapshot=snapshot, profile=config.utility_llm_profile, profile_resolver=repair_resolver, progress=progress, cancelled=cancelled)`（:494-505）。
10. `_validate_output(working)` → `save_canonical_srt`（:514-521；输出==输入路径时 raise，:518-520）。
11. `InterruptedError` → `_blocked_result(status="cancelled")`（:522-533）；其他异常 → `_module_failure_result(status="fallback")`（:534-545, :182-208）。两者都 `continue_downstream=False`，活动字幕回退初版。
12. 成功：`task.status="completed"`，写文件，`_publish_module_outputs`（:556-566，QA 报告/speed_changes/postprocess_state 进过程目录，失败只警告）→ progress 隐含完成（无 100 事件，线程层补）。

### 1.3 三个入口如何到达 seam

**A. GUI 独立后处理页**：`videocaptioner/ui/view/postprocess_interface.py:568-598`（`start()`）创建 `PostprocessThread(self.task)`（**不传 gateway → 线程内不注入 → runner 内 `borrow_utility_gateway` 自建默认 `LLMGateway()`，max_concurrency 默认 10**）；`:602` `cancel()` → `self._thread.stop()`。
线程：`videocaptioner/ui/thread/postprocess_thread.py:60-164`：
- `stop()` = `self.requestInterruption()`（:75-78）。
- `run()`（:87-164）：`set_task_context(task_id, file_name, stage="postprocess")`（:88-92）→ `run_postprocess_task(task, timing_resolver=_resolve_timing, gateway=self._injected_gateway, cancelled=self.isInterruptionRequested, progress=lambda v,m: ... emit)`（:101-111）。cancelled 直接绑 Qt 中断标志。
- 取消后抑制 finished（`_finish_if_cancelled` :80-85，:112-113, :141-142）；结果 fallback 时 emit `finished(video, initial_path)`（:146-159）。
- 完成后 `TaskFactory.save_stage_subtitle` 导出（:122-138）、sidecar（:139）、`publish_stage_summary(build_postprocess_stage_summary(result))`（:144）。

**B. GUI 编排（完整 workflow）**：`videocaptioner/ui/thread/subtitle_pipeline_thread.py:61-226`：
- :71-72 `thread_num = subtitle_config.thread_num`（默认 10，`core/entities.py:737`）→ `gateway = LLMGateway(max_concurrency=thread_num)`，转录/字幕优化/后处理共享这一个 gateway 实例。
- :76-84 冻结 postprocess_task；:143-159 绑定 `subtitle_task.output_path`、`input_data=subtitle_task.result_data`、`bind_translation_snapshot(subtitle_task.translation_execution_snapshot)`（:149-151）、`explicit_assets=collect_upstream_assets(subtitle_task)`（:154）、输出命名 `【后处理字幕】{workflow_base_name}.srt`（:156-159）。
- :160-165 `PostprocessThread(postprocess_task, gateway=gateway)`；:161-162 progress 缩放 `60 + value*0.15`。
- :175-187 下游门禁：`result is None / not continue_downstream / status=="cancelled"` → 阻断合成并 emit error；否则 active_data 取 `postprocess_task.result_data`，合成用 `active_subtitle_path`（:194-201）。
- :224-226 finally `owned_gateway.close()`。
- 另有批量任务入口：`videocaptioner/ui/thread/batch_process_thread.py:336-388` `_start_postprocess` —— 同样 `PostprocessThread(task)`（**不传 gateway，走默认**），快照绑定 :360，`batch_task.current_thread = thread` 供 `stop_task`（:500-522）调用 `thread.stop()`。

**C. CLI**：`videocaptioner/cli/commands/postprocess.py:100-270`（`run(args, config)`）：
- profile 解析（:125-143），utility profile 经 `resolve_cli_utility_profile`（:148-157）。
- 构造 `PostprocessTask`（:171-190），`task.input_data`（:191）、`bind_translation_snapshot(args.translation_execution_snapshot)`（:194）、`explicit_assets`（:197-201）。
- `run_postprocess_task(task, profile_store=store, timing_resolver=_timing_resolver, gateway=getattr(args, "gateway", None))`（:211-216）。**没有 cancelled 参数 —— CLI 无停止入口**。
- 进度：`quiet=False` 时 `output.ProgressLine("Postprocessing subtitles").start()`（:209）；**但 progress 回调没有接到 runner（未传 progress）**，spinner 只转不更新百分比。模式：`-q` 静默（print 路径，:268-269）、默认一行 stage summary（:262-265 `output.stage(...)`）、`-v` 额外 info（sidecar 等）。警告逐条 `output.warn`（:227-229）。
- 下游接缝字段：`args.continue_downstream` / `args.active_subtitle_path`（:243-244）。

**C2. CLI process 管线**：`videocaptioner/cli/commands/process.py:130-247`：
- :135 `owned_gateway = LLMGateway(max_concurrency=get(config, "subtitle.thread_num", 10))`，subtitle 阶段与后处理共享。
- :186-232 组装 `post_args`（Namespace）→ `postprocess_run(post_args, config)`；快照从 `sub_args.translation_execution_snapshot` 传入（:179-181 → :222-224）；explicit_assets :225-229。
- :233-241 门禁同 GUI。

**快照冻结源头**：`videocaptioner/cli/commands/subtitle.py:486-502` `args.translation_execution_snapshot = cli_translation_snapshot(...)`（实现在 `core/postprocess/translation.py:250-274`，经 `snapshot_from_subtitle_config` :202-221）。GUI 的快照由 `SubtitleThread` 产生（`ui/thread/subtitle_thread.py:230-283`，`run_enhanced_translation` 后 task 上有 `translation_execution_snapshot`）。

---

## 2. 修复执行流程现状

全部在 `videocaptioner/core/postprocess/repair.py`。入口 `execute_viewing_repair`（:712-737 签名，:738-1023 主体）。

### 2.1 轮次循环结构（当前是严格串行）

- 常量（:71-78）：`DEFAULT_BUSINESS_RETRIES = 4`、`MAX_FRAGMENTS_CAP = 8`、`MAX_TRANSPORT_FAILURE_ROUNDS = 2`、`MAX_ROUNDS = 16`。
- 问题身份：`ProblemIdentity = (初版段序, side, kind)`（:82-83，:775-776 `_identity`）。
- 主循环 `while summary.rounds < MAX_ROUNDS`（:798）：每轮
  1. 取消检查 `_raise_if_cancelled()`（:793-795）。
  2. `scan_viewing_lengths` → `problems_from_viewing` 适配（:801-809）；过滤 `closed_regions`。
  3. 无 open 问题 → break（:810-811）。
  4. `plan_repair_batches(data, open_problems, boundary_context_radius=radius)`（:812）—— **没有传 `token_budget`，也没有传 `max_subjects_per_batch`，走 planning.py:329-338 的无预算分支（每批最多 10 个合并主体，从不收缩）**。
  5. `plan.unplannable` → 封闭区域+警告（:813-822）。
  6. 业务重试耗尽检查（:826-844）：`attempts[identity] >= 1+4` → `_rollback(region, "业务修复重试耗尽")` + continue。
  7. progress 一次（:859-863）：`min(90, 55 + rounds*4)`，文案 `正在修复观看问题（第 N 轮，M 个未解决）` —— **轮内不发任何批级/请求级进度**。
  8. **批循环 `for batch in reversed(plan.batches)`（:864）**：降序应用保护拆分后索引（:856-858 注释）。每批：
     - 取消检查（:865）。
     - `payload = _build_payload(state, cfg, batch, segment_problems, feedback)`（:876，payload 形状 :284-335：limits + boundary_context + repair_subjects + feedback）。
     - `response = runtime.complete(repair_profile, LLMRequest(messages=..., max_output_tokens=repair_profile.max_output_tokens, metadata={"stage": "viewing_repair", "role": "utility"}), cancelled=cancelled)`（:878-887）—— **串行逐批 await，无 response_schema（结构化输出靠 prompt+json_repair）**。
     - 传输异常不消耗业务重试（:888-893，`round_transport_failed=True`）。
     - `_parse_response`（:572-609）显式 problem_id 绑定；未知绑定整批拒绝（协议级违规）。
     - 请求发生后计入 attempts/last_subject（:895-902）。
     - **主体循环 `for subject in reversed(batch.subjects)`（:908）**：`_validate_segment_candidate`（:619-709：output_index 连续、片段数上限、原文紧凑等价 `_compact` :108-110、单行换行、非空性守恒、绝对上限、CPS 不回退、`_allocate_times` 确定性时间分配 :227-270）→ 重复候选指纹回退（:936-941）→ `state.splice`（:957-959）→ 区域状态重复（无改善）回退（:962-973）。
     - **高级校对（逐主体，物理上一次请求一个主体）**：`if flow.mode == "main_review": _review_pass(...)`（:977-996）。`_review_pass`（:414-569）构造单主体 review payload（`review_subjects` 只含一个主体的 segments，:493-516），边界上下文读**当前 state**（:466-486 —— 即邻批刚完成的修改，这正是 ADR-0021 要冻结掉的），`runtime.complete(..., metadata={"stage": "viewing_repair_review"})`（:522-530）；失败/解析拒绝保留主翻译候选只警告（:531-538）；逐条修正验收（非空性/换行/上限，:549-568）。
  9. 轮末：`round_accepted`/`round_transport_failed` → `transport_streak` 计数，连续 2 轮传输失败停止循环（:999-1005）。
- 终态：`report.viewing_problems = scan_viewing_lengths(repaired, ...)` 以终态重扫描为准（:1007-1012）。

### 2.2 回退 / 验收 / 重试簿记

- `_WorkingState`（:158-217）：working 段列表 + `origin[i]`（初版段序追踪）；`splice`（:178-192）、`rollback(initial_indices)`（:194-208，区域不连续拒绝部分回退）、`region_state` 指纹（:210-217）。
- `_rollback`（:778-791）：恢复快照、封闭区域、撤销 accepted 计数、写 `RegionRollback`。
- 修复方式选择：`select_repair_flow`（:85-105）→ `resolve_repair_flow`（translation.py:305-361）：enhanced_llm 需要 main+review 都可解析否则 report_only；single_llm 只用 main；non_llm/缺快照 report_only（不静默升级）。`store_profile_resolver`（translation.py:364-405）按 profile_id+name+model 三元组验证后复用方案库。
- 请求的 profile：`flow.main_profile` / `flow.review_profile` 直接来自快照运行期对象或方案库 —— **携带真实 max_output_tokens / request_options，不经过工具角色 `_stripped`（llm/utility.py:42-50）**；`borrow_utility_gateway` 只管网关生命周期（utility.py:116-135：注入的 gateway 用完不关；缺失则自建并在退出时 close）。
- `main_guidance` / `review_guidance` = 快照的 `main_prompt` / `review_prompt`（:761-762），只作为附加指引注入 user 消息（:338-377）。

### 2.3 现有假网关注入方式（实现票可直接扩展）

- `tests/test_postprocess/test_repair_execution.py:42-65` `_ScriptedGateway`：`complete(profile, request, *, cancelled=None)` 按脚本列表回放（str=文本 / Exception=抛 / None=空文本），并把每次请求 payload（`<input>...</input>` 之间 JSON，`_payload_text` :60-64）记录到 `self.requests`。
- `tests/test_postprocess/test_end_to_end_delivery.py:52-63` 同款 `_ScriptedGateway` + `_RepairGateway`（:107-114：按请求载荷就地构造合法拆分响应 `_split_repairs`，用于自动修复场景）。这是 01 基准票「受控响应」的直接模板。
- 辅助构造器：`_data(*pairs)`（每段 4s）、`_config(**overrides)`、`_response(repairs)`、`_repairs_for(payload, chunk, translated)`（test_repair_execution.py:67-98）。
- 注意 fake 网关的 `cancelled` 参数会被收到但当前脚本不模拟延迟 —— 06 票需要让脚本支持 barrier/延迟/可释放事件。

---

## 3. 并发与网关现状

### 3.1 LLMGateway（ADR-0018 的落点）

`videocaptioner/core/llm/gateway.py:43-168`：
- 构造（:44-63）：`adapter_factory` / `sleep`（**可注入，测试用它断言退避**）/ `random_source` / `response_cache`（None → **模块级共享 `_shared_response_cache = GatewayResponseCache()`（:38-40），跨实例、跨任务共享磁盘缓存，缓存冷热分桶必须隔离它**）/ `max_concurrency: int = 10`。
- **每 profile 信号量**：`_resources(profile)`（:75-88）按 `profile_id` 惰性创建 adapter + `threading.BoundedSemaphore(profile.clamped_concurrency(self._max_concurrency))`，缓存于 `self._adapters` / `self._semaphores`。同 profile 复用同一闸（测试 test_gateway.py:164-180 钉住），不同 profile 独立闸（:305-315 `test_gateway_profiles_keep_independent_gates`）。
- **夹钳**：`LLMModelProfile.clamped_concurrency(task_concurrency)`（models.py:193-204）：`max_concurrency is None → 原样返回`；否则 `min(task, max_concurrency)`。`max_concurrency` 校验 1..50（models.py:175-179）。ADR-0018：thread_num 直通为任务并发，profile.max_concurrency 仅显式设置时作为保护夹钳。
- **complete**（:100-168）：
  - 缓存命中直接返回（:111-115），**不走信号量也不检查取消**（缓存命中即时；但"取消后缓存命中返回结果"是 06 票要复查的点）。
  - `for attempt in 1..max_attempts(默认4)`：尝试边界检查 `cancelled()`（:119-120）→ `with semaphore:`（**:122 —— BoundedSemaphore 阻塞获取不可取消，这是排队不可中断的根因**）→ 拿到槽后再查取消（:123-124）→ `begin_gateway_request` / `adapter.complete(request)` / `finish_gateway_request`（:125-134）→ 成功写缓存返回。
  - 重试分类（:138-153）：`is_output_limit_finish_reason` → attempt_limit=1（不重试）；`INVALID_RESPONSE` → 2；其余 retryable → max_attempts。**没有 tenacity —— 仓库声明依赖 tenacity（pyproject.toml:32）但代码中无 import（仅 VideoCaptioner.spec 打包清单引用），重试是手写循环**。
  - 退避（:154-157）：`min(30.0, 2**(attempt-1)) * (0.75+rand*0.5)`，`delay = max(backoff, retry_after_seconds or 0)`；`self._sleep(delay)` **不可中断**（:166）。Retry-After 被尊重但 sleep 无法唤醒。
  - `max_attempts=4` 由 complete 调用方缺省；修复循环未传 → 网络重试 4 次。

### 3.2 适配器层（timeout 与传输）

`videocaptioner/core/llm/adapters.py`：
- `DEFAULT_TIMEOUT_SECONDS = 120.0`（:43）、`TIMEOUT_SECONDS_PER_OUTPUT_TOKEN = 0.015`（:44）、`request_timeout_seconds(max_output_tokens, baseline)` = `baseline + cap*0.015`（:47-56）。**这是 ADR-0019 的超时缩放，已在 adapter 层实现**。
- `_effective_timeout(request)`（:428-433）：`request.timeout` 覆盖 > 按输出上限缩放。`LLMRequest.timeout: Optional[float]`（models.py:314-325，正数或 None）。
- `OpenAICompatibleAdapter`（:469-482）：`openai.OpenAI(base_url, api_key, timeout=timeout)` —— **OpenAI SDK 默认 `max_retries=2`（实测），gateway 的 attempt 循环之外还有 SDK 隐式重试，实际尝试次数是乘积**；per-request timeout 经 `_transport_options`（:555-558）。异常映射 :484-546（429/5xx retryable，Retry-After 提取 :490-496，OUTPUT_LIMIT/CONTEXT_LIMIT 分类）。
- `AnthropicMessagesAdapter`（:925-935）：`requests.Session`，`session.post(..., timeout=...)`（:848-849）—— urllib3 默认 `Retry(total=0)`（实测，无 SDK 重试）。Gemini 适配器 :931 起。
- **在途请求不可中断**：`adapter.complete(request)` 无取消参数，阻塞在网络 IO 上；gateway 只能在尝试边界检查 cancelled。06 票的"任务/请求级尽力中断通道"没有任何现成机制。

### 3.3 现有批并发执行能力（可复用候选）

`videocaptioner/core/translate/enhanced/batch_executor.py:16-113` `execute_batches(batches, *, concurrency, cancellation, on_complete=None)`：
- 滑动窗口 ThreadPoolExecutor；**:47-50 首批串行预热（`len(batches) > concurrency` 时先同步执行 batch 0）—— spec 明确"不把缺乏收益证据的首批串行等待设为必经步骤"，复用时需评估去掉**。
- 取消：`CancellationToken`（enhanced/models.py:352-358，`Event` + `raise_if_cancelled` 抛 `InterruptedError`）；协调线程每 `_CANCEL_POLL_SECONDS = 0.05s` 轮询 wait(FIRST_COMPLETED)（:74-79）；异常时 `future.cancel()` 全部 pending（:98-101）—— **只能取消未启动的 future，在途的杀不掉**。
- `on_complete` 在协调线程逐批回调（:41-44, :91-92）。
- 上游翻译 orchestrator 用它跑分析窗口（orchestrator.py:1053）、术语（:1258）、翻译批（:1512）、审计（:1671）。
- spec 决策 4：优先复用此构件与网关，但"复用前检查结果收集、取消传播、等待退出和回调行为；不复用上游翻译的 checkpoint 交付策略"。

### 3.4 入口配置传递现状（ticket 04 的证据）

- GUI 编排：pipeline 线程把 `subtitle_config.thread_num` 建成共享 gateway 注入后处理（subtitle_pipeline_thread.py:71-72, :160）——已贯通。
- GUI 独立页 & 批量任务：`PostprocessThread(task)` **不带 gateway**（postprocess_interface.py:578；batch_process_thread.py:363）→ runner 自建默认 max_concurrency=10 的 gateway —— 与用户 thread_num 无关，属"悄悄退回网关默认值"。
- CLI 独立：无 gateway 传参入口（postprocess.py:215 只透传 `args.gateway`，正常 CLI 调用为 None）→ 自建默认。
- CLI process：共享 `LLMGateway(max_concurrency=subtitle.thread_num)`（process.py:135, :218）。
- `PostprocessConfig` 本身**没有并发字段**（config.py 全文无 thread_num）；entities.py:737 `SubtitleConfig.thread_num: int = 10`、:747 `boundary_context_radius: int = 3`。
- `_MockLLMGateway` 约定：conftest patch 各消费者模块的 `LLMGateway` 符号（tests/conftest.py:236-262 monkeypatch 列表）——新并发路径若自建 gateway，需把 patch 点补进 conftest。

---

## 4. 超时与取消现状

### 4.1 请求 timeout

- 基线 120s 固定 + 输出上限缩放（adapters.py:43-56）；`max_output_tokens=None`（工具角色剥离后即 None，utility.py:42-50）→ 纯 120s。
- 修复循环传 `max_output_tokens=repair_profile.max_output_tokens`（repair.py:756 附近 `repair_profile = flow.main_profile`，:883）——快照角色可能带 cap（缩放生效）也可能 None（120s）。
- **没有端到端逻辑期限**：无"排队+传输+退避"总预算；`request.timeout` 字段存在（models.py:314）但无人对总期限计时。06 票需新增。

### 4.2 取消传播链（现有完整路径）

```
UI 停止按钮 postprocess_interface.py:602 cancel()
  → PostprocessThread.stop() → QThread.requestInterruption()   (postprocess_thread.py:75-78)
  → cancelled=self.isInterruptionRequested 传给 run_postprocess_task (:101-111)
  → runner: 阶段边界轮询（:329, :489-490 raise InterruptedError）+ 传给 execute_viewing_repair (:504-505)
  → repair: _raise_if_cancelled 每轮/每批（:793-795, :865）+ gateway.complete(cancelled=cancelled)（:886, :990）
  → gateway: 尝试边界 + 信号量后检查（gateway.py:119-124）
  → InterruptedError 冒出 → runner._blocked_result(status="cancelled", continue_downstream=False)（:522-533）
  → 线程层 _finish_if_cancelled 抑制 finished，emit cancelled 信号（postprocess_thread.py:80-85, :112-113）
  → pipeline 层 status=="cancelled" 阻断合成（subtitle_pipeline_thread.py:175-187）
```

### 4.3 不可取消的等待（已核实的清单）

1. **信号量排队**：`with semaphore:`（gateway.py:122）——`threading.BoundedSemaphore.acquire()` 无超时阻塞，取消不唤醒。
2. **退避 sleep**：`self._sleep(delay)`（gateway.py:166）——可注入 sleep 函数但运行时是 `time.sleep`，不可中断。
3. **在途 HTTP**：`adapter.complete(request)`（gateway.py:127）——无取消通道；OpenAI SDK/requests 阻塞至 timeout（最长 120s+缩放）。
4. **缓存命中路径**：`complete()` 开头查缓存并直接 return（gateway.py:111-115），不查 cancelled —— 取消后仍可能返回缓存结果（迟到结果语义 06 票要管）。
5. **修复循环候选应用段**：响应返回后 `_parse_response`→验收→`splice`→`_review_pass` 之间无取消复查（repair.py:894-996）；`_review_pass` 内部只有一次发送前检查（:519-520）。
6. **本地确定性阶段**（`run_post_stage` 速度优化/审计）不可取消，但纯 CPU、无长等待。
7. **workspace 资产 IO**：多处 `except InterruptedError`（workspace.py:280,374,454,565,579,604,632,740,764）——文件读取可被打断（测试 test_workspace_assets.py::test_interrupted_asset_read_is_cancellation_not_unreadable 钉住此语义）。
8. **下游竞态**：runner 返回 cancelled 后线程不再 emit finished（有守卫），但 gateway 缓存/在途响应在 `_blocked_result` 之后完成的写入路径无人复查任务版本（spec 决策 6 的"迟到写入"缺口）。

### 4.4 其他入口停止语义

- 批量任务：`BatchProcessThread.stop_task(file_path)`（batch_process_thread.py:500-522）→ `task.current_thread.stop()`（即 PostprocessThread.stop）；`stop_all`（:525-545）遍历 + `thread.wait(3000)`（**等待上限 3 秒后放弃等待，线程可能仍在跑**）。
- 编排 pipeline 线程本身无 stop 方法（SubtitlePipelineThread 无取消入口；只有 `has_error` 中断链）。
- CLI：无停止。
- `SubtitleThread.stop()`（subtitle_thread.py:534-545）是对比先例：`cancellation.cancel()` + `requestInterruption()` + 条件变量 notify —— **用 Condition 唤醒等待**是仓库内已有的"可取消等待"先例（term/audit 确认等待，:193-213）。

---

## 5. 容量规划现状

### 5.1 token 估计（上游复用，单一来源）

`videocaptioner/core/translate/enhanced/token_planner.py`：
- `estimate_tokens(text)`（:16-26）：ASCII 3 字符/token，非 ASCII 1 字符/token，保守。
- `estimate_cues_tokens(cues)`（:29-33）：对序列化 JSON payload 估计。
- `plan_translation_batches`（:121-…）：`batch_size` 只防呆，实际批大小由 `working_context_tokens - fixed_prompt_tokens - output_reserve_tokens` 收缩；收缩顺序 = 先减主体再减上下文（:182-202）；单主体仍超 → `TokenBudgetExceeded`。
- 输出预留：翻译/审计用输入比例式（ADR-0019：主体输入×输出比 ≈1.2/1.3 + JSON 开销），`output_reserve_estimator` 钩子（:135-160）。

### 5.2 后处理侧现状（"无预算规划"已被证实）

- `estimate_plan_tokens(asr_data, subjects, context)`（planning.py:237-278）：复用上游 `estimate_tokens`，对 repair payload JSON 估计 —— **只估输入，不含固定 prompt、不含输出预留**。
- `plan_repair_batches(asr_data, problems, *, boundary_context_radius=3, token_budget=None, max_subjects_per_batch=10)`（planning.py:281-367）：
  - `token_budget=None` → 无预算分支（:329-338）：直接按 `max_subjects_per_batch=10` 切批，fits 恒真。
  - 有预算 → 收缩顺序（:340-355）：先减主体数（保留完整上下文）→ 单主体逐步减 radius 到 0 → 仍放不下进 `plan.unplannable`（不截断）。
  - **执行路径从未传 token_budget**（repair.py:812），也没有从 profile 推导预算的代码 —— 03 票要补：预算应来自 `profile.work_context_tokens`（models.py:132，默认 65_536，最低 16_384）减 prompt/输出预留。
- `RepairBatch.estimated_tokens`（planning.py:214）已存在但仅记录输入估计。
- **结构化输出开销**：主修复/校对请求均无 `response_schema`（repair.py:879-887, :522-530 用裸 messages），结构由 prompt 约定 + `json_repair.loads` 兜底（:581-583, :389-391）；"结构化输出开销"目前只是 JSON 文本的输出 token，无 schema 强制通道。输出上限：`max_output_tokens=profile.max_output_tokens`（可能 None → 适配器不传 max_tokens，OpenAI 默认或 provider 默认）。

### 5.3 计数口径现状

- `RepairSummary`（repair.py:132-155）：rounds / requests / spliced_fragments / resolved_problem_count / rollbacks / unplannable_subjects / warnings / translation_method / flow_mode / main_role / review_role / boundary_context_radius / review_corrections。**没有：批次计数、批内主体数、字幕段计数、token 估计、耗时分布、尝试次数（attempt 级）**。07 票的摘要与 01 票的基准都要在此扩展或旁路记录。

---

## 6. 进度呈现现状

### 6.1 核心进度回调

- runner → repair 的 `progress(value, message)`（int 百分比 + 中文文案）。
- 全部刻度：10 已读取初版字幕 → 18 正在发现过程资产 → 25 正在规范化字幕 → 45 正在优化阅读速度 → 55 正在修复观看问题 → 修复循环内 `min(90, 55+rounds*4)`（每轮一次）。**轮内批次/请求/校对零事件**。
- GUI 线程转 Qt 信号：`PostprocessThread.progress = pyqtSignal(int, str)`（postprocess_thread.py:64），`progress.emit(value, self.tr(message))`，且 `isInterruptionRequested()` 时吞掉 emit（:106-110）。页面 `postprocess_interface.py:622-625 _on_progress` 更新 progress_bar + status_label（`_cancelling` 时只动 bar 不动文案）。
- 编排层二次缩放：pipeline `60 + value*0.15`（subtitle_pipeline_thread.py:161-163）；批量任务 full_process `60+value*0.15` / 独立 `80+value*0.2`（batch_process_thread.py:369-386）。

### 6.2 阶段摘要（ADR-0009 机制）

- `StageSummary`（core/utils/stage_summary.py:22-29）：`stage` + 有序 `counts: [(label, int)]` + `warnings` + `status`；`format_stage_summary`（:41-56）渲染单行 `postprocess · 120 段 · · · ⚠ 2 [status]`。
- 后处理专用构建：`core/postprocess/summary.py:15-70` `build_postprocess_stage_summary(result)`——段数、各 StageReport 变更数（`report._STAGE_LABELS`）、压缩失败、硬超速、校对修正、未解决问题、回退区域、precise_timing 徽章、`修复 {method}->{flow_mode_label}`、`活动字幕=后处理字幕/初版字幕`。**CLI 与 GUI 消费同一构建（单一事实源）**。
- GUI 发布：`ui/common/log_bridge.py` `publish_stage_summary(summary)`（:60-70）→ 模块级 `_summary_emitter`（RLock 守护）→ `LogRecordEmitter.stage_summary_emitted` Qt 信号跨线程投递。worker 不碰控件。
- CLI 渲染：`cli/output.py:32-35` `output.stage(summary)`；`ProgressLine`（:66-118）单行 spinner（`-q` 不建；非 TTY 不起线程）。
- `publish_stage_summary` 只在**任务完成时调用一次**（postprocess_thread.py:144；CLI postprocess.py:262-265）——没有过程中的阶段性摘要事件。
- 文件日志：`core/llm/request_logger.py` —— `llm_requests.jsonl`（LOG_PATH 下，:18），`begin_gateway_request`（:162-187：time/request_id/stage/role/profile/attempt/max_output_tokens，内容仅 opt-in）+ `finish_gateway_request`（:190-212：duration_ms/status/usage/error）+ `log_gateway_cache_hit`（:215-）。**stage/role 来自 `request.metadata`，repair 已传 `stage=viewing_repair/viewing_repair_review`；但无 task_id/file_name —— 历史 4.4h 空档无法任务级关联的根因**。任务上下文存在于 `core/llm/context.py`（`set_task_context(task_id, file_name, stage)` 模块级 + 全局锁，:21-59；线程池不复制 contextvars 的注释），gateway 写日志时**没有**读它 —— 07 票接入点就是这里。
- QualityReport（core/postprocess/report.py:102-130）：`viewing_problems` / `viewing_repair` / `unresolved_viewing_problems()`（:121-123）/ `stage(name)` 计数器。

---

## 7. 测试基础设施

### 7.1 后处理测试清单（tests/test_postprocess/，共 14 文件 4408 行）

| 文件 | 覆盖 |
|---|---|
| `test_end_to_end_delivery.py` (557) | **穿 `run_postprocess_task` 的端到端交付矩阵**：完整 workflow 语义、独立调用、显示策略、重试回退、门禁（`_assert_export`）、过程资产导出；`_RepairGateway` 自动响应 |
| `test_task_seam.py` (241) | seam 级快照/冻结配置/状态机（completed/fallback/cancelled/skipped）|
| `test_runner.py` (401) | runner：初版保护、precise_timing 三态、timing_windows 注入 |
| `test_repair_execution.py` (697) | 修复循环全语义：显式绑定、一对多、原文等价、重复候选、耗尽回退、传输失败、仅报告、进度 |
| `test_repair_planning.py` (400) | `plan_repair_batches`：主体合并、上下文、token 预算收缩、unplannable、max_subjects |
| `test_translation_snapshot.py` (546) | 快照冻结/持久化/解析/方式选择/身份漂移 |
| `test_translation_to_postprocess_handoff.py` (189) | 两阶段资产交接（翻译产物→独立后处理复用） |
| `test_workspace_assets.py` (301) | 过程目录、manifest、显式补充资产、InterruptedError 语义 |
| `test_persistence_and_export.py` (395) | 持久化输出与导出门禁 |
| `test_viewing_lengths.py` (228) | 扫描器 |
| `test_profiles.py` (91) / `test_task_factory.py` (165) / `test_legacy_config_removal.py` (196) | 方案库 / 任务工厂 / 旧配置删除 |

### 7.2 其他相关测试

- `tests/test_llm/test_gateway.py`（9 tests，0.22s）：退避 sleep 注入断言（:60-72 `sleeps == [1.0, 2.0, 4.0]`）、random 注入、INVALID_RESPONSE 单次重试、output-cap 不重试、信号量复用/夹钳/profile 独立闸（:164-330，`_SlowAdapter` + threading.Barrier 式 started/release 事件观察在途峰值 —— **04 票并发重叠断言的现成模板**）。
- `tests/test_thread/test_postprocess_thread.py`：线程层（进度转发、取消抑制 finished、fallback）；`tests/test_thread/conftest.py` 有 `qapp` fixture 与 `run_thread_with_timeout`。
- `tests/test_ui/test_postprocess_interface.py`：offscreen **子进程**惯例（`_run_qt_script`，subprocess + `QT_QPA_PLATFORM=offscreen` + 30s 超时，:1-15）；同款还有 test_video_synthesis_interface.py。
- `tests/conftest.py`（根）：**进程启动前设 `VIDEOCAPTIONER_APPDATA_PATH` 隔离 AppData**（:15-19）；`cache.disable_cache()`（:26）；`_guard_isolated_appdata` session fixture（:44-49）；`mock_llm_client` fixture patch 各模块 `LLMGateway` 符号（:236-262）。
- 本地 stub 服务器先例：**无**。仓库测试目前没有 loopback HTTP stub（只有 `tests/test_cli/test_config.py:480` 一个 localhost URL 字符串）；06 票的真实传输边界测试需要新建（可用 threading + `http.server` 或三方 stub；注意 pytest markers `integration`/`slow`/`llm` 已在 pyproject.toml:158-162 注册，`-m "not integration"` 是默认回归口径）。

### 7.3 运行方式（uv 工具链，实测）

```bash
uv sync --python 3.12                     # 环境准备（README.md:176）
uv run pytest -m "not integration"        # 默认回归（README.md:177）
uv run pytest tests/test_postprocess -x   # 后处理全部
uv run pytest tests/test_postprocess/test_repair_execution.py -k "rollback"
uv run pytest tests/test_llm/test_gateway.py -q
```

- pytest 配置：pyproject.toml:151-166（`testpaths=["tests"]`、`-v --strict-markers --tb=short --disable-warnings`、log_cli）。dev 依赖含 pytest>=8.0.0（pyproject.toml:110）。
- **实测耗时**：`tests/test_postprocess` 全部 165 tests ≈ 25.4s（27s wall，含收集）；`test_repair_execution.py` 28 tests 0.38s；`test_gateway.py` 9 tests 0.22s。整个 `tests/` 估计数分钟量级（Qt 子进程测试最贵）。基准票的新增测试应保持离线、确定性，避免把回归拖到分钟级以上。

---

## 8. 风险与坑（实现票易踩）

1. **同轮可变状态是并发的最大障碍**（ticket 02 的靶心）：`execute_viewing_repair` 的批循环直接读写共享 `_WorkingState`（repair.py:876 payload 从当前 state 取文本；:908-996 逐主体 splice/回退改 state），并共享 `attempts/last_subject/last_error/accepted/candidate_fps/state_fps/closed_regions` 多个 dict/set。**不能简单把 `for batch` 换成线程池**——需先冻结轮次快照、隔离工作单元、集中归并（ADR-0021）。降序应用（:864, :908）保护索引的前提是"串行 splice 立即生效"；并发归并必须换一套索引策略（origin 追踪 + 固定段序归并已具备雏形）。
2. **信号量排队与退避不可取消**：`threading.BoundedSemaphore` acquire（gateway.py:122）与 `time.sleep`（:166）无唤醒通道；且 `with semaphore` 是 with 块语义，改成可中断获取需小心 `cancel` 后槽位泄漏/重复释放。`Semaphore` 的 release 计数在异常路径由 with 保证，但自定义可取消等待若用 Condition 替换，注意 `_resources` 里闸与 adapter 的生命周期耦合（同 profile 复用同一闸对象，gateway.py:75-88）。
3. **OpenAI SDK 隐式重试 × gateway attempt 循环 = 尝试乘积**：`openai.OpenAI()` 默认 `max_retries=2`（实测），gateway 默认 `max_attempts=4` → 传输层最多 12 次 HTTP。06 票"禁止不可见嵌套重试乘积"必须显式设 SDK `max_retries=0` 或核算总预算；Anthropic/requests 路径 urllib3 `Retry(total=0)` 无此问题。构造点在 adapters.py:478-482（client 可注入 —— 测试可用它模拟连接停滞）。
4. **模块级共享响应缓存**：`_shared_response_cache`（gateway.py:38-40）跨任务/跨网关实例共享磁盘目录；冷/热基准与"暖缓存冒充并发"断言都必须隔离它（`GatewayResponseCache(cache=...)` 注入空 cache，或 `cache.disable_cache()` —— conftest 已全局禁用缓存，**注意 fake 网关路径根本不经过真实缓存**；真实网关基准票需显式控制冷热）。缓存命中路径不查 cancelled（gateway.py:111-115）。
5. **Qt 信号连接方式**：`thread.cancelled.connect(lambda: ..., Qt.DirectConnection)`（test_postprocess_thread.py:142）——跨线程信号默认 QueuedConnection，测试里断言要等事件循环；`PostprocessThread.run()` 直接调用（非 start）时信号也是直接投递。worker 内不得触碰控件（ADR-0009）；`publish_stage_summary` 的模块级 emitter 是进程单例（log_bridge.py:26-30），多任务并发时共用。
6. **全局单例/进程级状态清单**：`_shared_response_cache`（gateway.py:40）、`set_task_context` 模块级单值（context.py:21-34，**并发多任务互相覆盖 task 上下文 —— 07 票任务级日志关联不能依赖它，需把任务身份放进 request.metadata**）、`_summary_emitter`（log_bridge.py:26）、`cache.disable_cache()` 全局。批量任务同时跑多个 PostprocessThread 时这些都会互相影响。
7. **进度百分比是硬编码假刻度**：runner 的 10/18/25/45/55 与 repair 的 `55+rounds*4`（上限 90）都是静态值；07 票引入批次级事件时保持 `Callable[[int,str]]` 兼容（spec 决策 7），新增结构化事件须可选。注意 GUI 线程在取消后吞 progress（postprocess_thread.py:106-110），CLI process.py 的 postprocess 阶段**根本没把 progress 传进 runner**（postprocess.py:211-216 无 progress 参数）。
8. **Windows 平台**：仓库 win32 优先（`pywin32`/`CREATE_NO_WINDOW` 惯例，test_thread/conftest.py:60-63）；文件锁/路径编码（中文文件名 `【初版字幕】`/`【后处理字幕】` 是命名契约）；subprocess 测试注意 `creationflags`。本地 DHH 样本在 `E:\` 盘（spec 补充说明），公共测试不得依赖。
9. **fake 网关 duck-type 边界**：`_ScriptedGateway.complete(profile, request, *, cancelled=None)` 只模拟最小面；真实 `LLMGateway.complete` 还有 `max_attempts`/`use_cache` 关键字。若并发实现开始依赖 gateway 的新行为（如批完成回调、任务身份），fake 需要同步演进，否则端到端测试与真实传输测试漂移。反之给 `LLMGateway` 加参数时保持 keyword-only 兼容（`complete` 的调用方遍布 translate/split/optimize/dubbing/postprocess）。
10. **`borrow_utility_gateway` 嵌套语义**：runner.py:480 先借一次（needs_repair 时），repair.py:797 内层再借 —— 注入路径原样透传（不重复 close），自建路径由 runner 层 close。新增并发路径若在别处再建 gateway，务必沿用"注入不关、自建 finally close"约定（utility.py:116-135 docstring 明示）。dubbing/rewriter.py:110 与 compress.py:189 共用此 seam。
11. **`MAX_ROUNDS=16` 与重试口径**：轮数上界（repair.py:78, :1013-1014）与每问题 1+4 次请求（:826-844）是既有防震荡契约（spec 决策 5"新调度不得增加实际尝试机会"）；并发化后 attempts 记账从"请求发生即计数"（:895-902）改为并发工作单元时，注意请求已发出但响应未归并的窗口内，同一问题不得被再次规划进新请求。
12. **`plan.unplannable` 只有在传了 token_budget 后才可能出现**：当前执行路径永远为空（repair.py:812 无预算）；03 票接通预算后，现有的 `unplannable_subjects` 警告路径（repair.py:813-822）才第一次在真实任务中生效 —— 其"封闭区域"副作用要有测试。
13. **规格冻结的数值**：边界上下文半径默认 3（planning.py:35，与 entities.py:747 同源）；每批最多 10 主体（planning.py:287 默认参）；业务重试 4（repair.py:72）；传输失败轮 2（:76）；轮上界 16（:78）。这些是 spec 明示"第一版基准的内部起点"，改数值需基准依据（spec 决策 3）。
