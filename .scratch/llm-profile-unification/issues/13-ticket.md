# 13 - 配音改写迁移到工具角色方案

**构建什么.** 配音改写器（rewrite_segments_if_needed）从裸 OpenAI 客户端（直接构造 OpenAI(api_key, base_url)）迁到模型配置方案体系：构造签名统一为 profile: LLMModelProfile + gateway: Optional[LLMGateway] = None（None 时惰性构造 gateway），照翻译路径既有先例。改写响应 {items:[{index,text}]} 形式化为 JSON Schema 挂 LLMRequest.response_schema，按 dialect 自动分档（OpenAI 系 json_schema / DeepSeek 系 forced tool / generic json_object），generic 档与现状 response_format json_object 完全等价、零回归。DubbingConfig 三元组（llm_api_key/llm_api_base/llm_model）换成单一 Optional[LLMModelProfile]；GUI dub 装配改调 resolve_utility_profile 解析方案填 DubbingConfig；请求全部经 gateway，带阶段/角色标签进请求日志。CLI dub 装配临时用旧 [llm] 标量构 profile 喂新签名保 CI 绿（14 号票一次性删桥）。

**阻塞于.** 09, 10

**状态.** resolved

- [ ] 配音改写器构造签名统一为 profile: LLMModelProfile + gateway: Optional[LLMGateway] = None，None 时惰性构造，照翻译路径既有先例
- [ ] DubbingConfig 三连接标量（llm_api_key/llm_api_base/llm_model）删除，换成单一 Optional[LLMModelProfile]
- [ ] 改写响应 {items:[{index,text}]} 形式化为 JSON Schema 挂 LLMRequest.response_schema
- [ ] adapter 按 dialect 自动分档：OpenAI 系 json_schema / DeepSeek 系 forced tool / generic json_object；generic 档请求体与现状 response_format json_object 完全等价、零回归（请求体直测）
- [ ] 请求经 gateway 带 stage/role 标签进请求日志（配音改写阶段标签 + role=utility）
- [ ] GUI dub 装配经 resolve_utility_profile 解析方案填 DubbingConfig（照主翻译装配先例）
- [ ] 消费点构造缝测试：注入 fake gateway 观察请求形状（含 response_schema 挂载）
- [ ] CLI dub 装配临时用旧 [llm] 标量构 profile 喂新签名保 CI 绿（14 号票一次性删桥），桥上有注释标记临时性
- [ ] ruff/pyright 干净，全量 -m "not integration" 测试绿
