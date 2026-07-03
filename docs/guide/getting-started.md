---
title: 快速开始 - VideoCaptioner
description: 快速安装和配置 VideoCaptioner，5分钟开始处理你的第一个视频字幕。支持 Windows、macOS、Linux 多平台。
head:
  - - meta
    - name: keywords
      content: VideoCaptioner安装,快速开始,视频字幕教程,Whisper安装,LLM配置,字幕处理入门
---

# 快速开始

本指南将帮助你快速上手 VideoCaptioner，开始处理你的第一个视频字幕。

## 系统要求

- **Windows**: Windows 10/11 (64位)
- **macOS**: macOS 10.15 或更高版本
- **Linux**: Ubuntu 20.04+ / Debian 11+ / Fedora 35+
- **Python**: Python 3.10-3.12（仅源码开发需要；推荐 3.12）
- **内存**: 建议 4GB 以上（使用本地 Whisper 需要 8GB+）

## 安装方式

### 普通用户：下载桌面 Release

本 fork 面向普通用户发布二进制桌面包，不发布 PyPI 包。桌面包会内置 `ffmpeg` / `ffprobe` 和 `uv`，基础功能不要求本机安装 Python。

1. 从 [Release](https://github.com/JsonBorn98/VideoCaptioner/releases) 页面下载最新版本。
2. 选择与你系统匹配的 `VideoCaptioner-<version>-<platform>.zip`。
3. 解压后运行 `VideoCaptioner` / `VideoCaptioner.exe`。
4. 首次运行后可在设置里配置 LLM、ASR、翻译等功能。

::: tip 提示
如果只使用必剪语音识别、必应/谷歌翻译等基础能力，可以先不配置 API Key。
:::

### Qwen 本地 ASR 组件

Qwen3ASR / Qwen3-ForcedAligner 依赖 `qwen-asr`、PyTorch 和模型权重，默认不会打进桌面包。需要时在软件中打开：

**设置 → 转录配置 → Qwen 组件管理**

然后按顺序执行：

1. 根据机器选择 **安装 CPU 运行时** 或 **安装 CUDA 运行时**。
   - 没有 NVIDIA GPU、只想先验证功能，选择 CPU。
   - 有 NVIDIA GPU 并准备把 Qwen 的 **运行设备** 设为 `cuda:0`，选择 CUDA。
2. 等待安装进度完成。CUDA 运行时会下载较大的 PyTorch wheel，时间较长是正常现象。
3. 下载 `Qwen3-ASR` 和 `Qwen3-ForcedAligner` 模型。
4. 选择 `Qwen3ASR [Local]`，或在 MiMoASR 中使用本地 Qwen 对齐。

Qwen 运行时安装在独立的 `runtimes/qwen` 环境中，不会污染主程序环境。源码启动 GUI 时也推荐使用这里的组件管理安装 Qwen 运行时。

::: warning CUDA 安装检查
安装 CUDA 运行时后，组件管理里应显示类似 `PyTorch 2.11.0+cu128 (CUDA 12.8, CUDA available)`。如果显示 `+cpu`，说明 PyTorch 被解析成 CPU 版，请关闭正在运行的转录任务后重新点击 **安装 CUDA 运行时**。
:::

### 开发者：源码运行

源码运行使用 `uv` 管理 Python 环境。不要使用上游旧文档中的 `pip install -r requirements.txt` 或 `python main.py`。

```bash
git clone https://github.com/JsonBorn98/VideoCaptioner.git
cd VideoCaptioner

# 安装依赖并启动 GUI
uv sync --python 3.12
uv run videocaptioner doctor --profile gui
uv run videocaptioner
```

需要 Qwen 依赖的源码开发环境：

```bash
# 推荐：先启动 GUI，再通过 Qwen 组件管理安装独立 runtime
uv run videocaptioner
uv run videocaptioner doctor --profile qwen
```

只有在调试 `qwen-asr` 源码级集成、希望把 Qwen 依赖装进当前 `.venv` 时，才需要执行：

```bash
uv sync --python 3.12 --extra qwen
```

## 基础配置

在开始处理视频之前，建议先完成以下基础配置：

### 1. LLM API 配置（可选但推荐）

LLM 用于字幕断句、优化和翻译。软件内置了基础模型，但配置自己的 API 可以获得更好的效果。

打开 **设置 → LLM 配置**，选择以下任一服务：

| 服务商           | 特点               | 推荐模型                                |
| ---------------- | ------------------ | --------------------------------------- |
| **OpenAI**       | 质量最好           | `gpt-4o-mini` (经济), `gpt-4o` (高质量) |
| **DeepSeek**     | 性价比高           | `deepseek-chat`                         |
| **SiliconCloud** | 国内可用，并发较低 | `Qwen/Qwen2.5-72B-Instruct`             |
| **Ollama**       | 本地运行，完全免费 | `llama3.1:8b`                           |

详细配置方法请查看 [LLM 配置指南](/config/llm)。

### 2. 语音识别配置

打开 **设置 → 转录配置**，选择语音识别引擎：

| 引擎                 | 支持语言 | 运行方式 | 推荐场景                      |
| -------------------- | -------- | -------- | ----------------------------- |
| **FasterWhisper** ⭐ | 99种语言 | 本地     | 最推荐，准确度高，支持GPU加速 |
| **B接口**            | 中英文   | 在线     | 快速测试，无需下载模型        |
| **J接口**            | 中英文   | 在线     | 备用选项                      |
| **WhisperCpp**       | 99种语言 | 本地     | 轻量级本地方案                |
| **Whisper API**      | 99种语言 | 在线     | 使用 OpenAI API               |

::: tip 推荐配置

- **中文视频**: FasterWhisper + Medium 模型或以上
- **英文视频**: FasterWhisper + Small 模型即可
- **其他语言**: FasterWhisper + Large-v2 模型

首次使用需要在软件内下载模型，国内网络可直接下载。
:::

详细配置方法请查看 [ASR 配置指南](/config/asr)。

### 3. 翻译配置（可选）

如果需要翻译字幕，打开 **设置 → 翻译配置**：

| 翻译服务        | 特点                 | 推荐场景     |
| --------------- | -------------------- | ------------ |
| **LLM 翻译** ⭐ | 质量最好，理解上下文 | 追求翻译质量 |
| **Bing 翻译**   | 速度快，免费         | 快速翻译     |
| **Google 翻译** | 速度快，需要科学上网 | 英语翻译     |
| **DeepLX**      | 质量好，需要自建服务 | 专业翻译     |

详细配置方法请查看 [翻译配置指南](/config/translator)。

## 开始处理视频

### 全流程处理（最简单）

这是最简单的方式，一键完成所有步骤：

1. 在主界面点击 **"任务创建"** 标签
2. 拖拽视频文件到窗口，或点击选择文件
   - 也可以输入 YouTube、B站等视频链接
3. 点击 **"开始全流程处理"** 按钮
4. 等待处理完成，输出文件保存在 `work-dir/` 目录

::: info 处理流程
全流程会依次执行：

1. 语音识别转录
2. 字幕智能断句（可选）
3. 字幕优化（可选）
4. 字幕翻译（可选）
5. 视频合成
   :::

### 分步处理

如果你需要更精细的控制，可以分步处理：

#### 步骤 1：语音识别转录

1. 切换到 **"语音转录"** 标签
2. 选择视频或音频文件
3. 配置转录参数：
   - 转录语言（自动检测或手动指定）
   - VAD 方法（建议保持默认）
   - 是否启用音频分离（嘈杂环境推荐）
4. 点击 **"开始转录"**
5. 转录完成后会生成字幕文件

#### 步骤 2：字幕优化与翻译

1. 切换到 **"字幕优化与翻译"** 标签
2. 加载字幕文件（自动加载或手动选择）
3. 配置处理选项：
   - **智能断句**：重新分段，阅读更流畅
   - **字幕校正**：修正错别字、优化格式
   - **字幕翻译**：翻译为目标语言
4. （可选）填写文稿提示，提升准确度
5. 点击 **"开始处理"**
6. 处理完成后可以实时预览和编辑

#### 步骤 3：字幕视频合成

1. 切换到 **"字幕视频合成"** 标签
2. 选择字幕样式（科普风、新闻风等）
3. 选择合成方式：
   - **硬字幕**：烧录到视频中
   - **软字幕**：内嵌字幕轨道（需要播放器支持）
4. 点击 **"开始合成"**
5. 输出视频保存在 `work-dir/` 目录

## 实用技巧

### 1. 提升字幕质量

- ✅ 使用 FasterWhisper Large-v2 模型
- ✅ 启用 VAD 过滤，减少幻觉
- ✅ 在嘈杂环境中启用音频分离
- ✅ 使用智能断句（语义分段）
- ✅ 填写文稿提示（术语表、原文稿等）

### 2. 加快处理速度

- ✅ 使用在线 ASR（B接口/J接口）跳过模型下载
- ✅ 提高 LLM 并发线程数（如果 API 支持）
- ✅ 使用软字幕合成（速度极快）
- ✅ 关闭不需要的功能（如翻译、优化）

### 3. 批量处理

如果需要处理多个视频：

1. 切换到 **"批量处理"** 标签
2. 选择处理类型（批量转录/字幕处理/视频合成）
3. 添加视频文件到队列
4. 点击 **"开始批量处理"**

详细说明请查看 [批量处理指南](/guide/batch-processing)。

## 常见问题

### 转录时出现幻觉或重复

::: details 解决方案

- 启用 VAD 过滤
- 更换更大的模型（如 Medium → Large）
- 尝试 Large-v2 而不是 Large-v3
- 在嘈杂环境中启用音频分离
  :::

### LLM 请求失败

::: details 解决方案

- 检查 API Key 是否正确
- 检查 Base URL 是否正确
- 降低线程数（某些服务商限制并发）
- 检查网络连接
- 查看日志文件获取详细错误信息
  :::

### 字幕时间轴不准确

::: details 解决方案

- 使用 FasterWhisper（时间轴最准确）
- 启用智能断句时使用语义分段模式
- 手动在字幕编辑界面调整
  :::

更多问题请查看 [常见问题解答](/guide/faq)。

## 下一步

- 📖 了解 [工作流程](/guide/workflow)
- ⚙️ 查看 [详细配置指南](/guide/configuration)
- 🎨 自定义 [字幕样式](/guide/subtitle-style)
- 📝 使用 [文稿匹配](/guide/manuscript) 提升准确度

---

如果在使用过程中遇到问题，欢迎提交 [Issue](https://github.com/JsonBorn98/VideoCaptioner/issues) 或加入社区讨论。

