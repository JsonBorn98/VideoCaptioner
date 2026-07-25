# 翻译模式与双角色校对

VideoCaptioner 提供三种并行的翻译工作模式。选择模式时应综合考虑质量要求、
调用成本、处理时间、隐私和服务可用性，而不是默认认为 LLM 一定优于传统翻译。

## 模式对比

| 模式 | 标识 | 工作方式 | 适合场景 |
|---|---|---|---|
| 非 LLM | `non_llm` | Bing、Google 或 DeepLX 直接翻译 | 快速、无需 LLM 凭据 |
| 单 LLM | `single_llm` | 一个模型完成翻译，可选反思优化 | 希望沿用原有 LLM 流程 |
| 增强型双角色 LLM | `enhanced_llm` | 主翻译 + 高级校对，含术语与全量审计 | 长内容、术语密集或质量要求较高的项目 |

`--reflect` 只适用于 `single_llm`。增强模式有独立的校对流程，不使用该开关。

## 增强型双角色工作流

增强模式不是让两个模型并列投票，而是把职责拆成两个角色：

1. **主翻译**分层分析完整字幕，生成主题、人物、语域和上下文简报。
2. 主翻译提取疑难术语与候选译法。
3. **高级校对**判断候选是否属于术语，并接受、修正或标记为不确定。
4. 不确定项会补充更多上下文再裁决；随后由用户人工确认，或按非交互策略自动确认。
5. 系统生成可复用的 `.vcglossary.json` 项目术语表。
6. 主翻译按 token 预算切分字幕，为每批注入全文简报、相关术语与前后边界语境。
7. 高级校对覆盖全部原文和译文，输出 Markdown 质量审计报告。

本地审计还会检查空译文、原文照抄，以及数字、URL、占位符、标签和代码等
protected tokens。选择“自动修复客观问题”时，模型建议只有通过确定性校验后才会
应用；自然度、语域等主观问题只记录在报告中。

## GUI 配置

在 **设置 → 翻译设置** 中：

1. 选择“增强型 LLM”。
2. 为主翻译和高级校对分别选择命名模型方案。
3. 按需编辑两个角色各自的 Prompt。
4. 选择术语确认方式：
   - **人工确认**：在术语页选择主翻译建议、校对结论、自定义译法或忽略。
   - **自动确认**：适合批量或无人值守任务。
5. 选择审计策略：
   - **仅报告**：不自动改写审计发现。
   - **自动修复客观问题**：只应用通过本地校验的候选。
6. 可导入同一项目此前生成的 `.vcglossary.json`。

GUI 可以让两个角色使用不同模型方案，也允许复用同一方案。

### 原语言与目标语言

翻译工作区会始终要求选择**目标语言**，不会提供“自动”目标语言。原语言可保留为
“自动检测”，或在单 LLM / 增强型双角色 LLM 模式中手动指定；该设置会写入任务快照并
注入各 LLM 阶段的语言约束。

非 LLM（Bing、Google、DeepLX）模式目前固定由服务端自动检测原语言，界面会禁用手动
原语言选择并明确提示。这样不会把 LLM 的原语言偏好误解为传统翻译服务已收到的请求参数；
切换回 LLM 模式后，先前的手动选择仍会保留。

## CLI

```bash
uv run videocaptioner subtitle input.srt \
  --translation-mode enhanced_llm \
  --source-language auto \
  --target-language en \
  --review-prompt "Pay special attention to product terminology."
```

首次运行不需要 `--glossary`，会生成 `【项目术语表】input.vcglossary.json`。后续处理同一
项目时，再把这个已经存在的文件传给 `--glossary`；传入不存在的路径会直接报错。

相关参数：

| 参数 | 说明 |
|---|---|
| `--translation-mode` | `non_llm`、`single_llm` 或 `enhanced_llm` |
| `--translator` | 非 LLM 服务；旧值 `llm` 会选择增强模式 |
| `--glossary FILE` | 导入项目术语表，仅增强模式 |
| `--review-prompt TEXT` | 高级校对 Prompt，仅增强模式 |
| `--reflect` | 单 LLM 反思翻译，仅单 LLM 模式 |
| `--source-language AUTO\|LANG` | LLM 原语言；可用 `auto`、语言代码或语言名称 |
| `--target-language CODE` | BCP 47 目标语言代码 |
| `--main-llm-endpoint` / `--review-llm-endpoint` | 每个角色选择 `chat_completions` 或 `responses` |
| `--main-llm-max-output-tokens` / review 对应项 | 每个角色使用 `auto` 或正整数输出预算 |
| `--main-llm-request-options-json` / review 对应项 | 每个角色的 provider-native JSON 参数 |

CLI 和批量任务不会打开人工术语确认页，会强制使用自动确认和客观问题自动修复策略。
CLI 支持 `[translate.llm.main]` 与 `[translate.llm.review]` 字段级继承；review 继承 main，
main 再继承旧 `[llm]`。没有新 section 时仍把 legacy profile 用于两个角色，保持旧行为。

## 模型方案与 Transport

增强模式使用命名模型方案保存以下信息：

- Transport / dialect
- Base URL 与 API Key
- 模型名称
- 工作上下文预算
- OpenAI Chat/Responses endpoint
- 最大输出 token 与 provider-native 高级 JSON
- 最大并发

GUI 的命名模型方案支持 OpenAI-compatible、Anthropic Messages 和 Gemini transport。
CLI 不读取 GUI 的命名方案文件，而是从 `[llm]` 和两个角色 section 构建不可变 profile。
完整能力测试会分别验证文本和结构化输出；运行时遇到 context limit 时，当前任务会降低
预算重排，但不会静默修改已保存方案或缩小显式 output cap。

详细配置见 [LLM 模型方案](/config/llm)。

## 产物与日志

增强翻译会保留下列项目产物：

- 初版字幕
- `.vcglossary.json` 项目术语表
- Markdown 翻译审计报告
- 按角色和阶段统计的 token usage
- `llm_requests.jsonl` 请求日志

请求日志默认只包含调用元数据。GUI 显式开启内容日志后会额外保存 Prompt 与最终文本，但
不会保存原始响应、高级参数或推理内容；旧版本日志可能仍含字幕全文。

## 失败与回退

- 瞬态 API 错误会有限重试；永久错误不会重复请求。
- JSON/schema/字幕 ID 等机械错误会在固定次数内重新请求。
- context limit 会在当前任务内降低预算并重新规划。
- 必要阶段失败时增强翻译会 fail-fast，不会把不完整结果伪装成成功，也不会继续进入后处理。
- 批量任务中单个文件失败不会阻止后续文件。

---

相关文档：

- [LLM 模型方案](/config/llm)
- [工作流程](/guide/workflow)
- [CLI 参考](/cli)
