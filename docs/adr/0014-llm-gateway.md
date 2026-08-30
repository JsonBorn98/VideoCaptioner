---
status: accepted
---

# 统一工具角色 LLM 解析到模型配置方案并全量迁移 gateway

工具角色（断句/优化/连接测试/后处理/配音改写）与翻译角色统一走模型配置方案（profile）体系：core/llm 新模块 resolve_utility_profile(store, main_profile_id, utility_profile_id=None) 是 GUI 与 CLI 共用的单一解析入口，独立工具绑定优先、无则从主翻译 profile 派生、都无则抛带指引的专用异常（不静默回退）；派生与独立绑定一律 dataclasses.replace() 剥离翻译专属三字段（openai_endpoint/request_options/max_output_tokens 回退 dataclass 默认），绑定丢失即报错。五处工具消费点全部改经 LLMGateway.complete() 发请求，client.py 与 env 中继随之退役；DubbingConfig 三元组换 Optional[LLMModelProfile]；任务启动时仅做本地校验 fail-fast，不发真请求预检。这决定了任务期所有 LLM 请求的观测、重试、transport 支持收敛到 gateway 单通道，替代方案（保留 env 隐式通道、各消费点自行拼连接、或只迁移部分消费点）都会延续配置漂移与 legacy 日志分裂，被否决。gateway 磁盘缓存的键设计与命中观测另行调研（阻塞 client.py 退役）。
