# Agent Context Compaction Research

调研日期：2026-07-19

本文研究 Pi Agent、OpenClaw 和 Hermes Agent 如何管理接近上下文上限的 system prompt、用户指令、长期记忆、历史消息和工具结果，并判断哪些策略适合 VideoCaptioner 的 LLM 字幕翻译。

## 共同模式

三个项目虽然实现复杂度不同，但共享以下结构：

1. 当前 system prompt 或运行硬约束独立于普通历史，通常每次重建或原文保护。
2. 旧对话历史允许生成式摘要，近期用户请求和近期消息保留原文。
3. 可重建的大型工具结果、附件或原始证据最先裁剪。
4. 长期事实不会全部永久塞入上下文，而是写入结构化 memory 或任务状态，并在需要时召回。
5. 压缩在接近上限前触发，同时为输出保留空间。
6. 摘要失败或压缩后仍溢出时只进行有限次数恢复；最终明确失败，不无限循环或静默丢失硬约束。

这说明成熟 agent 的“自动压缩”主要处理可恢复的历史，而不是让任意大小的固定 system prompt 无限制进入模型。

## Pi Agent Harness

官方仓库：[earendil-works/pi](https://github.com/earendil-works/pi)，研究快照 `3da591ab74ab9ab407e72ed882600b2c851fae21`。

### 源码事实

- `buildSystemPrompt()` 将系统身份、工具说明、skills、工作目录和 AGENTS/CLAUDE 项目指令构造成独立 system prompt；compaction 只替换 message history，不改 system prompt。
- 默认自动压缩在 `contextTokens > contextWindow - reserveTokens` 时触发，默认保留最近约 20k tokens，并为输出保留约 16k tokens。
- 旧历史被总结为 Goal、Constraints、Progress、Decisions、Next Steps 和 Critical Context；后续压缩用 previous summary 与新历史滚动更新。
- 工具结果在进入摘要输入时只保留前约 2,000 字符；近期原始消息不经过该截断。
- overflow 恢复只进行一次 compact-and-retry；再次溢出则提示减少上下文或换更大窗口。
- 普通用户历史中的旧 instructions 只靠摘要的 Constraints 字段语义保留，不能保证逐字精确。

### 参考代码

- [system-prompt.ts](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/coding-agent/src/core/system-prompt.ts)
- [compaction.ts](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/coding-agent/src/core/compaction/compaction.ts)
- [compaction.md](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/coding-agent/docs/compaction.md)

## OpenClaw

官方仓库：[openclaw/openclaw](https://github.com/openclaw/openclaw)，研究快照 `c95a8e3df1e6e8d1ea4925b615707bc97f6f52b5`。

### 源码事实

- system prompt 每次运行重新构建，不作为 compaction 对象；workspace bootstrap 文件另有单文件与合计字符上限。
- 结构化摘要要求保存 Goal、Constraints & Preferences、Progress、Key Decisions、Next Steps 和 Critical Context，并优先保留最新用户请求。
- 默认把最近 3 个 turn 原样附在摘要后部；旧用户消息仍可能只留下摘要语义。
- MEMORY.md 保存精炼长期事实，详细日记按需通过 memory search/get 召回，而不是全部注入每次请求。
- 旧 tool result 最先 soft trim，再 hard clear；最近 turn 和普通对话受保护。
- pre-prompt estimator 同时计算 messages、system prompt 和当前 prompt，决定只裁工具结果、只 compaction 或先 compaction 后裁剪。
- 分块摘要有重试、partial summary、质量审计和 fallback；摘要失败时可取消 compaction 以保留原历史。
- overflow 恢复次数有限，耗尽后提示 reset/new 或更大上下文，不静默新建会话。

### 参考代码与文档

- [Context concepts](https://docs.openclaw.ai/concepts/context)
- [Compaction concepts](https://docs.openclaw.ai/concepts/compaction)
- [System prompt](https://docs.openclaw.ai/concepts/system-prompt)
- [Memory](https://docs.openclaw.ai/concepts/memory)

## Hermes Agent

官方仓库：[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)，研究快照 `09109fec98016ffd7fef8622223073d296c02fa4`。

### 源码事实

- `ContextCompressor` 永远保护首条 system message；system prompt 又分 stable、context 和 volatile 层构造。
- memory 在压缩语义中保持权威，外部 memory provider 可在丢弃历史前提取耐久信息。
- 最新用户消息必须进入原文 tail；旧用户指令进入结构化历史摘要，不再作为当前主动指令。
- 旧 tool results 和 media 最先被去重、截短或替换为占位摘要；近期 tail 原样保留。
- 当前代码针对较小上下文模型通常在有效输入预算约 75% 附近触发压缩，而不是简单照搬文档中的 50%。
- previous summary 与新历史滚动更新，并设置独立摘要输出上限。
- overflow 最多进行有限次数压缩重试；如果不可压缩底座过大，会停止 anti-thrashing。
- 摘要模型失败可回退主模型或确定性 fallback，但认证、配额和网络错误会中止并保留会话。

### 参考代码与文档

- [Context compression and caching](https://github.com/NousResearch/hermes-agent/blob/09109fec98016ffd7fef8622223073d296c02fa4/website/docs/developer-guide/context-compression-and-caching.md)
- [context_compressor.py](https://github.com/NousResearch/hermes-agent/blob/09109fec98016ffd7fef8622223073d296c02fa4/agent/context_compressor.py)
- [system_prompt.py](https://github.com/NousResearch/hermes-agent/blob/09109fec98016ffd7fef8622223073d296c02fa4/agent/system_prompt.py)

## 对字幕翻译的适用边界

### 可以借鉴

- 把不可丢的执行协议、用户角色指令和当前任务输入，与可压缩的长上下文证据分层。
- 全文原始分析、代表语境和完整术语状态保存为任务 artifact，不要求每次完整注入。
- 翻译上下文简报采用带稳定字段和版本的结构化 checkpoint，可以递归压缩并在替换前验证。
- 当前字幕块及少量邻接语境保持原文，较早内容只通过简报或检索进入。
- 请求前先执行 admission control：工作上下文上限减去输出、推理和安全余量后，再决定固定前缀和动态批次预算。
- 先裁可重建证据，再压缩简报，再缩小动态批次；每次变更记录原因和前后 token。
- 压缩失败时保留上一版任务状态，只进行有限次数重试。

### 不能照搬

- 不能用生成式摘要替代用户确认的精确术语映射、禁译项、数字、占位符或字幕编号协议。
- 不能把 agent 的最近 3 turns、20k tail、50%/75% 阈值等默认数值直接移植到字幕翻译。
- 字幕块是独立并行任务，不应积累完整 assistant/tool conversation history。
- 缓存命中只降低重复前缀计算成本，不扩大上下文窗口；普通非缓存请求也无法解决前缀过长。
- 一次视频的术语和翻译简报必须绑定任务、语言对和提示词版本，不能污染全局长期 memory。

## 建议的翻译上下文分层

1. **执行核心**：输出 schema、字幕编号一一对应、目标语言等最短不可丢协议。
2. **用户角色指令**：主翻译或高级校对的用户自定义 Prompt，原文保留。
3. **全局任务简报**：结构化、可递归压缩的主题、背景、人物和风格 checkpoint。
4. **权威术语状态**：完整表保存在任务 artifact；每次请求注入全局锁定项和与当前字幕块相关的精确子集。
5. **局部原文尾部**：当前字幕块及少量邻接语境，原文保留。
6. **可重建证据**：候选代表语境、原始分析输出和旧审计细节按需读取，不进入每次稳定前缀。

该分层使“权威术语不丢失”与“每次调用不必携带整张表”同时成立。稳定缓存前缀可包含执行核心、用户角色指令、压缩后的全局简报和全局锁定术语；按块相关术语与局部原文放在其后的动态部分。

## 建议的超预算处理顺序

1. 去除重复和可重建证据。
2. 递归压缩全局任务简报，并验证人物、主题、风格和稳定 ID 覆盖。
3. 从完整术语 artifact 中确定性选择当前块相关术语；全局锁定项始终注入。
4. 缩小局部邻接语境和动态字幕批次。
5. 依次采用 32k、16k 的运行时回退预算并警告。
6. 如果执行核心、用户角色指令、当前字幕和相关权威术语仍无法放入预算，则明确失败；不静默截断。

硬失败因此是最后一道安全边界，而不是遇到长前缀时的第一反应。
