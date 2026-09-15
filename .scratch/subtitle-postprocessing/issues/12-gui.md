# 12 - GUI：置灰联动与限长钳制

**构建什么.** auto_wrap 侧仅置灰 4 限长输入框（每侧独立：原文侧 auto_wrap 只置灰原文那 4 个，译文侧不受影响）；值照常持久化（核心忽略该侧限长，旧值保留以便切回恢复）。目标/绝对四值 UI 层即时钳制：仿 _connectCompensationClamp/_onCompensationChanged 模式（防重入 guard、写回合法值、收紧滑块范围），保证 UI 层目标 ≤ 绝对与核心校验一致。

**阻塞于.** 11

**状态.** ready-for-agent

- [ ] 切模式联动：display_mode 项 valueChanged → 该侧 4 输入框 setEnabled；每侧独立，另一侧不受影响；值照常持久化不因置灰跳过
- [ ] 目标 > 绝对时 UI 即时规整（仿补偿 clamp 防重入 guard）：规整幂等，合法输入原样返回，不与持久化/应用流程死循环；规整后与核心 PostprocessConfig 校验一致（目标 ≤ 绝对、两上限为正）
- [ ] 切回 single_line：输入框恢复可用，旧值保留（不是置灰期间被清掉）
- [ ] offscreen 断言：两侧各切 auto_wrap 互不影响、非法组合规整、双 typing 输入框联动范围收紧仿 _onCompensationChanged 风格
