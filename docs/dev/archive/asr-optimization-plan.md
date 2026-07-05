# 本地转录（Qwen/MiMo ASR）优化方案

日期：2026-07-04
状态：已归档；主体方案已落地。当前实现与验收记录见 `docs/dev/asr-mimo-qwen-lessons.md`。
产出方式：代码深度审计（42 项发现）+ 提交历史/内部文档挖掘 + 互联网社区最佳实践调研（2024–2026）三线交叉验证
原始材料：`work-dir/audit-code.md`（代码审计）、`work-dir/audit-web.md`（社区调研）、`work-dir/audit-history.md`（提交历史/内部文档/测试回归挖掘）

> 本文档面向执行 agent。每个工作项包含：问题定位（文件:行号）、方案、验收标准。按 P0→P4 顺序执行，P0 是其余多项的前置。

---

## 0. 现状与瓶颈诊断（TL;DR）

当前架构：`ChunkedASR` 固定 5 分钟切块（word 模式下 MiMo 为 180s）→ 每块调用 ASR 后端（本地 Qwen 或远端 MiMo）→ 纯文本经 Qwen3-ForcedAligner 对齐出词级时间戳 → `ChunkMerger` 滑窗合并。

按影响排序的五大瓶颈：

| # | 瓶颈 | 根因位置 | 影响 |
|---|------|---------|------|
| 1 | **每个 chunk 冷启动一次 Qwen worker 子进程、重新加载模型** | `qwen_local_asr.py:118`、`qwen_worker.py`（一次性进程）、`qwen_runtime.py:107-110`（进程内缓存随进程消亡） | 灾难级耗时：1.7B ASR + 0.6B Aligner 每块从磁盘重载入 GPU + CUDA context 重建，×N 块 ×重试次数 |
| 2 | **Qwen/MiMo 写死 `chunk_concurrency=1`，全链路串行** | `transcribe.py:216,241` | MiMo 网络等待与本地 GPU 对齐互相闲置；GPU 在 IO/子进程启动期间空转 |
| 3 | **音频多轮无谓编解码**：16k wav → pydub 全量解码 → 每块再有损 mp3 → worker 落盘 → qwen 内部再解码 | `transcript_thread.py:293` → `chunked_asr.py:82,175,195` → `qwen_local_asr.py:50` | CPU 浪费 + 有损压缩损伤输入质量 + base64 体积膨胀 |
| 4 | **无 VAD/静音感知**：固定时间硬切、纯静音块照样送 ASR | `chunked_asr.py:163 _split_audio` | 边界切词诱发幻觉；静音块是幻觉重灾区；`docs/dev/asr-smart-boundary-chunking.md` 设计稿被搁置 |
| 5 | **幻觉防范不对称、阈值体系脆弱**：MiMo 有三层启发式 + 重试阶梯，本地 Qwen 路径几乎裸奔；阈值全是经验硬编码，已有误杀真实语音烧掉整条重试阶梯的事故记录 | `mimo_asr.py:55-75`（阈值常量群）、`qwen_local_asr.py:465-501` | 误判块最坏白跑 7 次 ASR；漏判则幻觉入字幕 |

社区调研关键输入（详见 `work-dir/audit-web.md`）：
- 进程级"每 worker 一份模型"相比"单常驻进程 + 队列 batch"多耗 4× 显存、3.4× 算力（🟢 共识）。
- qwen-asr 官方支持 `max_inference_batch_size=32`、`attn_implementation="flash_attention_2"`、bf16、batch 输入；Qwen3-ASR 为**非自回归**模型，transformers 后端即够，无需 vLLM。
- Qwen3-ForcedAligner 是 NAR 单次前向、原生 5 分钟窗口、torch.compile 友好——当前 180s 上限（`transcribe.py:18`）有放宽空间（文献实证 180s 稳妥，官方上限 300s）。
- Whisper 系反幻觉共识：VAD（Silero）是比 logprob/压缩比阈值更稳健的第一道防线；幻觉常以高置信度生成，纯阈值不可靠；`condition_on_previous_text=False` 与 n-gram 去重是标配。

---

## P0 — Qwen 常驻 Worker（最高优先级，预计最大加速项）

### P0.1 一次性子进程改为常驻 worker + IPC 队列

**问题**：`qwen_local_asr.py:118` 每块 `subprocess.Popen` 新进程；`qwen_worker.py` 处理一个请求即退出；`qwen_runtime.py:107-110` 的 `_asr_cache`/`_aligner_cache` 是进程内缓存，形同虚设。IPC 走磁盘 JSON + 临时 mp3 + 0.25s 轮询（`qwen_local_asr.py:49-72,129-136`）。

**方案**：
- 把 `qwen_worker.py` 改造为长生命周期进程：启动后加载模型一次，循环从 stdin（JSON Lines）或本地 socket/命名管道读请求、写响应。保留子进程隔离（历史教训 A7：隔离 torch/CUDA 与 Qt 进程是刚需，勿回退到进程内加载）。
- 新增 `QwenWorkerPool`（单例，容量 1）：管理 worker 生命周期——首次请求拉起、任务全程复用、任务结束/显存压力/空闲超时后关闭；崩溃自动重启（带指数退避与次数上限）。
- 保留现有 PATH 剥离（`_without_qt_dll_paths`）、`CREATE_NO_WINDOW`/进程组、taskkill 进程树逻辑，只在 worker 启动时执行一次（当前每块重算，`qwen_local_asr.py:78,236`）。
- 协作式取消：向 worker 发 cancel 消息，worker 在块间检查；解决 `transcript_thread.py:99-101` 取消后仍要跑完当前块完整加载+推理的问题。
- MiMo 的对齐调用（`mimo_asr.py:649 run_qwen_alignment_worker`）同样走该常驻 worker（`--mode align` 变成一种请求类型）。

**验收**：任务日志中"模型加载"只出现一次/任务；多块任务第 2 块起单块耗时显著下降；取消在数秒内生效；worker 崩溃后任务能续跑。

### P0.2 worker 内推理配置优化

**问题**：`qwen_runtime.py:449` 写死 `max_inference_batch_size=1`；`_load_kwargs`（`qwen_runtime.py:266-277`）不设 `attn_implementation`、无 `torch.inference_mode()`；`qwen_runtime.py:271` `device_map="auto"` 走 accelerate 分片（对单卡可整放的 1.7B 模型引入 hooks 开销）；`qwen_runtime.py:411,464` 每次推理 finally 都 `gc.collect()+empty_cache()`。

**方案**：
- 单 GPU 场景 `device_map` 直接指定 `cuda:0`（用户 device=auto 且检测到单卡时）。
- 探测并启用 flash-attention 2（可用时 `attn_implementation="flash_attention_2"`，否则 SDPA）。注意历史教训：CUDA runtime 安装用 `--torch-backend cu128`，flash-attn 是否预编译可用需在 runtime 安装器中处理，装不上时静默回退 SDPA。
- `max_inference_batch_size` 提为可配置（默认按 VRAM 自适应：16GB 卡 + 1.7B 建议 4–8），worker 支持一次请求携带多段音频批推理。
- 推理包裹 `torch.inference_mode()`。
- `empty_cache()` 从每次推理 finally 移到任务结束/收到显存压力信号时执行一次（常驻 worker 下每块清缓存 = 反复 cudaMalloc）。
- Aligner（NAR 单次前向）评估 `torch.compile`（社区确认其结构 compile 友好），作为可选开关。

**验收**：`uv run pytest tests/test_asr/ -q` 通过；同一 3 分钟音频块在 CUDA 上 ASR+对齐耗时相比基线下降（记录基准数字进 PR）。

### P0.3 CPU 路径与显存文档化

`qwen_runtime.py:204-218` CPU 走纯 fp32。低优先：CPU 路径尝试 bf16/动态 int8 量化；若不做，至少在 `docs/config/asr.md` 明示 CPU 模式仅用于验证不适合生产。历史教训 A6（1.7B+0.6B 逼近 16GB VRAM）写入自适应 batch 的依据。

---

## P1 — 音频链路去冗余（提速 + 提准）

### P1.1 全程 PCM，消灭 mp3 再编码

**问题**：`video_utils.py:83-96` 已产出理想的 16k 单声道 wav，但 `chunked_asr.py:195` 每块 `export(format="mp3")` 有损重编码，worker 再落盘 mp3（`qwen_local_asr.py:49-51`），qwen-asr 内部再解码。子块重试再来一遍（`chunked_asr.py:345,353,404`）。

**方案**：
- 本地后端（Qwen）：chunk 导出 wav/PCM；worker IPC 直接传音频文件路径 + 起止偏移，或共享内存 PCM（qwen-asr 支持 `(np.ndarray, sr)` 输入，优先用它，省掉落盘）。
- 父进程整段音频只解码一次（pydub 或直接 numpy 读 wav），后续按样本切片，杜绝 `AudioSegment.from_file` 在 `_split_chunk_bytes`/`_transcribe_with_retry` 中的重复解码。
- MiMo（受 base64 10MB 限制，`mimo_asr.py:20,584-589`）：保留 mp3 但按码率精确计算最大块时长，替代当前"超限直接抛错"。
- `base.py:80-112` 每实例重复读文件 + CRC32 + pydub 解码求时长：切片时长由切块器直接传入；cache key 改用原始 PCM 切片哈希（当前基于重编码 mp3 bytes，编码非确定性会打散缓存命中，`qwen_local_asr.py:503`、`mimo_asr.py:686`）。

**验收**：单任务内音频全量解码次数 = 1；Qwen 路径无 mp3 中间文件；缓存命中率不因重复运行同一文件而失效。

### P1.2 ffmpeg 预处理增强（低风险增量）

依据社区共识（`audit-web.md §6`）：提取命令追加两遍 loudnorm（EBU R128, I=-16:TP=-1.5:LRA=11）作为可选开关（默认关，音量参差的素材开）。顺序：解码 → （可选降噪）→ VAD/静音分析（P2）→ loudnorm → 16k mono PCM。

---

## P2 — VAD 接入：智能切块 + 静音跳过 + 幻觉第一道防线

落地 `docs/dev/asr-smart-boundary-chunking.md` 已有设计（Status: Proposed, deferred），按其"边界吸附"首版方案执行，**不做** VAD packing / 删静音 / 重写 ChunkMerger（设计稿 Non-Goals 依然有效）。

### P2.1 边界吸附切块

- `chunked_asr.py:163 _split_audio` 重构为"边界规划 / 分块导出"两步（设计稿 §Integration Points 有伪代码与参数表）。
- 首选 Silero VAD（CPU 单线程 30s 块约 1ms，可直接对整段 16k PCM 跑一遍拿 speech intervals）；能量检测作为无依赖 fallback。设计稿的失败模式表全部落为测试用例。
- 仅对 MIMO_ASR_API / QWEN_LOCAL_ASR 启用，flag 控制，fixed 模式行为不变（回归锁定）。

### P2.2 静音块跳过与空文本判别

- 全段 VAD 结果同时用于：① 语音占比≈0 的块直接跳过 ASR（省 MiMo 计费 + GPU 时间，静音是幻觉重灾区）；② 修复 `mimo_asr.py:519-523` 的盲点——当前空文本一律当静音返回 `[]`，改为"VAD 判定有语音但 ASR 返回空 → 触发重试"，反之真静音不再进入重试阶梯（历史问题 B1 的根治：区分真静音与整块漏识别）。

### P2.3 子块重试切点复用 VAD

`chunked_asr.py:343-355` 拆 2/3 子块目前硬等分且**无重叠**直接拼接（`chunked_asr.py:441-443`）。改为在 VAD 静音候选点拆分；子块间加小重叠（2–5s）并复用 ChunkMerger 或至少去重边界词。

**验收**：含 ≥30s 纯静音的测试素材总耗时下降且静音区无幻觉字幕；边界切词造成的合并失败 warning（`chunk_merger.py:151-167`）频次下降；fixed 模式回归测试全绿。

---

## P3 — 并发调度：MiMo 网络与本地 GPU 流水线化

**问题**：`transcribe.py:216` MiMo `chunk_concurrency=1`。MiMo 的 `_run` 内网络请求（`mimo_asr.py:608`）与对齐子进程串行（`:649`），两种资源交替空闲。

**方案**：
- 把"MiMo API 转录"与"本地对齐"拆为两个阶段：API 阶段并发 3–4（纯网络 IO，`ChunkedASR` 现有线程池即可支撑）；对齐阶段所有请求排队进 P0 常驻 worker（GPU 天然串行，或 batch）。生产者-消费者衔接，块 N 在对齐时块 N+1 已在等 API 响应。
- 本地 Qwen 路径：保持外层单并发，但依托 P0 常驻 worker 消除加载开销；若 P0.2 放开 batch，可由切块器一次投喂多块。
- `chunked_asr.py:307-312` 异常时只 cancel 未开始的 future，已跑的 worker 子进程不终止（`qwen_runtime_manager.py:490` 甚至提示用户手动杀残留进程）——常驻 worker pool 统一持有进程句柄，异常/取消时统一终止或清空队列。
- 保底修复：进度回调锁（`chunked_asr.py:234`）在高并发下的正确性顺带验证。

**验收**：1 小时视频 MiMo word 模式端到端耗时较基线明显下降（记录数字）；任务异常后 `nvidia-smi` 无残留 python 进程。

---

## P4 — 幻觉防范与准确率体系化

### P4.1 本地 Qwen 路径补齐异常检测（对称性）

**问题**：MiMo 有词密度/重复/对齐覆盖率三层检测，Qwen 路径只查空时间戳（`qwen_local_asr.py:465-501`）。本地模型同样会幻觉。

**方案**：把 `mimo_asr.py` 中与后端无关的检测抽到公共模块（如 `asr/anomaly.py`）：`_check_transcript_anomaly`、`_detect_repetition`、`_detect_repeated_ngram`、`_clamp_segments_to_duration`、`_alignment_problems`、`_alignment_coverage`（注意 `text_timing.py` 与 `mimo_asr.py:354-456` 已存在一份近似重复的 split/估算代码，一并合并）。Qwen 路径复用同一套 clamp + coverage + 密度检测。

### P4.2 重试阶梯瘦身

**问题**：`chunked_asr.py:357-465` 误判块最坏跑 1+1+2+3=7 次 ASR；历史事故 B2 证实真实讲座重复语句烧掉整条阶梯。

**方案**：
- 本地 Qwen（确定性模型）跳过"同块重试"层——同输入同输出，重试无意义（`qwen_local_asr.py:479-493` 无时间戳多为配置问题，重载重试更无益，应直接报配置错误）。
- 重试复用常驻 worker（不再重载模型）与已解码 PCM（不再重解码）。
- MiMo API 响应在任务内做请求级 memo，拆子块重试时父块文本可先复用对齐再决定是否重新转录。
- 阈值常量（`mimo_asr.py:55-75`）收敛到一个可配置的 dataclass，默认值不变；以对齐 coverage 为主信号、文本密度为辅（历史教训：文本启发式误杀率高，对齐验证才可靠）。

### P4.3 对齐语言透传

**问题**：`qwen_runtime.py:468` transcribe 已拿到模型检测语言却丢弃；MiMo 路径 `mimo_asr.py:653` 传 zh/en/auto 归一化值，`normalize_aligner_language` 对 11 语言外的输入瞎猜 Chinese/English → 对齐错乱 → 假性 coverage 失败 → 白跑重试。

**方案**：ASR 返回的检测语言透传给 aligner；MiMo 无语言检测时用轻量文本语言识别替代 is_mainly_cjk 二分。

### P4.4 降级路径改进

`text_timing.py:91-113` 按词数线性摊时间是所有失败的最终降级，漂移明显。有 VAD 后（P2）：估算时间戳按语音区间分布约束（静音区不放字幕），显著减轻降级观感。同时保留历史原则 A3：need_word_time_stamp 时估算结果必须显式标记/警告，不得伪装成真实对齐。

### P4.5 合并与断句健壮性（次优先）

- `chunk_merger.py:222-268` 滑窗 O((L+R)²) + 句级 difflib：overlap ≤60s 时可接受，先不动；若 P2 增大 chunk/overlap 再优化（滚动哈希）。
- `_detect_repetition`（`mimo_asr.py:96-108`）O(len×n²)：改滚动哈希/Counter 预筛（低成本顺手改）。
- `split.py:834-838` 未匹配句 >5 直接抛异常炸整个断句流程：改为对未匹配句局部降级规则断句。
- `split.py:282-309` LLM 断句单段失败整段退化规则：加一次指数退避重试再降级（先核实 `split_by_llm` 内部是否已有重试）。
- `alignment.py SubtitleAligner` 在 ASR 主链路未见引用：确认用途，死代码则删。

---

## P5 — 工程保障（贯穿执行）

1. **基准先行**：动手前用固定素材（当前验收门槛：≥20 分钟中文演讲 + ≥20 分钟英文讲座，各含静音段）跑基线，记录端到端耗时/各步耗时（现有 `_timed_step` 日志可直接用）/字幕抽检质量。每个 P 完成后复测同素材。
2. **测试**：现有 `tests/test_asr/`（test_chunked_retry、test_chunk_merger、test_mimo_qwen_asr、test_qwen_runtime_manager）覆盖了大量历史事故回归（静默截断、时间戳溢出、真实重复误杀、Qt DLL 污染、CPU torch 回退等），任何重构必须保持全绿。新增：常驻 worker 生命周期/崩溃重启/取消、VAD 边界吸附（设计稿 §Tests 有完整清单）、PCM 链路缓存 key。CI 只跑 `tests/test_cli`、`tests/test_dubbing`、部分 `tests/test_asr`，本地务必 `uv run pytest tests/test_asr/ -q` + `uv run ruff check .` + `uv run pyright`。
3. **文档与提交纪律**：历史挖掘发现 8 个相关 commit 的 message body 全空，设计决策只活在代码注释与 lessons 文档里——本轮改动必须写实质性 commit body，并同步更新 `docs/dev/asr-mimo-qwen-lessons.md` 与 `docs/config/asr.md`。
4. **别踩的坑（历史教训清单）**：
   - worker 必须保持子进程隔离 + PATH 剥离 Qt DLL（`_without_qt_dll_paths`），否则 torch 解析到 Qt 打包的 MSVC runtime。
   - 强制取消不得 `QThread.terminate()` Qwen/MiMo 任务（`transcript_thread.py:110` 守卫保留）。
   - CUDA runtime 安装的 `--torch-backend cu128` + 末尾重装 torch 的流程不要动（防依赖链把 torch 回退成 CPU 版）；Windows `UV_LINK_MODE=copy`。
   - 缓存：先 `_make_segments()` 成功再写缓存；语义变更时 bump cache key 版本（当前 MiMo v4 / Qwen v2）；重试路径 `use_cache=False` 语义保留。
   - ForcedAligner 返回形状三态（dict 列表 / 对象 / `.items` 包装）的 normalize 逻辑与测试保留。
   - 连接测试只用内置小样本音频，不碰用户媒体。

---

## 执行顺序与依赖

```
P0 常驻 worker ──┬─→ P1 PCM 链路（worker 接收 ndarray 依赖 P0 IPC 改造）
                 ├─→ P3 流水线并发（对齐队列依赖常驻 worker）
                 └─→ P4.2 重试瘦身（复用常驻模型）
P2 VAD ──────────┬─→ P2.2 静音跳过 / P2.3 子块切点
                 └─→ P4.4 降级时间戳约束
P4.1/P4.3 与 P0-P3 无强依赖，可并行
P5 贯穿所有阶段
```

预期收益（定性）：P0+P1+P3 解决"转录耗时长、CUDA 利用率低、无并发调度"；P2+P4 解决"幻觉防范不足、准确率"；1 小时视频 word 模式的端到端耗时预计数倍下降（主要来自消除 N× 模型重载与流水线化），以基准实测为准。
