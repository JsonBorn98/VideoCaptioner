# Translation Prompt Caching Research

调研日期：2026-07-19

本文只记录供应商官方文档已经承诺的行为，并将工程推论单独标出。目标场景是：一次字幕翻译任务具有很长且稳定的翻译规则、上下文简报和确认术语表，同时需要并行翻译多个动态字幕块。

## 共同结论

- 缓存复用的是模型处理输入前缀时产生的中间状态，不是复用旧响应；每个字幕块仍会生成新输出。
- 稳定内容应保持相同顺序并放在请求前部，动态字幕块放在后部。
- 缓存命中不等于免计费，也通常不免除 token 配额或速率限制。
- 冷缓存下同时发出大量并行请求可能产生重复写入或 miss；应先完成一次预热或创建显式缓存，再 fan-out。
- 不应为了跨过最低 token 门槛而填充无意义内容。是否值得缓存要根据实际读写 token、延迟和复用次数判断。
- 供应商、模型代际、API 入口和中间网关都会改变能力；配置模型名称并不足以证明缓存可用，必须运行时探测并记录 usage。

## 供应商差异

| 供应商 | 缓存模型 | 当前官方门槛 | 生命周期 | OpenAI 兼容入口 | 关键 usage |
| --- | --- | --- | --- | --- | --- |
| OpenAI | `gpt-4o` 及更新模型自动前缀缓存；GPT-5.6+ 支持显式 breakpoint | 1,024 tokens | GPT-5.6+ 当前至少 30 分钟；旧模型依 retention 策略 | 原生 Chat Completions 与 Responses 支持；第三方兼容服务不保证 | `cached_tokens`、GPT-5.6+ `cache_write_tokens` |
| Anthropic Claude | Messages API 通过顶层或 block 级 `cache_control` 选择前缀 | 随模型变化，当前官方表为 512–4,096 tokens | 默认 5 分钟，可选 1 小时；命中会刷新 | Anthropic 官方明确说明 OpenAI SDK compatibility 不支持 prompt caching | `cache_creation_input_tokens`、`cache_read_input_tokens` |
| Google Gemini | Gemini 2.5+ 隐式缓存；显式缓存创建命名 `CachedContent` 资源 | 当前常见模型为 2,048 或 4,096 tokens，依模型与端点变化 | 显式缓存默认约 1 小时，可更新 TTL、查询和删除 | Chat 兼容入口可通过 Google 专用 `extra_body` 引用缓存；创建和管理仍使用原生 API | cached content token count / usage metadata，字段依 API 入口 |

## OpenAI

### 官方事实

- 命中依赖相同输入前缀。官方建议固定指令、示例、工具和图片在前，动态内容在后。
- 自动缓存对至少 1,024 tokens 的请求生效；较短请求的 `cached_tokens` 为零。
- `prompt_cache_key` 帮助相同前缀路由到同一缓存，但不是缓存对象 ID，也不会放宽前缀匹配。
- 官方建议单个 key 的总流量约不超过 15 RPM；更高流量需要稳定分片，但每个分片会形成自己的缓存热度。
- GPT-5.6+ 支持 `implicit` 和 `explicit` 模式以及显式 breakpoint。显式模式可以只缓存固定前缀，避免把一次性的动态字幕块写入缓存。
- GPT-5.6+ 当前缓存写入按未缓存输入价格的 1.25 倍计费；旧代际写入没有额外费用。读取和写入分别通过 usage 字段观察。
- `previous_response_id` 管理对话状态，但链中历史输入仍计为输入 token，不能用它规避成本。
- 缓存 token 仍计入 TPM；缓存也不保证输出确定性。

### 对翻译场景的推论

- GPT-5.6+ 优先使用显式模式，在“翻译规则 + 上下文简报 + 确认术语表”末尾设置一个 breakpoint。
- 缓存 key 应包含供应商、模型、语言对、提示词版本和术语表版本等稳定维度。
- 先完成一次真实请求或专门预热，再并行发送其余字幕块；官方没有承诺并发冷请求会共享尚未完成的写入。

## Anthropic Claude

### 官方事实

- Claude 原生 Messages API 的缓存需要通过 `cache_control` 启用，可由顶层自动选择最后一个可缓存 block，也可精确标记 block。
- 缓存前缀顺序为 `tools → system → messages`；改变前层会使其后层缓存失效。
- 当前最低缓存长度依模型不同，为 512、1,024、2,048 或 4,096 tokens。低于门槛通常不会报错，只是不缓存。
- 默认 TTL 为 5 分钟，可选 1 小时。当前官方价格语义为：5 分钟写入 1.25 倍、1 小时写入 2 倍、读取 0.1 倍普通输入价格。
- 缓存条目要到第一个请求开始响应后才可用；官方建议先等待预热请求，再发送并发请求，并提供 `max_tokens: 0` 预热方式。
- Anthropic 的 OpenAI SDK compatibility 明确不支持 prompt caching；需要原生 Messages API。

### 对翻译场景的推论

- 使用一个 block 级断点标记稳定 system 内容末尾，每个字幕块作为独立动态 user message。
- 连续翻译任务优先使用 5 分钟缓存；只有任务间隔和前缀规模证明值得时才使用 1 小时缓存。
- 不能把 Claude 配置成普通 OpenAI-compatible URL 后仍声称显式缓存生效。

## Google Gemini

### 官方事实

- Gemini 2.5+ 默认提供隐式缓存，但官方不保证命中或成本节省；只建议把共同内容放在开头并在较短时间内发送相似前缀。
- 显式缓存先创建 `CachedContent`，后续请求通过资源名引用。缓存内容在模型输入中充当前缀，不需要每次重新发送。
- Gemini Developer API 的显式缓存默认 TTL 约为一小时，可更新 TTL 或过期时间，也可删除。
- 当前常见最低门槛为 Gemini 2.x 的 2,048 tokens 和 Gemini 3.x 的 4,096 tokens，但模型与 Vertex/Developer API 入口的列表会变化。
- 显式缓存产生缓存输入费用和按 token×时间计算的存储费用；创建缓存是否另计普通输入 SKU 不能从当前官方说明中武断推断，应通过账单探测。
- Gemini 的 OpenAI Chat 兼容入口可用 Google 专用 `extra_body.google.cached_content` 引用显式缓存，但缓存的创建、续期和删除仍依赖原生 Google API。
- 官方没有承诺显式缓存对象的无限并发或刚创建后的并发可见性 SLA；仍受标准 RPM、TPM 或 Provisioned Throughput 限制。

### 对翻译场景的推论

- 先用原生 API 创建包含稳定翻译前缀的缓存资源，成功获得资源名后再并发翻译字幕块。
- TTL 应覆盖预计任务时间和重试余量；只更新 TTL，内容或模型变化时创建新缓存。
- 可继续用 OpenAI SDK 发 Chat 请求引用缓存，但生命周期管理必须由 Gemini 适配器负责；直接使用原生 GenerateContent 更清晰。

## 推荐的供应商中立请求模型

统一编排层只表达以下意图，不假定具体参数：

1. 构造不可变的任务级稳定前缀。
2. 计算前缀指纹和版本，判断是否达到供应商最低门槛。
3. 由供应商适配器准备缓存：自动缓存、显式 breakpoint、创建缓存资源，或声明不支持。
4. 等待缓存准备完成，再并行发送动态字幕块。
5. 记录每次请求的普通输入、缓存写入、缓存读取、输出、延迟、缓存身份和命中状态。
6. 缓存准备或读取失败时，按明确策略回退为普通独立请求；不能伪报命中。
7. 任务结束后，由支持生命周期管理的适配器清理或自然过期缓存资源。

## 国内 OpenAI 兼容服务

国内头部服务说明“通用 OpenAI 兼容”并不等于“没有缓存”。稳定前缀仍是所有自动缓存机制的共同优化条件，但命中契约、指标和可选参数各不相同。

| 服务 | OpenAI 兼容请求中的缓存 | 命中观测 | 官方未承诺的部分 |
| --- | --- | --- | --- |
| DeepSeek | 默认自动启用磁盘上下文缓存；从第 0 token 开始匹配稳定公共前缀 | `usage.prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` | 当前 V4 的最小 token、精确 TTL、并发写入去重 |
| Moonshot/Kimi | 所有模型请求自动识别高频重复的初始上下文；可选稳定 `prompt_cache_key` 改善亲和性 | `usage.cached_tokens` | 最小 token、TTL、“高频”阈值、冷并发共享 |
| 智谱 GLM | 隐式自动缓存相同或高度相似的重复上下文，无需缓存参数 | `usage.prompt_tokens_details.cached_tokens` | 相似度算法、最小 token、TTL、所有模型的固定折扣 |
| 阿里云百炼 Qwen | 支持模型默认隐式缓存；部分模型还支持 OpenAI 兼容请求中的显式 `cache_control` | `usage.prompt_tokens_details.cached_tokens`，显式模式另有创建 token | 隐式缓存固定 TTL、命中保证、跨地域或模型共享 |

### DeepSeek

- 当前官方文档称磁盘上下文缓存对所有用户默认开启，无需改变 Chat Completions 请求。
- 命中依赖从第 0 token 开始的公共前缀。大小写、空白、标点、消息顺序和历史 assistant 内容都会影响 token 前缀。
- 缓存写入异步且需要数秒，属于 best effort；闲置后通常数小时到数天清理，但不是固定 TTL。
- 当前 V4 文档不再承诺历史版本的 64-token 存储粒度，不能继续把它写死为现行规则。
- 对不同动态后缀，系统可能需要先观察多个请求才能单独持久化公共前缀；冷启动时应先串行预热并观察 usage，再放开并发。

### Moonshot/Kimi

- Context Caching 自动启用，无需缓存 ID 或手动管理 TTL；后台识别高频重复的初始 system、知识文档和工具定义。
- 可选 `prompt_cache_key` 用于提高相似请求的缓存亲和性，但它不是缓存对象 ID，也不能替代稳定前缀。
- 官方没有公布精确 TTL、最低缓存 token 或“高频”的阈值，因此产品不能承诺第二次请求必然命中。

### 智谱 GLM

- 隐式缓存自动识别相同或高度相似的重复上下文；完全相同时命中机会最高，轻微格式变化仍可能影响效果。
- 官方只描述合理时效性和通常的价格优惠，没有公布精确 TTL、最低 token 或相似度阈值。
- 通用兼容请求无需 GLM 专有缓存参数，但应保留原始 usage 字段以区分“未命中”和“不可观测”。

### 阿里云百炼 Qwen

- 支持模型的隐式缓存自动开启且不能关闭。常见最低长度约为 256 tokens，但部分新模型约为 1,000 tokens，必须按模型能力判断。
- 命中部分通常按标准输入价格的一定比例计费，但不能把一次价格表永久写死为协议契约。
- 部分模型在 OpenAI-compatible Chat Completions 中直接支持 `cache_control: {"type": "ephemeral"}`：至少 1,024 tokens、5 分钟 TTL、命中刷新、首次创建 1.25 倍输入价格、读取 0.1 倍。
- 显式缓存与隐式缓存互斥，且显式支持模型范围较窄；因此应由 Qwen 方言适配与能力探测决定是否启用，而不需要仅为缓存引入 DashScope SDK。

### 对通用兼容模式的修正

- 通用模式始终构造规范化稳定前缀，即使不知道供应商身份也能尽量利用服务端自动缓存。
- 客户端必须保留原始响应和未知 usage 扩展字段，分别识别 DeepSeek、Kimi、GLM、Qwen 等常见命中指标。
- “字段缺失”应显示为缓存状态不可观测，不能等同于未命中；延迟降低也不能单独证明命中。
- 请求端应固定消息顺序、空白、JSON key 顺序、工具顺序和模型名称，避免在前缀中放入时间戳、任务 ID、路径或随机值。
- 已知供应商可以在不更换 OpenAI SDK 传输的前提下使用轻量方言适配，例如 Kimi 的 cache key 或 Qwen 的显式 cache control。
- 冷缓存最好先顺序预热再并发；对于只承诺自动缓存的未知供应商，这是一种待 usage 验证的优化，不是命中保证。

## 当前代码缺口

- `videocaptioner/core/llm/client.py` 只使用 OpenAI SDK 的 Chat Completions，无法表达 Claude 原生缓存或 Gemini 缓存生命周期。
- `call_llm` 的一小时本地 memoize 只会命中完整参数完全相同的请求，不会复用“相同前缀、不同字幕块”的模型前缀计算。
- `BaseTranslator` 会立即并发提交全部字幕块，没有“预热完成后再 fan-out”的阶段。
- 当前翻译缓存键只包含字幕块、目标语言和模型，遗漏自定义提示词、反思模式、模型端点及未来的上下文简报和确认术语表，会造成错误复用。
- 当前全局 LLM client 是单例，不能安全承载两个独立供应商配置的主翻译模型与高级校对模型。
- 请求日志的完整 response dump 可能已经保留供应商 usage 扩展，但业务层和 UI 尚未统一解析缓存写入、读取、命中率和成本依据。

## 运行时能力探测

每个模型配置第一次使用或配置变化后，应执行小型探测并缓存结果：

- 是否支持显式缓存、隐式缓存或仅普通请求。
- 实际最低缓存 token 门槛。
- 缓存创建/断点参数是否被 SDK、网关和端点接受。
- 预热后的第二个请求是否报告预期缓存读取 token。
- usage 字段的位置和含义。
- 多并发共享缓存时的 429、miss、重复写入和延迟。
- TTL 更新、过期、删除和模型绑定行为。

能力探测失败只能证明当前配置链路未验证，不能推断供应商整体不支持。

## 官方来源

- OpenAI: [Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)
- OpenAI: [Conversation state](https://developers.openai.com/api/docs/guides/conversation-state)
- Anthropic: [Prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
- Anthropic: [OpenAI SDK compatibility](https://platform.claude.com/docs/en/api/openai-sdk)
- Gemini Developer API: [Context caching](https://ai.google.dev/gemini-api/docs/generate-content/caching)
- Gemini Developer API: [OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai)
- Vertex AI: [Context cache overview](https://cloud.google.com/vertex-ai/generative-ai/docs/context-cache/context-cache-overview)
- Vertex AI: [OpenAI migration and extra body](https://cloud.google.com/vertex-ai/generative-ai/docs/migrate/openai/overview)
- DeepSeek: [Context caching on disk](https://api-docs.deepseek.com/guides/kv_cache)
- DeepSeek: [Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion)
- Moonshot/Kimi: [Context caching](https://platform.moonshot.cn/docs/guide/use-context-caching-feature-of-kimi-api)
- Moonshot/Kimi: [Chat Completions](https://platform.moonshot.cn/docs/api/chat)
- 智谱 GLM: [上下文缓存](https://docs.bigmodel.cn/cn/guide/capabilities/cache)
- 智谱 GLM: [OpenAI API 兼容](https://docs.bigmodel.cn/cn/guide/develop/openai/introduction)
- 阿里云百炼 Qwen: [上下文缓存](https://help.aliyun.com/zh/model-studio/context-cache)
- 阿里云百炼 Qwen: [OpenAI Chat 接口兼容](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope)
