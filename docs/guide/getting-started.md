---
title: 快速开始 - VideoCaptioner
description: 从源码启动 VideoCaptioner，并配置转录、翻译、字幕后处理与 FFmpeg 合成。
---

# 快速开始

这是一个偏个人使用、按需更新的 fork，目前主要按源码方式运行。

## 系统要求

- Windows 10/11、macOS 或常见 Linux 发行版
- Python 3.10–3.12（推荐 3.12）
- Git 与 [uv](https://docs.astral.sh/uv/)
- FFmpeg / FFprobe；源码环境可使用系统安装或项目配置的核心
- 本地 Qwen3 ASR 需要额外磁盘、内存和独立 Runtime；CUDA 模式需要兼容的 NVIDIA 环境

## 安装与启动

```bash
git clone https://github.com/JsonBorn98/VideoCaptioner.git
cd VideoCaptioner

uv sync --python 3.12
uv run videocaptioner doctor --profile gui
uv run videocaptioner
```

CLI：

```bash
uv run videocaptioner --help
uv run videocaptioner transcribe --help
```

项目不发布 PyPI 包，不建议使用 `pip install videocaptioner`。

## 选择转录方案

在 **设置 → 转录配置** 中选择后端：

| 后端 | 运行方式 | 主要特点 |
|---|---|---|
| MiMo ASR | API + 可选本地对齐 | 并发文本转录，可用 Qwen3-ForcedAligner 补齐精确时间轴 |
| Qwen3ASR [Local] | 本地 Runtime | 本地 ASR、字词级对齐、CPU/CUDA 可选 |
| FasterWhisper | 本地 | 通用 Whisper 生态后端 |
| WhisperCpp | 本地 | 轻量本地后端 |
| Whisper API | API | OpenAI-compatible 音频转录 |
| 必剪 / 剪映 | 在线 | 无需 LLM Key 的兼容后端，适用范围受服务本身限制 |

不要把 VAD 与词级对齐混为一谈：VAD 用于识别语音区间、寻找分块边界和跳过静音；
字词级时间证据由 ASR 或 ForcedAligner 生成。

详细参数见 [ASR 配置](/config/asr)。

## 安装 Qwen 本地组件

通过 GUI 组件管理安装时，Qwen3-ASR、Qwen3-ForcedAligner 与 PyTorch 位于独立
Runtime，不会混入主程序环境；模型权重保存在应用模型目录。

1. 打开 **设置 → 转录配置 → Qwen 组件管理**。
2. 选择 **CPU Runtime** 或 **CUDA Runtime**。
3. 等待 Runtime 安装与状态检查完成。
4. 下载 Qwen3-ASR 与 Qwen3-ForcedAligner 模型。
5. 返回转录配置，选择 `Qwen3ASR [Local]`，或为 MiMo 启用本地对齐。

Runtime 位于独立的 `runtimes/qwen`。只使用源码 CLI、无法通过 GUI 创建 Runtime 时，
可把 Qwen 依赖直接同步到当前项目 `.venv`：

```bash
uv sync --python 3.12 --extra qwen
```

该方式会把 PyTorch 等重型依赖装入主项目环境，可用
`uv run python -c "import qwen_asr, torch; print(torch.__version__)"` 检查依赖。
`uv run videocaptioner doctor --profile qwen` 专门检查 GUI 管理的独立 Runtime 与应用模型
目录，不用于判断当前 `.venv` 的 extra 是否安装成功。模型会在首次使用或通过组件管理时
下载，请预留与所选模型、PyTorch 和缓存相匹配的磁盘空间。CUDA 是否可用还取决于显卡、
驱动及所安装的 PyTorch build。

## 配置翻译

翻译有三种模式：

| 模式 | 说明 |
|---|---|
| 非 LLM | Bing、Google、DeepLX |
| 单 LLM | 一个模型完成翻译，可选反思 |
| 增强型双角色 LLM | 主翻译 + 高级校对，含术语表和完整审计 |

增强模式需要先创建 LLM 模型方案。GUI 可为两个角色绑定不同方案；CLI 当前使用同一个
legacy profile 承担两个角色。GUI 命名方案支持 OpenAI-compatible、Anthropic Messages
与 Gemini transport；CLI legacy profile 当前按 OpenAI-compatible 配置。完整说明见
[LLM 模型方案](/config/llm)与[翻译模式](/config/translator)。

## 运行完整工作流

GUI 的完整任务按以下顺序执行：

```text
转录与时间轴
  → 本地聚合或 LLM 语义断句
  → 字幕优化
  → 翻译
  → 字幕后处理与 QA
  → 视频合成
```

每个字幕阶段都会保留规范 SRT 检查点；后处理失败时会保留初版字幕，不覆盖输入。
详情见 [工作流程](/guide/workflow)。

## 分步使用

### 只转录

```bash
uv run videocaptioner transcribe video.mp4 \
  --asr qwen-local --word-timestamps
```

### 翻译已有字幕

```bash
uv run videocaptioner subtitle input.srt \
  --translation-mode enhanced_llm \
  --target-language en
```

### 修复闪轴、单句时长与阅读速度

```bash
uv run videocaptioner postprocess input.srt \
  --profile balanced --qa-report
```

### 合成视频

```bash
uv run videocaptioner synthesize video.mp4 -s subtitle.srt \
  --subtitle-mode hard \
  --video-encoder h264_nvenc \
  --cq 23
```

先检查最终 FFmpeg 命令但不执行：

```bash
uv run videocaptioner synthesize video.mp4 -s subtitle.srt \
  --subtitle-mode hard --video-encoder h264_nvenc --print-command
```

完整参数见 [CLI 参考](/cli)与 [FFmpeg 合成导出](/guide/video-synthesis)。

## 日志与产物

- **运行日志**：查看 GUI/CLI 各阶段进度、回退和产物。
- **LLM 请求日志**：按 stage、role、attempt 记录请求与响应。
- **翻译产物**：初版字幕、`.vcglossary.json`、Markdown 审计报告。
- **后处理产物**：规范 SRT、可选 `.qa.md`、速度阶段 `.speed-changes.json`，以及可选
  `.vctiming.json`。
- **合成日志**：FFmpeg 命令、Console 输出和编码器能力探测结果。

LLM 请求日志可能包含字幕与 Prompt 全文，分享日志前请检查敏感内容。

## 下一步

- [ASR 与时间轴配置](/config/asr)
- [翻译模式与双角色校对](/config/translator)
- [字幕后处理](/guide/subtitle-postprocessing)
- [FFmpeg 合成导出](/guide/video-synthesis)
- [常见问题](/guide/faq)
