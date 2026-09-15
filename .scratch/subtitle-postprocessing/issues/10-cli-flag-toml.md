Autopilot: true
Claimed-at: 2026-09-15T02:45:40.077Z
# 10 - CLI：显示模式 flag 与 TOML 键

**构建什么.** 把显示侧长度策略接进 CLI 两条入口：--original-display-mode / --translated-display-mode 两个 flag（choices: single_line/auto_wrap），TOML postprocess.* 全 6 键（2 模式 + 4 限长）。_add_postprocess_options 加 2 flag（postprocess/process 两命令自动同享），_build_cli_overrides 加 2 行映射；_CONFIG_OVERRIDE_FIELDS 补 6 键、cli/config.py DEFAULTS 补 6 键，replace(resolved) 现有路径自动消费。验收：flag→postprocess.* 映射、TOML section→resolved 覆盖、非法值 USAGE_ERROR。

**阻塞于.** 无，可立即开始

**状态.** resolved

- [x] flag→postprocess.* 映射测试（parser Namespace 先例），postprocess 与 process 两命令都可用
- [x] TOML 6 键→replace(resolved) 覆盖测试（_CONFIG_OVERRIDE_FIELDS 消费路径），限长 4 值经 TOML 可调（无 flag）
- [x] 非法 flag 值（argparse choices）与非法 TOML 组合（目标>绝对）报 USAGE_ERROR 不静默
- [x] 默认态（无 flag 无 TOML）：键不出现在 overrides，方案 JSON 值生效
