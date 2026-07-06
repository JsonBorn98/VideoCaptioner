# 字幕样式

VideoCaptioner 通过样式预设（`resource/subtitle_style/ass-*.json`）控制烧录字幕的外观。
运行 `videocaptioner style` 可列出全部预设及其字段。

## 渲染模式

- **ASS 模式**（默认）：传统描边/阴影样式，双语分 `Default`（主）/ `Secondary`（辅）两条样式。
- **圆角背景模式**：Pillow 圆角矩形背景。

## ASS 样式字段

| 字段 | 说明 |
|------|------|
| `font_name` / `font_size` | 主样式字体与字号 |
| `primary_color` | 填充色（`#RRGGBB`） |
| `outline_color` / `outline_width` | 描边色与宽度 |
| `bold` | 是否加粗 |
| `spacing` | 字间距 |
| `margin_bottom` | 底部边距（MarginV） |
| `shadow` | 阴影大小（默认 0） |
| `margin_l` / `margin_r` | 左 / 右边距（默认 10） |
| `wrap_style` | ASS 换行模式（默认 1；0=均衡换行，2=不自动换行）。当前仅作为样式元数据保留 |
| `secondary` | 副样式：`{font_name, font_size, color, outline_color, outline_width, spacing, shadow, margin_bottom}`；`margin_bottom` 为 `null` 时沿用主样式 |

样式数值以 720p（`reference_width`/`reference_height`）为基准设计，合成时按目标分辨率等比缩放。
生成的 ASS 头包含 `ScaledBorderAndShadow: yes`，使描边/阴影随分辨率一致缩放，实现一套网格跨分辨率复用。

## 内置 house 预设

`house` 预设采用"双语一体化"理念：主辅同字体（LXGW WenKai）、同近白填充色（`#F8F8F6`），
语言区分仅体现在两种低饱和描边色上（中文深蓝 `#182030`、英文深棕 `#331E15`），无阴影，单层样式。

```bash
videocaptioner synthesize video.mp4 -s sub.srt --subtitle-mode hard --style house
```

## 自定义

使用 `--style-override` 内联覆盖任意字段：

```bash
videocaptioner synthesize video.mp4 -s sub.srt --subtitle-mode hard \
  --style house --style-override '{"outline_width": 3.5, "shadow": 0.5}'
```
