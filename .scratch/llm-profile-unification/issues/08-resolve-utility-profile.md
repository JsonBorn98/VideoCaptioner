Claimed-at: 2026-08-30T06:20:42.923Z
# 08 - 工具角色解析器 resolve_utility_profile

**构建什么.** GUI 与 CLI 共用的工具角色单一解析入口（ADR-0014 划定）：调用方传入方案库与主翻译方案 id（可选工具绑定 id），得到一个可直接用于工具请求的模型配置方案——独立工具绑定优先，无绑定则从主翻译方案派生，两者都无则抛带指引的专用异常（错误文案指向「翻译设置页·工具模型卡」），绝不静默回退。无论派生还是独立绑定，解析器一律剥离翻译专属调优三字段（openai_endpoint 回退 CHAT_COMPLETIONS、request_options 回退空、max_output_tokens 回退 None），九个基础设施字段（profile_id/name/transport/dialect/base_url/api_key/model/work_context_tokens/max_concurrency）原样保留——工具请求形态由解析器统一保证，与方案来源无关。绑定丢失（方案已被删除）同样报错。另提供任务启动时的 fail-fast 预检：仅做本地校验（复用既有请求选项校验 + 断言三字段为默认值），不发真请求，与翻译路径预检强度对齐。

**阻塞于.** 无，可立即开始

**状态.** resolved

- [ ] 独立工具绑定优先：有 utility_profile_id 时返回该方案；绑定丢失（方案已删）抛专用异常，不静默回退派生
- [ ] 无绑定从主翻译方案派生：剥离三字段（openai_endpoint→CHAT_COMPLETIONS、request_options→空、max_output_tokens→None），九个基础设施字段原样保留
- [ ] 独立绑定的方案同样强制剥离三字段——请求形态保证与方案来源无关
- [ ] 主翻译与工具绑定都无：抛带指引专用异常，错误文案指向「翻译设置页·工具模型卡」，错误处理只写在解析器一处
- [ ] 启动预检仅本地校验：请求选项校验 + 断言三字段默认，不发真请求；validate_structured_output_compatibility 兜底保留（剥离后对任何 transport 恒通过）
- [ ] 解析器缝直测：以上全部路径有测试覆盖，单张 ticket 塞进一个干净上下文窗口可完成
## Answer

已实现（commit dd9c14d，TDD red→green，双轴 code-review 后修 2 条提交）。新模块 videocaptioner/core/llm/utility.py：resolve_utility_profile(store, main_profile_id, utility_profile_id=None) 是 GUI/CLI 共用的工具角色单一解析入口——独立工具绑定优先（绑定丢失抛 UtilityProfileError 指向「翻译设置页·工具模型卡」，不静默回退派生）；无绑定从主翻译方案派生；两者都无抛带指引专用异常，错误处理只写在解析器一处。派生与独立绑定一律 dataclasses.replace() 剥离翻译专属三字段（openai_endpoint→CHAT_COMPLETIONS、request_options→{}、max_output_tokens→None），九个基础设施字段原样保留，工具请求形态由解析器统一保证与方案来源无关。validate_utility_profile 是任务启动本地预检：复用 validate_profile_request_options + 三字段非默认值抛 UtilityProfileError（code-review 修正：原用裸 assert，python -O 下会被剥离导致预检静默消失，改为公共异常）+ validate_structured_output_compatibility 兜底保留（剥离后对任何 transport 恒通过，Anthropic thinking 冲突随剥离消失——有直测）；不发真请求，与翻译路径预检强度对齐。导出经 core/llm/__init__.py（UtilityProfileError/resolve_utility_profile/validate_utility_profile）。直测 tests/test_llm/test_utility.py 15 用例盖住六条验收全路径：绑定优先（含主翻译为空时绑定仍解析）、绑定丢失不回退、派生剥离逐字段断言（九基础设施字段逐一比对 + 存储方案调优字段不被破坏）、独立绑定同样剥离、都无报错文案含卡片指引、id strip、三 transport（openai/anthropic/gemini）预检全通过、Anthropic 原方案被 structured-output 校验拒绝而剥离后通过的兜底验证、预检对三字段非默认拒绝、预检复用请求选项校验（受保护路径照抛 RequestOptionsError）。验证：test_llm 168 passed、全量 -m "not integration" 1158 passed 5 skipped、ruff/pyright 干净。code-review 结论：spec 轴六条验收全部已满足、无需求缺失无实现错误；两条轻微范围蔓延（id strip、主翻译悬空也报「已不存在」）判定为「绝不静默回退」精神的自然延伸保留不动。遗留上游张力（记给后续 CLI 票）：错误文案硬编码 GUI 卡片指引是票面明文要求，但 spec 故事 23 要求 CLI 错误指引方案库文件——CLI 装配票若不重新包装该异常，故事 23 挂，届时需在 CLI 侧包装或上游改票面。

> 未提升到 ADR：本票是 ADR-0014 已定架构的执行落地，八条 resolver 决策由 02 票落定并提升 ADR-0014 承载；实现细节（异常类型选择、断言改 raise、导出面）逐条可逆，不构成难逆转/无上下文意外/真实体系级权衡任一条
