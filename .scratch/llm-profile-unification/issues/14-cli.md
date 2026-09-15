# 14 - CLI 配置面坍缩进方案库

**构建什么.** CLI 的 LLM 配置面全坍缩进模型配置方案库（ADR-0015）：TOML [llm] 终局只剩 profile_id / review_profile_id / utility_profile_id 三键引用 GUI 同一个 LLMModelProfileStore。硬切删除：translate.llm.main/review inline 表、[llm] 标量五键、TRANSLATE_LLM_* 全套环境变量、OPENAI_* 到 llm.* 的映射，以及 config.py 整条 [llm]→main→review 继承链（约 250 行旧构建函数）。凭证唯一来源是方案库（含 key），仅留 VIDEOCAPTIONER_LLM_API_KEY 窄覆盖（只换已解析方案的凭证、不动 base_url/model，请求日志记 key_source=env_override）；OPENAI_API_KEY 事实标准名不认作凭证来源。方案选择覆盖走 VIDEOCAPTIONER_LLM_PROFILE_ID 及 _REVIEW/_UTILITY 三键；三面旗子 --llm-profile / --review-profile / --utility-profile 挂所有 LLM 消费子命令（subtitle、process、dub、postprocess）——主旗名 --llm-profile（postprocess/process 的 --profile 已被后处理模板 id 占用，argparse 一号不能两 dest）；优先级：旗标 > 环境变量 > TOML。新增顶层 videocaptioner profile 命令组：list / show <id>（掩码 key，原文直接读方案库文件）/ set-default <id>（校验存在，失败列可用 id），不嵌进 config 子命令。旧键可见性：build_config 检测残留旧键时打一次性 stderr 警告加迁移指引。语义对齐 GUI：增强型翻译方式下 review_profile_id 为空即 fail-fast 指引绑定校对方案；单模型 LLM 翻译不需要校对；utility_profile_id 空 = 从主翻译派生。机械跟随：DEFAULTS 三键；config init 的 LLM 问题与模板缩为 profile_id 占位加注释指引；validators 的 LLM 校验与 doctor 的 LLM 检查改走方案库解析（库非空 + 三 id 有效）；dub 装配改调 resolve_utility_profile 填 DubbingConfig。CLI 侧环境变量写入全删（subtitle/postprocess/transcribe——transcribe 的写入是零读取者的死代码）。11-13 号票的临时构桥删除。CLI 定位纯面向 Agent：无引导式创建，错误输出服务 agent 自我纠错，人类用户用 GUI。

**阻塞于.** 11, 12, 13

**状态.** resolved

- [ ] TOML [llm] 终局只剩三键：profile_id（主翻译，兼工具派生源）、review_profile_id（高级校对）、utility_profile_id（工具独立绑定，空=派生）——命名跟随 GUI cfg 既有先例
- [ ] 硬切删除：translate.llm.main/review inline 表、[llm] 标量五键、TRANSLATE_LLM_* 全套环境变量、OPENAI_* 到 llm.* 的映射，以及 config.py 整条 [llm]→main→review 继承链（约 250 行旧构建函数）
- [ ] 凭证唯一来源是方案库（含 key）；仅留 VIDEOCAPTIONER_LLM_API_KEY 窄覆盖——只换已解析方案的凭证、不动 base_url/model，请求日志记 key_source=env_override；OPENAI_API_KEY 事实标准名不认
- [ ] 方案选择覆盖：VIDEOCAPTIONER_LLM_PROFILE_ID 及 _REVIEW/_UTILITY 三键；优先级旗标 > 环境变量 > TOML
- [ ] --llm-profile / --review-profile / --utility-profile 三面旗子挂所有 LLM 消费子命令（subtitle、process、dub、postprocess）——主旗 --llm-profile：--profile 已被 postprocess 模板 id 占用
- [ ] 新增顶层 videocaptioner profile 命令组：list / show <id>（掩码 key，原文直接读方案库文件）/ set-default <id>（校验存在，失败列可用 id）；不嵌进 config 子命令
- [ ] 增强型翻译方式下 review_profile_id 为空即 fail-fast 指引绑定校对方案（不静默回退主翻译）；单模型 LLM 翻译不需要校对；utility_profile_id 空 = 从主翻译派生
- [ ] CLI 无 profile 的 LLM 翻译 fail-fast 带指引（GUI 侧 11 号票已做，CLI 侧本票闭环），错误指引方案库文件与字段形状（故事 23）——必要时包装 resolve_utility_profile 的 GUI 卡片文案
- [ ] 旧键可见性：build_config 检测 TOML/环境残留旧键时打一次性 stderr 警告加迁移指引加可用方案 id 列表；死数据容忍不迁移
- [ ] validators 的 LLM 校验与 doctor 的 LLM 检查改走方案库解析（库非空 + 三 id 有效）；dub 装配改调 resolve_utility_profile 填 DubbingConfig
- [ ] CLI 侧环境变量写入全删（subtitle/postprocess/transcribe——transcribe 的写入是零读取者的死代码）
- [ ] 11-13 号票的临时构桥删除，CLI 装配统一走三键 + resolve_utility_profile
- [ ] CLI 三键解析、三旗与环境覆盖优先级、profile list/show/set-default 行为、旧键残留警告全有测试覆盖
- [ ] ruff/pyright 干净，全量 -m "not integration" 测试绿
