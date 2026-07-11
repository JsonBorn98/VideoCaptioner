# VideoCaptioner CLI

## 安装

普通用户请下载 GitHub Release 桌面包。本 fork 不发布 PyPI 包，`pip install videocaptioner` 不再作为推荐安装方式。

源码开发环境：

```bash
uv sync --python 3.12
uv run videocaptioner --help
```

免费功能（转录、必应/谷歌翻译）无需任何配置，安装后直接使用。
需要桌面版时运行 `videocaptioner-gui`、`videocaptioner gui`，或直接运行无参数的 `videocaptioner`。

---

## 快速开始

```bash
# 语音转字幕（免费）
videocaptioner transcribe video.mp4 --asr bijian

# 翻译字幕（免费必应翻译）
videocaptioner subtitle input.srt --translator bing --target-language en

# 独立处理已经成型的字幕
videocaptioner postprocess input.srt --profile balanced

# 全流程：转录 → 优化 → 翻译 → 后处理 → 合成
videocaptioner process video.mp4 --asr bijian --translator bing --target-language ja

# 给视频加字幕
videocaptioner synthesize video.mp4 -s subtitle.srt --subtitle-mode hard

# 根据字幕生成配音音轨（默认 Edge TTS，无需 API key）
videocaptioner dub subtitle.srt -o dub.wav

# 全流程：视频 → 转录 → 翻译 → 配音视频
videocaptioner process video.mp4 --translator bing --to zh-Hans \
  --dub-only
```

---

## 命令

### `transcribe` — 语音转字幕

将音视频文件转为字幕文件。支持 mp3/wav/mp4/mkv 等格式，视频自动提取音频。

```bash
videocaptioner transcribe <文件> [选项]
```

| 选项 | 说明 |
|------|------|
| `--asr` | ASR 引擎：`bijian`(默认,免费) `jianying`(免费) `faster-whisper` `whisper-api` `whisper-cpp` `mimo-asr` `qwen-local`。bijian/jianying 仅支持中英文，其他语言可用 whisper-api/faster-whisper/MiMo/Qwen |
| `--language CODE` | 源语言 ISO 639-1 代码，如 `zh` `en` `ja`，或 `auto`（默认） |
| `--word-timestamps` | 输出词级时间戳（完整流程会先做内部确定性聚合） |
| `--audio-loudnorm` | 抽取视频音频时启用 EBU R128 loudnorm，适合音量忽大忽小的素材 |
| `--whisper-api-key` | Whisper API 密钥（仅 `--asr whisper-api`） |
| `--whisper-api-base` | Whisper API 地址 |
| `--whisper-model` | Whisper 模型名（whisper-api 默认 whisper-1，whisper-cpp 默认 large-v2） |
| `--mimo-api-key` | MiMo ASR API 密钥（仅 `--asr mimo-asr`） |
| `--mimo-api-base` / `--mimo-model` / `--mimo-timeout` / `--mimo-concurrency` | MiMo ASR API 地址、模型名、超时时间、并发分块请求数 |
| `--qwen-asr-model` / `--qwen-aligner-model` | Qwen3 ASR / ForcedAligner 模型名 |
| `--qwen-model-dir` | 本地 Qwen 模型目录 |
| `--qwen-device` / `--qwen-dtype` | Qwen 运行设备与精度，例如 `cuda:0`、`bfloat16` |
| `--qwen-max-new-tokens` | Qwen 单块最大生成 token 数 |
| `--qwen-chunk-overlap` | Qwen/MiMo 分块重叠秒数 |
| `--qwen-compile-aligner` | 实验性编译 Qwen3-ForcedAligner，失败自动回退 |
| `-o PATH` | 输出文件或目录路径 |
| `--format` | 输出格式：`srt`(默认) `ass` `txt` `json` |

---

### `subtitle` — 字幕优化与翻译

处理已有字幕文件，支持以下步骤：

1. **拆分与断句** — 按现有字数上限和语义边界重组字幕
2. **优化** — 修正 ASR 错误和翻译前文本（LLM）
3. **翻译** — 翻译到其他语言（LLM / 必应 / 谷歌）

该命令只生成可直接使用的初版字幕，不执行标点清理、阅读速度优化、间隙修复或媒体对齐。
普通 cue 级字幕继续沿用原有估算字词时间与重新断句逻辑。默认开启拆分和文本优化，翻译默认
关闭；指定 `--translator` 或 `--target-language` 自动开启翻译。

字幕处理阶段的主结果固定保存为 `【初版字幕】<名称>.srt`。即使输入是 ASS/VTT，
或 `-o` 使用了其他扩展名，阶段交付文件仍会规范为 SRT；输入 ASS 的样式不会继承。
其他观看格式应从完成的字幕工作稿另行导出。

```bash
videocaptioner subtitle <字幕文件> [选项]
```

| 选项 | 说明 |
|------|------|
| `--translator` | 翻译服务：`llm`(默认) `bing`(免费) `google`(免费) |
| `--target-language CODE` | 目标语言 BCP 47 代码：`zh-Hans` `en` `ja` `ko` `fr` `de` 等 |
| `--no-optimize` | 跳过优化 |
| `--no-translate` | 跳过翻译 |
| `--no-split` | 关闭 LLM 智能断句，使用本地快速合并 |
| `--max-cjk N` | CJK 单段最大字符数 |
| `--max-english N` | 英文单段最大单词数 |
| `--reflect` | 反思式翻译（仅 LLM，质量更高但更慢） |
| `--layout` | 双语布局：`target-above` `source-above` `target-only` `source-only` |
| `--prompt TEXT` | 自定义提示词（辅助 LLM 优化/翻译） |
| `--api-key` | LLM API 密钥（或设置 `OPENAI_API_KEY` 环境变量） |
| `--api-base` | LLM API 地址（或设置 `OPENAI_BASE_URL` 环境变量） |
| `--model` | LLM 模型名（如 gpt-4o-mini） |
| `-o PATH` | 规范 SRT 输出文件或目录；其他扩展名会替换为 `.srt` |

### `postprocess` — 独立字幕后处理

接收完整的单语或双语成型字幕，执行标点、阅读速度、结构、时间轴、语义修复和质量验收。
输入文件永不覆盖，默认生成 `【后处理字幕】<名称>.srt`。输入 SRT/VTT/ASS 都会先
规范化为纯文本字幕数据；ASS 样式、定位和特效不会继承。`-o` 使用非 SRT 扩展名时，
扩展名会被替换为 `.srt`。

```bash
videocaptioner postprocess <字幕文件> [选项]
```

| 选项 | 说明 |
|------|------|
| `--layout` | 输入结构：`auto`、`target-above`、`source-above`、`target-only`、`source-only` |
| `--remove-placeholders` | 删除 `[Music]`/`[音乐]`/`♪` 等占位符行 |
| `--normalize-quotes` | 中文引号统一为 `「」`/`『』`，并对中文行清理扩展弱尾标点 |
| `--keep-trailing-punct` | 保留行尾弱标点（关闭默认的尾标点清理） |
| `--speed-optimize` / `--no-speed-optimize` | 显式开启或关闭统一速度优化 |
| `--mode apply\|analyze` | 应用修改，或只分析并生成结果 |
| `--profile ID` | 使用 `loose`/`balanced`/`smooth` 模板或自定义后处理方案 |
| `--speed-profile-file PATH` | 直接使用导出的版本化方案 JSON，不写入应用方案库 |
| `--primary-side translate\|original\|layout` | 选择驱动阅读体验的显示侧 |
| `--media PATH` | 关联可选视频或音频 |
| `--precise-timing` | 对关联媒体运行 ForcedAligner；失败窗口局部降级 |
| `--speed-save-timing-sidecar` | 保存可复用的 `.vctiming.json` 时间证据 |
| `--speed-reference-audit` | 审计参考显示侧，不改写参考文本 |
| `--speed-semantic-repair` / `--no-speed-semantic-repair` | 开关受验证约束的 LLM 局部修复 |
| `--speed-semantic-window N` | 语义修复上下文大小，范围 1-15，默认 5 |
| `--no-speed-llm-review` | 不把确定性校验无法裁决的候选交给 LLM 独立复核 |
| `--qa-report` | 在输出旁生成统一 Markdown 质量报告 |
| `-o PATH` | 规范 SRT 输出路径；其他扩展名会替换为 `.srt` |

### `synthesize` — 字幕视频合成

将字幕烧录到视频中，支持美观的样式化字幕。

```bash
videocaptioner synthesize <视频> -s <字幕> [选项]
```

| 选项 | 说明 |
|------|------|
| `-s FILE` | **必填**，字幕文件 |
| `--subtitle-mode` | `soft`(默认,嵌入轨道) 或 `hard`(烧录画面) |
| `--quality` | 视频质量：`ultra`(CRF18) `high`(CRF23) `medium`(默认,CRF28) `low`(CRF32) |
| `--layout` | 双语字幕布局 |
| `--style NAME` | 样式预设（运行 `videocaptioner style` 查看） |
| `--style-override JSON` | 内联 JSON 覆盖样式字段，如 `'{"outline_color": "#ff0000"}'` |
| `--render-mode` | 渲染模式：`ass`(默认,描边样式) 或 `rounded`(圆角背景) |
| `--font-file PATH` | 自定义字体文件 (.ttf/.otf) |

#### 字幕样式

VideoCaptioner 支持两种渲染模式，让字幕更美观：

**ASS 模式**（默认）— 传统描边/阴影样式，支持自定义字体、颜色、描边宽度：
```bash
# 使用动漫风格预设
videocaptioner synthesize video.mp4 -s sub.srt --subtitle-mode hard --style anime

# 自定义红色描边
videocaptioner synthesize video.mp4 -s sub.srt --subtitle-mode hard \
  --style-override '{"outline_color": "#ff0000", "font_size": 48}'
```

**圆角背景模式** — 现代圆角矩形背景，支持自定义背景色、圆角半径、内边距：
```bash
# 使用圆角背景
videocaptioner synthesize video.mp4 -s sub.srt --subtitle-mode hard --render-mode rounded

# 自定义白字红底
videocaptioner synthesize video.mp4 -s sub.srt --subtitle-mode hard \
  --style-override '{"text_color": "#ffffff", "bg_color": "#ff000099", "corner_radius": 12}'
```

运行 `videocaptioner style` 查看所有预设及其参数。样式选项仅对硬字幕（`--subtitle-mode hard`）生效。

---

### `dub` — 字幕配音

根据字幕时间轴生成配音音轨，可选把音轨写回视频。普通 SRT 可直接使用；多说话人可在字幕文本里写：

```text
[Alice] 你好，今天开始测试。
Bob: This line uses another voice.
```

```bash
# Edge TTS（默认，无需 API key，依赖网络）
videocaptioner dub input.srt \
  --preset edge-cn-female \
  -o output.wav

# SiliconFlow CosyVoice2
videocaptioner dub input.srt \
  --preset siliconflow-cn-female \
  --tts-api-key "$VIDEOCAPTIONER_TTS_API_KEY" \
  -o output.wav

# Gemini TTS
videocaptioner dub input.srt \
  --preset gemini-en-friendly \
  --tts-api-key "$VIDEOCAPTIONER_TTS_API_KEY" \
  -o output.wav

# 多说话人音色映射，并输出视频
videocaptioner dub input.srt --video video.mp4 \
  --speaker-voice Alice=anna \
  --speaker-voice Bob=benjamin \
  -o video_dubbed.mp4
```

| 选项 | 说明 |
|------|------|
| `--preset` | 配音预设：如 `siliconflow-cn-female`、`gemini-en-friendly`、`edge-cn-female` |
| `--tts-api-key` | TTS API key。SiliconFlow/Gemini 需要；Edge TTS 不需要 |
| `--voice` | 默认音色。SiliconFlow 可用 `anna`、`alex`、`benjamin`；Gemini 使用 `Kore`、`Achird`；Edge 可用 `xiaoxiao`、`yunxi` 或完整 voice ID |
| `--speak auto/first/second` | 双语字幕时选择朗读第一行还是第二行 |
| `--speaker-voice NAME=VOICE` | 给字幕中的说话人指定音色，可重复 |
| `--speaker-clone NAME=AUDIO\|TEXT` | SiliconFlow 音色克隆参考音频与对应文本 |
| `--clone-audio` / `--clone-text` | 给默认说话人使用 SiliconFlow 音色克隆；Gemini/Edge 不支持 |
| `--timing balanced/strict/natural/none` | 时间轴策略：默认平衡；`strict` 更贴字幕；`natural` 更保留自然语速 |
| `--adapt-length` | 使用 LLM 缩短明显过长的台词 |
| `--audio-mode replace/mix/duck` | 输出视频时替换原声、混合原声，或压低原声作为背景 |

命令会额外生成 `*.dubbing.json` 报告，记录每句使用的说话人、音色、生成时长、变速倍数和时间轴 warning。

---

### `process` — 全流程处理

一键完成：转录 → 断句 → 优化 → 翻译 → 字幕后处理 → 合成。后处理默认开启，每个实际
字幕阶段固定保存规范 SRT：`【转录字幕】`、`【初版字幕】` 和 `【后处理字幕】`。
阶段之间只使用 SRT 语义的数据，不把 ASS 作为模块交付；后处理失败自动回退初版字幕。

```bash
videocaptioner process <音视频文件> [选项]
```

额外选项：

| 选项 | 说明 |
|------|------|
| `--no-synthesize` | 跳过视频合成（只输出字幕） |
| `--no-postprocess` | 跳过字幕后处理，直接使用初版字幕 |
| `--dub` | 在转录/处理字幕后生成配音音轨或配音视频 |
| `--dub-only` | 只输出配音结果，跳过字幕烧录/嵌入 |

`process` 的 `-o` 只控制最终视频、音频或输出目录，不改变各字幕阶段固定的 SRT 格式。
CLI 完整流程本次不增加阶段自动导出格式矩阵。

示例：

```bash
# 英文视频配成中文视频
videocaptioner process talk.mp4 \
  --asr bijian \
  --translator bing --to zh-Hans \
  --dub-only \
  --timing strict

# 中文视频配成英文视频
videocaptioner process input.mp4 \
  --translator bing --to en \
  --dub-only \
  --preset gemini-en-friendly \
  --tts-api-key "$VIDEOCAPTIONER_TTS_API_KEY"
```

音频文件自动跳过合成步骤。

---

### `download` — 下载在线视频

```bash
videocaptioner download <URL> [-o 目录]
```

支持 YouTube、B站等 yt-dlp 支持的平台。

---

### `style` — 查看字幕样式

```bash
videocaptioner style
```

列出所有可用样式预设及其配置参数，包括 ASS 和圆角背景两种模式。

---

### `config` — 配置管理

```bash
videocaptioner config show              # 查看配置
videocaptioner config set <key> <value> # 设置配置项
videocaptioner config get <key>         # 获取配置项
videocaptioner config path              # 配置文件路径
videocaptioner config init              # 交互式初始化
videocaptioner config init --non-interactive --profile dubbing
videocaptioner config init --print-template
```

---

### `doctor` — 环境诊断

```bash
videocaptioner doctor          # 检查依赖和配置
videocaptioner doctor --json   # Agent/CI 友好的 JSON 输出
```

会检查 Python、FFmpeg/FFprobe、yt-dlp、配置文件、ASR、LLM、翻译和配音关键配置。缺失项会给出对应修复命令。

---

## 配置

配置优先级：命令行参数 > 环境变量 > 配置文件 > 默认值。

### 环境变量

| 变量 | 说明 |
|------|------|
| `OPENAI_API_KEY` | LLM API 密钥 |
| `OPENAI_BASE_URL` | LLM API 地址 |
| `OPENAI_MODEL` | LLM 模型名 |
| `VIDEOCAPTIONER_DUB_PRESET` | 配音预设 |
| `VIDEOCAPTIONER_TTS_API_KEY` | 配音 TTS API 密钥 |
| `VIDEOCAPTIONER_TTS_API_BASE` | 配音 TTS API 地址 |
| `VIDEOCAPTIONER_TTS_MODEL` | 配音 TTS 模型 |
| `VIDEOCAPTIONER_TTS_VOICE` | 配音默认音色 |
| `VIDEOCAPTIONER_TTS_WORKERS` | 并发 TTS 请求数 |
| `VIDEOCAPTIONER_DUB_TIMING` | 配音时间轴策略 |
| `VIDEOCAPTIONER_DUB_AUDIO_MODE` | 原声处理方式 |
| `VIDEOCAPTIONER_TTS_MAX_SPEED` | 配音最大变速倍数 |
| `VIDEOCAPTIONER_TTS_REWRITE_TOO_LONG` | 是否启用 LLM 缩短过长台词 |
| `VIDEOCAPTIONER_AUDIO_LOUDNORM` | 转录前是否启用 EBU R128 loudnorm |
| `VIDEOCAPTIONER_MIMO_ASR_API_KEY` | MiMo ASR API 密钥 |
| `VIDEOCAPTIONER_MIMO_ASR_API_BASE` | MiMo ASR API 地址 |
| `VIDEOCAPTIONER_MIMO_ASR_MODEL` | MiMo ASR 模型名 |
| `VIDEOCAPTIONER_MIMO_ASR_TIMEOUT` | MiMo ASR 超时时间 |
| `VIDEOCAPTIONER_MIMO_ASR_CONCURRENCY` | MiMo ASR 并发分块请求数（默认 2；遇 429 限流请调低） |
| `VIDEOCAPTIONER_QWEN_ASR_MODEL` | Qwen3 ASR 模型 |
| `VIDEOCAPTIONER_QWEN_ALIGNER_MODEL` | Qwen3 ForcedAligner 模型 |
| `VIDEOCAPTIONER_QWEN_MODEL_DIR` | 本地 Qwen 模型目录 |
| `VIDEOCAPTIONER_QWEN_DEVICE` | Qwen 运行设备 |
| `VIDEOCAPTIONER_QWEN_DTYPE` | Qwen 计算精度 |
| `VIDEOCAPTIONER_QWEN_MAX_NEW_TOKENS` | Qwen 单块最大生成 token 数 |
| `VIDEOCAPTIONER_QWEN_CHUNK_OVERLAP_SECONDS` | Qwen/MiMo 分块重叠秒数 |
| `VIDEOCAPTIONER_QWEN_COMPILE_ALIGNER` | 是否启用实验性 ForcedAligner 编译 |

### 配置文件

位置：`~/.config/videocaptioner/config.toml`（macOS/Linux）

推荐先运行：

```bash
videocaptioner config init
videocaptioner doctor
```

非交互环境可以这样初始化：

```bash
videocaptioner config init --non-interactive --profile dubbing \
  --translator bing \
  --timing balanced --audio-mode replace
```

```toml
[llm]
api_key = "sk-xxx"
api_base = "https://api.openai.com/v1"
model = "gpt-4o-mini"

[transcribe]
asr = "bijian"

[subtitle]
optimize = true
# split 仅供完整 ASR 流程对真实词级时间戳启用语义分组；直接字幕任务忽略该项
split = false

[translate]
service = "bing"

[dubbing]
preset = "edge-cn-female"
api_key = ""
voice = "xiaoxiao"
timing = "balanced"
audio_mode = "replace"
tts_workers = 5
```

运行 `videocaptioner config show` 查看完整配置项。

---

## 通用选项

| 选项 | 说明 |
|------|------|
| `-v` / `--verbose` | 详细输出 |
| `-q` / `--quiet` | 静默模式，仅输出结果路径（适合管道使用） |
| `--config FILE` | 指定配置文件 |

## 退出码

| 码 | 含义 |
|----|------|
| 0 | 成功 |
| 1 | 一般错误 |
| 2 | 参数/配置错误 |
| 3 | 输入文件不存在 |
| 4 | 依赖缺失（FFmpeg 等） |
| 5 | 运行时错误（API 失败等） |
