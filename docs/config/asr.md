# ASR 配置指南

语音识别（ASR）用于把音频或视频转成字幕。不同后端的接口形态、成本、时间戳能力差异很大，选择前建议先确认目标是“只要文本”还是“要可用的 SRT/ASS 时间轴”。

## 支持的 ASR 引擎

| 引擎 | 特点 | 推荐场景 |
|------|------|---------|
| **B 接口 / J 接口** | 免费在线接口 | 快速测试、轻量任务 |
| **Whisper [API]** | OpenAI Whisper 兼容的 `/v1/audio/transcriptions` 接口 | 已有 Whisper 兼容服务、无需本地模型 |
| **FasterWhisper** | 本地 Whisper 推理，支持 GPU | 通用本地转录 |
| **WhisperCpp** | 轻量本地 Whisper.cpp | CPU 环境、低资源设备 |
| **MiMoASR [API]** | 小米 MiMo ASR API，文本质量高；本身不返回时间戳 | 愿意使用 API 成本，且可搭配本地 Qwen3-ForcedAligner 生成时间戳 |
| **Qwen3ASR [Local]** | 本地 Qwen3 ASR + Qwen3-ForcedAligner | 希望本地部署、需要字/词级时间戳 |

## 时间戳能力

| 引擎 | 原生时间戳 | 字/词级时间戳 | 说明 |
|------|------------|----------------|------|
| Whisper [API] | 取决于服务端 | 取决于服务端是否支持 `timestamp_granularities` | 程序会从高能力请求逐步降级到纯文本请求 |
| FasterWhisper | 支持 | 支持 | 通用字幕场景较稳 |
| WhisperCpp | 支持 | 取决于模型/参数 | 适合轻量部署 |
| MiMoASR [API] | 不支持 | 通过本地 Qwen3-ForcedAligner 对齐 | 开启字幕时间戳时需要本地 Qwen 对齐模型 |
| Qwen3ASR [Local] | 通过 ForcedAligner | 支持 | 长视频会自动按 5 分钟分块处理 |

如果某个 API 只返回纯文本，程序可以把文本拆成多条字幕，但时间只能按文本长度估算。对需要严肃时间轴的 SRT/ASS，不建议把估算结果当作真实对齐。

## MiMoASR [API]

配置项：

- **API Base URL**：默认 `https://api.xiaomimimo.com/v1`
- **API Key**：MiMo API Key
- **模型**：默认 `mimo-v2.5-asr`
- **超时时间**：长音频分块同步等待秒数，默认 600 秒
- **分块重叠秒数**：默认 10 秒，用于降低固定切分点漏词风险
- **Qwen3 对齐模型**：默认 `Qwen/Qwen3-ForcedAligner-0.6B`

注意：

- MiMo 当前返回转录文本，不返回原生 SRT 时间戳。
- 当需要字幕时间戳时，VideoCaptioner 会调用本地 Qwen3-ForcedAligner 对齐 MiMo 返回文本。
- “测试 MiMo ASR 连接”只会发送内置短音频 `resource/assets/en.mp3`，不会发送当前选择的完整视频。
- 长视频会自动转音频、分块、对齐，用户不需要手动切分。

## Qwen3ASR [Local]

配置项：

- **ASR 模型**：`Qwen/Qwen3-ASR-1.7B` 或 `Qwen/Qwen3-ASR-0.6B`
- **对齐模型**：`Qwen/Qwen3-ForcedAligner-0.6B`
- **模型目录**：本地模型保存目录；程序会优先从这里查找模型
- **运行设备**：`auto` / `cuda:0` / `cpu`
- **计算精度**：`auto` / `bfloat16` / `float16` / `float32`
- **最大输出 Tokens**：默认 2048；如果分块文本被截断再调大
- **分块重叠秒数**：默认 10 秒，范围 0-60 秒

依赖：

```bash
uv pip install -U qwen-asr
```

如果通过 `videocaptioner gui` 启动桌面程序，请确认依赖安装到了启动 GUI 的同一个 Python/uv tool 环境中。

性能建议：

- `Qwen3-ASR-1.7B + Qwen3-ForcedAligner-0.6B` 对 16 GB 显存比较紧张。
- 显存不足或长视频更稳的选择是 `Qwen3-ASR-0.6B`。
- `dtype=auto` 通常优先使用 GPU 半精度；出现兼容问题时再手动尝试 `bfloat16`、`float16` 或 `float32`。

## 分块与临时文件

MiMoASR 和 Qwen3ASR 会自动使用 5 分钟音频块，并保留可配置的重叠时长。临时音频工作区会创建在原视频所在目录下，形如 `.videocaptioner-xxxxxx`，任务完成、失败或停止后会自动清理。

未来计划支持“智能边界切分”：仍然保持 5 分钟目标窗口，但在边界附近寻找静音或 VAD 非语音点，降低单词被切开的概率。

---

相关文档：

- [快速开始](/guide/getting-started)
- [LLM 配置](/config/llm)
- [ASR Smart Boundary Chunking Design](/dev/asr-smart-boundary-chunking)
- [MiMo and Qwen ASR Backend Lessons](/dev/asr-mimo-qwen-lessons)
