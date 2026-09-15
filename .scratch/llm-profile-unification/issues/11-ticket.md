# 11 - 断句与字幕优化迁移到工具角色方案

**构建什么.** 字幕断句与字幕优化两个工具角色消费点从旧客户端+环境变量中继迁到模型配置方案体系：构造签名统一为 profile: LLMModelProfile + gateway: Optional[LLMGateway] = None（None 时惰性构造 gateway），照翻译路径既有先例。断句超时保真 30 秒、字幕优化从不传超时（SDK 默认 600 秒）迁移后落 120 秒（gateway 四次重试兑底，与翻译路径语义对齐）。请求全部经 gateway，带阶段/角色标签进请求日志。SubtitleConfig 六个连接标量（base_url/api_key/llm_model 与 utility_llm_base_url/utility_llm_api_key/utility_llm_model）全删，新增 utility_llm_profile: Optional[LLMModelProfile]；GUI cfg 新增 utility_llm_profile_id 绑定键（默认空 = 跟随主翻译方案），任务工厂装配段直接塞方案对象、不展开标量，改经 resolve_utility_profile 解析。任务启动 fail-fast 预检（validate_utility_profile，仅本地校验不发真请求）换到旧 need_legacy_llm 判定同位置。GUI 侧同时落地：无 profile 的 LLM 翻译从静默读环境变量改为带指引 fail-fast；任务线程不再写 OPENAI_BASE_URL/OPENAI_API_KEY 环境变量中继。CLI 侧临时用旧 [llm] 标量构 profile 喂新签名保 CI 绿（14 号票一次性删桥）。

**阻塞于.** 09, 10

**状态.** resolved

- [ ] 断句与字幕优化构造签名统一为 profile: LLMModelProfile + gateway: Optional[LLMGateway] = None，None 时惰性构造，照翻译路径既有先例
- [ ] 断句超时保真 30 秒、字幕优化落 120 秒（经 LLMRequest.timeout 断言）
- [ ] 请求经 gateway 带 stage/role 标签进日志：断句与优化各自己的阶段标签 + role=utility
- [ ] SubtitleConfig 六个连接标量删除，新增 utility_llm_profile: Optional[LLMModelProfile]；主翻译侧继续用 main_llm_profile / review_llm_profile
- [ ] cfg 新增 utility_llm_profile_id 绑定键，默认空 = 跟随主翻译方案（解析器派生路径）
- [ ] GUI 任务工厂装配段改经 resolve_utility_profile 塞方案对象、不展开标量
- [ ] 启动 fail-fast 预检换到旧 need_legacy_llm 判定同位置：仅本地校验不发真请求，无方案时报错文案指向「翻译设置页·工具模型卡」
- [ ] GUI 无 profile 的 LLM 翻译从静默读环境变量改为带指引 fail-fast（不静默回退）
- [ ] GUI 任务线程不再写 OPENAI_BASE_URL/OPENAI_API_KEY 环境变量中继（subtitle_thread 两处）
- [ ] 消费点构造缝测试：注入 fake gateway 观察请求形状，取代环境变量假值+符号打补丁旧缝
- [ ] CLI 侧临时用旧 [llm] 标量构 profile 喂新签名保 CI 绿（14 号票一次性删桥），桥上有注释标记临时性
- [ ] ruff/pyright 干净，全量 -m "not integration" 测试绿
