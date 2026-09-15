# 12 - 压缩与语义修复迁移并升格 JSON Schema

**构建什么.** 字幕压缩（后处理 F5 压缩重译）与语义修复（语义修复阶段）两个工具角色消费点迁到模型配置方案体系：构造签名统一为 profile: LLMModelProfile + gateway: Optional[LLMGateway] = None（None 时惰性构造 gateway），照翻译路径既有先例。字幕压缩从不传超时（SDK 默认 600 秒）迁移后落 120 秒；语义修复保真 60 秒。语义修复的两套响应形状——改写窗口的 {window_id, segments:[{cue_id,text}]} 与复核窗口的 {window_id, decision, changed_facts, explanation}——形式化为 JSON Schema 挂 LLMRequest.response_schema；adapter 按 dialect 自动分档（OpenAI 系 json_schema / DeepSeek 系 forced tool / generic json_object），generic 档与现状 response_format json_object 完全等价、零回归；语义修复的多形态响应解析随之简化为纯文本。后处理配置实体收可选方案字段替代模型名字符串。请求全部经 gateway，带阶段/角色标签进请求日志。CLI 侧临时用旧 [llm] 标量构 profile 喂新签名保 CI 绿（14 号票一次性删桥）。

**阻塞于.** 09, 10

**状态.** resolved

- [ ] 字幕压缩与语义修复构造签名统一为 profile: LLMModelProfile + gateway: Optional[LLMGateway] = None，None 时惰性构造，照翻译路径既有先例
- [ ] 字幕压缩超时落 120 秒、语义修复保真 60 秒（经 LLMRequest.timeout 断言）
- [ ] 语义修复两套响应形状（改写窗口 {window_id, segments:[{cue_id,text}]}、复核窗口 {window_id, decision, changed_facts, explanation}）形式化为 JSON Schema 挂 LLMRequest.response_schema
- [ ] adapter 按 dialect 自动分档：OpenAI 系 json_schema / DeepSeek 系 forced tool / generic json_object；generic 档请求体与现状 response_format json_object 完全等价、零回归（请求体直测）
- [ ] 语义修复的多形态响应解析简化为纯文本（JSON 解析），不再手工校验降档读 content
- [ ] 后处理配置实体收可选方案字段（替代模型名字符串），后处理配置实体的模型名字段随之退役
- [ ] 请求经 gateway 带 stage/role 标签进日志（压缩、语义修复各自己的阶段标签 + role=utility）
- [ ] 消费点构造缝测试：注入 fake gateway 观察请求形状（含 response_schema 挂载）
- [ ] CLI 侧临时用旧 [llm] 标量构 profile 喂新签名保 CI 绿（14 号票一次性删桥），桥上有注释标记临时性
- [ ] ruff/pyright 干净，全量 -m "not integration" 测试绿
