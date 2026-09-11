# 后处理离线性能基准 v1

基线标识：`postprocess-v1`。本票只测量现状，不实现并发、批量校对或主动网络取消，不更改用户模型方案。

## 复跑

在仓库根目录、已有项目开发环境中运行：

```bash
uv run --no-sync python -m scripts.postprocess_benchmark --output benchmark-output/postprocess-v1-cold --repeats 5
uv run --no-sync python -m scripts.postprocess_benchmark --output benchmark-output/postprocess-v1-warm --repeats 5 --case independent --cache-state warm --skip-probes
uv run --no-sync pytest tests/test_postprocess/test_performance_baseline.py tests/test_postprocess/test_transport_baseline.py tests/test_postprocess/test_ui_baseline.py
```

`--output` 必须是不存在的目录；重复执行请换新名字，不覆盖旧基线。公共入口不需要 API key，不读取 DHH 或用户配置，AppData、缓存、输入与过程目录全部隔离。每个采样独立进程运行；任务 180 秒、探针 60 秒 watchdog 只约束测试基础设施，**不是产品的整片截止期限**。异常/断言失败以非零码退出，不能当成测量成功；已完成的逐项 JSON 和失败日志保留，不生成貌似成功的 aggregate。

`--skip-probes` 只适合任务诊断，不能宣称完成停止/UI 验收。`--concurrency 1` 与默认的 `4` 可做同机对照；记录的是显式注入网关的额度，当前 `PostprocessTask`/翻译快照尚无对应任务并发字段，不能把它说成已验证 GUI/CLI 配置交接。

## 场景与测量口径

| 场景 | 输入 | 受控服务 |
| --- | --- | --- |
| local-long | 7,257 段合成双语，完整导入、速度处理、观看扫描、QA、保存 | 不调用模型 |
| independent | 120 段，40 个相互独立的问题主体，中间有安全上下文 | 主修复 50ms、校对 5ms |
| slow-main | 同 independent | 主修复 400ms、校对 5ms |
| mixed | 120 段，40 主体/50 问题输入段，含相邻合并及 10 个一对多拆分 | 主修复 50ms、校对 5ms |
| partial-failure | mixed 的第一个原文响应非法，其他合法项仍需交付 | 不放松原文校验，局部回退 |
| queue/network/backoff | 真实网关、OpenAI adapter/SDK 到 loopback 服务 | 阻塞并发槽、挂起响应、429/Retry-After |
| ui | offscreen Qt 的实际 worker 信号/事件循环 | 请求屏障、停止及有界释放 |

任务通过 `run_postprocess_task`，不 mock 规划、修复、复校、归并和交付。受控模型返回确定的合法短译文；校对必须把 `你好` 改为 `您好`，并在输出断言实际应用，不能用漏校对换取少请求。mixed 必须输出 130 段；partial-failure 保留局部失败且输出 129 段，合法区域继续交付。所有任务都验证原文非空白字符顺序不变、初版文件字节不变、QA/过程状态/活动字幕和下游门禁。合成短译文只用来验证协议与质量门禁通路，不是语义质量模型。

- `task_wall_seconds` 是一次完整入口调用墙钟，不含输入生成、进程启动、cache warm-up 与清理；**不启用 cProfile**。每次随后另跑一个独立缓存/资产的 diagnostic task，主采样与诊断输出指纹必须一致。
- `process_cpu_seconds` 是任务期间进程 CPU；不是每阶段 CPU。`diagnostic_profile.local_stages` 为第二次任务的 cProfile self/inclusive 时间，嵌套 inclusive **不得相加**，诊断开销不能混入 headline 墙钟。
- `local_wall_excluding_gateway` 从墙钟扣除逻辑请求区间的并集；不是把并发请求 duration 相加再相减，也不等于纯 CPU。
- `logical` 是业务网关提交数，`attempts` 是真实网关调用受控 adapter 的次数。传输探针另报 SDK 实际 HTTP 次数，不把它们混为一层。
- 主/校对逐请求记录主体、问题和输入段计数、review fragment 数、输入/输出 token 估计、请求/响应指纹、角色参数、timeout 与在途数。token 为仓库保守估计，不是 provider tokenizer 或计费量。容量报告只是观察，当前执行没有预算钳制。
- 任务信号量排队没有独立埋点时报告 `null`，不捏造为 0；实际排队/退避见传输探针。完整任务的阶段回调静默不等于 GUI event loop 阻塞。
- cold 每个样本使用空的独立磁盘 cache；warm 先跑单独预热任务再测同输入。预热不算入墙钟。暖缓存请求仍走验收，命中次数与 adapter 尝试分别报告，不能作为模型吞吐证据。
- `report.json` 保存代码 revision、工作区状态、Python 源码指纹、uv.lock 指纹、执行环境与逐次原始数据。不同 restart、cache 状态与配置不合并统计。

## 冻结验收矩阵

验收版本 `postprocess-acceptance-v1`，在优化票开始前冻结。每个场景独立重复 **5 次**，保留全部原始样本、median 与 min–max，不删除慢样本。后续只能对照，不得根据优化结果降低门槛。

### 数值门槛（适用受控离线条件，不是整片 SLA）

| 维度 | 冻结目标 | 依据与适用条件 |
| --- | --- | --- |
| 并发改善 | cold slow-main，额度 4，相同 fixture/角色/响应，完整任务 median ≤ 本基线 median × **0.75**；且实际有至少 2 个请求重叠、peak ≤ 4 | 4 个独立主批各 400ms，串行模拟主负载 1.6s；25% 墙钟改善保留本地成本/调度余量，不要求理想 4 倍 |
| 主请求数 | independent/slow-main/mixed 无故障时主请求 **≤4**，每个必要输入段均有覆盖 | 当前 40 主体按最多 10/批为 4；不得以遗漏问题减少调用 |
| 校对批量化 | independent/slow-main/mixed 校对逻辑请求 **≤8**；覆盖 40/40/50 个修复输入段以及全部一对多片段，输出校订仍是 `您好` | 同 65,536 context / 8,192 output cap 的合成短文，多主体组批至少平均 5 主体/次，较现状 40 次减少 ≥80%；真实容量不足可小批但本 fixture 不应触发 |
| 停止—排队 | queue 中未发 HTTP 的两逻辑请求，取消到终态每个 **≤0.30s** | 真实 gate=2/逻辑=4；必须在受控释放前退出，30s 网络 timeout 不能代替主动取消 |
| 停止—在途 | queue 中两条已到达真实 HTTP 的请求，取消到本地终态每个 **≤0.30s**，均早于服务释放 | 服务响应被屏障挂起，计划 2s 释放、网络 timeout=30s；不承诺服务端撤单，迟到结果不可交付 |
| 停止—退避 | backoff 已确认进入 gateway 1.2s sleep 后，取消到终态 **≤0.30s** | 取消落在 sleep 内，必须早于完整退避结束，不通过忽略 Retry-After 提前重发达标 |
| 停止—SDK 重试 | network 取消后终态 **≤0.30s**、不再开始新 HTTP 重试 | 此探针 read timeout=1s，当前可观察 SDK 嵌套尝试；与 queue 的长 timeout 在途场景共同验收，不能单靠 timeout 结束冒充抢占 |
| 完整任务停止 | ui 在途请求期间停止，终态 **≤0.30s**，无字幕/过程成果交付、无正常完成信号且下游阻断 | 合成请求固定 1.2s，不能缩短服务模拟来通过；此项仅证明任务传播，HTTP 抢占由真实传输探针证明 |
| 状态刷新 | 等待中可见状态间隔 **≤0.50s**；Qt 信号送达 max **≤0.10s**、事件循环心跳 max **≤0.10s** | 1.2s 受控等待可覆盖至少 2 次刷新；10ms QTimer 允许 10 倍调度余量。当前只测 worker 信号/事件循环；后续 GUI 票须补实际控件呈现，不可宣称本票已验 widget |
| 本地性能 | 同机同工具 local-long median 增幅 **≤15% 且 ≤0.75s**；min–max 跨度超过 median 的 15% 时本轮无效，整组重新采样 | 本地场景不受假服务 delay 影响；双门槛既限制比例又限制绝对退化。五次样本噪声用于判有效性，不作为事后放宽门槛的理由 |
| 质量与清理 | 原文/初版/安全上下文不变，合法校对全部应用，局部失败仍可交付但失败区域不计通过；取消/模块失败阻断；所有 worker、服务与 handler 有界清理 | 硬门槛，不以统计容忍，任何一项失败均不接受性能结果 |

固定延迟模型只验证调度/往返成本，不证明真实 token 吞吐增加。组批后必须同时报告请求数、每主体/片段覆盖、输入输出 token 估计与服务模拟总负载；不允许调整本基线延迟配方、cache 状态或角色请求参数伪造提速。

## 冻结实测结果（2026-09-11）

可提交的合成数据证据见 [`postprocess-performance-baseline-v1.json`](postprocess-performance-baseline-v1.json)：保留每组全部五次墙钟、CPU、角色请求分布、覆盖、容量、诊断阶段及停止/清理证据，不包含字幕正文、密钥、用户绝对路径或本地 tracker。完整逐请求 JSON/log 留在 gitignored 的 `benchmark-output/`。测量时父 revision 为 `f13e8c464699c52705491aff71044bd2aba115e1`，新脚本尚在工作区，**父 revision 单独不能复现本基准**；artifact 的 231 个 Python 源码 SHA256 与 uv.lock SHA256 才固定测量代码，三组采样指纹一致，导出时再次核对一致。

环境：Windows 11 `10.0.26200`、20 logical CPU、Python 3.12.13、uv 0.12.13、openai 2.15.0、httpx 0.28.1、diskcache 5.6.3、PyQt5 5.15.11、pytest 9.0.2。所有任务 gateway 额度 4、profile 无 clamp；角色为合成 `controlled-main` / `controlled-review`，context 65,536、output cap 8,192、temperature 0、boundary context radius 2。完整配置/输入 SHA256 收录在 artifact，未使用用户角色或模型设置。无故障场景每主批 10 主体、每校对调用 1 主体。

### 完整任务墙钟与请求数

单位为秒；每行独立 5 次。请求列为 **logical / adapter attempts / cache hits**；这里不涉及真实 HTTP，不能把受控 adapter 次数叫作 provider 调用。

| 场景 / cache | median（min–max） | 主修复 | 校对 | peak | 输出段数 / 校对输入段 / 校对提交片段 |
| --- | --- | --- | --- | --- | --- |
| local-long / cold | 2.193483（2.130651–2.242574） | 0/0/0 | 0/0/0 | 0 | 7,257 / 0 / 0 |
| independent / cold | 0.974034（0.967924–0.979268） | 4/4/0 | 40/40/0 | 1 | 120 / 40 / 40 |
| slow-main / cold | 2.401658（2.399540–2.560969） | 4/4/0 | 40/40/0 | 1 | 120 / 40 / 40 |
| mixed / cold | 1.009015（0.984009–1.031487） | 4/4/0 | 40/40/0 | 1 | 130 / 50 / 60 |
| partial-failure / cold | 1.128824（1.115717–1.178917） | 8/5/3 | 40/40/0 | 1 | 129 / 49 / 58 |
| independent / warm | 0.357742（0.316432–0.428968） | 4/0/4 | 40/0/40 | 0 | 120 / 40 / 40 |

partial-failure 的 cold 指**任务开始时**空 cache；同一次任务中的失败主体重复业务请求命中 3 次缓存，不能将 8 个逻辑请求当成 8 次 adapter 尝试。主输入段累计提交 54（含重复失败项），不是 54 个独立修复段；最终 unresolved=1，失败区域回退，其他合法校订仍交付。mixed 的 60 个校对片段包含 10 个一对多增加片段，不能只检查有 `Item` 前缀的第一片段。warm 的 44 个请求全部缓存命中，噪声跨度为 31.46%，仅作缓存分层观察，不用作 cold 性能或吞吐验收。

主修复/校对合计估计 input tokens：independent、slow-main 为 12,083 / 51,087；mixed 为 13,075 / 52,226。它们是按角色分别累计的请求估计量，不是计费 token；artifact 同时记录 output 估计、各请求容量与耗时分布。所有完整任务原文/初版保护、QA/状态交付、下游门禁和校订断言均通过。

### CPU 与本地诊断

| 场景 | 无 profiler 任务 CPU median | 无 profiler 扣请求区间并集后的本地墙钟 median |
| --- | --- | --- |
| local-long | 2.078125 | 2.193483 |
| independent | 0.531250 | 0.508689 |
| slow-main | 0.578125 | 0.531181 |
| mixed | 0.562500 | 0.543476 |
| partial-failure | 0.656250 | 0.608215 |
| independent warm | 0.343750 | 0.331295 |

独立 cProfile 诊断任务中，local-long 的 inclusive median：`run_post_stage` 5.169355s、`scan_viewing_lengths` 3.345194s、`_load_and_classify` 0.423798s、`_publish_module_outputs` 0.220612s、`save_canonical_srt` 0.031083s、`_validate_output` 0.003737s、`run_pre_stage` 0.000003s。**这些带显著 profiler 开销，且有嵌套，不能相加或代替上表墙钟**；只提供定位线索，不据此声称已找到历史静默根因。

### 等待、停止与 Qt

每个探针独立 5 次；下表停止时间是每轮取消到最晚本地终态的时差。

| 探针 | logical / adapter / HTTP | 停止 median（min–max），秒 | 现状与冻结目标 |
| --- | --- | --- | --- |
| queue：2 排队 + 2 在途 | 4 / 2 / 2 | 1.360728（1.263010–1.367999） | gate=2、read timeout=30s，仍等计划 2s 服务释放；不满足各请求 ≤0.30s |
| network：SDK 重试 | 1 / 1 / 3 | 5.085254（4.891111–5.123878） | read timeout=1s，取消后仍经历 SDK 重试及 gateway 退避；不满足 ≤0.30s / 无新重试 |
| backoff：真实 gateway 退避 | 1 / 1 / 3 | 0.900213（0.900020–0.900323） | 先确认进入 1.2s sleep，再过 0.3s 取消，仍等待剩余退避；不满足 ≤0.30s |
| ui：完整任务在途停止 | 1 / 1 / 无 HTTP | 1.202910（1.201846–1.206381） | 请求固定 1.2s，终态仍等自然返回；不满足 ≤0.30s |

所有传输探针 `ok=true`，worker、server、handler 清理全部通过；停止慢是预期暴露的产品现状，不是基础设施失败。queue 的在途请求还会返回迟到结果，报告保留这一事实，不将“线程最后退出”误称主动取消成功。UI 的五次结果均为 cancelled、初版不变、无成果/过程资产交付且下游阻断。

Qt 信号最大送达延迟 **1.340ms**，事件循环最大心跳间隔 **43.537ms**，本机满足各自 100ms 门槛；但所有五次都在请求等待期间没有进度刷新，最后进度到终态最长 **1.206407s**，不满足 0.50s 刷新门槛。`progress_max_gap_s` 只包含相邻 progress 事件，不能遗漏尾部静默而误报达标。本票测的是 offscreen 信号送达，不是实际页面控件呈现。

### 采样有效性与数值锚点

- 完整矩阵使用 `postprocess-v1-final`；其中 local-long 原五次跨度为 median 的 **19.57%**，整组判无效，原五个样本仍保留在 artifact 的 `excluded_groups`，未删除单个慢样本。
- local-long 整组重采使用 `postprocess-v1-local-final`，原始五次为 **2.149036、2.193483、2.242574、2.130651、2.207163s**；跨度 **5.10%**，通过冻结的 15% 噪声规则。复跑重采命令：`uv run --no-sync python -m scripts.postprocess_benchmark --output benchmark-output/postprocess-v1-local-resample --repeats 5 --case local-long --skip-probes`，新组只替换无效的 local-long，不代替完整探针。
- warm 使用独立的 `postprocess-v1-warm-final`，不混入 cold 统计。早期 headline 含 cProfile 的开发组全部不作为本基线依据。
- 后续 slow-main 完整任务 median 的冻结上限为 **1.801243725s**（2.401658300 × 0.75），同时必须满足并发重叠、请求数量与质量门槛。
- 后续 local-long median 的冻结上限为 **2.522504990s**（min(2.193482600 × 1.15, 2.193482600 + 0.75)），比较须在同机同工具和有效五次采样条件下进行。数字展示有舍入，精确值以 JSON 为准。
- 基准测试中标记当前请求数/延迟的断言是**现状锁定**，不是要求未来一直停止缓慢或一直发 40 次校对。优化票应在保留上述冻结基线的前提下更新现状测试，增加对应门槛验收，不能重写基线来使优化达标。

## 交付验证

- 最终离线回归：`QT_QPA_PLATFORM=offscreen uv run --no-sync pytest -m 'not integration and not llm' -q`，**1472 passed、5 skipped、61 deselected、1 warning**，254.98s；包含本票新增的 18 个测试。
- 新增 Python 文件 `ruff check` 通过；全项目 `pyright` 为 0 errors / 20 既有 warnings。三个基准脚本额外单独检查为 0 errors / 6 warnings（受控对象 duck typing 与 Optional operand），不是零告警。
- Spec / 规范双轴审查后完成一轮修复回审：独立 profiler 诊断、拆分后第二片段校订断言、本地零模型断言、UI `connect_ex` 离线守卫均已验证。
- 不安装新依赖、不改产品实现或用户配置，不进行真实 provider 测试；无产品 UI 改动，未执行前端构建。

## 验证范围声明

本票传输探针不覆盖缓慢分段响应/5xx，后续传输取消票仍需补充；没有修改实际页面控件，本票不代表已完成 GUI 呈现验收。

**未执行真实模型质量或性能验证。** 真实小样本需另行确认样本范围与费用上限；不默认重跑整片。本基准未读取或发布 DHH 正文。DHH 仅是本地可选输入，本公共基准不依赖其存在。

**历史约 4.4 小时静默空档仍未归因。** 当前受控场景只定位各类等待的可观察现状，不能将离线提速/停止结果解释为历史故障已修复。请求的本地停止不保证 provider 撤销请求或停止计费。
