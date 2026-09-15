# 09 - gateway 磁盘缓存与缓存命中观测

**构建什么.** 相同请求短时间内重跑命中磁盘缓存，不重复花钱：LLMGateway.complete() 加 keyword-only use_cache=True，工具角色找回旧 memoize 的省钱平价，翻译路径新增省钱能力。缓存键为内容寻址 sha256，键素材显式 allowlist 两级——方案侧（transport/dialect/base_url/api_key/model/openai_endpoint/request_options）+ 请求侧（messages/max_output_tokens/response_schema/request_options_override/cacheable_system_prefix），排除纯标识与观测字段；api_key 全量入键（修掉旧 memoize 键不含连接信息的洞）；素材带 key_version 版本串，请求构造逻辑变更即 bump、旧条目全量作废。落盘值只存 text（带值级 schema 标记）；raw 丢弃，usage 不存——命中回放 usage 全 None 的 LLMResult，计费报告只记真实花费。缓存命中写 cache_hit 日志条目：time、request_id、当次请求的阶段与角色标签、status:"cache_hit"、方案 id 与模型名、duration_ms:0，无 usage、无 attempt，include_content 开启时附缓存文本，与成功条目对称。只缓存 complete() 正常返回；异常与 INVALID_RESPONSE 一律不缓存，应用层解析失败不驱逐缓存（同请求重试快速失败省钱，cap/reasoning 升级改变请求即换键真实重试）。能力测试与连接测试传 use_cache=False 双向旁路——测试验证当下真实链路，不读不写缓存。复用全局缓存开关 is_cache_enabled()，lookup 与 store 双侧判定，关 = 完全不读不写；缓存目录用全新 llm_gateway 目录（不与旧 llm_translation 目录混用）。fail-open 铁律：缓存内部一切异常（磁盘满、反序列化失败、损坏条目）捕获降级当未命中/放弃写入，绝不打断请求路径。方案编辑任何请求塑形字段自动换摘要、旧条目不可达（内容寻址构造性消解）；卫生靠 expire=3600 自然回收，不做 tag 索引、不做按方案驱逐。

**阻塞于.** 无，可立即开始

**状态.** resolved

- [ ] 缓存键回归洞测试：两个方案只差 api_key 必须互不命中（旧 memoize 正是漏了连接信息）
- [ ] 全局缓存开关关闭时不读不写；能力测试与连接测试 use_cache=False 双向旁路
- [ ] cache_hit 日志条目形状：time/request_id/stage/role/status="cache_hit"/profile{id,model}/duration_ms=0，无 usage 无 attempt
- [ ] 命中回放 usage 全 None——计费报告零污染
- [ ] 坏载荷（损坏条目/反序列化失败）当未命中；缓存内部异常 fail-open 不打断请求路径
- [ ] key_version bump 后旧条目全量作废
- [ ] 只缓存正常返回：异常与 INVALID_RESPONSE 不缓存；应用层解析失败不驱逐缓存
- [ ] 方案编辑任一请求塑形字段自动换键，旧条目不可达
- [ ] 覆盖面全量含翻译：翻译路径 complete() 默认走缓存
- [ ] conftest 全局禁用缓存下测试默认零缓存副作用，退出时关闭新缓存实例（Windows 句柄先例）
## Answer

已实现（commit 8dc9370 主实现 + 5967faf review 修正，TDD，双轴 code-review 后修 1 major 落第二条提交）。新模块 videocaptioner/core/llm/response_cache.py：GatewayResponseCache——缓存键为内容寻址 sha256（规范 JSON，素材=显式 allowlist 两级：方案侧 transport/dialect/base_url/api_key/model/openai_endpoint/request_options[thaw 后]/max_output_tokens + 请求侧 messages/max_output_tokens/response_schema/request_options_override/cacheable_system_prefix；排除 profile_id/name/work_context_tokens/max_concurrency/metadata/废弃 temperature；素材带 key_version="gateway-cache-v1" 版本串）；api_key 全量入键（修掉旧 memoize 键不含连接信息的洞）。落盘值只存 text（JSON dict 带 schema 标记），raw 丢弃、usage 不存——命中回放 LLMResult(text=...) usage 全 None，计费报告零污染。LLMGateway 构造器加可选 response_cache（默认模块级共享单例 _shared_response_cache，跨实例跨 run 去重），complete() 加 keyword-only use_cache=True：lookup 在信号量与 adapter 之前（命中不进信号量不建 adapter），成功返回后 store；异常与 INVALID_RESPONSE 一律不缓存，应用层解析失败不驱逐。fail-open 铁律：lookup/store 内部一切异常捕获降级当未命中/放弃写入。复用 is_cache_enabled() lookup/store 双侧判定；全新 llm_gateway 缓存目录（cache.py 新实例 + get_gateway_cache，conftest pytest_unconfigure 已注册 close，Windows 句柄先例）。request_logger.py 新增 log_gateway_cache_hit()：time/request_id/当次 stage/role/status:"cache_hit"/profile{id,model}/duration_ms:0，无 usage 无 attempt，include_content 时附缓存文本；与 begin_gateway_request 共享 _base_entry() 骨架（review 修正：原两处五字段逐字重复）。两处测试（check_llm.py 能力测试+连接测试）传 use_cache=False 双向旁路，test_connection_probe 补 use_cache is False 断言。覆盖面全量含翻译：llm_translator.py:223 与 orchestrator.py:732 的 complete() 均默认 use_cache=True 走缓存。直测 tests/test_llm/test_response_cache.py 18 用例盖住十条验收：api_key 回归洞（两方案只差 api_key 互不命中）、profile 级 max_output_tokens 回归洞（review 发现：adapters._effective_output_cap 让方案级 cap 覆盖请求级进请求体，原键漏了它——已补进键+回归测试）、开关关不读不写、use_cache=False 双向旁路、cache_hit 日志条目逐字段形状（含 content 开关两态）、命中回放 usage 全 None、坏载荷/坏 schema 标记当未命中、lookup/store 抛异常 fail-open、KEY_VERSION bump 作废（monkeypatch 模块变量）、异常与 INVALID_RESPONSE 不缓存、model 编辑换键、name/max_concurrency/metadata/temperature 不换键、请求塑形字段（cap/cacheable_system_prefix）换键。验证：test_llm 186 passed、全量 -m "not integration" 1176 passed 5 skipped、ruff 干净、pyright 0 errors（20 warnings 全在未改动文件）。code-review：spec 轴十条验收除发现 1 全部忠实落地、无范围蔓延，发现 1（profile 级 cap 漏键，major）已修；standards 轴 1 硬违规（>100 字符行）+6 坏味已修 5（重复 entry 骨架/死参数/私有成员穿透/KEY_VERSION 惯用法/重复 adapter 类），VALUE_SCHEMA 双保险判可接受保留。实现中撞两坑已记 Shoals：diskcache.Cache 空实例 falsy（or 默认值模式静默丢弃注入缓存）；profile 级 max_output_tokens 静默覆盖请求级（缓存键必入）。

> 未提升到 ADR：本票是 ADR-0014 已定架构（工具角色统一走 gateway）的执行落地：缓存设计八条决策（内容寻址键/api_key 入键/只存 text/测试旁路/fail-open/复用全局开关/新目录/expire 回收）已由 06 票调研落定并提升 ADR-0014 承载，research/06 文件是细节证据。实现层逐条可逆（键素材增删、序列化形状、日志字段）且均无上下文会显得意外，不构成难逆转/无上下文意外/真实体系级权衡任一条。review 发现的 profile 级 cap 入键属键素材修正，不新增权衡。
