# LLM 模型方案

VideoCaptioner 的 LLM 能力用于语义断句、字幕优化、单 LLM 翻译、增强型双角色翻译，
以及可选的字幕后处理语义修复。API Key 由用户自行配置，项目不提供或推广 API 中转服务。

## 两类配置

### 通用 LLM 配置

通用配置继续服务于字幕断句、字幕优化和部分兼容功能。可选择 OpenAI、DeepSeek、
SiliconCloud、Ollama、LM Studio 等预设，或填写 OpenAI-compatible 地址。

### 命名模型方案

增强型翻译使用可复用的命名模型方案。每个方案保存：

- 方案名称
- Transport 与 dialect
- Base URL
- API Key
- 模型名称
- 工作上下文预算
- OpenAI endpoint（Chat Completions 或 Responses）
- 最大输出 token（自动或固定值）
- Provider-native 高级请求参数 JSON
- 最大并发

GUI 可以为主翻译和高级校对分别绑定方案，也可以让两个角色复用同一方案。

## 支持的 Transport

| Transport | 说明 |
|---|---|
| OpenAI-compatible | OpenAI Chat Completions、标准 Responses 及兼容服务 |
| Anthropic Messages | Anthropic 原生 Messages API |
| Gemini | Google Gemini 原生 API |

不同服务对 response schema、Prompt cache、reasoning token 和 usage 字段的支持并不相同。
VideoCaptioner 会通过对应 adapter 统一请求和 usage 记录，但不会伪造服务端未返回的统计。

## Dialect 与结构化输出

OpenAI-compatible 服务表面协议一致，但强制 JSON schema 的方式不同。不少网关会接受
`response_format` 的 `json_schema` 再静默忽略它，返回不受约束的 JSON 且不报任何错误。
Dialect 决定 Chat Completions 请求用哪种方式传递 schema：

| Dialect | 传递方式 |
|---|---|
| `openai`、`qwen`、`gemini` | `response_format` 的 `json_schema`，`strict: true` |
| `deepseek`、`kimi`、`glm`、`anthropic` | 强制函数调用，schema 作为工具参数 |
| `generic` | 仅 `json_object`（JSON 模式） |

若服务端拒绝强制函数调用，请求会自动降级为一次 JSON 模式重试，因此不支持函数调用的模型
仍可使用。`generic` 面向未识别的端点，只发所有兼容服务都接受的 JSON 模式；此时 schema 仅
通过提示词约束，不做强制。能力测试的“结构化输出”一项会区分这两种情况：它故意下发与
schema 冲突的指令，只有真正强制 schema 的组合才能通过。

Responses endpoint 与 Anthropic Messages、Gemini 原生 Transport 各有唯一的 schema 表达
方式，不受 dialect 影响。

## GUI 配置步骤

1. 打开 **设置 → 翻译设置**。
2. 在模型方案区域创建方案。
3. 选择 Transport，填写 Base URL、API Key 和模型名称。
4. OpenAI-compatible 方案选择 Chat Completions 或 Responses；原生 Transport 不显示该项。
5. 设置工作上下文、最大输出 token 与最大并发。`自动` 会沿用工作流的既有预算策略。
6. 需要思考控制时展开“高级请求参数”，手写 JSON 或应用一次性模板。
7. 点击能力测试；程序会分别发送文本与结构化输出请求，可能产生费用。
8. 在翻译模式页把方案绑定给主翻译、单 LLM 或高级校对角色。

能力测试只验证当前凭据、端点、参数和模型能否完成两个最小请求。实际长字幕仍可能受配额、
并发限制、上下文长度或服务端策略影响。

## 高级请求参数

高级 JSON 是最终请求体的附加补丁，不是完整请求体。未知且非保护字段会原样发送，因而可
配置不同服务的 `reasoning`、`reasoning_effort`、`thinking`、`output_config`、
`generationConfig.thinkingConfig`、`extra_body` 等参数。对象按顶层浅合并；嵌套对象整体
替换。应用不会在请求中附加 `temperature`；高级 JSON 也不能重新加入该参数。

应用始终保护模型、消息/input、工具、结构化输出和输出 token 字段。尝试覆盖这些字段会在
保存时直接报错。模板只提供静态起点，不保证具体 provider/model 接受；请以能力测试和服务商
文档为准。若显式配置 `store: true`，GUI 每次保存都会确认服务端可能保留字幕内容。

## 常见端点示例

以下仅用于说明字段格式，请以各服务商当前官方文档为准：

| 服务 | Base URL 示例 |
|---|---|
| OpenAI | `https://api.openai.com/v1` |
| DeepSeek | `https://api.deepseek.com/v1` |
| SiliconCloud | `https://api.siliconflow.cn/v1` |
| Ollama | `http://localhost:11434/v1` |
| LM Studio | `http://localhost:1234/v1` |

项目不保证任何第三方服务的价格、地区可用性、并发额度或模型列表。

## CLI 配置

旧 `[llm]` 继续用于字幕断句、优化，并作为翻译角色的兼容默认值：

```bash
uv run videocaptioner config set llm.api_key <your-key>
uv run videocaptioner config set llm.api_base https://api.openai.com/v1
uv run videocaptioner config set llm.model <model-name>
```

也可以在单次命令中传入：

```bash
uv run videocaptioner subtitle input.srt \
  --api-key <your-key> \
  --api-base https://api.openai.com/v1 \
  --model <model-name>
```

翻译可按字段覆盖主译与校对角色；review 缺失字段继承 main，main 缺失字段继承 `[llm]`：

```toml
[translate.llm.main]
openai_endpoint = "responses"
max_output_tokens = 8192
request_options_json = '{"reasoning":{"effort":"high"}}'

[translate.llm.review]
model = "review-model"
max_output_tokens = "auto"
request_options_json = '{}'
```

连接字段也可写入角色表，或使用
`VIDEOCAPTIONER_TRANSLATE_LLM_MAIN_*` / `VIDEOCAPTIONER_TRANSLATE_LLM_REVIEW_*`
环境变量。API Key 建议使用配置文件或环境变量，避免进入 shell history。命令行仅提供
`--main-llm-endpoint`、`--main-llm-max-output-tokens`、
`--main-llm-request-options-json` 及对应 review 参数。

完整优先级从低到高为：

```text
默认值 → [llm] 配置/全局环境变量/全局 CLI
→ main 配置/角色环境变量/角色 CLI
→ review 配置/角色环境变量/角色 CLI
```

没有新角色配置时，single/enhanced 行为与旧版本一致；增强模式复用同一个 legacy profile。

## 并发与上下文预算

- 从较低并发开始，根据服务商的 rate limit 和本地网络逐步调整。
- 429 或频繁超时时应降低并发，而不是无限重试。
- 工作上下文预算应小于模型的公开上限，为系统指令、响应和服务端差异留出空间。
- 增强翻译遇到 context-limit 时，会在当前任务内降低预算到更保守的档位并重新规划；
  已保存方案不会被静默改写。

## API Key 与日志

- API Key 保存在用户本地配置中，不会上传到项目服务器。
- 不要把设置文件、终端历史或测试凭据提交到 Git。
- `llm_requests.jsonl` 默认只记录模型、阶段、状态、耗时和 token usage 等元数据。
- 只有显式开启“记录 LLM 内容”后才额外保存提示词与规范化最终文本；原始响应、高级 JSON、
  推理内容和完整 provider 错误正文始终不记录。
- 升级前生成的旧日志可能仍含完整内容，可在“请求日志”页确认后清理当前及轮转文件。

## 排障

### 连接失败

1. 检查 Base URL 是否包含服务要求的版本路径。
2. 检查 API Key、模型名和 Transport 是否匹配。
3. 检查代理、防火墙和地区限制。
4. 查看 GUI 请求日志或 `app.log` 中的结构化错误类别。

### 429、超时或并发错误

1. 降低最大并发。
2. 查看服务商配额与 rate limit。
3. 减小单批上下文预算。
4. 确认没有多个任务同时复用同一配额。

### 输出格式错误

增强翻译会对 schema 和字幕 ID 做机械校验并有限重试。重试耗尽时，必要阶段会失败退出，
不会继续产生看似成功但缺失审计的字幕。

---

相关文档：

- [翻译模式与双角色校对](/config/translator)
- [CLI 参考](/cli)
- [常见问题](/guide/faq)
