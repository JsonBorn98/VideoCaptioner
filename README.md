<div align="center">
  <img src="./docs/images/logo.png" alt="VideoCaptioner Logo" width="100">
  <h1>VideoCaptioner</h1>
  <p>面向桌面与 CLI 的视频字幕工具：转录、翻译、后处理与 FFmpeg 合成</p>

  [在线文档](https://jsonborn98.github.io/VideoCaptioner/) ·
  [快速开始](#快速开始) ·
  [核心增强](#fork-核心增强) ·
  [CLI](#cli-命令行)
</div>

这是一个偏个人使用、按需更新的 VideoCaptioner fork，主要补了本地 ASR 管理、
时间轴对齐、双角色 LLM 翻译校对、字幕后处理和 FFmpeg 合成。GUI 与 CLI 共享
核心处理引擎，但功能面并不完全相同：GUI 适合交互式精修与配置，CLI 适合批量处理
和自动化工作流。

![VideoCaptioner 字幕工作台](./docs/public/preview1.png)
![VideoCaptioner 字幕编辑与预览](./docs/public/preview2.png)

## Fork 核心增强

### 1. 转录与时间轴

- **MiMo ASR + Qwen3 本地 ASR**：支持 MiMo API 转录，以及
  Qwen3-ASR 0.6B / 1.7B 本地推理。
- **Qwen3-ForcedAligner**：为 Qwen 转录生成字词级时间戳，也可为 MiMo
  的纯文本结果补齐本地精确对齐。
- **独立 Qwen Runtime 管理**：可在 GUI 中安装 CPU / CUDA Runtime、
  检查 PyTorch 与 CUDA 状态，并下载或更新 ASR、ForcedAligner 模型；
  通过该入口安装时，重型依赖不会混入主程序环境。仅使用源码 CLI 时也可通过
  `uv sync --extra qwen` 将依赖装入当前项目环境。
- **长音视频处理**：Silero VAD 感知分块，失败时回退能量检测；
  支持静音边界切分、重叠窗口、空块跳过和 MiMo payload 限制处理。
- **性能与容错**：Qwen 常驻 Worker 复用模型并批量处理分块；
  MiMo 使用并发转录、顺序本地对齐的两阶段流水线，并处理 429 退避、
  Worker 重启、异常对齐重试和降级时间轴。
- **词级字幕快速聚合**：按停顿、边界和长度在本地合并字词级结果；
  只有显式启用语义断句时才调用 LLM。

详见 [ASR 配置](docs/config/asr.md)。

### 2. 增强型 LLM 上下文翻译与独立校对

- 保留 **非 LLM / 单 LLM / 增强型双角色 LLM** 三种翻译模式。
- 增强模式由**主翻译**和**高级校对**承担不同职责：先分层理解完整字幕，
  生成上下文简报与疑难术语，再经校对裁决形成可复用的
  `.vcglossary.json` 项目术语表。
- 翻译按 token 预算切分，并为每批注入全文简报、相关术语和前后边界语境；
  完成后由校对角色覆盖全部原文与译文，输出可定位的 Markdown 审计报告。
- GUI 可为两个角色绑定不同模型方案、编辑独立 Prompt、人工确认术语，
  并选择“仅报告”或“自动修复客观问题”。自动修正只有通过本地确定性校验
  后才会应用，主观质量问题只进入报告。
- CLI 与批量任务使用非交互策略，保存术语表，并在审计报告内附分阶段 token usage。
  当前 CLI 的两个角色共用同一模型 profile，但 Prompt 与调用阶段相互独立。
- GUI 命名模型方案支持 OpenAI-compatible、Anthropic Messages 与 Gemini transport；
  当前 CLI 的 legacy profile 使用 OpenAI-compatible transport。

详见 [翻译模式与双角色校对](docs/config/translator.md)。

### 3. 自适应字幕后处理

- 独立 `postprocess` 阶段可导入 SRT / VTT / ASS，并统一生成规范 SRT 工作稿；
  VTT / ASS 的样式、定位与特效不会保留。它既可接在完整 workflow 后，也可直接
  处理已有字幕，且不会覆盖输入文件。
- 提供占位符清理、中文引号与弱尾标点规范化、短间隙闭合和单调尾部补偿，
  用于减少闪轴、重叠和字幕过早消失。
- 按 CJK / Latin 阅读速度、单句最短与最长显示时长、相邻负载跳变进行审计；
  可安全借用间隙、平滑边界，并对过碎或过长字幕执行受验证的合并、拆分和文本迁移。
- 确定性算法无法解决的硬超速片段可选用 LLM 做局部语义修复；
  候选不满足结构、时间轴或内容约束时自动回滚。
- 支持“宽松 / 均衡 / 平滑优先”及自定义方案、仅分析模式、双语侧选择、
  可选 `.qa.md` 质量报告、速度阶段 `.speed-changes.json` 变更报告，以及关联媒体后的
  ForcedAligner 精确时间证据与可选 `.vctiming.json` sidecar。

详见 [字幕后处理](docs/guide/subtitle-postprocessing.md)。

### 4. FFmpeg 合成与导出

- 软字幕使用视频/音频流复制快速封装；ASS 硬烧和圆角背景接入集中编码引擎。
- 提供 14 个目录编码器：x264 / x265、SVT-AV1、AOM-AV1、VP9，以及
  H.264 / HEVC / AV1 的 NVENC、QSV、AMF；另支持软字幕/无视频滤镜路径的视频直通
  和自定义编码器，硬字幕始终需要重编码。
- 支持 CQ / ABR、CPU 2-pass（CLI）、preset / tune / profile / level、
  分辨率、VFR / CFR、音频编码与码率、MP4 / MKV、faststart、元数据和额外参数。
- GUI 可切换内置或用户替换的 FFmpeg 核心，执行“编译存在 + 硬件真实初始化”
  能力检测，并提供实时命令预览、FFmpeg Console、暂停、继续和停止。
- CLI 为硬字幕提供完整编码参数、`--print-command` 与 `--extra-args`；高级用户还可用
  `--raw-ffmpeg` 执行自行核对的完整命令。

详见 [FFmpeg 合成导出](docs/guide/video-synthesis.md)。

### 5. 双语样式、阶段交付与可观察任务

- SRT / ASS 的双语布局可贯穿导入、编辑与合成；ASS 支持字体、扩展样式字段，以及
  1080p、4K、house 等预设。
- 转录、初版字幕与后处理阶段始终保存规范 SRT 检查点，也可按统一布局和样式为每个
  实际完成的阶段自动导出 ASS 或 VTT。
- GUI 提供独立的全局运行日志；GUI 与 CLI 都会输出结构化阶段摘要，明确记录实际产物、
  ForcedAligner 应用/降级状态、规则回退和失败原因。

详见 [工作流程](docs/guide/workflow.md)与[字幕样式](docs/guide/subtitle-style.md)。

## 端到端工作流

```text
音视频输入
  → ASR 转录与字词级对齐
  → 本地聚合或 LLM 语义断句
  → 字幕优化
  → 非 LLM / 单 LLM / 双角色 LLM 翻译
  → 字幕后处理与质量审计
  → 软字幕封装或硬字幕合成
```

每个阶段都可独立使用。CLI 和 GUI 会输出阶段摘要、降级状态与日志，
便于定位长任务中的失败、回退和实际产物。

## 快速开始

需要 Python 3.10–3.12，推荐 Python 3.12、`uv` 和 Git：

```bash
git clone https://github.com/JsonBorn98/VideoCaptioner.git
cd VideoCaptioner
uv sync --python 3.12
uv run videocaptioner doctor --profile gui
uv run videocaptioner
```

桌面运行时会使用内置或用户配置的 FFmpeg。Qwen3 ASR / ForcedAligner
属于可选重型组件，可在 **设置 → 转录配置 → Qwen 组件管理** 中按需安装。

完整安装说明见 [开始使用](docs/guide/getting-started.md)。

## CLI 命令行

```bash
# MiMo API 转录 + Qwen3-ForcedAligner
uv run videocaptioner transcribe video.mp4 --asr mimo-asr --word-timestamps

# Qwen3 本地转录
uv run videocaptioner transcribe video.mp4 --asr qwen-local --word-timestamps

# 增强型双角色 LLM 翻译
uv run videocaptioner subtitle input.srt \
  --translation-mode enhanced_llm \
  --target-language en

# 独立字幕后处理与 QA
uv run videocaptioner postprocess input.srt \
  --profile balanced --qa-report

# 硬字幕 + NVENC；先预览 FFmpeg 命令
uv run videocaptioner synthesize video.mp4 -s subtitle.srt \
  --subtitle-mode hard --video-encoder h264_nvenc \
  --cq 23 --print-command

# 完整工作流
uv run videocaptioner process video.mp4 --target-language ja
```

运行 `uv run videocaptioner <命令> --help` 查看实时参数；完整说明见
[CLI 文档](docs/cli.md)。

## LLM 与隐私

LLM 功能需要用户自行选择服务商并配置凭据。项目不提供或推广 API 中转服务，
也不会把 API Key 上传到项目服务器。增强型翻译的请求日志可包含字幕与 Prompt
全文，请按内容敏感度决定是否保留日志。

## 开发

```bash
uv sync --python 3.12
uv run ruff check .
uv run pyright
uv run pytest -m "not integration"
cd docs
bun install
bun run docs:build
```

桌面构建：

```bash
uv run --with pyinstaller --with static-ffmpeg python scripts/build_desktop.py --clean
```

## 许可证与归属

本项目按 [GPL-3.0](LICENSE) 发布。本仓库基于 VideoCaptioner 的历史代码继续开发；
原作者版权声明保留在 LICENSE 与 Git 历史中。
