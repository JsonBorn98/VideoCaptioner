Autopilot: true
Claimed-at: 2026-09-15T03:04:40.603Z
# 11 - GUI：显示限长 tab 与 qconfig 项

**构建什么.** 后处理设置页新增 tab「显示限长」（路由 viewing，插「文本处理」之后）：原文侧/译文侧各 1 显示模式选择（单行限长/自动换行）+ 4 限长输入框（目标/绝对 × 中文/拉丁）。ui/common/config.py 新增 6 项 qconfig（组名建议 SubtitleViewing，默认取 _POSTPROCESS_DEFAULTS 权威）；持久化接 _connectPolicyPersistence/_persistProfileValue 绑定表（set_field 白名单已含 6 字段名）；方案切换 _applyProfileConfig 同步刷新。

**阻塞于.** 无，可立即开始

**状态.** resolved

- [x] qconfig 6 项默认与核心 PostprocessConfig 权威一致（ui/common/config.py 与 _POSTPROCESS_DEFAULTS 同步注释）
- [x] 新 tab 「显示限长」路由 viewing，插入「文本处理」之后，8 tab 导航完整
- [x] offscreen 驱动：改值→set_field→方案 JSON 落盘；方案切换（_onPresetChanged）快照应用刷新新 tab 全部 6 控件
- [x] 每项挂 _addProfileReset 复位按钮，复位回出厂值
