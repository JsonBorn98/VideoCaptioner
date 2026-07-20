# Separate LLM transports from provider dialects

LLM 上下文翻译将传输协议与供应商方言分离：OpenAI-compatible、Anthropic Messages 和 Gemini 构成传输层，OpenAI、DeepSeek、Kimi、GLM、Qwen 及未知兼容服务构成方言层。国内兼容服务继续复用 OpenAI-compatible 传输，并由方言层处理专有缓存参数、usage 指标和能力验证；未知方言仍通过规范化稳定前缀尽量利用自动缓存。旧配置一律保留为通用方言，其他方言只能由用户手动选择，系统不根据 URL、模型名或响应自动识别供应商。缓存是接口内部能力：用户只选择接口类型并填写连接信息，不配置缓存键、断点、TTL、预热或资源生命周期；缓存不可用时适配层记录诊断并退回普通请求，不阻断翻译。只有兼容协议无法表达供应商能力时才增加原生传输，这增加了适配与测试成本，但避免为每家模型复制翻译业务逻辑，也避免把“通用兼容”错误等同于“无法缓存”。
