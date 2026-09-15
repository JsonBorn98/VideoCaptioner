Claimed-at: 2026-08-30T05:25:30.131Z
# 05 - CLI 配置面设计

**构建什么.** CLI 配置面设计

**阻塞于.** 02

**状态.** resolved


## Answer

设计共识（2026-08-30 grilling 三轮落定，ADR-0015 承载体系级决策，八条）。(1) TOML [llm] 终局只剩三键：profile_id（主翻译，兼工具派生源）、review_profile_id（enhanced 校对）、utility_profile_id（工具独立绑定，空=派生）——review/utility 命名跟随 GUI cfg 的 main/review/utility_llm_profile_id 先例；翻译侧 inline 表全删：translate.llm.main/review 表、[llm] 标量五键、TRANSLATE_LLM_* 全套 env、OPENAI_*→llm.* 映射，config.py 整条 [llm]→main→review 继承链（build_legacy_llm_profile/_translation_llm_role_config/_build_translation_llm_profile/build_translation_llm_profile(s)/translation_llm_role_allows_empty_api_key，约 240 行）删除；CLI 与 GUI 完全对称，resolver（ADR-0014 入口）单一消费面。(2) CLI 定位宏观共识：纯面向 Agent，人类用户用 GUI——无引导式创建，Agent 直接生成/补全 llm_model_profiles.json，错误输出（一次性 stderr 警告+迁移指引+失败列可用 profile id）服务 agent 自我纠错。(3) env 终局：store 持 key 作凭证唯一来源；唯一保留 VIDEOCAPTIONER_LLM_API_KEY 窄覆盖（只换已解析 profile 的凭证、不动 base_url/model，llm_requests.jsonl 记 key_source=env_override，供 agent 从 CI/env 注入 key 免落盘）；OPENAI_API_KEY 事实标准名不认（防 shell 里给别的工具设的 key 被静默采用，同 Shoals 隐式通道教训）；profile_id 选择覆盖走 VIDEOCAPTIONER_LLM_PROFILE_ID + _REVIEW/_UTILITY 三键（ENV_MAP 惯例）。OpenClaw 式结构/凭证全分离否决——AppData store 本就机器本地永不进 git，其威胁模型不存在，全分离会把 profile 定义劈成两半。(4) 三旗对称：--profile / --review-profile / --utility-profile 挂所有 LLM 消费子命令（subtitle、process、dub、postprocess），优先级旗标 > env > TOML。(5) 命令形式：顶层 videocaptioner profile 组——list / show <id>（掩码 key，agent 要原文直接读 JSON）/ set-default <id>（校验 id 存在，失败列可用 id）；不嵌 config 子命令（store 与 config.toml 各管各的文件）。(6) 旧键可见性：build_config 检测 TOML/env 残留旧键打一次性 stderr 警告+迁移指引，死数据容忍不迁移（类推 GUI settings.json 共识；与 GUI 不同点在 CLI 面向 agent，静默不生效是调试地狱）。(7) 语义对齐 GUI（task_factory.py:53-62、entities.py:790-809 先例）：enhanced 模式 review_profile_id 空 → fail-fast 指引绑定校对 profile（不静默回退 main）；single_llm 不需 review；utility_profile_id 空=从主翻译派生（resolver 语义）。(8) 机械后果随实现落：DEFAULTS [llm] 换三键；config init 的 LLM 三问与模板 LLM 块缩为 profile_id 占位+注释指引；validators.validate_llm/validate_translation_llm 改走 resolve_utility_profile/main profile 解析；doctor 的 llm.api_key/model 检查改为 profile 库非空+三 id 有效；dub.py:133-135 三元组装配改为调 resolve_utility_profile 填 DubbingConfig（ADR-0014 已定 DubbingConfig 收 Optional[LLMModelProfile]，本票只管 CLI 装配侧）。边界交接：utility.py resolver 本体与五消费点迁 gateway 归实现阶段（02/03 票既有边界不变）；gui 侧不动。

> 决策提升：ADR-0015
