Claimed-at: 2026-08-29T16:08:27.525Z
# 02 - 工具角色解析层设计

**构建什么.** 工具角色解析层设计

**阻塞于.** 01

**状态.** resolved


## Answer

设计共识（2026-08-30 grilling 两轮落定，ADR-0014 承载）。Resolver 八条：(1) 新模块 videocaptioner/core/llm/utility.py，GUI/CLI 共用单一入口；(2) 签名 resolve_utility_profile(store, main_profile_id, utility_profile_id=None)——独立工具绑定优先，无则从主翻译 profile 派生，都无则抛异常；(3) 返回单个 LLMModelProfile，无绑定/绑定丢失抛专用异常，错误文案指引「翻译设置页·工具模型卡」，错误处理只写在 resolver 一处；(4) 派生用 dataclasses.replace() 强制剥离翻译专属三字段（openai_endpoint→CHAT_COMPLETIONS、request_options→{}、max_output_tokens→None），九个基础设施字段原样保留；(5) 独立绑定的 profile 同样强制剥离三字段——工具请求形态由 resolver 统一保证，与来源无关（迁到 gateway 后 request_options 会经 adapter 真实合入请求体，静默继承即静默生效）；(6) 绑定丢失（profile 被删）报错不静默回退派生；(7) fail-fast 在任务启动时（need_legacy_llm 同位置），不等到首个请求；(8) 启动预检仅本地校验（validate_profile_request_options + 断言三字段默认），不发真请求——与翻译路径预检强度对齐。请求通道三条：(9) 五处工具消费点（断句/优化/压缩/语义修复/配音改写）全迁 LLMGateway.complete()，client.py 随 env 中继退役（ticket 03 拆除对象就此锁定）；(10) DubbingConfig 三元组（llm_api_key/llm_api_base/llm_model）换成单一 Optional[LLMModelProfile]，rewriter 改走 gateway，CLI dub.py 装配改造归 ticket 05；(11) 并发维持双层（消费点线程池 + gateway 信号量）不对齐，工具 profile 与主翻译共享同 profile_id 信号量为已知已接受副作用，留观测数据后再定。缓存三条：(12) gateway 磁盘缓存覆盖面=全量含翻译（工具角色找回 memoize 平价，翻译路径新增省钱能力；现状缓存键不含 base_url/api_key 的洞由按 profile 做键天然修掉）；(13) 缓存命中也写 llm_requests.jsonl（status:"cache_hit"、stage/role 照常、无 usage）；(14) 新开 research ticket 06「gateway 磁盘缓存设计」，阻塞 ticket 03——缓存落地前 client.py 不退役，避免工具角色缓存空窗。关键事实依据：翻译路径今天本就无磁盘缓存（gateway.complete 无缓存），缓存是新增非平价；call_llm 的 memoize 是 diskcache expire=3600 且键不含连接信息（client.py:190，cache.py:72-105）；GUI 绑定先例 main_llm_profile_id/review_llm_profile_id 在 task_factory.py:354-375 解析成 frozen snapshot。遗留到其他票：GUI/CLI 装配侧改造归 03/05，工具模型卡 UI 与绑定键归 04，缓存键设计/LLMResult 序列化/profile 编辑失效/全局开关联动归 06。

> 决策提升：ADR-0014
