# FFmpeg 合成导出

VideoCaptioner 的软字幕路径通过 FFmpeg 复制视频/音频流并加入字幕轨；ASS 硬烧和
圆角背景渲染使用集中编码引擎。GUI 与 CLI 共享硬字幕编码器目录、参数校验和命令构建；
GUI 另提供暂停、继续、停止和实时 Console，CLI 则提供 CPU 2-pass 与原始参数入口，
功能面并不完全相同。

## 字幕模式

| 模式 | 说明 |
|---|---|
| 软字幕 | 复制视频/音频流并把字幕作为可选择轨道封装；最终显示由播放器决定 |
| ASS 硬字幕 | 使用 ASS 样式把字幕永久烧录到画面 |
| 圆角背景硬字幕 | 先渲染圆角背景字幕，再进入统一编码路径 |

双语布局贯穿 SRT 导入、字幕编辑、ASS 输出和视频合成。ASS 样式提供扩展字段以及
1080p、4K、house 等预设。

## 视频编码器

内置目录包含 14 个编码器：

- CPU：x264、x265、SVT-AV1、AOM-AV1、VP9
- NVIDIA NVENC：H.264、HEVC、AV1
- Intel QSV：H.264、HEVC、AV1
- AMD AMF：H.264、HEVC、AV1

另外支持：

- `copy` 视频直通（仅适合软字幕等无需重编码的路径；硬字幕选择时 CLI 会回退 x264）
- 自定义 FFmpeg 编码器名称

目录中出现不代表当前机器一定可用。GUI 会先检查 FFmpeg 是否编译了对应编码器，
再尝试真实的硬件初始化；不可用项会被标记或置灰。

## 质量与码率

- **CQ**：使用编码器原生的 constant-quality 标度；通常数值越低质量越高。
- **ABR**：指定平均视频码率。
- **CPU 2-pass**：CLI 可在 CPU ABR 路径启用两遍编码。
- **高级选项**：preset、tune、profile、level，以及 x264/x265 fast-decode。

CLI 的 `--quality` 是 CQ 数值档位（18/23/28/32），仅在没有显式指定 `--cq` 时作为
回退；显式 `--cq` 优先。选择 `--encode-mode abr` 后改由 `--bitrate` 控制平均码率，
单位为 kbps，CQ 档位不参与码率计算。

不同编码器的 CQ 标度、preset 名称和可用 profile 并不相同。切换编码器后应重新检查
命令预览，不要直接套用另一个编码器的数值。

## 画面、音频与容器

可配置：

- 输出高度（保持宽高比且默认不放大）
- 源帧率、指定 FPS、VFR 或 CFR
- 音频直通，或 AAC / Opus / AC3 / MP3 / FLAC
- 音频码率（CLI 单位为 kbps）
- MP4 或 MKV
- faststart
- 是否保留元数据
- 输出文件名中的高度、编码后端、codec 与质量标识

容器、视频编码器和音频编码器并非任意组合都兼容，最终取决于当前 FFmpeg build。
MP4 适合常见 H.264/H.265 + AAC 组合；需要封装 ASS、Opus、FLAC 或实验性编码组合时，
通常优先选择 MKV，并先检查命令预览或做短片段测试。

## FFmpeg 核心

GUI 会显示当前使用的 FFmpeg 来源：

- **内置核心**：随桌面构建固定，由应用版本管理。
- **用户替换核心**：放在应用的数据目录中，可跨应用升级保留。

当前版本只提供手动替换和打开目录，不包含联网下载器。源码环境会优先使用用户替换的
核心，再回退到项目内置或 PATH 中的 FFmpeg。

## GUI 工作流

1. 选择视频与字幕。
2. 选择软字幕或硬字幕；硬字幕再选择 ASS 或圆角背景。
3. 软字幕选择容器；硬字幕选择编码器、CQ/ABR、画面、音频和容器设置。
4. 运行编码器可用性测试。
5. 在只读命令预览中检查最终 FFmpeg 参数。
6. 按需填写额外参数。
7. 开始任务，并在 FFmpeg Console 查看实时输出。

运行期间可暂停、继续或停止。取消会终止 FFmpeg 并清理未完成的输出文件。

## CLI

基本示例：

```bash
uv run videocaptioner synthesize video.mp4 -s subtitle.srt \
  --subtitle-mode hard \
  --render-mode ass \
  --video-encoder h264_nvenc \
  --encode-mode cq \
  --cq 23 \
  --audio-encoder aac \
  --audio-bitrate 192 \
  --container mp4
```

### 预览命令

```bash
uv run videocaptioner synthesize video.mp4 -s subtitle.srt \
  --subtitle-mode hard \
  --video-encoder x265 \
  --height 1080 \
  --print-command
```

`--print-command` 只打印硬字幕编码引擎将要执行的命令，不启动 FFmpeg。

### 额外参数

```bash
uv run videocaptioner synthesize video.mp4 -s subtitle.srt \
  --subtitle-mode hard \
  --video-encoder x264 \
  --extra-args "-movflags +faststart"
```

`--extra-args` 会把参数追加到硬字幕构建命令中。用户需要自行确保参数不会与结构化选项
冲突。该参数当前不由软字幕封装路径消费；软字幕需要自定义时请使用原始命令。

### 原始命令

```bash
uv run videocaptioner synthesize video.mp4 -s subtitle.srt \
  --raw-ffmpeg "ffmpeg -i video.mp4 -vf subtitles=subtitle.srt -c:v libx264 -c:a copy output.mp4"
```

`--raw-ffmpeg` 按给定 argv 执行，但可执行文件会被强制替换为 VideoCaptioner 管理的
FFmpeg。该模式绕过大部分结构化构建能力，应只在明确了解 FFmpeg 参数时使用。

## 常用参数

除 `--raw-ffmpeg` 外，下列结构化编码参数以硬字幕路径为准；软字幕固定走流复制封装。

| 参数 | 说明 |
|---|---|
| `--video-encoder` | 目录 ID 或自定义编码器 |
| `--encode-mode cq\|abr` | 质量或平均码率模式 |
| `--quality` | 未指定 `--cq` 时使用的 CQ 数值档位 |
| `--cq` / `--bitrate` | CQ 数值或视频码率（kbps） |
| `--two-pass` | CPU ABR 两遍编码 |
| `--preset` / `--tune` / `--profile` / `--level` | 编码器高级选项 |
| `--height` / `--fps` | 输出高度与帧率 |
| `--vfr` / `--cfr` | 帧率模式 |
| `--audio-encoder` / `--audio-bitrate` | 音频设置；码率单位为 kbps |
| `--container mp4\|mkv` | 输出容器 |
| `--faststart` / `--no-faststart` | MP4 faststart |
| `--keep-metadata` / `--no-keep-metadata` | 元数据策略 |
| `--extra-args` | 追加 FFmpeg 参数 |
| `--print-command` | 只打印命令 |
| `--raw-ffmpeg` | 执行原始命令 |

完整实时参数以 `videocaptioner synthesize --help` 为准。

## 能力边界

- “完整可控”指当前公开的结构化编码面和原始参数透传，不代表覆盖 FFmpeg 的全部功能。
- 默认内置 FFmpeg 的实际编码器集合取决于构建；自定义核心可扩展它。
- 硬件编码器需要驱动、GPU 和 FFmpeg 构建同时支持。
- CPU 2-pass 当前只在 CLI 的适用编码路径开放。
- 默认软字幕路径不消费结构化视频/音频编码参数、`--extra-args` 或 `--print-command`。
- 自定义参数和原始命令可能绕过安全校验，使用前应先预览并备份目标文件。

---

相关文档：

- [工作流程](/guide/workflow)
- [字幕样式](/guide/subtitle-style)
- [CLI 参考](/cli)
