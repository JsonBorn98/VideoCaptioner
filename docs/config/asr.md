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

如果某个 API 只返回纯文本，程序可以把文本拆成多条字幕，但时间只能按文本长度估算。对需要严肃时间轴的 SRT/ASS，不建议把估算结果当作真实对齐。MiMo/Qwen 在 VAD 感知分块模式下会优先使用 Silero 语音区间，Silero 不可用时回退到能量检测，并把检测到的语音区间传给估算逻辑，使降级字幕优先落在语音区间内，而不是平均铺到长静音段里。

## 音频预处理

转录设置里可以开启 **音量标准化**。该选项默认关闭；开启后，VideoCaptioner 在从视频抽取 16 kHz 单声道 WAV 前，会应用两遍 EBU R128 `loudnorm=I=-16:TP=-1.5:LRA=11`。它适合音量忽大忽小的会议、课程或录屏素材；音量本身稳定的素材通常不需要开启。CLI 可使用 `videocaptioner transcribe --audio-loudnorm ...`。

## MiMoASR [API]

配置项：

- **API Base URL**：默认 `https://api.xiaomimimo.com/v1`
- **API Key**：MiMo API Key
- **模型**：默认 `mimo-v2.5-asr`
- **超时时间**：长音频分块同步等待秒数，默认 600 秒
- **并发请求数**：默认 2；遇到 429 限流时可调低
- **分块重叠秒数**：默认 10 秒，用于降低固定切分点漏词风险
- **Qwen3 对齐模型**：默认 `Qwen/Qwen3-ForcedAligner-0.6B`

注意：

- MiMo 当前返回转录文本，不返回原生 SRT 时间戳。
- 当需要字幕时间戳时，VideoCaptioner 会调用本地 Qwen3-ForcedAligner 对齐 MiMo 返回文本。
- 长视频会把 MiMo API 转录和本地 Qwen 对齐拆成两阶段流水线：API 阶段按配置并发请求多个音频块，对齐阶段按顺序进入同一个常驻 worker，避免反复加载对齐模型。
- MiMo API 返回 429 限流时会按 Retry-After 或指数退避重试；如果仍频繁限流，请调低并发请求数。
- 长视频分块会优先按 Silero VAD 的非语音区间吸附边界；如果 Silero 模型不可用或调用失败，会自动回退到现有能量检测。
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
- **实验性编译对齐模型**：默认关闭；开启后会尝试用 `torch.compile` 编译 Qwen3-ForcedAligner，失败时自动回退到未编译模型

依赖安装：

- 桌面 Release：打开 **设置 → 转录配置 → Qwen 组件管理**，选择 **安装 CPU 运行时** 或 **安装 CUDA 运行时**，再下载 ASR / ForcedAligner 模型。
- 源码运行 GUI：同样推荐在 **Qwen 组件管理** 中安装独立运行时，然后用 `uv run videocaptioner doctor --profile qwen` 检查状态。
- 源码级调试 `qwen-asr` 集成：如果确实希望把依赖装进当前 `.venv`，再执行 `uv sync --python 3.12 --extra qwen`。

Qwen runtime 会安装到用户数据目录下的独立 `runtimes/qwen` 环境，避免把 PyTorch / qwen-asr 混入主程序包。源码运行时路径通常是项目目录下的 `AppData/runtimes/qwen`；桌面 Release 会使用系统用户数据目录。

转录任务会启动一个独立的常驻 Qwen worker 子进程。这个 worker 会在任务期间复用已加载的 ASR / ForcedAligner 模型，避免每个音频块都重新创建 CUDA context 和从磁盘加载模型；Qwen 首轮分块会在缓存检查后作为一个 batch 请求进入 worker，异常块再独立进入重试流程。任务结束或程序退出后会关闭 worker。仍然保留子进程隔离，以免 PyTorch/CUDA 原生库污染 PyQt 主进程。

Qwen 本地转录同样使用 VAD 感知分块：优先用 Silero 找语音/静音区间，失败时回退到能量检测；纯静音块会直接跳过，非静音块如果返回空字幕会进入重试流程。

### Qwen 运行时选择

| 按钮 | 安装内容 | 适用场景 |
|------|----------|----------|
| **安装 CPU 运行时** | `qwen-asr` + CPU PyTorch | 无 NVIDIA GPU、先验证功能、或排查 CUDA 问题 |
| **安装 CUDA 运行时** | `qwen-asr` + CUDA PyTorch (`cu128`) | NVIDIA GPU，且运行设备选择 `auto` / `cuda:0` |

CUDA 安装流程会使用 `uv --torch-backend cu128` 解析依赖，并在最后重新安装 PyTorch。这样可以避免 `qwen-asr` / `accelerate` 依赖链把 PyTorch 回退成默认 CPU 版。

安装完成后应看到类似：

```text
PyTorch 2.11.0+cu128 (CUDA 12.8, CUDA available)
```

如果显示 `+cpu` 或 `CUDA unavailable`，不要继续用 `cuda:0` 转录。请关闭正在运行的转录任务和残留 `python` / `uv` 进程，再重新点击 **安装 CUDA 运行时**。

### 安装进度与日志

安装过程中组件管理会显示当前步骤和摘要进度。CUDA 版 PyTorch wheel 较大，网络较慢时停留在某一步几十秒到数分钟都可能是正常的。

完整安装输出写入：

```text
AppData/logs/app.log
```

遇到安装失败时优先查看日志中 `qwen_runtime_manager`、`Installing Qwen runtime dependencies`、`Installing CUDA PyTorch runtime` 附近的内容。

常见错误：

- `Windows 拒绝访问` / `0x80070005`：通常是运行时文件被正在运行的转录任务、残留 Python 进程、杀毒软件或索引器占用。关闭任务后重试；必要时重启软件。
- `PyTorch ... +cpu`：说明当前运行时仍是 CPU PyTorch。重新执行 **安装 CUDA 运行时**，安装结束后确认状态显示 `+cu128`。
- `No module named 'nagisa'` / `No module named 'qwen_asr'`：说明 `qwen-asr` 依赖没有完整安装。重新执行对应运行时安装按钮。

性能建议：

- `Qwen3-ASR-1.7B + Qwen3-ForcedAligner-0.6B` 对 16 GB 显存比较紧张。
- 显存不足或长视频更稳的选择是 `Qwen3-ASR-0.6B`。
- CPU 运行时主要用于功能验证或排查 CUDA 问题，不适合长视频生产转录。
- `dtype=auto` 通常优先使用 GPU 半精度；出现兼容问题时再手动尝试 `bfloat16`、`float16` 或 `float32`。
- 单卡 CUDA 环境下，`auto` 会优先把模型放在 `cuda:0`，并在可用时使用 FlashAttention 2；不可用时自动回退到 SDPA。
- “实验性编译对齐模型”适合愿意测试启动预热开销与长任务吞吐收益的用户。它不会作为默认路径启用；如果当前 PyTorch 或模型结构不支持编译，程序会记录警告并继续使用原模型。

### 长视频性能验收

源码运行时可以用基准脚本记录 Qwen CUDA 长视频表现。建议固定一段 20 分钟以上中文素材和一段 20 分钟以上英文素材，且都包含代表性的停顿或静音。仓库内已准备好 `tests/fixtures/audio/zh_20min.mp3` 和 `tests/fixtures/audio/en_20min.mp3`：

```bash
uv run python scripts/asr_benchmark.py \
  --case zh=tests/fixtures/audio/zh_20min.mp3 \
  --case en=tests/fixtures/audio/en_20min.mp3 \
  --asr qwen-local \
  --variant cuda="--word-timestamps --qwen-device cuda:0" \
  --variant cuda-compile="--word-timestamps --qwen-device cuda:0 --qwen-compile-aligner" \
  --required-label zh \
  --required-label en \
  --required-variant cuda \
  --required-variant cuda-compile \
  --min-duration-seconds 1200 \
  --check-qwen-cleanup
```

脚本会写入 `benchmark-output/asr/.../report.json`，包含每个 case 的命令、耗时、媒体时长、实时率、字幕条数、stdout/stderr 日志路径和验收摘要。`--check-qwen-cleanup` 会在每个 case 前后记录 Qwen worker 相关进程快照；如果运行结束后出现新的未退出 worker，验收摘要会标记失败。

MiMo word-timestamp 模式需要真实 API Key，同时会使用本地 Qwen worker 做对齐，也可以用同一脚本验收：

```bash
VIDEOCAPTIONER_MIMO_ASR_API_KEY=your-key \
uv run python scripts/asr_benchmark.py \
  --case zh=tests/fixtures/audio/zh_20min.mp3 \
  --case en=tests/fixtures/audio/en_20min.mp3 \
  --asr mimo-asr \
  --variant word="--word-timestamps" \
  --required-label zh \
  --required-label en \
  --required-variant word \
  --min-duration-seconds 1200 \
  --check-qwen-cleanup
```

## 分块与临时文件

MiMoASR 和 Qwen3ASR 会自动分块并保留可配置的重叠时长。Qwen3ASR 使用 5 分钟源音频范围，主进程只把原始音频路径、起点和时长传给常驻 worker；worker 内部优先把源范围解码成 qwen-asr 支持的内存 PCM 输入，只有兼容性回退时才写临时 WAV。MiMoASR 在需要本地 Qwen 对齐时使用 3 分钟块，纯文本模式使用 5 分钟块。MiMo/Qwen 会在块边界附近优先寻找静音点，找到安全候选时把边界吸附过去，找不到时回退到固定边界。MiMo 的 10 MB base64 上限会先换算成可发送的原始音频字节数，导出后的块如果仍超限，切块器会按实际导出码率继续拆分。切块器会把已知的块时长传给后端，避免每个 ASR 实例再次解码音频只为计算时长；重试拆子块时复用已加载的源音频范围，避免再次解码父 chunk；分块缓存 key 使用原始音频哈希和源时间范围，而不是导出后的 MP3/WAV 字节，因此同一源片段不会因为导出格式差异打散缓存。纯静音块不会导出临时音频 payload，会直接跳过 ASR；如果能量检测认为块内有语音但 ASR 返回空结果，会进入重试阶梯而不是默认为静音。重试拆子块时也会优先复用静音切点，并给相邻子块保留短重叠以降低边界漏词。当 ForcedAligner 失败或纯文本后端只能降级估算时间戳时，切块器会把相对语音区间传给后端，估算出的 cue 会避开检测到的长静音区。临时音频工作区会创建在原视频所在目录下，形如 `.videocaptioner-xxxxxx`，任务完成、失败或停止后会自动清理。

---

相关文档：

- [快速开始](/guide/getting-started)
- [LLM 配置](/config/llm)
- [MiMo and Qwen ASR Backend Lessons](/dev/asr-mimo-qwen-lessons)
