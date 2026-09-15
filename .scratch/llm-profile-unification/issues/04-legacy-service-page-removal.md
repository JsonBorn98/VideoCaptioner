Claimed-at: 2026-08-29T16:09:03.410Z
# 04 - 旧服务页移除与凭证并入 profile 编辑器

**构建什么.** 旧服务页移除与凭证并入 profile 编辑器

**阻塞于.** 01

**状态.** resolved


## Answer

共识（2026-08-30 grilling 两轮落定，五问全答）。范围：删除面四层全删——(1) UI 层：llmGroup 整组移除（setting_interface.py:75-78 组创建、:293-447 __createLLMServiceCards 含 22 张凭证卡+服务选择+检查连接按钮、:716-721 入组、:727 布局、:741-751 信号连接、:870-958 三个回调、:963-993 __onLLMServiceChanged、:1256-1275 LLMConnectionThread）；import 清理 :32/:35-36/:42-43（注意 LineEditSettingCard 被转录组共用 :470,:511,:577 不能删）。(2) cfg 键层：ui/common/config.py:111-158 全部 22 个键（llm_service + 7 服务 × api_key/api_base/model）定义删除——用户推翻 2026-08-29「只删 UI 留键」倾向，选择连键一起删。settings.json 里已写入的 LLM.* 键值留盘成死数据、不迁移不清理（qconfig 对未知键容忍）。(3) 装配层（本票必须改，否则删键即崩）：task_factory.py:377-420 subtitle 装配段整段换成调 resolve_utility_profile（ADR-0014 入口）——返回的 profile 展开 utility_llm_base_url/utility_api_key/utility_llm_model 三标量继续喂 SubtitleConfig（env 中继链本票不动，留 ticket 03 拆），主翻译 base_url/api_key/llm_model 继续从 main_profile 来；task_factory.py:528-539 postprocess 的 model_items 映射段删除，llm_model 改从 resolve_utility_profile 注入。(4) 测试层：tests/test_ui/test_translation_setting_widget.py:496-510 断言 llmGroup 存在的用例删改；tests/test_postprocess/test_task_factory.py:49-67 保存/恢复旧键的用例改。新增面两条：(a) 工具模型卡——TranslationSettingWidget 页签（pivot）上方加一张顶层共享卡（非嵌进任何页签，断句/优化在三种翻译模式下都运行），复用 ProfileSelectionCard，新增绑定键 cfg.utility_llm_profile_id，下拉默认项「跟随主翻译模型」=空绑定（resolver 派生路径），选独立方案=覆盖；无主翻译 profile 且未绑独立方案时 resolver fail-fast 指引到本卡（02 票已定）。（02 票 Answer「卡与绑定键归 04」兑现。）(b) profile 编辑器模型发现能力——_ProfileDialog 的 modelEdit 从 LineEdit 升级为可编辑下拉（复用 ui/components/EditComboBoxSettingCard 同款交互），旁加「获取模型列表」按钮，复用 get_available_models(base_url, api_key)（check_llm.py:270）拉取填充，接进现有 ModelContextProbeThread 同款线程模式防 UI 阻塞；拉取按钮失败只 InfoBar 提示不阻塞保存。旧页「检查连接」的能力由编辑器现有 probeButton（测试文本与结构化能力）承接，不再单独保留。凭证不迁移：「从服务商导入」被推翻——新 profile 体系已覆盖旧配置全部信息（7 服务即 base_url+model，profile 表单全部可表达且更强），旧 LLM.* 块留盘死数据，用户需配置时在 profile 编辑器手填。边界交接：消费点迁 gateway、client.py/env 中继拆除、SubtitleConfig utility_llm_* 三标量终局形态归 ticket 03；CLI 侧不动（CLI 不读这些 GUI cfg 键，侦察证实 cli/ 下零引用）；resolve_utility_profile 本体实现归 02→实现阶段（本票只做装配侧调用与 UI）。冲突核查：与 ADR-0014 无冲突——resolver 签名 (store, main_profile_id, utility_profile_id=None) 不依赖旧键，工具模型卡与绑定键本就划归本票；02 票「GUI 装配侧改造归 03」被本票部分前移（删键迫使装配段先接 resolver），已在 03 边界中注明（03=消费点迁 gateway+env 拆除）。顺手修正：02 票会话说要新建「gateway 磁盘缓存设计」票但当时未建成，06 空壳已由并发会话补建，本票会话补齐其正文（02 票 Answer 决策 12-14 为权威）并确认其阻塞 03；误建的 07 重复票已 wontfix。

> 未提升到 ADR：本票决策是旧共识（ADR-0014 划定的体系）在 UI/配置删除面上的执行细节：删除清单、卡放哪、装配段怎么接 resolver。可逆（UI 加回来即可）、删旧 UI 面不会让后来者意外（这正是目的地的直接表达）、无真实替代方案之争（唯一实质推翻「从服务商导入」是丢弃而非迁移，属简化不是权衡）。体系级不可逆决策已由 ADR-0014 承载。
