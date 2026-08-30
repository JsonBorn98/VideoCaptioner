---
status: accepted
---

# 工具角色统一从模型配置方案解析

背景：GUI 与 CLI 长期存在两套独立 LLM 配置（翻译 profile 体系与旧「LLM 服务」槽），断句/优化/连接测试/后处理/配音改写仍消费旧槽并经 OPENAI_* 环境变量中继隐式传参，导致已废弃配置在网关侧静默计费。决定：这些工具角色统一从模型配置方案（profile）体系解析——默认跟随主翻译 profile、可绑独立工具 profile 覆盖，剥离翻译专属 request_options、保留基础设施项；旧服务槽退役为纯凭证存储，CLI 以 llm.profile_id 引用同一 profile 库。理由：消除双轨漂移的唯一治本路径；断句+优化占请求量约 2/3，需要独立"选个便宜模型"的出口；为翻译调优的请求选项（如 reasoning effort max）不应泄漏进工具角色。
