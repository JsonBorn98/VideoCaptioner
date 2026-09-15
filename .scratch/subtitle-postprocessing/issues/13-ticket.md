Autopilot: true
Claimed-at: 2026-09-15T13:07:38.622Z

# 13 - 交付：显示限长入口全组审查+提交

**构建什么.** 全部实现票 resolved 后领这张：对整组 diff 跑 code-review（双轴：编码规范 + spec 符合），修完过修复回审一轮，提交到当前分支。本票使「整组已审查且已提交」成为可判文件事实，不再增加实现范围。

**阻塞于.** 01, 02, 03, 04, 05, 06, 07, 08, 09, 10, 11, 12

**状态.** resolved

- [x] 双轴审查报告已出（规范轴 + spec 符合轴），发现项修复并过修复回审一轮
- [x] 代码库干净：lint/类型检查/全量测试通过，无机器本地数据（tracker/字幕正文/密钥/benchmark 产物）混入
- [x] 提交到当前工作分支（非默认分支需先建分支），不擅自 push，提交信息符合仓库风格（`56ae0fa fix: preserve valid viewing length profile values`）

## 审查记录（2026-09-15）

- 固定点：`master`（merge-base `dd8cb15`）。整组双轴审查覆盖 `master...HEAD` 及当时工作区 diff。
- 规范轴：无硬规则违例；判断类坏味（共享谓词/联动结构）不扩范围处理。曾误报 `.scratch/` 本地数据混入，复核 `.gitignore` 与祖先提交 `1328b6c` 后撤回：spec/ticket/review 已是受跟踪交付物，未见被忽略的运行态混入。
- spec 轴：确认共享四限长值、两侧均 auto_wrap 才置灰输入本体、复位按钮可用的勘误口径已落实。发现 GUI 把核心允许的正整数限长静默夹到 200，修复为 Qt 32 位整数表示范围；并在范围更新后回填 slider/spinBox，防止有效高值方案显示旧裁剪值。票 11 清单同步勾选。
- 修复验证：新增 `test_postprocess_viewing_settings_keep_valid_high_profile_limits`；受影响 UI/workspace 测试 31 passed；`uv run ruff check videocaptioner tests` 通过；`uv run pyright` 0 errors（20 项既有 warnings）；全量 `uv run pytest -m "not integration"` 1554 passed、5 skipped、61 deselected。全仓 Ruff 仍被 master 已有 `.scratch/subtitle-postprocessing/debug/handoff_probe.py` 的 8 个 E402/I001 阻断，生产代码和 tests 范围通过。
- 另有票 09 的 `context` 资产裁决删除同时在工作区，已保持为独立提交边界，不将其当作本票新增实现范围。
