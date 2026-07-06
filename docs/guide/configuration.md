# 配置指南

详细的配置选项说明。

## 全局配置

待补充...

## 高级配置

### 字幕后处理（`[subtitle]`）

规则型清理与质量审计选项，写在 CLI 配置文件（`config.toml`）的 `[subtitle]` 表下。
全部默认关闭（`trim_trailing_punct` 除外），不开启时行为与旧版一致。

| 键 | 默认 | 说明 |
|----|------|------|
| `remove_placeholders` | `false` | 删除 `[Music]`/`[音乐]`/`♪` 等占位符 |
| `normalize_quotes` | `false` | 中文引号统一为 `「」`/`『』`，并对中文行清理扩展弱尾标点 |
| `trim_trailing_punct` | `true` | 删除行尾弱标点（复刻旧行为；关闭以保留） |
| `fix_gaps` | `false` | 闭合相邻字幕微小间隙以减少闪烁 |
| `max_gap_ms` | `800` | 闭合的最大间隙（音乐类建议 500） |
| `gap_mode` | `"extend"` | `extend`（前段延长）或 `midpoint`（吸附 3/4 点） |
| `audit_reading_speed` | `false` | 阅读速度 / 时长异常审计（只报告） |
| `max_cps_cjk` | `11.0` | 中文每秒字符硬限 |
| `max_cps_latin` | `20.0` | 外文每秒字符硬限 |
| `comfort_cps_cjk` | `9.0` | 中文舒适 CPS |
| `comfort_cps_latin` | `16.0` | 外文舒适 CPS |
| `min_duration_ms` | `1000` | 感知最短显示时长 |
| `max_duration_ms` | `7000` | 普通字幕最长显示时长 |
| `compress_fast_subtitles` | `false` | 对超速中文行做局部 LLM 压缩重译（需 LLM） |
| `qa_report` | `false` | 生成 `<输出>.qa.md` 质量报告（隐含开启审计） |

对应的命令行开关见 [CLI 参考](/cli#字幕后处理选项可选默认全部关闭)。

---

更多配置细节，请参考：
- [LLM 配置](/config/llm)
- [ASR 配置](/config/asr)
- [翻译配置](/config/translator)
