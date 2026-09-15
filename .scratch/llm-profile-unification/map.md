## Destination

任务期所有 LLM 请求——字幕断句、字幕优化、连接测试、字幕后处理、配音改写，GUI 与 CLI 两侧——统一从「模型配置方案」(profile) 体系解析模型与连接；旧【通用 LLM 工具配置】服务槽退役为纯凭证存储（任务期零消费）；llm_requests.jsonl 不再出现 legacy 标签请求；任务面板按阶段显示实际使用的模型，配置漂移在第一个请求即可见。

## Notes

- 领域文档：CONTEXT-TRANSLATION.md（模型配置方案、主翻译模型、高级校对模型、工作上下文上限等术语）；CONTEXT.md（字幕后处理）。相关 ADR：ADR-0010（翻译/校对角色分离）、ADR-0011（provider-native 适配器）、本工程新增的统一解析 ADR。
- 共识决策（2026-08-29 grilling 三轮落定）：CLI 也统一读 profile 库；旧服务页整页移除；旧凭证不做导入、直接丢弃（2026-08-30 ticket 04 推翻「从服务商导入」共识——新 profile 体系已覆盖旧配置全部信息）；settings.json LLM.* 块留盘成死数据、不迁移不清理；存量不迁移、旧槽静默退役；工具角色默认跟随主翻译 profile + 可绑独立工具 profile 覆盖，剥离翻译专属 request_options、保留基础设施项；无绑定且非 LLM 翻译时 fail-fast 指引到工具模型卡；绑定 UI = 翻译设置页新增「工具模型」卡；CLI 用 TOML llm.profile_id + --profile 覆盖，旧 llm.* 标量键硬切；CLI 管理命令最小集 list/show/set-default；观测性随解析层一起做；env 中继拆除放最后。
- .scratch/ 已 gitignore（公开 fork，机器本地数据，不提交不外传）。
- AppData/ 是运行数据（settings.json、llm_model_profiles.json 含 API key），不得提交或外传；AppData/logs/llm_requests.jsonl 是核验 legacy 标签的证据源。
- 翻译主/校对 profile 路径（SINGLE_LLM / ENHANCED_LLM）已正常工作，明确不动。
- 验证：uv run pytest（可 -m "not integration"）、uv run ruff check .、uv run pyright；风格遵循 AGENTS.md（4 空格、100 字符行宽、Ruff import 排序）。

## Decisions so far

<!-- 索引，每个关闭的 wayfinder decision ticket 一行。用 wayfinder.resolveTicket 追加决策票指针。 -->
- [工具角色 request_options 字段边界调研](issues/01-request-options-field-boundary.md) — 工具角色剥离翻译专属三字段（openai_endpoint/request_options/max_output_tokens）回退 dataclass 默认，保留九个基础设施字段；四个 env 隐式消费点需补齐显式连接参数面
- [工具角色解析层设计](issues/02-utility-profile-resolver-design.md) — 工具角色统一走 resolve_utility_profile 单一入口（core/llm/utility.py），派生与独立绑定一律剥离翻译专属三字段，五处消费点全迁 gateway，DubbingConfig 收 profile，缓存覆盖面全量含翻译（新票 06 阻塞 03）
- [旧服务页移除与凭证并入 profile 编辑器](issues/04-legacy-service-page-removal.md) — 旧服务组/cfg 22 键/装配段全删，task_factory 两处改接 resolve_utility_profile，工具模型卡=页签外共享卡+cfg.utility_llm_profile_id 默认跟随主翻译，编辑器模型框升级可编辑下拉+拉取模型列表，「从服务商导入」推翻改直接丢弃
- [gateway 磁盘缓存设计调研](issues/06-gateway-disk-cache-design.md) — gateway 磁盘缓存设计调研：内容寻址键（api_key 全量入键修 memoize 洞 + key_version 逃生门）、只存 text 命中零 usage、编辑失效靠内容寻址自然消解、探测 use_cache=False 双向旁路、cache_hit 日志无 usage、复用全局开关 + 新 llm_gateway 目录——落地清单归 ticket 03
- [CLI 配置面设计](issues/05-cli-profile-config-surface.md) — TOML [llm] 坍缩为 profile_id/review/utility 三键引用 store，inline 表与 TRANSLATE_LLM_*/OPENAI_* env 全硬切，store 持 key+VIDEOCAPTIONER_LLM_API_KEY 窄覆盖，顶层 profile 组+三旗，CLI 纯面向 Agent
- [环境变量中继拆除与旧客户端显式传参](issues/03-env-relay-removal.md) — env 中继拆除九条落定：消费点 profile+gateway 惰性两参、六标量全删换 utility_llm_profile、LLMRequest 加 timeout、semantic/rewriter 升格 response_schema（generic 档零回归）、client.py 整文件退役（normalize_base_url 搬 check_llm）、check_llm_connection 删、legacy 三函数连带消亡、优化/压缩 timeout 收 120s + 无 profile 翻译 fail-fast 两个行为变化
- [工具角色解析器 resolve_utility_profile](issues/08-resolve-utility-profile.md) — 工具角色解析器 resolve_utility_profile 落地 core/llm/utility.py：绑定优先/派生/都无三路全走剥离三字段，本地预检改公共异常防 -O 剥离，15 直测全绿，六验收全满足；上游张力（CLI 故事 23 vs GUI 卡片文案）留给 CLI 装配票
- [gateway 磁盘缓存与缓存命中观测](issues/09-gateway.md) — gateway 磁盘缓存落地：内容寻址键修 api_key 洞+profile 级 cap 洞、只存 text 命中零 usage、cache_hit 日志、探测旁路、fail-open，18 直测全绿
- [LLMRequest 超时字段与 adapter 120 秒默认](issues/10-llmrequest-adapter-120.md) — LLMRequest.timeout 落地：三 transport 支持 120 秒默认+请求级覆盖，OpenAI 兼容默认从 SDK 600 秒收紧对齐

## Not yet specified

<!-- 战争迷雾：能感知到但还无法 ticket 的范围内迷雾，随前沿推进而毕业。 -->

## Out of scope

<!-- 范围外：被判定在目的地之外的工作，关闭，永不毕业。 -->
