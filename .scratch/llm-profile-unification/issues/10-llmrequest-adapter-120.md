# 10 - LLMRequest 超时字段与 adapter 120 秒默认

**构建什么.** 工具角色迁移前先给 gateway 补齐请求级超时能力：LLMRequest 加 timeout: Optional[float] = None 字段，adapter 发请求时以 request.timeout 覆盖 gateway 构造默认 120 秒。OpenAI 兼容 transport 现在不传 timeout、落到 OpenAI SDK 默认 600 秒（连接 5 秒）——本票把它显式收紧为 120 秒构造默认，与 Anthropic/Gemini 适配器现有 120 秒对齐；三 transport 都支持请求级 timeout 覆盖。这个默认值变化同时作用于已走 gateway 的翻译路径（增强翻译、单模型 LLM 翻译），与 spec「gateway 四次重试兑底、与翻译路径语义对齐」一致。后续消费点迁移票（11-13）依赖本票：断句 30 秒、语义修复 60 秒保真靠它传，字幕优化/压缩从不传超时（SDK 默认 600 秒）迁移后落 120 秒也靠它。

**阻塞于.** 无，可立即开始

**状态.** resolved

- [ ] LLMRequest 新增 timeout: Optional[float] = None 字段，默认 None 表示用 adapter 构造默认
- [ ] OpenAI 兼容 transport 构造默认从 SDK 默 CU 600 秒显式收紧为 120 秒，与 Anthropic/Gemini 现有 120 秒对齐
- [ ] 三个 transport（OpenAI 兼容/Anthropic/Gemini）都支持 request.timeout 覆盖构造默认
- [ ] 请求级 timeout 传递路径有直测：不传 timeout 落构造默认，传了则覆盖
- [ ] 不传 timeout 的现有翻译路径请求形状零回归（除超时默认值本身收紧外）
- [ ] ruff/pyright 干净，全量 -m "not integration" 测试绿
## Answer

已实现（commit ac0ff25 主实现 + 300f54a review 修正，TDD，双轴 code-review 后修 2 minor 落第二条提交）。LLMRequest（models.py）加 timeout: Optional[float] = None——None 表示用 adapter 构造默认；__post_init__ 校验正数有限性（拒绝 0/负数/inf/nan/bool/非数值，合并为单条件单消息）。三 transport 全支持请求级覆盖：OpenAI 兼容（adapters.py）构造默认从 SDK 默认 Timeout(connect=5.0, read=600, ...) 显式收紧为 120 秒传入 openai.OpenAI(timeout=...)，与 Anthropic/Gemini 既有 120 对齐；请求级覆盖经 _transport_options() 以 per-request SDK kwarg 传递（已用真实 SDK 验证接受该参数），chat completions 与 responses 两个端点都覆盖，timeout 是传输选项绝不进 HTTP body（extra_body 形状零回归，测试断言）。Anthropic/Gemini 经基类共享 _effective_timeout() 在请求驱动的 session.post 处生效；Gemini 的缓存簿记调用（_prepare_cached_prefix/_delete_cached_content）保持构造默认——它们不隶属于某个请求。120 秒默认收敛为单一 DEFAULT_TIMEOUT_SECONDS 模块常量，三 transport 构造器共引（review 修正：原 120.0 字面量三处复制会静默漂移）。设计判断：timeout 不进 gateway 磁盘缓存键——它是传输层时限而非请求塑形字段，超时错误从不落缓存，缓存命中的是已完成响应体，与内容寻址键 allowlist 设计一致；OpenAI 路径不委托 _effective_timeout 是有意设计——不覆盖时尊重被注入 client 自身的超时配置（fake client 注入场景），native transport 则无条件显式传。直测 7 用例（test_adapters.py）：自建 client 落 120 秒默认、自定义 timeout 保留、chat/responses 端点覆盖传递、Anthropic/Gemini 覆盖+回退默认、LLMRequest 非法 timeout 拒绝。验证：test_llm 193 passed、全量 -m "not integration" 1183 passed 5 skipped、ruff 干净、pyright 0 errors（20 warnings 全在未改动文件）。code-review：spec 轴六条验收全部满足、无 major，2 minor（校验轻度范围蔓延判无害保留、注入 client 路径 self.timeout 不参与请求生效判有意设计）；standards 轴 0 硬违规，3 minor 坏味修 2（120.0 三处字面量收敛为常量、重复 raise 合并），第 3 个（_effective_timeout 与 _transport_options 的 None 检查表面重复）判不修——两处语义不同，委托会静默覆盖注入 client 的自定义超时，是行为劣化。下游票 11-13 的断句 30 秒、语义修复 60 秒保真与优化/压缩落 120 秒现在可依赖本字段。

> 未提升到 ADR：不满足 ADR 三条件：本票是 spec「请求语义」节已定决策（LLMRequest 加 timeout、adapter 以 request.timeout 覆盖构造默认 120 秒）的执行落地，实现层每个决定（字段校验、kwargs 传递方式、常量收敛）逐条可逆且无上下文不会显得意外；120 秒默认值本身在 spec 与后续票 11-13 的超时分配里已是一致约定，无独立权衡需要 ADR 承载。
