# 01 - 工具角色 request_options 字段边界调研（发现文件）

调研范围：`LLMModelProfile` 全字段、`request_options` schema 与消费、`validate_structured_output_compatibility`、各工具角色消费点（断句/优化/连接测试/后处理/配音改写）的最小参数面。所有结论带 file:line，均来自代码实证。

---

## 一、LLMModelProfile 字段全表

定义位置：`videocaptioner/core/llm/models.py:121-136`（`@dataclass(frozen=True) class LLMModelProfile`）。持久化在 `videocaptioner/core/llm/profiles.py:78`（`LLMModelProfileStore`，schema v2，`profiles.py:17-18`）。

| 字段 | 类型 | 默认值 | 语义 | 翻译路径是否消费 |
|---|---|---|---|---|
| `profile_id` | `str` | 必填 | profile 唯一 ID，1-64 位小写 ASCII `[a-z0-9][a-z0-9._-]{0,63}`（models.py:36, 139-142） | 是——gateway 适配器/信号量缓存键（gateway.py:61-66） |
| `name` | `str` | 必填 | 显示名，1-80 可打印字符，strip 后落盘（models.py:143-147, 187） | 是——重试日志显示（gateway.py:130） |
| `transport` | `LLMTransport` | 必填 | 传输协议：`openai-compatible` / `anthropic-messages` / `gemini`（models.py:14-17） | 是——adapter 分发（gateway.py:48-55） |
| `dialect` | `ProviderDialect` | 必填 | 服务商方言：generic/openai/deepseek/kimi/glm/qwen/anthropic/gemini（models.py:25-33） | 是——决定结构化输出策略（adapters.py:420-432） |
| `base_url` | `str` | 必填 | API 端点，strip 后存储（models.py:161-164, 188） | 是——adapter 客户端构造（adapters.py:347-350） |
| `api_key` | `str` | 必填 | 凭证（models.py:165-166） | 是——同上 |
| `model` | `str` | 必填 | 模型名，strip 后存储（models.py:167-170, 189） | 是——请求体 `model` 字段（adapters.py:455） |
| `work_context_tokens` | `int` | `65_536` | 工作上下文上限，≥16384（models.py:171-174） | 是——enhanced token 规划（orchestrator.py:463-464, 612）；连接探测 cap 上限（check_llm.py:89） |
| `max_concurrency` | `int` | `4` | 1-50，per-profile 并发信号量（models.py:175-178; gateway.py:67-69） | 是——enhanced 并发上限（orchestrator.py:1381） |
| `openai_endpoint` | `OpenAIEndpoint` | `CHAT_COMPLETIONS` | chat completions vs Responses 端点选择；仅 `openai-compatible` transport 可选 `responses`（models.py:20-22, 152-160） | **是，仅翻译/探测走 adapter 时**（adapters.py:354-356, 554-587） |
| `request_options` | `Mapping[str, JSONValue]` | `{}` | provider-native 请求体补丁，深冻结 JSON，≤64KB、深度≤16（models.py:38-39, 135; freeze_json_object models.py:97-118） | **是，翻译专属消费**（见下节） |
| `max_output_tokens` | `Optional[int]` | `None` | 每请求输出上限；`1 ≤ x < work_context_tokens`（models.py:179-185） | **是，翻译专属消费**（orchestrator.py:646-659, 858-883; llm_translator.py:230） |

v1→v2 迁移：旧 profile 补 `openai_endpoint=chat_completions`、`request_options={}`、`max_output_tokens=None`（profiles.py:46-57）——即 v1 时代这三个字段都不存在，天然是「后来为翻译路径加的字段」的历史证据。

---

## 二、request_options 完整 schema 与合法取值

实现：`videocaptioner/core/llm/request_options.py`。本质是 **provider 请求体的顶层浅合并 patch**：`merge_profile_request_options`（:253-284）先 deepcopy 应用层请求体，浅合并 patch，再恢复受保护路径，最后强制删除全部 temperature 路径。

### 2.1 全局禁止项（与 transport 无关）

- **temperature 全路径禁止**（`_temperature_option_paths` :120-138，校验 :201-208，合并后强制删除 :279-283）：`temperature`、`extra_body.temperature`、`chat_template_kwargs.temperature`、Gemini 的 `generationConfig.temperature`。
- **`$omit` 仅允许 `["temperature"]`**，且仅作历史模板迁移兼容标记（:175-185, 219-222）。
- 结构约束：Responses 端点下 `text` 必须为对象；Gemini 下 `generationConfig` 必须为对象（:187-199）。

### 2.2 应用层保护的路径（profile 不得覆盖，按 transport/endpoint）

| transport/endpoint | 保护路径 | 位置 |
|---|---|---|
| OpenAI chat completions | model, messages, stream, n, tools, tool_choice, parallel_tool_calls, functions, function_call, max_tokens, max_completion_tokens, response_format | `_OPENAI_CHAT_PROTECTED` :23-39 |
| OpenAI Responses | model, input, instructions, stream, background, tools, tool_choice, parallel_tool_calls, max_tool_calls, previous_response_id, conversation, prompt, max_output_tokens, `text.format` | `_OPENAI_RESPONSES_PROTECTED` :41-58 |
| Anthropic Messages | model, messages, system, stream, max_tokens, tools, tool_choice | `_ANTHROPIC_PROTECTED` :60-71 |
| Gemini | model, contents, systemInstruction, cachedContent, tools, toolConfig, `generationConfig.{candidateCount,maxOutputTokens,responseMimeType,responseSchema}` | `_GEMINI_PROTECTED` :73-86 |

### 2.3 合法且当前被「有意图地」消费的取值（= 翻译专属调优的核心证据）

**reasoning effort 路径**（不在任何保护名单内，可自由设置）：
- `reasoning_effort` / `reasoning.effort` / `output_config.effort` —— 仅被翻译降级逻辑枚举：`orchestrator.py:70-74`（`_EFFORT_PATHS`），在输出预算耗尽时被 `_lower_reasoning_options`（orchestrator.py:215-257）降为 `"low"` 后经 `request_options_override` 重发（orchestrator.py:755-772）。

**thinking budget 路径**：
- `thinking.budget_tokens`、`generationConfig.thinkingConfig.thinkingBudget`、`extra_body.thinking_budget`、`extra_body.thinking.budget_tokens`、`chat_template_kwargs.thinking_budget` —— `request_options.py:88-94`（`_THINKING_BUDGET_PATHS`）。两处消费：
  - 翻译降级：orchestrator.py:242-255（压到 `output_cap // 8`）；
  - 连接探测 cap 适配：`known_thinking_budget`（request_options.py:287-295）→ `connection_probe_output_cap`（check_llm.py:85-89）把探测上限抬到 `budget + 512`——这是「为翻译 profile 验证而做的探测适配」。

**其余任意 provider-native 参数**（如 `store`、`metadata`、`top_p` 等）：代码中未发现任何按 key 的显式消费（仅 UI 编辑器有 `store is True` 的提示，TranslationSettingWidget.py:549）。语义上属于「随 profile 的连接层透传」。

**request_options_override**（LLMRequest 字段，models.py:284；adapter 侧 `_effective_profile` adapters.py:310-316）：**仅翻译路径使用**的按请求覆盖通道（orchestrator.py:719-726, 772）。工具角色所有消费点均无此机制。

### 2.4 翻译路径的完整消费方式（对照组）

- single LLM：`llm_translator.py:220-236`——`gateway.complete(profile, LLMRequest(..., max_output_tokens=profile.max_output_tokens, metadata={"stage": "single_llm_translation", "role": "main"}))`。不传 `response_schema`（靠 json_repair），但 request_options/端点/dialect 全部经 adapter 生效。
- enhanced：`orchestrator.py:723-736`——最完整消费：`response_schema` + `max_output_tokens`（cap 阶梯 `_AUTO_OUTPUT_CAP_TIERS` :68）+ `request_options_override` + metadata stage/role；cap 耗尽时联动降 reasoning（:740-810）。
- `validate_structured_output_compatibility` 在 GUI 任务启动时对主翻译/校对 profile 预检（subtitle_thread.py:158-174）。

---

## 三、validate_structured_output_compatibility

位置：`videocaptioner/core/llm/request_options.py:231-250`。

逻辑：
1. `transport` 不是 `ANTHROPIC_MESSAGES` 直接返回（:240-241）；
2. 是 Anthropic 时读取 `request_options.thinking.type`（:242-243），若 `== "enabled"` 抛 `RequestOptionsError`（:244-250）——因为 Anthropic 结构化输出 = 强制 `tool_choice`（adapters.py:685-696），与手动 extended thinking 互斥。

校验的 profile 字段：`transport` + `request_options`（仅 `thinking.type` 一个路径）。**不校验** `max_output_tokens`、`openai_endpoint`、`dialect`。

调用方：
- `adapters.py:332-340`（`LLMAdapter._validate_structured_output_compatibility`）——由 `AnthropicMessagesAdapter.complete` 在 `request.response_schema is not None` 时调用（adapters.py:653-655）；
- `subtitle_thread.py:158-174`（`_validate_enhanced_profile_compatibility`，GUI 对主翻译/高级校对 profile 预检）。

注意语义边界：docstring 明说「Keep the profile usable for ordinary text calls and reject only schema requests」——即该函数是「schema 请求 × profile 组合」的校验，不是 profile 本身的校验。

---

## 四、各工具角色消费点实况

### 4.1 split（断句）

- 调用链：`split.py:357-362`（`split_by_llm(text, model, ...)`）→ `split_by_llm.py:85-89` `call_llm(messages=..., model=model, timeout=30)` → `client.py:171` `get_llm_client()` → **隐式读 `OPENAI_BASE_URL` / `OPENAI_API_KEY`**（client.py:111-113，缺失即抛 ValueError :115-118）。
- model 来源：GUI `subtitle_thread.py:331` `subtitle_config.utility_llm_model or subtitle_config.llm_model`；CLI `cli/commands/subtitle.py:300` `llm_model`（来自 config `llm.model`，:221）。
- 请求形态：纯文本（无 response_format / schema / cap / extra_body）。失败降级为规则断句（split.py:336-343）。
- 实际显式拿到的只有：`model` 字符串 + timeout。连接三要素中 base_url/api_key 全从 env 隐式来。

### 4.2 optimize（优化）

- `optimize.py:245-248` `call_llm(messages=..., model=self.model)`——同样 env 隐式连接。
- model 来源：GUI `subtitle_thread.py:365-374`（utility 优先，无则 raise）；CLI `cli/commands/subtitle.py:324`。
- 请求形态：纯文本，输出靠 `json_repair.loads` 解析（optimize.py:255），无结构化输出契约。

### 4.3 connection_probe（连接测试）

- legacy 入口 `check_llm_connection(base_url, api_key, model)`（check_llm.py:243-267）：**三参全显式**，内部临时拼一个 `OPENAI_COMPATIBLE / GENERIC / CHAT_COMPLETIONS / request_options={} / max_output_tokens=None` 的 `LLMModelProfile`（:258-266）再走 `check_model_profile_connection`。
- `check_model_profile_connection`（:205-240）：完整走 `LLMGateway` + adapter——**这是工具角色中唯一已经完整消费 profile 的消费点**。发送 system `"Return only OK."` + user `"OK"`，`max_output_tokens = connection_probe_output_cap(profile)`（:80-89：`profile.max_output_tokens` 或 4096，被 thinking budget 抬到 `budget+512`，封顶 `work_context_tokens // 2`），metadata `{"stage": "connection_probe", "role": "utility"}`。
- `probe_model_profile_capabilities`（:172-202）：文本 + 结构化双探测（`response_schema` 强制嵌套整数 ID 契约，:31-63），metadata stage 为 `connection_probe_text` / `connection_probe_structured`。
- 调用方：GUI 设置页 `setting_interface.py:1269`；GUI 任务前验证 `subtitle_thread.py:142`（用 utility_* 标量三元组）。

### 4.4 postprocess（字幕后处理，含压缩重译与语义速度修复）

- config 注入：GUI `task_factory.py:528-539`（`PostprocessConfig.llm_model` 从旧 `llm_service` 标量服务槽的 model 字段注入，`llm_model` 为 None 时才回填）；CLI `cli/commands/postprocess.py:159`（`llm.model`）+ **:169-174 直接设置 env**（`OPENAI_API_KEY`/`OPENAI_BASE_URL`）。
- 两个 LLM 消费点：
  - F5 压缩重译：`postprocess/compress.py:135` `call_llm(messages, model=cfg.llm_model or "")`——纯文本 + json_repair（:137），env 隐式连接。
  - 语义速度修复：`speed/semantic.py:279-297` / `:300-331`（`_default_rewriter` / `_default_reviewer`）→ `call_llm(..., model=model, response_format={"type": "json_object"}, timeout=SEMANTIC_LLM_TIMEOUT_SECONDS)`（:283-294, 320-328）。model 由 `postprocess/__init__.py:173` 传 `semantic_model=cfg.llm_model`，再经 `speed/pipeline.py:530-537`。env 隐式连接。**这是工具角色中唯一使用结构化输出（json_object）的请求形态**。
- 侧记：`PostprocessConfig.llm_model` 语义注释明确「由调用方从各自配置注入」（postprocess/config.py:78-79），即 core 层不持有连接信息。

### 4.5 配音改写（dubbing rewriter）

- `dubbing/rewriter.py:29-92`：消费 `DubbingConfig.llm_api_key / llm_api_base / llm_model` 三字段（dubbing/models.py:75-77）。
- `rewriter.py:41` `OpenAI(api_key=..., base_url=...)`——**三处工具角色消费中唯一不走 `get_llm_client()` 也不走 env 的**：直接建独立 SDK client。请求 `chat.completions.create` + `response_format={"type": "json_object"}`（:69-73）。
- 三字段校验 fail-fast（rewriter.py:33-34）。来源：CLI `cli/commands/dub.py:133-135`（config `llm.api_key` / `llm.api_base` / `llm.model`）。GUI 无配音改写入口（grep `DubbingConfig(` / `DubbingTask` 在 `ui/` 下无匹配——GUI 配音走 dubbing pipeline 但未发现 rewrite 配置注入点，未确认）。

### 4.6 LLM 翻译器的 legacy 回退分支

`llm_translator.py:235`：`profile is None` 时 `call_llm(messages=messages, model=self.model)`——翻译路径自身也存在 env 隐式回退（工厂在无 profile 时走这条路）。这是「翻译主/校对 profile 路径已正常工作，明确不动」（map.md Notes）之外的遗留分支，统一后应随 env 中继拆除一并退役。

---

## 五、get_llm_client() 调用方全景

`client.py:104-126`：线程安全单例，只读 `OPENAI_BASE_URL`（经 `normalize_base_url` :81-101 补 `/v1`）和 `OPENAI_API_KEY`。**env 写入点共三处**：

| env 写入点 | 位置 | 值来源 |
|---|---|---|
| GUI 任务启动 | `subtitle_thread.py:149-150`（`_setup_llm_config`） | `utility_llm_base_url or base_url` 等旧标量（:138-140） |
| CLI subtitle | `cli/commands/subtitle.py:222-225` | config `llm.api_key` / `llm.api_base` |
| CLI postprocess | `cli/commands/postprocess.py:169-174` | 同上 |

经 `call_llm` 发请求的模块（全部 env 隐式连接，model 为唯一显式参数）：

| 模块 | 调用点 | 显式传参 | 隐式依赖 env 的参数 |
|---|---|---|---|
| split | `split_by_llm.py:85` | model, timeout | base_url, api_key |
| optimize | `optimize.py:245` | model | base_url, api_key |
| 翻译 legacy 回退 | `llm_translator.py:235` | model | base_url, api_key |
| postprocess 压缩 | `postprocess/compress.py:135` | model | base_url, api_key |
| speed 语义修复 | `speed/semantic.py:283, 320` | model, response_format, timeout | base_url, api_key |

（map.md Shoals 已记录的暗礁：翻译阶段还会中途改写 env 目标——subtitle_thread.py:138-150。）

---

## 六、逐字段分类裁决

裁决三档：**A 翻译专属调优（工具角色必须剥离/覆盖）**、**B 基础设施（工具角色保留）**、**C 仅翻译路径存在（工具角色不适用）**。

| 字段 | 裁决 | 依据 |
|---|---|---|
| `profile_id` | B | gateway 适配器与信号量缓存键（gateway.py:61-66）；与请求语义无关 |
| `name` | B | 日志/任务面板显示（gateway.py:130）；观测性需要 |
| `transport` | B | adapter 分发必需（gateway.py:48-55, adapters.py:344-356）；连接层语义 |
| `dialect` | B | 决定结构化输出策略（adapters.py:420-432）。工具角色中仅 semantic 用 json_object（semantic.py:292, 326——现状硬编码、不经 dialect）；保留 dialect 使工具角色未来走 adapter 时策略正确。注意：dialect 为 GENERIC 时工具角色的 json_object 策略与现状一致（adapters.py:432 兜底即 json_object），无行为漂移 |
| `base_url` / `api_key` / `model` | B | 请求构造必需（adapters.py:347-350, 455）；三个工具角色消费点现状全部显式或隐式需要这三个值 |
| `work_context_tokens` | B（用途不同） | 翻译：token 规划（orchestrator.py:463-464, 612）；工具角色：连接探测 cap 封顶（check_llm.py:89）。断句/优化/后处理现状不消费该值（无批预算规划）。保留无害，探测需要 |
| `max_concurrency` | B（需对齐） | gateway per-profile 信号量（gateway.py:67-69）。工具角色现状用自己的 `thread_num` 线程池（optimize.py:78, split.py 的并发），profile 的 max_concurrency 并未约束它们——迁移后两层并发并存，ticket 02 需决定对齐方式（未确认，属设计决策） |
| `openai_endpoint` | **A（剥离）** | 唯一消费在 adapter 分流（adapters.py:354-356 → `_complete_responses` :554-639）；Responses 保护路径集合（request_options.py:41-58）完全为 Responses 请求体设计。工具角色现状**全部走 chat.completions**（client.py:175, rewriter.py:69, semantic.py 经 call_llm 亦然）。继承 `responses` 会把工具角色请求体切到 `input`/`text.format` 形态，行为漂移大 |
| `request_options` | **A（剥离）** | 全部「有意图」消费都是翻译专属：effort 降级（orchestrator.py:70-74, 215-257）、thinking budget 降级（:242-255）、probe cap 适配（check_llm.py:85-89，服务翻译前验证）。工具角色五处消费点现状**零 request_options**（call_llm 不支持该参数，rewriter 直接 SDK 调用无 extra_body）。继承翻译的 effort/thinking 会给短输出的断句/优化请求引入无谓的推理开销与截断风险 |
| `max_output_tokens` | **A（剥离）** | 翻译专属：single LLM 逐请求传参（llm_translator.py:230）、enhanced cap 阶梯与协同（orchestrator.py:68, 646-659, 858-883）。工具角色现状**无一处设 cap**（call_llm 无此参数；probe 自算 cap）。继承翻译 cap 会全局压低断句/优化输出，长批次输出可能被截断——这是最危险的静默继承项 |

### request_options 内部（若 ticket 02 决定按 key 细拆而非整体剥离）

- effort 三路径（`reasoning_effort` / `reasoning.effort` / `output_config.effort`）：A——仅 orchestrator.py:70-74 枚举消费。
- thinking budget 五路径（request_options.py:88-94）：A——orchestrator 降级 + probe cap 适配（后者服务于翻译验证）。
- 其余任意 provider-native key：无代码级显式消费（未发现按 key 消费者），整体剥离时无需细拆；`$omit`/temperature 禁令由 `prepare_profile_request_options` / `merge_profile_request_options` 全局强制（request_options.py:201-208, 279-283），与剥离正交、剥离后天然合规。

---

## 七、被剥离字段的替代默认值建议

| 剥离字段 | 工具角色替代默认 | 依据 |
|---|---|---|
| `openai_endpoint` | `OpenAIEndpoint.CHAT_COMPLETIONS`（即 dataclass 默认，models.py:134） | 与工具角色现状一致：三处消费点全走 chat.completions（client.py:175, rewriter.py:69）；check_llm_connection legacy 入口现状也是硬编码 CHAT_COMPLETIONS（check_llm.py:261） |
| `request_options` | `{}`（dataclass 默认，models.py:135） | 工具角色现状零 request_options；temperature 保护由 merge 层全局强制，空 patch 天然合规。工具角色不需要 effort 控制：断句/优化是短输出任务，现状从未有过该控制 |
| `max_output_tokens` | `None`（dataclass 默认，models.py:136） | 工具角色现状无 cap。connection probe 自带 `connection_probe_output_cap`（check_llm.py:80-89）：剥离 request_options 后 `known_thinking_budget` 恒返回 None，回退 `min(4096, work_context_tokens // 2)`——该函数无需改动即自洽 |

reasoning effort 剥离后的具体行为：工具角色不发送任何 effort/budget 参数（跟随 provider 默认）。若未来某个工具角色确需控制（如长文本优化），应通过工具角色自己的显式参数（类似 `LLMRequest.request_options_override`），而非继承翻译 profile。

---

## 八、各消费点改造的最小参数面

脱离 env 隐式读取，每个消费点需要**显式**拿到的值：

| 消费点 | 现状显式 | 需要补齐的最小面 | 备注 |
|---|---|---|---|
| split（split_by_llm.py:85） | model, timeout | `base_url`, `api_key`（最低 3 值）；若统一走 gateway/adapter 则直接整份工具 profile | SubtitleSplitter 构造已有 model 参数位（split.py:357），扩为连接三元组或 profile |
| optimize（optimize.py:245） | model | 同上 3 值 | SubtitleOptimizer 构造已有 model（optimize.py:57） |
| postprocess compress（compress.py:135） | model（`cfg.llm_model`） | 同上 3 值 | `PostprocessConfig.llm_model` 是单值字段，需扩为连接描述（postprocess/config.py:78-79 注释已预告「由调用方注入」） |
| speed semantic（semantic.py:283, 320） | model, response_format, timeout | 同上 3 值 | `_default_rewriter(model)` / `_default_reviewer(model)` 是闭包工厂（:279-331），model 单参 |
| dubbing rewriter（rewriter.py:41） | **已全显式**：api_key, api_base, model（DubbingConfig 三字段） | 无需补齐，只需把三元组换成 profile 引用 | 唯一不依赖 env 的工具角色消费点；fail-fast 校验已在 rewriter.py:33-34 |
| connection probe legacy 入口（check_llm.py:243） | **已全显式**：base_url, api_key, model | 直接改为接受/解析 profile | 内部已是拼 profile 再走完整路径（:258-266） |
| GUI `_setup_llm_config`（subtitle_thread.py:133-152） | — | 整段退役：探测改为对工具 profile 的 `check_model_profile_connection`，env 写入拆除 | map.md Shoals 已记录 |
| CLI subtitle / postprocess 的 env 写入（subtitle.py:222-225, postprocess.py:169-174） | — | 整段退役（env 中继拆除放最后，map.md Notes 共识） | |

---

## 九、工具角色 profile 校验建议（对照 validate_structured_output_compatibility）

1. **兜底校验复用** `validate_profile_request_options`（request_options.py:225-228）：工具角色解析层产出 profile 后调用一次，空 `{}` 天然通过；若 ticket 02 允许工具角色保留部分基础设施类 request_options（如 `store`），此函数自动校验保护路径与 temperature 禁令。
2. **结构化输出兼容**：工具角色中只有 speed/semantic 用 `response_format={"type": "json_object"}`（semantic.py:292, 326）。json_object 不依赖 tool_choice，与 Anthropic `thinking.type=enabled` 无冲突——但若工具角色统一走 adapter 并保留 `response_schema` 通道，应直接复用 `validate_structured_output_compatibility`（request_options.py:231-250），无需新写校验。工具 profile 剥离 request_options 后该函数对任何 transport 都恒通过（:240-241 非 Anthropic 早退；Anthropic 且无 thinking.type 亦通过）。
3. **openai_endpoint 强制**：工具角色解析层应显式置 `CHAT_COMPLETIONS`（或允许 responses 但必须走 adapter 的 Responses 分支——后者属 ticket 02 设计决策，未确认）。建议在解析层断言，避免静默继承。
4. **max_output_tokens 显式置 None**：同上，解析层显式覆盖而非信任继承（防止翻译 cap 泄漏进断句/优化）。dataclass 的 `__post_init__` 已保证 None 合法（models.py:179-185）。
5. **fail-fast 指引**（map.md Notes 共识「无绑定且非 LLM 翻译时 fail-fast 指引到工具模型卡」）：校验失败错误信息应指向工具模型卡（翻译设置页新增「工具模型」卡），模式可参照 subtitle_thread.py:173-174 的现有错误包装（`{role_name}模型方案无法用于增强翻译：{exc}`）。

---

## 十、遗留不确定项（标注「未确认」，供 ticket 02/04 处理）

- GUI 配音改写的配置注入点：`ui/` 下 grep 未发现 `DubbingConfig(` 构造或 rewrite 相关注入，仅 CLI dub.py:133-135——GUI 是否根本未暴露配音改写（rewrite_too_long）未确认。
- 工具角色并发对齐：profile 的 `max_concurrency`（gateway 信号量）与现状 `thread_num`（各消费点自己的线程池）是两层并发；统一后如何对齐属 ticket 02 设计决策。
- `call_llm` / `get_llm_client` 的最终去留：若所有消费点迁到 gateway，client.py 整体可退役（含 memoize 缓存与 tenacity 重试，client.py:154-218）——缓存语义（expire=3600）在 gateway 路径无对应物，迁移时需注意断句/优化的去重缓存行为变化。
