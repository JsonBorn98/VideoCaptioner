# 任务方案：LLM Responses 接口与推理参数支持

状态：**实施完成**
最后更新：2026-07-25

> 本文是本功能的实现基准与进度追踪文件。实现、测试或范围发生变化时，必须先更新本文；对话记录不作为唯一事实源。

## 1. 背景

VideoCaptioner 有两种 LLM 翻译工作流：

1. `single_llm`：单模型分批翻译，可选反思翻译；
2. `enhanced_llm`：主译与高级校对双角色，包含全文分析、术语裁决、结构化翻译与审计。

两者已经通过 `LLMModelProfile`、`LLMGateway` 和 provider adapter 共享模型调用基础设施。当前 OpenAI-compatible adapter 只调用 `/v1/chat/completions`，模型方案也无法保存 endpoint、思考参数或独立输出 token 预算。

不同模型和 provider 的推理控制没有统一字段，例如 `reasoning`、`reasoning_effort`、`thinking`、`output_config`、`extra_body`、`chat_template_kwargs` 和 Gemini 的 `generationConfig.thinkingConfig`。本方案采用受约束的 provider-native JSON 参数补丁，而不是维护容易过时的统一推理枚举。

## 2. 目标与非目标

### 2.1 目标

- GUI 与 CLI 的 `single_llm`、`enhanced_llm` 均支持标准 OpenAI Responses API。
- OpenAI Chat/Responses、Anthropic Messages、Gemini profile 均支持 provider-native 高级请求参数。
- endpoint、请求参数和输出 token 预算属于模型方案；主译与校对可使用不同方案。
- 保持字幕输入、结构化输出、重试和 token 规划由应用控制。
- 默认不让服务端存储请求；默认日志不落字幕、回答、raw response 或推理内容。
- 旧 GUI profile 与 CLI `[llm]` 配置无损兼容。

### 2.2 非目标

- 不为字幕断句、字幕校正等通用 LLM 功能增加 Responses endpoint。
- 不支持 Chat 或 Responses 流式响应。
- 不实现 AthenAI 文档中的非标准 Responses `response_format` 兼容；Responses 只发送标准 OpenAI `text.format`。
- 不自动探测 endpoint，不按 URL 或模型名推断 provider。
- 不在 Responses 失败时自动回退 Chat Completions。
- 不展示、持久化或复用模型推理内容。

日志基础设施会做全局隐私收敛，但不会改变通用 LLM 功能使用的 API endpoint。

## 3. 已确认决策

### 3.1 Endpoint 与传输层

- `LLMTransport` 继续表示协议族：OpenAI-compatible、Anthropic Messages、Gemini。
- OpenAI-compatible profile 额外保存 `openai_endpoint`：
  - `chat_completions`（默认，兼容旧行为）；
  - `responses`。
- Responses 允许任意 Base URL，但该服务必须实现标准 OpenAI Responses 请求和响应格式。
- Anthropic/Gemini UI 隐藏 endpoint；配置中出现非默认 OpenAI endpoint 时直接报错，不静默忽略。

### 3.2 Profile 字段

`LLMModelProfile` 新增：

```python
openai_endpoint: OpenAIEndpoint = OpenAIEndpoint.CHAT_COMPLETIONS
request_options: Mapping[str, JSONValue] = {}
max_output_tokens: int | None = None  # None 表示 auto
```

- profile 保持不可变；`request_options` 递归复制并冻结。
- JSON 顶层必须是 object，并设置合理的大小、深度和 JSON 类型限制。
- profile equality 包含新字段，因此修改后 gateway 会重建 adapter，翻译缓存键也会自然失效。

### 3.3 高级请求参数合并

用户配置的是“附加请求参数 JSON”，不是完整原始请求体。适配器按以下顺序构造请求：

```text
1. 由 LLMRequest 构造应用请求
2. 顶层浅合并 request_options
3. 重新写入并断言应用保护字段
4. 发送最终请求
```

规则：

- 未知且非保护字段原样透传，由 provider 的真实请求验证。
- 顶层对象浅合并；嵌套对象和数组整体替换，不做通用递归合并。
- 应用不再向任何 transport 发送 `temperature`；高级 JSON 也不得在各 transport 的原生位置重新加入它。
- 为兼容已有方案，旧的 `$omit: ["temperature"]` 会作为无操作接受，且不会发送给 provider。
- 保护字段冲突在保存、CLI 预检和调用前均明确报错，不忽略、不静默覆盖。
- URL、Authorization、headers、query 和 timeout 不属于请求体 JSON 配置面。

受保护语义包括：

- 模型与输入：`model`、Chat/Anthropic `messages`、Responses `input`/`instructions`、Anthropic `system`、Gemini `contents`/`systemInstruction`；
- 执行形态：`stream`、`background`、`n`；
- 工具与会话：`tools`、`tool_choice`、`parallel_tool_calls`、`functions`、`function_call`、`max_tool_calls`、`previous_response_id`、`conversation`、`prompt`；
- 结构化合约：Chat `response_format`、Responses `text.format`、Anthropic 结构化工具定义、Gemini `responseMimeType`/`responseSchema`；
- 输出预算：`max_tokens`、`max_completion_tokens`、`max_output_tokens`、Gemini `maxOutputTokens`。

允许用户配置采样参数、`reasoning`、`reasoning_effort`、`thinking`、`output_config`、`extra_body`、`chat_template_kwargs`、`service_tier`、`metadata`、`user` 和 `store` 等非保护字段。

Gemini 的 `generationConfig` 仍按顶层浅合并规则整体替换；适配器随后恢复其中受保护的输出 token 与结构化输出子字段，并移除 `temperature` 子字段。

保护规则检查**精确 JSON path**，不递归拒绝其他位置的同名普通字段：

| Transport / endpoint | 禁止的 request option path |
|---|---|
| OpenAI Chat | `model`、`messages`、`stream`、`n`、`tools`、`tool_choice`、`parallel_tool_calls`、`functions`、`function_call`、`max_tokens`、`max_completion_tokens`、`response_format`、`temperature` |
| OpenAI Responses | `model`、`input`、`instructions`、`stream`、`background`、`tools`、`tool_choice`、`parallel_tool_calls`、`max_tool_calls`、`previous_response_id`、`conversation`、`prompt`、`max_output_tokens`、`text.format`、`temperature` |
| Anthropic Messages | `model`、`messages`、`system`、`stream`、`max_tokens`、`tools`、`tool_choice`、`temperature` |
| Gemini | `model`、`contents`、`systemInstruction`、`cachedContent`、`tools`、`toolConfig`、`generationConfig.candidateCount`、`generationConfig.maxOutputTokens`、`generationConfig.responseMimeType`、`generationConfig.responseSchema`、`generationConfig.temperature` |

Responses 的 `text` 和 Gemini 的 `generationConfig` 可以包含其他非保护子字段。适配器先接受用户的整个对象，再恢复上表中的保护子字段。例如用户可以设置 `text.verbosity` 或 `generationConfig.thinkingConfig`，但不能替换 `text.format` 或 `generationConfig.responseSchema`。

对于 OpenAI Python SDK，`request_options` 表示**最终 HTTP JSON body 的补丁**。适配器把非保护补丁整体传给 SDK 的特殊 `extra_body=` 调用参数，由 SDK 合并到最终 body。若 provider 本身要求一个字面量为 `extra_body` 的 JSON 字段，用户配置必须写成 `{"extra_body": {...}}`；适配器将其作为补丁中的普通嵌套字段传递，不把它解释成第二层 SDK 控制参数。

JSON 验证边界固定为：UTF-8 编码后最多 64 KiB、最大嵌套深度 16、所有 object key 必须是字符串、数值必须有限，value 只能是 JSON 的 null/boolean/number/string/array/object。

### 3.4 输出 token 预算

- `max_output_tokens=None`（UI/CLI 显示 `auto`）：
  - `single_llm` 不发送 provider token cap，保持当前 provider 默认行为；
  - `enhanced_llm` 保持当前启发式：`clamp(work_context_tokens // 8, 1024, 8192)`。
- 数值模式应用于该 profile 的所有调用，并由 adapter 映射：
  - Chat：`max_completion_tokens`；
  - Responses：`max_output_tokens`；
  - Anthropic：`max_tokens`；
  - Gemini：`generationConfig.maxOutputTokens`。
- 增强翻译 token planner 必须为当前角色准确预留相同数值，不能只在发送前覆盖参数。
- 合法范围为 `1 <= value < work_context_tokens`。
- 小于 1024、超过上下文一半，或识别到 `thinking budget >= output cap` 时显示警告但允许保存，由完整探测和 provider 响应作最终裁决。

`work_context_tokens` 始终取**当前调用角色的已冻结 profile**：主译调用使用 main profile，校对调用使用 review profile。数值 output cap 也来自同一 profile。enhanced 的运行时 context-limit fallback 只可选择仍满足 `runtime_context_tokens > max_output_tokens` 的 32k/16k 档位；不能静默缩小用户明确设置的 output cap。没有可用档位时以 context/output 配置错误失败。auto 模式则按每个运行时 fallback 档位重新计算启发式 reserve。

### 3.5 Responses 请求与响应

- 将 `LLMRequest.messages` 全部按顺序转换为 Responses `input` items，保留 system/user/assistant 角色。第一版永远不由应用发送顶层 `instructions`；它仍是保护字段，因为用户注入它会改变系统约束。
- 使用标准 OpenAI `text.format` 表达 JSON object/schema；不得发送 AthenAI 风格顶层 `response_format`。
- 非流式调用 `client.responses.create(...)`。
- 遍历 `output[]` 中全部 `type=message` 项及其 `type=output_text` content，按响应顺序无分隔拼接所有文本片段（与流式 delta 聚合语义一致）；不得假设 `output[0]` 是最终消息，不得把 reasoning/refusal 当译文。
- 只接受 `status=completed`。`incomplete`、`failed`、`cancelled`、拒答和无最终文本均成为明确的非瞬态错误，不自动重试。
- 429、5xx、超时和网络断开继续使用 gateway 的既有瞬态重试。
- usage 映射 `input_tokens`、`output_tokens`、cached tokens 和 `output_tokens_details.reasoning_tokens`。

### 3.6 结构化输出

- Chat：继续使用 `response_format`；OpenAI/Qwen dialect 使用 strict JSON schema，其他 dialect 使用 JSON object。
- Responses：标准 `text.format`。有 schema 时构造 `{"format": {"type": "json_schema", "name": "structured_response", "strict": true, "schema": <schema>}}`；若未来只有 JSON object 要求，则使用 `{"format": {"type": "json_object"}}`。
- Anthropic：继续使用受控 tool schema，并从 tool input 提取结构化结果。
- Gemini：继续使用 `responseMimeType` 与 `responseSchema`。
- `single_llm` 保留现有 JSON repair 与最多三轮自纠错。
- `enhanced_llm` 保留每阶段 schema、解析校验和纠错重试。
- 结构化失败不切换 endpoint。

### 3.7 GUI

模型方案编辑器新增：

- OpenAI-compatible endpoint 下拉；native transport 隐藏该控件；
- 最大输出 token：`自动` 或正整数；
- 可折叠高级 JSON 编辑器，保存前定位 JSON/保护字段错误；
- 一次性模板：空白、GPT、Claude、Gemini、Qwen、GLM、DeepSeek、Kimi、Doubao；
- 模板仅替换高级 JSON，应用前确认，不修改连接、模型或 endpoint；模板更新不影响已有 profile；
- “文本能力”和“结构化输出能力”两项探测结果。

探测规则：

- 使用最终 profile、真实高级参数和独立小请求，可能产生费用，执行前明确提示；
- `max_output_tokens` 为数值时使用该值；auto 时基础为 4096，若识别到更高 thinking budget，则用 `budget + 512`，最高不超过上下文一半；
- 结构化探测使用最小 schema，并对返回 JSON 做本地精确校验；
- 结果只在当前编辑窗口显示，不持久化、不阻止保存或选择翻译模式。

内置模板是带适用 transport/endpoint 说明的静态示例，不参加运行时推断：

| 模板 | 初始 JSON 要点 |
|---|---|
| 空白 | `{}` |
| GPT Chat | `reasoning_effort` |
| GPT Responses | `reasoning.effort` |
| Claude 手动思考 | `thinking.enabled` + `budget_tokens` |
| Claude 自适应思考 | `thinking.adaptive` + `output_config.effort` |
| Gemini | `generationConfig.thinkingConfig.thinkingBudget` |
| Qwen | provider-native `extra_body.enable_thinking` 示例 |
| GLM / DeepSeek | provider-native `extra_body.thinking` / budget 示例 |
| Kimi | provider-native启用/禁用示例，并注明部分模型不可关闭 |
| Doubao | `reasoning_effort` / provider-native thinking 示例 |

模板只保证 JSON 与本地保护规则有效，不保证目标 provider/model 接受；完整探测负责验证。thinking budget 只从以下已知数值路径取最大值用于警告和 probe cap：`thinking.budget_tokens`、`generationConfig.thinkingConfig.thinkingBudget`、`extra_body.thinking_budget`、`extra_body.thinking.budget_tokens`、`chat_template_kwargs.thinking_budget`。不通过模型名或响应猜测预算。

### 3.8 CLI

新增可部分覆盖、逐层继承的隐式翻译 profiles：

```toml
[translate.llm.main]
# 缺失字段继承旧 [llm]

[translate.llm.review]
# 缺失字段继承已解析的 translate.llm.main
```

解析顺序：

```text
旧 [llm] → translate.llm.main → translate.llm.review
```

- `single_llm` 只使用 main；`enhanced_llm` 使用 main 与 review。
- 高级参数在 TOML 中保存为 `request_options_json` 字符串。
- main/review 命令行只新增 endpoint、输出 token 和高级 JSON 参数。
- 完整连接字段与 API Key 通过 TOML 和角色专属环境变量配置，避免 secret 出现在 shell history。
- 旧配置未出现新 section 时，两个角色都等价于原 `cli-legacy` profile。

完整配置优先级从低到高为：

```text
内置默认值
→ 配置文件 [llm]
→ 既有 OPENAI_*/VIDEOCAPTIONER_LLM_* 全局环境变量
→ 既有全局 --api-key/--api-base/--model 参数
→ 配置文件 [translate.llm.main]
→ main 角色环境变量
→ main 角色 endpoint/token/options CLI 参数
→ 配置文件 [translate.llm.review]
→ review 角色环境变量
→ review 角色 endpoint/token/options CLI 参数
```

继承以“字段是否存在”为准：缺失才继承。`api_key=""` 是合法显式覆盖，用于本地免 Key 服务；其他必填字符串为空按既有 profile 校验失败。清空继承的高级参数必须显式写 `request_options_json="{}"`，空字符串不是清空语义而是无效 JSON。`max_output_tokens="auto"` 显式恢复 auto；缺失则继承。

### 3.9 服务端存储

- OpenAI Chat 与 Responses 最终请求默认注入 `store=false`。
- 用户可在高级 JSON 显式设置 `store=true`。
- GUI 每次保存含 `store=true` 的 profile 时要求确认；CLI 每次启动相关翻译任务时向 stderr 警告一次，不提供静默抑制参数，也不逐请求刷屏。

### 3.10 日志隐私

统一 gateway 与旧 LLM client 的日志策略，但不改变旧 client 的 endpoint：

- 默认只写 request id、profile id/model、stage/role、attempt、状态、耗时、usage 和已脱敏错误类别；
- 全局显式开启“LLM 内容日志”后，额外记录提示词和规范化最终文本；
- 永不落盘 raw response、高级 JSON、reasoning 内容、Authorization、API Key、cookie/token/secret-like 字段或完整 provider error body；
- reasoning token 数可以记录；
- 旧日志不自动删除，UI 提示其可能包含内容，并提供需要确认的手动清理入口。

## 4. 数据迁移

### 4.1 GUI profile collection

- profile collection schema 从 v1 升到 v2。
- v1 加载时只在内存补：`chat_completions`、空 options、auto output tokens。
- 仅当用户保存、新建或删除 profile 时，才通过现有原子写入流程落盘为 v2。
- 不根据 Base URL、model 或 dialect 自动改变 endpoint。

### 4.2 CLI

- 保留旧 `[llm]`、OpenAI 环境变量与 `--api-key/--api-base/--model` 行为。
- 没有 `[translate.llm.*]` 时，main/review 都继承旧配置，行为与当前版本一致。
- 新角色环境变量只覆盖对应角色；review 未配置字段继续继承 main。

## 5. 实施阶段与进度

### Phase 0：规格固化

- [x] 完成需求 grilling 与决策确认。
- [x] 创建本实现基准与追踪文档。
- [x] 通过无上下文读者测试并修正文档歧义。

### Phase 1：领域模型与持久化

- [x] 新增 endpoint、JSON value/options、输出 token 模型及校验。
- [x] profile collection v1→v2 内存迁移与延迟写回。
- [x] 更新缓存键、日志 profile metadata 和 legacy profile builders。
- [x] 完成 profile/迁移/校验单测。

### Phase 2：适配层

- [x] 实现共享的 options 校验、保护字段和浅合并；保留旧 `$omit: ["temperature"]` 作为兼容性无操作。
- [x] Chat 使用 `max_completion_tokens` 并支持高级参数与 `store=false`。
- [x] 新增标准 OpenAI Responses adapter 分支与响应/usage 解析。
- [x] Anthropic/Gemini 支持 provider-native options。
- [x] 更新错误分类，禁止非完成 Responses 自动重试。
- [x] 完成四种请求格式的 payload、解析和错误单测。

### Phase 3：token 规划与探测

- [x] profile 输出预算进入 single/enhanced 调用。
- [x] enhanced 各角色 planner 使用真实 reserve。
- [x] 实现文本/结构化双探测与 auto 探测预算。
- [x] 完成 planner、probe 和两种翻译模式回归测试。

### Phase 4：GUI

- [x] 扩展 profile dialog、transport/endpoint 联动与 JSON editor。
- [x] 增加内置模板、警告、`store=true` 确认与双探测结果。
- [x] 增加全局内容日志开关和旧日志手动清理入口。
- [x] 完成 profile CRUD、模板、探测与翻译模式 UI 测试。

### Phase 5：CLI

- [x] 实现 main/review section 继承与旧 `[llm]` fallback。
- [x] 增加 endpoint/token/options 参数和角色环境变量。
- [x] 增加 JSON、保护字段、store 警告与配置错误提示。
- [x] 完成配置优先级、main/review 快照及模式回归测试。

### Phase 6：日志与验收

- [x] gateway 与旧 client 统一 metadata-only 默认日志。
- [x] 内容日志 opt-in、推理/raw/options 永不落盘。
- [x] 更新用户配置文档和 CLI help。
- [x] 运行目标 pytest、完整非集成 pytest、Ruff 与 Pyright。
- [x] 按第 7 节逐项完成证据审计。

## 6. 测试策略

重点测试文件：

- `tests/test_llm/test_profiles.py`
- `tests/test_llm/test_adapters.py`
- `tests/test_llm/test_connection_probe.py`
- `tests/test_translate/test_llm_translator_unit.py`
- `tests/test_translate/test_enhanced_orchestrator.py`
- `tests/test_ui/test_translation_setting_widget.py`
- `tests/test_ui/test_translation_task_modes.py`
- `tests/test_cli/test_config.py`
- `tests/test_cli/test_translation_modes.py`

最低验证命令：

```powershell
uv run pytest tests/test_llm tests/test_translate/test_llm_translator_unit.py tests/test_translate/test_enhanced_orchestrator.py -q
uv run pytest tests/test_ui/test_translation_setting_widget.py tests/test_ui/test_translation_task_modes.py -q
uv run pytest tests/test_cli/test_config.py tests/test_cli/test_translation_modes.py -q
uv run pytest -m "not integration"
uv run ruff check .
uv run pyright
```

真实 provider 集成测试必须读取环境变量并在无凭据时跳过；不得把 Key、请求字幕、raw response 或生成媒体提交到仓库。

## 7. 完成定义与验收证据

只有下列项目都有直接证据时，功能才算完成：

1. GUI 创建的 Chat、Responses、Anthropic、Gemini profile 可 round-trip，v1 文件可读且未在只读加载时重写。
2. CLI 无新配置时行为不变；main/review 继承、参数、环境变量和旧 `[llm]` fallback 有测试证明。
3. 两种翻译模式均通过 profile gateway 使用正确 endpoint/options/token cap。
4. Responses 请求使用标准 `input`/`text.format`，并从 message/output_text 提取最终文本和 reasoning usage。
5. 保护字段、无 `temperature` 请求、浅合并、未知字段透传及 `store` 规则有逐 transport payload 测试。
6. 数值 output cap 与 enhanced planner reserve 一致；auto 保持既有行为。
7. 文本/结构化探测可独立成功或失败，不修改 profile、不阻止模式。
8. 默认日志没有字幕、回答、raw response、options 或 reasoning；opt-in 也绝不包含后三者。
9. 现有 single/enhanced 翻译、重试、结构化校验、上下文 fallback 和缓存测试保持通过。
10. 第 6 节全部命令通过，或对无法运行的项目记录明确、可复现的外部原因。

### 7.1 验收审计

| # | 结论 | 直接证据 |
|---|---|---|
| 1 | 通过 | profile v2 round-trip、四 transport/endpoint 组合及 v1 延迟迁移测试通过。 |
| 2 | 通过 | CLI legacy fallback、main/review 逐层继承、alias 规范化、环境变量与参数优先级测试通过。 |
| 3 | 通过 | single/enhanced 模式的 profile、endpoint、options 与独立角色 token cap 回归测试通过。 |
| 4 | 通过 | Responses 标准 payload、全部 message/output_text 聚合、状态与 usage 解析测试通过。 |
| 5 | 通过 | 四 transport 的保护路径、无 `temperature` 请求、浅合并、未知字段和 `store` payload 测试通过。 |
| 6 | 通过 | single 数值/auto cap 及 enhanced 分角色 reserve、context fallback 测试通过。 |
| 7 | 通过 | 文本与结构化探测的独立结果、预算、profile 不变性和 GUI 展示测试通过。 |
| 8 | 通过 | gateway/legacy client 默认与 opt-in 隐私测试通过；并发关联、失败清理和错误正文脱敏已覆盖。 |
| 9 | 通过 | 两种翻译模式、重试、结构化纠错、context fallback 与缓存回归均包含在非集成全套测试中。 |
| 10 | 通过 | 聚焦测试 256 passed；非集成全套 1060 passed、5 skipped、62 deselected；Ruff 通过；Pyright 0 errors（20 条既有 warning）；`git diff --check` 无空白错误。 |

## 8. 风险与约束

- 标准 Responses `text.format` 与部分兼容网关文档的顶层 `response_format` 不同；这是明确范围选择，不做自动兼容或回退。
- 原生 JSON 参数随 provider 演进，内置模板只提供起点，不构成能力保证；完整探测和 provider 错误是最终依据。
- reasoning 会消耗输出预算，过低 cap 可能产生无最终文本；UI 警告但不替用户猜测模型规则。
- 双探测会产生真实调用费用；必须在 UI 中明确提示。
- metadata-only 日志降低调试细节；用户可显式开启内容日志，但 reasoning/raw/options 始终不可记录。

## 9. 相关文档

- [LLM Context Translation Design](../design/llm-context-translation.md)
- [Enhanced LLM Translation Implementation Specification](../design/llm-context-translation-implementation.md)
- [ADR-0010: Separate translation and review roles](../adr/0010-separate-translation-and-review-roles.md)
- [ADR-0011: Separate LLM transports from provider dialects](../adr/0011-use-provider-native-llm-adapters.md)
- [OpenAI Responses API](https://platform.openai.com/docs/api-reference/responses)
- [AthenAI Responses reference（仅作差异参考，不是实现协议）](https://athenai.mihoyo.com/docs/endpoints/responses)
- [AthenAI Chat reasoning reference（仅作参数示例）](https://athenai.mihoyo.com/docs/endpoints/chat-completions#thinking--reasoning-%E6%8E%A7%E5%88%B6)

## 10. 实施记录

| 日期 | 阶段 | 变更 | 证据/备注 |
|---|---|---|---|
| 2026-07-23 | Phase 0 | 方案确认并归档 | 六轮决策树完成；本文成为实现基准 |
| 2026-07-23 | Phase 0 | 无上下文读者测试 | 修复保护路径、Responses system 映射、CLI 优先级、context fallback、SDK `extra_body` 等歧义 |
| 2026-07-23 | Phase 1–3 | 完成 profile v2、四类 adapter、Responses、options 与 token 规划 | 聚焦领域/adapter/翻译测试通过 |
| 2026-07-23 | Phase 4–5 | 完成 GUI、CLI、模板、双探测和角色配置继承 | GUI/CLI 回归及配置优先级测试通过 |
| 2026-07-23 | Phase 6 | 收敛 LLM 日志隐私并更新用户文档 | 默认 metadata-only；内容日志 opt-in；隐私专项测试通过 |
| 2026-07-23 | 验收 | 完成实现审查与全量非集成验证 | 1060 passed，Ruff 通过，Pyright 0 errors |
