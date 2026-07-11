<div align="center">
  <img src="./docs/images/logo.png" alt="VideoCaptioner Logo" width="100">
  <h1>VideoCaptioner</h1>
  <p>基于大语言模型的视频字幕处理工具 — 语音识别、字幕优化、翻译、视频合成一站式处理</p>

  [在线文档](https://jsonborn98.github.io/VideoCaptioner/) · [下载运行](#下载运行) · [开发](#开发) · [CLI 使用](#cli-命令行)
</div>

## 下载运行

普通用户请优先使用 GitHub Release 的桌面包。本 fork 不发布 PyPI 包，`pip install videocaptioner` 不再作为推荐安装方式。

1. 打开 [Releases](https://github.com/JsonBorn98/VideoCaptioner/releases)。
2. 下载与你系统匹配的 `VideoCaptioner-<version>-<platform>.zip`。
3. 解压后运行 `VideoCaptioner` / `VideoCaptioner.exe`。

桌面 Release 会内置 `ffmpeg` / `ffprobe` 和 `uv`。基础功能（必剪语音识别、必应/谷歌翻译）无需 API Key；Qwen 本地 ASR 等重型组件在软件内按需安装。

## GUI 桌面版

直接启动 Release 包中的程序即可打开 GUI。源码开发环境可运行：

```bash
uv sync --python 3.12
uv run --python 3.12 videocaptioner
```

## CLI 命令行

源码开发环境中使用 CLI：

```bash
# 语音转录（免费，无需 API Key）
uv run videocaptioner transcribe video.mp4 --asr bijian

# 字幕翻译（免费必应翻译）
uv run videocaptioner subtitle input.srt --translator bing --target-language en

# 全流程：转录 → 优化 → 翻译 → 后处理 → 合成
uv run videocaptioner process video.mp4 --target-language ja

# 独立处理已经成型的字幕（输出规范 SRT）
uv run videocaptioner postprocess input.srt --profile balanced

# 字幕烧录到视频
uv run videocaptioner synthesize video.mp4 -s subtitle.srt

# 下载在线视频
uv run videocaptioner download "https://youtube.com/watch?v=xxx"
```

需要 LLM 功能（字幕优化、大模型翻译）时，配置 API Key：

```bash
videocaptioner config set llm.api_key <your-key>
videocaptioner config set llm.api_base https://api.openai.com/v1
videocaptioner config set llm.model gpt-4o-mini
```

配置优先级：`命令行参数 > 环境变量 (VIDEOCAPTIONER_*) > 配置文件 > 默认值`。运行 `videocaptioner config show` 查看当前配置。

<details>
<summary>所有 CLI 命令一览</summary>

| 命令 | 说明 |
|------|------|
| `gui` | 打开桌面版。也可以直接运行 `videocaptioner-gui` |
| `transcribe` | 语音转字幕。引擎：`faster-whisper`、`whisper-api`、`bijian`（免费）、`jianying`（免费）、`whisper-cpp` |
| `subtitle` | 字幕优化/翻译。翻译服务：`llm`、`bing`（免费）、`google`（免费） |
| `postprocess` | 独立字幕后处理：阅读速度平滑、时间轴、标点和质量审计 |
| `dub` | 根据字幕生成配音音轨或配音视频 |
| `synthesize` | 字幕烧录到视频（软字幕/硬字幕） |
| `process` | 全流程处理 |
| `download` | 下载 YouTube、B站等平台视频 |
| `config` | 配置管理（`show`、`set`、`get`、`path`、`init`） |

运行 `videocaptioner <命令> --help` 查看完整参数。完整 CLI 文档见 [docs/cli.md](docs/cli.md)。

</details>

<!-- <div align="center">
  <img src="https://h1.appinn.me/file/1731487405884_main.png" alt="界面预览" width="90%" style="border-radius: 5px;">
</div> -->

![页面预览](https://h1.appinn.me/file/1731487410170_preview1.png)
![页面预览](https://h1.appinn.me/file/1731487410832_preview2.png)

## LLM API 配置

LLM 仅用于字幕优化和大模型翻译，免费功能（必剪识别、必应翻译）无需配置。

支持 OpenAI 官方接口以及常见 OpenAI-compatible 服务。请根据可访问性、价格、隐私和服务条款自行选择服务商：

| 服务商 | 官网 |
|--------|------|
| OpenAI | [platform.openai.com](https://platform.openai.com) |
| SiliconCloud | [cloud.siliconflow.cn](https://cloud.siliconflow.cn) |
| DeepSeek | [platform.deepseek.com](https://platform.deepseek.com) |

在软件设置或 CLI 中填入 API Base URL 和 API Key 即可。[详细配置教程](https://jsonborn98.github.io/VideoCaptioner/config/llm)

## Qwen 本地 ASR

Qwen3ASR 和 Qwen3-ForcedAligner 属于可选重型组件。桌面 Release 不会默认内置 PyTorch / qwen-asr / 模型权重；需要时在 **设置 → 转录配置 → Qwen 组件管理** 中：

1. 根据机器选择 **安装 CPU 运行时** 或 **安装 CUDA 运行时**。CUDA 用户安装后应看到类似 `PyTorch 2.11.0+cu128 (CUDA 12.8, CUDA available)`。
2. 下载 `Qwen3-ASR` 和 `Qwen3-ForcedAligner` 模型。
3. 回到转录配置选择 `Qwen3ASR [Local]` 或在 MiMoASR 中启用 Qwen 对齐。

Qwen runtime 会安装到独立 `runtimes/qwen` 环境中，源码启动 GUI 时也推荐通过组件管理安装。只有调试 `qwen-asr` 依赖本身时，才需要 `uv sync --python 3.12 --extra qwen` 把依赖装进当前 `.venv`。

## Claude Code Skill

本项目提供了 [Claude Code Skill](https://code.claude.com/docs/en/skills.md)，让 AI 编程助手可以直接调用 VideoCaptioner 处理视频。

安装到 Claude Code：

```bash
mkdir -p ~/.claude/skills/videocaptioner
cp skills/SKILL.md ~/.claude/skills/videocaptioner/SKILL.md
```

然后在 Claude Code 中输入 `/videocaptioner transcribe video.mp4 --asr bijian` 即可使用。

## 工作原理

```
音视频输入 → 语音识别 → 字幕断句 → LLM 优化 → 翻译 → 视频合成
```

- 词级时间戳 + VAD 语音活动检测，识别准确率高
- LLM 语义理解断句，字幕阅读体验自然流畅
- 上下文感知翻译，支持反思优化机制
- 批量并发处理，效率高

## 开发

```bash
git clone https://github.com/JsonBorn98/VideoCaptioner.git
cd VideoCaptioner
uv sync --python 3.12                 # 安装依赖
uv run videocaptioner doctor --profile gui
uv run videocaptioner                 # 运行 GUI
uv run videocaptioner --help          # 运行 CLI
uv run pyright                        # 类型检查
uv run pytest tests/test_cli/ -q      # 运行测试
uv run --with pyinstaller --with static-ffmpeg python scripts/build_desktop.py --clean
```

## 许可证

[GPL-3.0](LICENSE)

[![Star History Chart](https://api.star-history.com/svg?repos=JsonBorn98/VideoCaptioner&type=Date)](https://star-history.com/#JsonBorn98/VideoCaptioner&Date)
