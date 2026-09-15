Autopilot: true
Claimed-at: 2026-09-15T03:19:37.029Z
# 12 - GUI：置灰联动与限长钳制

**构建什么.** auto_wrap 侧仅置灰 4 限长输入框（每侧独立：原文侧 auto_wrap 只置灰原文那 4 个，译文侧不受影响）；值照常持久化（核心忽略该侧限长，旧值保留以便切回恢复）。目标/绝对四值 UI 层即时钳制：仿 _connectCompensationClamp/_onCompensationChanged 模式（防重入 guard、写回合法值、收紧滑块范围），保证 UI 层目标 ≤ 绝对与核心校验一致。

**阻塞于.** 11

**状态.** resolved

- [x] 切模式联动：display_mode 项 valueChanged → 置灰态刷新（口径见 Comments 裁决）；值照常持久化不因置灰跳过
- [x] 目标 > 绝对时 UI 即时规整（仿补偿 clamp 防重入 guard）：规整幂等，合法输入原样返回，不与持久化/应用流程死循环；规整后与核心 PostprocessConfig 校验一致（目标 ≤ 绝对、两上限为正）
- [x] 切回 single_line：输入框恢复可用，旧值保留（不是置灰期间被清掉）
- [x] offscreen 断言：切 auto_wrap 置灰态（裁决口径）、非法组合规整、双 typing 输入框联动范围收紧仿 _onCompensationChanged 风格

## Comments

### 裁决：置灰语义按共享结构实现（2026-09-15，autopilot 会话）

Ticket 字面「原文侧 auto_wrap 只置灰原文那 4 个」预设每侧各 4 个输入框，与核心事实冲突：

- `PostprocessConfig` 只有 6 个显示侧字段：2 模式 + **4 个两侧共享的限长值**（`viewing.py:scan_viewing_lengths` 对每个 single_line 侧用同一组值；`repair.py:_active_single_line_sides` 同）。
- CONTEXT.md 术语「显示侧长度策略」：限长「按**中英文**分别设置」，不按侧分设。
- spec-viewing-entry-points.md 实现决策段锁定「核心零改动 6 字段」「qconfig 6 项」——每侧独立限长需 10 字段，与决策直接矛盾；spec 方案段第 17 行「每侧 4 输入框」与自身实现决策段互相矛盾。
- 票 11 已按「2 模式卡 + 4 共享限长卡」交付、过审、resolved。

**采用的置灰口径**：仅当**两侧均 auto_wrap**（`any_viewing_single_line()` 为 False，限长不参与任何侧验收）时，置灰共享 4 卡的输入框本体（slider + spinBox）；**任一侧仍 single_line 时输入保持可用**（限长仍约束该侧）。仅灰输入框，复位按钮与卡片标题保持可用；值照常持久化，切回 single_line 旧值即恢复。共享值之下「一侧置灰另一侧不灰」会谎报「这些值不参与验收」，故不采用 ticket 字面的每侧独立置灰。曾发灵动岛提问（含「每侧各 4 卡」选项），用户未应答，按 autopilot 语义依证据链裁决；如需翻案改回每侧独立 UI，需同步扩核心字段与票 11 结构。

**用户已拍板（2026-09-15）**：接受共享结构口径（口径 C），不翻案做每侧独立限长。理由：主流差异化场景（原文拉丁/译文中文的不同上限）已由「按语言分设」覆盖；spec 实现决策段明令核心零改动；将来真有需求可作独立 feature 增量扩展（核心加字段对旧 profile JSON 向后兼容）。此裁决定案，后续不再重议。
