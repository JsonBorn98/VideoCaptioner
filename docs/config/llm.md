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
- 最大并发

GUI 可以为主翻译和高级校对分别绑定方案，也可以让两个角色复用同一方案。

## 支持的 Transport

| Transport | 说明 |
|---|---|
| OpenAI-compatible | OpenAI Chat Completions 及兼容服务 |
| Anthropic Messages | Anthropic 原生 Messages API |
| Gemini | Google Gemini 原生 API |

不同服务对 response schema、Prompt cache、reasoning token 和 usage 字段的支持并不相同。
VideoCaptioner 会通过对应 adapter 统一请求和 usage 记录，但不会伪造服务端未返回的统计。

## GUI 配置步骤

1. 打开 **设置 → 翻译设置**。
2. 在模型方案区域创建方案。
3. 选择 Transport，填写 Base URL、API Key 和模型名称。
4. 设置工作上下文预算与最大并发。
5. 点击连接检查。
6. 在翻译模式页把方案绑定给主翻译、单 LLM 或高级校对角色。

连接检查只验证当前凭据、端点和模型是否能完成最小请求。实际长字幕仍可能受配额、
并发限制、上下文长度或服务端策略影响。

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

## CLI 兼容配置

CLI 当前使用 legacy `[llm]` 配置：

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

配置优先级为：

```text
命令行参数 > 环境变量 > 配置文件 > 默认值
```

增强型 CLI 翻译会把同一个 legacy profile 同时用于主翻译和高级校对。需要为两个角色
选择不同模型时，请在 GUI 中使用命名模型方案。

## 并发与上下文预算

- 从较低并发开始，根据服务商的 rate limit 和本地网络逐步调整。
- 429 或频繁超时时应降低并发，而不是无限重试。
- 工作上下文预算应小于模型的公开上限，为系统指令、响应和服务端差异留出空间。
- 增强翻译遇到 context-limit 时，会在当前任务内降低预算到更保守的档位并重新规划；
  已保存方案不会被静默改写。

## API Key 与日志

- API Key 保存在用户本地配置中，不会上传到项目服务器。
- 不要把设置文件、终端历史或测试凭据提交到 Git。
- `llm_requests.jsonl` 会记录完整请求和响应，可能包含字幕与 Prompt 全文。
- 分享诊断日志前请先检查并清理敏感内容。

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
