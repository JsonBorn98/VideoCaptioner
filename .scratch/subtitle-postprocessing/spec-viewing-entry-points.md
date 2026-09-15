# Spec: 显示侧长度策略的用户入口（GUI 控件 + CLI flag/TOML 键）

日期: 2026-09-14
状态: 待拆票
来源: subtitle-postprocessing 票 09 验收发现 #3（「每侧显示模式无用户入口」——核心完整，GUI 绑定表/CLI flag 留白，当时裁决延后的产品决策，本轮决定补做）。grill 轮次 2026-09-14（同会话三轮：范围/GUI 摆放/联动、CLI 暴露面、置灰范围/定名）。

## 问题陈述

字幕后处理的显示侧长度策略（每侧单行限长或自动换行；单行限长侧的目标长度上限与绝对长度上限，按中文/拉丁分设）在核心完整可用且默认生效，但没有任何普通用户可用的入口：GUI 后处理设置页没有对应控件，CLI 没有 flag，TOML 配置文件没有键。用户想切换某侧显示模式或调整每行限长，只能手改 `AppData/postprocess_profiles.json`——这是验收时已知并记录的入口留白。

附带影响：用户在 GUI 里看到「阅读目标」页全是阅读速度（CPS，字/秒）参数，误以为每行字数上限也该在那里（ADR-0020 刻意把这两条轴分开）；显示长度这条轴没有自己的家。

## 方案

给显示侧长度策略补齐三个普通入口，核心配置对象不动：

1. **GUI**：后处理设置页新增 tab「显示限长」，原文侧/译文侧各一组：一个显示模式选择（单行限长/自动换行）+ 四个限长输入框（目标/绝对 × 中文/拉丁）。auto_wrap 侧仅置灰输入框（照常持久化；核心本来就忽略该侧限长）。选中的模式与限长照常经设置页现有的 profile 持久化通道（方案存储 set_field）写入方案，任务开始时快照冻结。
2. **CLI flag**：`--original-display-mode` / `--translated-display-mode`（choices: single_line/auto_wrap），`postprocess` 与 `process` 两条命令都可用（共用同一组后处理选项）。
3. **TOML**：`postprocess.*` 配置组补全 6 个键（2 个显示模式 + 4 个限长值），配置文件与 flag 两条管道并存，按既有覆盖顺序（CLI flag > 环境变量 > 配置文件 > 默认值）。

## 用户故事

1. 作为一名字幕制作者，我想在后处理设置页看到并切换原文侧的显示模式（单行限长/自动换行），以便原文侧是否受每行限长由我决定而不是硬编码。
2. 作为一名字幕制作者，我想对译文侧独立选择显示模式，以便两侧策略互不绑定（US 继承自上轮 spec 的两侧独立承诺）。
3. 作为一名字幕制作者，我想调整单行限长侧的中文目标长度上限，以便在语义完整和显示宽度之间按我的内容类型取平衡。
4. 作为一名字幕制作者，我想调整中文绝对长度上限，以便定义验收硬条件，超过即触发修复重拆。
5. 作为一名字幕制作者，我想独立调整拉丁侧（非中日韩）的目标与绝对长度上限，以便英文台词行不必套用中文字数的口径（折算字符数下 ≈42/50 半角字符的默认）。
6. 作为一名字幕制作者，我想把某侧切到自动换行后看到它的四个限长输入框置灰，以便我清楚当前哪些值参与验收、哪些已不参与（不是被藏起来悄悄失效）。
7. 作为一名字幕制作者，我想在输入框里填出目标 > 绝对的非法组合时被 UI 联动纠正而不是等运行报错，以便随手改值时即时得到合法组合（对齐核心校验：目标不得高于绝对）。
8. 作为一名字幕制作者，我想对「显示限长」tab 的每个值用「恢复默认」复位，以便改坏后能回到出厂值（与其他设置页行为一致）。
9. 作为一名 CLI 用户，我想用 `--original-display-mode auto_wrap` 跳过原文侧的每行限长，以便命令行批处理不用改方案 JSON。
10. 作为一名 CLI 用户，我想在 `process` 全流程命令里用同一组显示模式 flag，以便全流程和独立后处理两条路径一致（对齐 `--speed-semantic-repair` 等既有共用 flag 的模式）。
11. 作为一名自动化运维用户，我想在 TOML 配置文件里写 `postprocess.single_line_target_cjk = 18` 等全部 6 个键，以便长生命周期配置不用每次带 flag，限长微调不必动方案 JSON。
12. 作为一名 GUI 用户，我想切换后处理方案（宽松/均衡/平滑优先/自定义）时显示限长的值跟着方案快照走，以便方案切换后看到的值与该方案冻结值一致（复用 `_onPresetChanged` → `_applyProfileConfig` 现有机制）。
13. 作为一名双字幕语言用户，我想在方案 JSON、TOML、flag 三处看到同一套字段名口径（display_mode / single_line_*），以便排障时三处能对上。

## 实现决策

- **核心零改动**：`PostprocessConfig` 的 6 个显示侧字段、模式校验（目标 ≤ 绝对、模式取值）、`PostprocessProfileStore._CONFIG_FIELDS` 白名单（dataclass 字段自动收齐）全部现成。本 spec 不碰 `core/postprocess/`。
- **GUI 新 tab「显示限长」**：位置在「文本处理」之后，与「阅读目标」（速度轴）并列；tab 内两个分组（原文侧/译文侧），每侧 1 个模式选择 + 4 个数值输入框。不并入「阅读目标」——ADR-0020 刻意分开显示长度（每行字数）与阅读速度（字/秒）两条轴，归并会破坏该决策。
- **GUI qconfig**：`ui/common/config.py` 新增 6 项（OptionsConfigItem ×2 模式 + ConfigItem/RangeConfigItem ×4 限长），默认值取自 `_POSTPROCESS_DEFAULTS`（与核心 PostprocessConfig 单一权威保持一致，组名建议 `SubtitleViewing`）。
- **GUI 持久化**：6 项接入 `_connectPolicyPersistence`/`_applyProfileConfig` 两张绑定表（新增 `_VIEWING_BINDINGS` 或并入既有列表）——每项挂 `_addProfileReset` 复位按钮，写入走 `set_field`（`store.set_field(profile_id, field_name, value)`，白名单已含这 6 个字段名）。
- **置灰联动**：`display_mode` 项 valueChanged → 该侧 4 张卡 setEnabled；仅置灰输入框，值照常持久化（核心忽略该侧限长，旧值保留以便切回 single_line 时恢复）。联动规整仿 `_connectCompensationClamp`（防重入 guard + 写回合法值），保证目标 ≤ 绝对在 UI 层即时成立。
- **CLI flag**：`_add_postprocess_options` 加 `--original-display-mode` / `--translated-display-mode`（choices: single_line/auto_wrap），两命令（postprocess/process）共用该函数自动获得。`_build_cli_overrides` 加 2 行 `_set("postprocess.original_display_mode", ...)`。
- **CLI TOML**：`commands/postprocess.py` 的 `_CONFIG_OVERRIDE_FIELDS` 补 6 个键→字段映射；`cli/config.py` DEFAULTS 补 6 键默认值。`replace(resolved, **overrides)` 现有路径自动消费。
- **术语**：全部沿用 CONTEXT.md 既有词汇（显示侧长度策略/单行限长模式/自动换行模式/目标长度上限/绝对长度上限/折算字符数），无新术语。GUI tab 名「显示限长」为 UI 标签，不进词汇表。

## 测试决策

只测外部行为：flag→config 映射、TOML 键→profile 覆盖、GUI 控件→profile 持久化、置灰联动、方案切换刷新。不测内部函数重数。

- **CLI flag**：`tests/test_cli/test_parser.py::test_postprocess_flags_map_to_dedicated_config` 先例——Namespace 造 flag → `_build_cli_overrides` 断言 `postprocess.*` 键。新增 display-mode 两 flag 的映射断言。
- **CLI TOML 覆盖**：`tests/test_cli/test_postprocess_command.py` 先例（`_CONFIG_OVERRIDE_FIELDS` 消费路径）；补 6 键 section → `replace(resolved)` 断言。
- **GUI**：`tests/test_ui/test_postprocess_interface.py` 先例（offscreen Qt 子进程脚本，`_run_qt_script` 驱动 `PostprocessSettingInterface`）；`tests/test_postprocess/test_profiles.py::test_set_field` 先例（store 白名单）。新增：构造设置页 → 切模式断言置灰态、改值断言 profile JSON 落盘、方案切换断言快照应用。
- **联动校验**：目标>绝对 时 UI 规整（仿 `test_ui_baseline`/补偿 clamp 的断言风格），含核心 PostprocessConfig 校验（已有 `test_viewing_lengths.py::test_invalid_display_mode_is_rejected` 覆盖，不重复）。

## 范围外

- **显式补充资产入口**（票 09 验收发现 #2）：`explicit_assets` 的 CLI flag/GUI 控件——同属「核心完整、入口留白」，但涉及 workspace/manifest 交互面，另行开票。
- **阅读速度参数暴露**：CPS 系列已在 GUI/CLI 完整，不动。
- **subtitle_interface 主任务页**：不做每任务级的显示模式覆写（方案级已覆盖需求）。
- **上游断句/翻译**：显示限长不外泄（ADR-0020：上游只管内容语义分段，永不读取观看限长）。
- **profile JSON 导入导出 UI**：`--speed-profile-file` 已有，不加 GUI 导入导出按钮。
- **每行多语言混排规则**、渲染宽度估算：折算字符数口径不变。

## 补充说明

- 验收口径：GUI 改值 → 起任务 → 后处理结果符合该限长（现有 `test_end_to_end_delivery.py` 的混合模式用例已覆盖核心行为，本 spec 只验入口到核心的传递）。
- 本 spec 不修改 ADR-0020；其「运行代码尚未迁移」备注指显式资产/批量容量等遗留，显示模式核心部分已完成，此处仅补入口。
- GUI tab 数从 7 到 8：新增 tab 的注册、路由 key（建议 `viewing`）与标题「显示限长」。
