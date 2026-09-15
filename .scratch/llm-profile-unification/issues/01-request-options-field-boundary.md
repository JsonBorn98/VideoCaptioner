Claimed-at: 2026-08-29T15:55:08.867Z
# 01 - 工具角色 request_options 字段边界调研

**构建什么.** 工具角色 request_options 字段边界调研

**阻塞于.** 无，可立即开始

**状态.** resolved


## Answer

调研完成，发现文件：.scratch/llm-profile-unification/research/01-request-options-field-boundary.md（ticket 02/04 请读它）。核心裁决：LLMModelProfile 12 字段中 9 个为基础设施（profile_id/name/transport/dialect/base_url/api_key/model/work_context_tokens/max_concurrency），工具角色保留；openai_endpoint、request_options、max_output_tokens 三个为翻译专属调优，工具角色必须剥离并回退 dataclass 默认（CHAT_COMPLETIONS / {} / None）。剥离清单要点：(1) request_options 内全部有意图消费均属翻译——effort 三路径仅被 orchestrator 降级逻辑枚举（orchestrator.py:70-74,215-257）、thinking budget 五路径仅被翻译降级与 probe cap 适配消费（request_options.py:88-94, check_llm.py:85-89）；(2) max_output_tokens 是最危险静默继承项——工具角色现状无一处设 cap，继承翻译 cap 会全局压低断句/优化输出；(3) openai_endpoint=responses 会把工具请求体切到 input/text.format 形态，行为漂移大。替代默认值：三字段回退 dataclass 默认即可，connection_probe_output_cap 剥离 request_options 后自动回退 min(4096, work_context_tokens//2)，无需改动。消费点最小参数面：split/optimize/compress/semantic 四处现状只显式拿 model、连接全靠 OPENAI_BASE_URL/OPENAI_API_KEY env 隐式（写入点共 3 处：subtitle_thread.py:149-150、cli subtitle.py:222-225、cli postprocess.py:169-174），脱离 env 需补齐 base_url+api_key（或整份工具 profile）；dubbing rewriter 与 check_llm_connection legacy 入口已三参全显式。校验建议：复用 validate_profile_request_options 兜底 + validate_structured_output_compatibility（工具角色剥离后对任何 transport 恒通过），解析层显式断言 endpoint=CHAT_COMPLETIONS 与 max_output_tokens=None。未确认项：GUI 配音改写配置注入点未找到；max_concurrency 与 thread_num 两层并发对齐留 ticket 02。

> 未提升到 ADR：research ticket 产出的是事实清单而非不可逆决策；真正的决策（工具角色解析层设计）在 ticket 02 落地
