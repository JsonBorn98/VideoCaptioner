# spec — LLM 配置面统一进模型配置方案体系

来源：[llm-profile-unification 决策地图](map.md)六张决策票坍缩。权威决策记录：ADR-0014（统一工具角色解析并全量迁移 gateway）、ADR-0015（CLI LLM 配置面全坍缩进方案库）；细节证据存于各票 Answer 与 research 文件。

## 问题陈述

任务期的 LLM 请求分裂在两套配置体系里：翻译角色（主翻译模型、高级校对模型）已经走「模型配置方案」体系，但工具角色（字幕断句、字幕优化、字幕后处理、配音改写、连接测试）仍然从旧【通用 LLM 工具配置】服务槽读取连接，靠环境变量中继把 base_url/api_key 偷偷塞进旧客户端。用户视角的后果：

- 我为翻译精心配好了模型配置方案，断句和优化却在用另一套我早就忘了的服务槽，可能还在计费；
- 改了方案，工具角色纹丝不动；改了服务槽，翻译也不受影响——配置漂移要等任务跑到一半才暴露，而日志里混着 legacy 标签根本对不上号；
- 设置页里新旧两套 LLM 配置并存，分不清哪个在生效；
- CLI 侧还有第三套写法（TOML inline 表、环境变量映射），同一个「主模型」有三处定义。

## 方案

任务期所有 LLM 请求——字幕断句、字幕优化、连接测试、字幕后处理、配音改写，GUI 与 CLI 两侧——统一从「模型配置方案」（profile）体系解析模型与连接：工具角色默认跟随主翻译模型对应的方案，可在翻译设置页的「工具模型卡」绑定独立方案覆盖；解析由单一入口 `resolve_utility_profile` 完成，派生与独立绑定一律剥离翻译专属调优字段，保证工具请求形态不被静默污染。旧【通用 LLM 工具配置】服务页与全部配置键整页删除、凭证不迁移；环境变量中继与旧客户端整体退役，五处工具消费点全部改经 LLMGateway 发请求，磁盘缓存与请求日志随之收敛到 gateway 单通道——llm_requests.jsonl 不再出现 legacy 标签请求，每个请求（含缓存命中）都带着阶段、角色、方案 id 与模型名，配置漂移在第一个请求即可见。CLI 的 LLM 配置面坍缩为 TOML 三键引用同一个方案库，纯面向 Agent。

## 用户故事

### GUI 用户

1. 作为一个 GUI 用户，我想要翻译设置页有一张「工具模型」卡，以便一眼看到断句、字幕优化、语义修复、配音改写这些工具角色实际用哪个模型配置方案。
2. 作为一个 GUI 用户，我想要工具模型卡默认「跟随主翻译模型」，以便配好翻译就等于配好一切、零额外操作开始任务。
3. 作为一个 GUI 用户，我想要在工具模型卡绑定一个独立的模型配置方案，以便工具角色用更快或更便宜的模型而翻译质量不受影响。
4. 作为一个 GUI 用户，当我绑定的方案已被删除时，我想要任务启动即报错并指引进工具模型卡，以便立刻修正而不是任务中途才失败。
5. 作为一个 GUI 用户，当主翻译和工具绑定都不存在时，我想要带指引的明确报错，以便知道去哪里创建方案。
6. 作为一个 GUI 用户，我想要旧「通用 LLM 工具配置」服务页从设置页彻底消失，以便不再有两套 LLM 配置让我猜哪个生效。
7. 作为一个 GUI 用户，我想要在方案编辑器里点一下就拉取该服务的模型列表，以便不用手敲模型名也不会敲错。
8. 作为一个 GUI 用户，当模型列表拉取失败时，我想要一个提示但不阻塞保存，以便离线也能先配好方案。
9. 作为一个 GUI 用户，我想要每个 LLM 请求（包括缓存命中）都记进请求日志并带上阶段、角色、方案 id 与模型名，以便出问题时能核对每个阶段实际用的模型。
10. 作为一个 GUI 用户，我想要「测试连接」总是真实发起请求而不读缓存，以便它不会拿一小时前的旧结果给我假信号。
11. 作为一个 GUI 用户，我想要相同请求在短时间内重复时命中磁盘缓存，以便调试重跑不重复花钱。
12. 作为一个 GUI 用户，我想要缓存遵循现有的全局缓存开关，以便关掉缓存时它既不读也不写。
13. 作为一个 GUI 用户，我想要在没配任何方案的情况下选择 LLM 翻译方式时得到 fail-fast 指引，而不是静默读环境变量继续跑，以便不会用错模型还以为一切正常。
14. 作为一个 GUI 用户，我想要字幕优化与压缩的请求超时收敛到与翻译一致的语义（默认 120 秒、带重试兜底），以便挂死的请求不再拖满十分钟。

### 自动化 Agent（CLI 使用者）

15. 作为一个自动化 Agent，我想要 TOML 的 LLM 配置坍缩为 profile_id / review_profile_id / utility_profile_id 三键引用方案库，以便一份配置同时驱动翻译与工具角色。
16. 作为一个自动化 Agent，我想要 --llm-profile / --review-profile / --utility-profile 三面旗子挂所有 LLM 消费子命令（subtitle、process、dub、postprocess）——主旗名 --llm-profile，因 postprocess/process 的 --profile 已被后处理模板 id 占用——以便单次调用临时切换方案而不用改配置文件。
17. 作为一个自动化 Agent，我想要 VIDEOCAPTIONER_LLM_PROFILE_ID 及其 _REVIEW/_UTILITY 变体做环境级覆盖，以便 CI 里按环境选方案。
18. 作为一个自动化 Agent，我想要 VIDEOCAPTIONER_LLM_API_KEY 只覆盖已解析方案的凭证、不动 base_url 与模型，以便从 CI 注入 key 而不落盘，且日志能看出 key 来自环境覆盖。
19. 作为一个自动化 Agent，我想要 videocaptioner profile list / show / set-default 顶层命令组，以便发现、核对与设定默认方案。
20. 作为一个自动化 Agent，我想要 profile show 掩码显示 key 而方案库文件本身可直接读，以便终端输出不泄密、需要原文时又能拿到。
21. 作为一个自动化 Agent，我想要配置里残留旧键时收到一次性 stderr 警告加迁移指引加可用方案 id 列表，以便自我纠正配置而不是掉进静默不生效的调试地狱。
22. 作为一个自动化 Agent，我想要 OPENAI_API_KEY 这类事实标准名不被认作凭证来源，以便宿主 shell 里给别的工具设的 key 不会被静默采用。
23. 作为一个自动化 Agent，我想要错误信息在我该建方案时直接指引方案库文件与字段形状，以便无人值守也能自我修复。

### 维护者

24. 作为一个维护者，我想要五处工具消费点（断句、字幕优化、压缩、语义修复、配音改写）全部经 LLMGateway 发请求，以便观测、重试与 transport 适配收敛到单通道。
25. 作为一个维护者，我想要旧客户端与环境变量中继整文件退役，以便 llm_requests.jsonl 从源头不再产生 legacy 标签条目。
26. 作为一个维护者，我想要工具请求形态由解析器统一保证——无论派生还是独立绑定都剥离翻译专属调优字段（openai_endpoint、request_options、max_output_tokens 回退默认），以便任何消费点都无法静默继承翻译的输出上限或端点形态。
27. 作为一个维护者，我想要缓存键包含全部连接塑形字段（base_url、api_key、model、transport、dialect 等），以便换服务商或换账号永不误命中旧响应。
28. 作为一个维护者，我想要缓存层 fail-open——缓存的一切故障都降级为未命中，以便磁盘问题从不打断请求路径。
29. 作为一个维护者，我想要语义修复与配音改写的结构化输出升格为 JSON Schema 并按 provider 方言自动分档，以便不同服务商拿到各自最高的结构化档位，generic 档行为与现状零回归。
30. 作为一个维护者，我想要工具消费点接受注入的 gateway 实例，以便测试用 fake gateway 观察请求形状，而不是靠环境变量假值和符号打补丁。

## 实现决策

### 工具角色解析层

- 新模块 `core/llm/utility.py`，GUI 与 CLI 共用单一入口 `resolve_utility_profile(store, main_profile_id, utility_profile_id=None)`，返回单个模型配置方案（LLMModelProfile）。
- 解析顺序：独立工具绑定优先；无绑定则从主翻译方案派生；都无则抛带指引的专用异常（错误文案指向「翻译设置页·工具模型卡」）。绑定丢失（方案被删）同样报错，绝不静默回退派生。错误处理只写在解析器一处。
- 派生与独立绑定一律用 `dataclasses.replace()` 强制剥离翻译专属三字段：openai_endpoint 回退 CHAT_COMPLETIONS、request_options 回退空、max_output_tokens 回退 None；九个基础设施字段（profile_id、name、transport、dialect、base_url、api_key、model、work_context_tokens、max_concurrency）原样保留。工具角色请求形态由解析器统一保证，与方案来源无关。
- 剥离 request_options 后，连接探测输出上限自动回退 min(4096, 工作上下文上限 // 2)，无需额外处理。
- 任务启动时 fail-fast 预检（与现有 need_legacy_llm 判定同位置）：仅做本地校验（复用 validate_profile_request_options + 断言三字段为默认值），不发真请求——与翻译路径预检强度对齐。
- validate_structured_output_compatibility 在工具角色剥离后对任何 transport 恒通过，作为兜底校验保留。

### 工具消费点迁移

- 断句、字幕优化、压缩、语义修复、配音改写五处消费点构造签名统一为 `profile: LLMModelProfile + gateway: Optional[LLMGateway] = None`（None 时惰性构造 gateway），照翻译路径既有先例。门面对象与模块级单例方案否决。
- gateway 所有权 per-consumer 惰性，不做任务级共享；并发维持双层（消费点线程池 + gateway 信号量）不对齐，工具方案与主翻译共享同 profile_id 信号量是已接受的副作用。
- SubtitleConfig 六个连接标量（base_url/api_key/llm_model 与 utility_llm_base_url/utility_llm_api_key/utility_llm_model）全删，新增 `utility_llm_profile: Optional[LLMModelProfile]`；主翻译侧继续用已有 main_llm_profile / review_llm_profile。任务工厂装配段直接塞方案对象、不展开标量。
- DubbingConfig 三元组（llm_api_key/llm_api_base/llm_model）换成单一 `Optional[LLMModelProfile]`，配音改写器改走 gateway；CLI dub 装配见下文 CLI 节。

### 请求语义

- LLMRequest 加 `timeout: Optional[float] = None`；adapter 发请求时以 request.timeout 覆盖 gateway 构造默认 120 秒。断句 30 秒、语义修复 60 秒保真；字幕优化与压缩从不传超时（SDK 默认 600 秒）迁移后落 120 秒——gateway 四次重试兜底，与翻译路径语义对齐。
- 语义修复的两套响应形状（改写窗口的 {window_id, segments:[{cue_id,text}]} 与复核窗口的 {window_id, decision, changed_facts, explanation}）和配音改写的 {items:[{index,text}]} 形式化为 JSON Schema 挂 LLMRequest.response_schema；adapter 按 dialect 自动分档（OpenAI 系 json_schema / DeepSeek 系 tool / generic json_object），generic 档与现状 response_format json_object 完全等价、零回归；语义修复的多形态响应解析随之简化为纯文本。

### gateway 磁盘缓存

- 新模块 `core/llm/response_cache.py`（GatewayResponseCache）；LLMGateway 构造器加可选 response_cache（默认模块级共享单例），`complete()` 加 keyword-only `use_cache=True`。
- 缓存键为内容寻址 sha256：键素材 = 显式 allowlist 两级——方案侧（transport/dialect/base_url/api_key/model/openai_endpoint/request_options）+ 请求侧（messages/max_output_tokens/response_schema/request_options_override/cacheable_system_prefix），排除纯标识与观测字段；api_key 全量入键（修掉旧 memoize 键不含连接信息的洞）；素材带 key_version 版本串，请求构造逻辑变更即 bump 全量作废。
- 落盘值只存 text（JSON dict，带值级 schema 标记）；raw 丢弃（零消费者且耦合 SDK 布局）；usage 不存、命中回放全 None 的 LLMUsage——计费报告只记真实花费。
- 只缓存 complete() 正常返回的结果；异常与 INVALID_RESPONSE 一律不缓存。应用层解析失败不驱逐缓存：同请求重试快速失败省钱，cap/reasoning 升级改变请求即换键真实重试。
- 方案编辑失效靠内容寻址构造性消解：编辑任何请求塑形字段自动换摘要、旧条目不可达；卫生靠 expire=3600 自然回收，不做 tag 索引、不做按方案驱逐、不与方案库挂钩。
- 两处探测（能力探测、连接探测）传 use_cache=False 双向旁路——探测验证当下真实链路，不读不写缓存。
- 复用全局缓存开关 is_cache_enabled()，lookup 与 store 双侧判定，关 = 完全不读不写；缓存目录用全新 llm_gateway 目录（不与旧 llm_translation 目录混用），conftest 退出时注册 close。
- fail-open 铁律：缓存内部一切异常（磁盘满、反序列化失败、损坏条目）捕获降级当未命中/放弃写入，绝不打断请求路径。
- 覆盖面全量含翻译：工具角色找回 memoize 平价（键修洞后），翻译路径新增省钱能力。

### 请求观测日志

- 缓存命中写新日志函数 log_gateway_cache_hit()：条目带 time、request_id、当次请求的阶段与角色标签、status:"cache_hit"、方案 id 与模型名、duration_ms:0；无 usage、无 attempt；include_content 开启时附缓存文本，与成功条目对称。
- 旧 legacy 日志三函数（含其 base 条目构造）随旧客户端退役删除——legacy 标签从源头消失。

### GUI：旧服务页移除与工具模型卡

- 旧「通用 LLM 工具配置」服务页四层全删：UI 组与 22 张凭证卡及检查连接按钮与回调线程、cfg 全部 22 个键、装配段旧逻辑、相关测试断言。共用的控件类（如转录组也在用的输入框卡）保留。
- settings.json 已写入的 LLM.* 键值留盘成死数据，不迁移不清理（qconfig 容忍未知键）。
- 凭证不迁移：「从服务商导入」被推翻——新方案体系已覆盖旧配置全部信息，用户需要时在方案编辑器手填。
- 新增工具模型卡：翻译设置页页签区上方的一张顶层共享卡（不嵌进任何页签——断句、优化在三种翻译方式下都运行），复用既有方案选择卡组件，新增绑定键 utility_llm_profile_id；下拉默认项「跟随主翻译模型」即空绑定（解析器派生路径），选独立方案即覆盖。解析器 fail-fast 的错误文案指向这张卡。
- 方案编辑器模型框从纯输入升级为可编辑下拉，旁加「获取模型列表」按钮：复用 get_available_models 按方案的 base_url/api_key 拉取填充，走既有探测线程模式防 UI 阻塞；拉取失败只提示不阻塞保存。旧页「检查连接」的能力由编辑器既有探测按钮承接。

### CLI：配置面坍缩

- TOML [llm] 终局只剩三键：profile_id（主翻译，兼工具派生源）、review_profile_id（高级校对）、utility_profile_id（工具独立绑定，空 = 派生）——命名跟随 GUI cfg 既有先例。
- 硬切删除：translate.llm.main/review inline 表、[llm] 标量五键、TRANSLATE_LLM_* 全套环境变量、OPENAI_* 到 llm.* 的映射，以及 config.py 整条 [llm]→main→review 继承链（约 240 行旧构建函数）。
- 凭证唯一来源是方案库（含 key）；仅留 VIDEOCAPTIONER_LLM_API_KEY 窄覆盖——只换已解析方案的凭证、不动 base_url/model，请求日志记 key_source=env_override。OPENAI_API_KEY 事实标准名不认。OpenClaw 式结构/凭证全分离否决——方案库本就机器本地永不进 git。
- 方案选择覆盖走 VIDEOCAPTIONER_LLM_PROFILE_ID 及 _REVIEW/_UTILITY 三键；三面旗子 --llm-profile / --review-profile / --utility-profile 挂所有 LLM 消费子命令（subtitle、process、dub、postprocess）——主旗名 --llm-profile（--profile 已被 postprocess/process 的后处理模板 id 占用）。优先级：旗标 > 环境变量 > TOML。
- 新增顶层 videocaptioner profile 命令组：list / show <id>（掩码 key，原文直接读方案库文件）/ set-default <id>（校验存在，失败列可用 id）；不嵌进 config 子命令——两个文件各管各的。
- 旧键可见性：build_config 检测 TOML/环境残留旧键时打一次性 stderr 警告加迁移指引；死数据容忍不迁移。
- 语义对齐 GUI：增强型翻译方式下 review_profile_id 为空即 fail-fast 指引绑定校对方案（不静默回退主翻译）；单模型 LLM 翻译不需要校对；utility_profile_id 空 = 从主翻译派生。
- 机械跟随：DEFAULTS 三键；config init 的 LLM 问题与模板缩为 profile_id 占位加注释指引；validators 的 LLM 校验与 doctor 的 LLM 检查改走方案库解析（库非空 + 三 id 有效）；dub 装配改调 resolve_utility_profile 填 DubbingConfig。
- CLI 定位纯面向 Agent：无引导式创建，错误输出服务 agent 自我纠错；人类用户用 GUI。

### 退役清单

- client.py 整文件退役（get_llm_client/call_llm 及其全部私有辅助函数随删）。
- normalize_base_url 搬到 check_llm.py（探测函数的家），四个存活消费者改 import。
- check_llm_connection 三标量旧入口删除；profile 版连接探测保留给方案编辑器探测按钮。
- 环境变量写入五处全删（GUI 任务线程两处、CLI subtitle/postprocess/transcribe——transcribe 的写入是零读取者的死代码）。
- llm_translator 的 call_llm 回退分支与 GUI 重翻译线程的环境写入分支删除。
- 旧 memoize 的 get_llm_cache 在 src 零消费者后删除（memoize 装饰器本体保留——通用工具且有直测）；旧 llm_translation 缓存目录留盘成死运行数据。
- 测试面：测旧客户端私有行为的用例随文件退役删除；测旧 UI 组存在性与旧键保存恢复的用例改写。

## 测试决策

好的测试只测外部行为契约，不测实现细节。三个测试缝：

- **消费点构造缝（最高缝）**：五处消费点接受 `profile + gateway=None`，测试注入 fake gateway，一个测试盖住「任务装配 → 解析 → 请求形状 → 响应处理」整条行为路径。这是取代旧缝（环境变量假值 + 五处符号打补丁 + patch 连接检查）的新缝，旧缝随退役清单删除。
- **解析器缝**：resolve_utility_profile 的契约直测——派生优先级、绑定丢失报错、无方案报错文案、三字段剥离、本地预检。
- **缓存缝**：GatewayResponseCache 与 complete(use_cache=...) 的语义直测。

必测要点：

- 缓存键回归洞测试：两个方案只差 api_key 必须互不命中（旧 memoize 正是漏了连接信息）。
- 全局缓存开关关闭时不读不写；探测路径 use_cache=False 双向旁路。
- cache_hit 日志条目形状（status/stage/role/profile、无 usage）；命中回放 usage 全 None。
- 坏载荷当未命中；key_version bump 后旧条目作废。
- 解析器对派生与独立绑定都断言三字段为默认值。
- 优化/压缩超时落 120 秒、断句 30 秒语义修复 60 秒保真（经 LLMRequest.timeout 断言）。
- 语义修复与配音改写的 response_schema 按 dialect 分档（generic 档请求体与现状等价）。
- SubtitleConfig 六标量删除后任务工厂装配经解析器；工具模型卡绑定键默认空。
- CLI 三键解析、三旗与环境覆盖优先级、profile list/show/set-default 行为、旧键残留警告。
- 无 profile 的 LLM 翻译 fail-fast（GUI 与 CLI 两侧）。

测试先例：gateway 行为（test_gateway.py）、方案库（test_profiles.py）、请求日志形状（test_request_logger.py）、探测（test_connection_probe.py）、请求选项校验（test_request_options.py）。conftest 退出时关闭新缓存实例（Windows 句柄先例）；conftest 已全局禁用缓存，测试默认零缓存副作用。

## 范围外

- 翻译主/校对方案路径（单模型 LLM 翻译与增强型 LLM 上下文翻译的既有解析）已正常工作，明确不动。
- 旧凭证迁移或导入（「从服务商导入」已推翻，直接丢弃）；settings.json 与 TOML 里旧键值的磁盘清理（留盘死数据）。
- 缓存 tag 索引、按方案驱逐、方案库与缓存挂钩（内容寻址 + expire 已覆盖）。
- OpenClaw 式结构/凭证全分离（威胁模型不存在，否决）。
- 双层并发对齐（已接受的副作用，留观测数据后再议）；增强翻译跨 run 缓存命中率（观测指标，不承诺）；diskcache 多进程并发（理论安全，实现阶段如发现问题按发现项处理）。
- CLI 面向人类的引导式创建（GUI 是人类配置面）。

## 补充说明

- **落地顺序硬依赖**：磁盘缓存必须先落地，旧客户端才退役——否则工具角色出现缓存空窗。顺序：response_cache 模块 → gateway use_cache → 探测旁路 → 五消费点迁移 → client.py 与环境变量中继删除。
- **三个明示行为变化**（grilling 三轮已向用户确认）：优化/压缩超时 600 秒收紧为 120 秒；无 profile 的 LLM 翻译从静默读环境变量改为 fail-fast 指引；generic 方言下语义修复/配音改写的结构化输出从手工校验升为 schema 档（json_object 档行为等价、零回归）。
- 地图 Destination 中「任务面板按阶段显示实际使用的模型」由既有阶段/角色标签日志（每个请求含缓存命中都带方案 id 与模型名）加工具模型卡的可见绑定实现，不新造任务面板控件。
- AppData/ 是运行数据（含 API key），不得提交或外传；AppData/logs/llm_requests.jsonl 是核验 legacy 标签消失的证据源。
- 验证命令：uv run pytest（可 -m "not integration"）、uv run ruff check .、uv run pyright；风格遵循 AGENTS.md。
