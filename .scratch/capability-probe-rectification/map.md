## Destination

能力测试升级三态判定、编辑器内新增输出上限探查、任务开始前挂载连接测试——三个行为明确分叉的操作（ADR 0016 三态语义、ADR 0017 行为分叉），凭真 400 错误体驱动判定，绿灯严格等价于「当前配置原样能跑真实任务」。

## Notes

领域：VideoCaptioner（PyQt5 字幕工具）的 LLM 模型配置方案与任务启动链路。每次会话先读 spec.md（.scratch/capability-probe-rectification/spec.md）与 ADR 0016/0017（docs/adr/）。LLMCallError 是模型输出上限事实的唯一携带通道；能力测试测的是用户配置而非模型能力。主 seam 是能力测试模块的 LLM 网关注入点，次 seam 是适配器 session/client 注入点，均不新建。

## Decisions so far

<!-- 索引，每个关闭的 wayfinder decision ticket 一行。用 wayfinder.resolveTicket 追加决策票指针。 -->

## Not yet specified

<!-- 战争迷雾：能感知到但还无法 ticket 的范围内迷雾，随前沿推进而毕业。 -->

## Out of scope

<!-- 范围外：被判定在目的地之外的工作，关闭，永不毕业。 -->
