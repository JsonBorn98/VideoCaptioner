---
status: accepted
---

# CLI LLM 配置面全坍缩进模型配置方案库

CLI 的 LLM 配置（翻译与工具角色）不再保留任何 inline 定义：TOML [llm] 终局只剩 profile_id / review_profile_id / utility_profile_id 三键引用 GUI 同一个 LLMModelProfileStore，translate.llm.main/review inline 表、[llm] 标量五键、TRANSLATE_LLM_* 全套 env、OPENAI_*→llm.* 映射全部硬切，config.py 整条 [llm]→main→review 继承链删除。凭证唯一来源是 store（含 key），仅留 VIDEOCAPTIONER_LLM_API_KEY 窄覆盖（只换凭证不动连接，供 agent 从 CI 注入 key 免落盘）；CLI 定位纯面向 Agent，无引导式创建，新增顶层 profile list/show/set-default 命令组与 --profile/--review-profile/--utility-profile 三旗。替代方案（只迁工具角色保留 inline 翻译表、OpenClaw 式结构/凭证分离）都会让「主 profile」出现多套定义或开出 profile 之外的请求通道，被否决。
