# 快速示例：从视频到成片

这个示例展示当前 fork 的四个主要阶段。实际耗时、费用和输出质量取决于素材、
模型、硬件与服务商配额，文档不提供固定倍率或成本承诺。

## 1. 转录与时间轴

本地 Qwen：

```bash
uv run videocaptioner transcribe demo.mp4 \
  --asr qwen-local \
  --word-timestamps \
  -o demo.srt
```

MiMo API + 本地 ForcedAligner：

```bash
uv run videocaptioner transcribe demo.mp4 \
  --asr mimo-asr \
  --word-timestamps \
  -o demo.srt
```

请求字词级时间戳后，对齐异常会重试；仍无法取得可靠证据时会生成并标记降级时间轴。

## 2. 双角色 LLM 翻译

```bash
uv run videocaptioner subtitle demo.srt \
  --translation-mode enhanced_llm \
  --target-language en \
  -o demo-translated.srt
```

增强模式会额外生成项目术语表与 Markdown 审计报告。再次处理同一项目时可导入术语表：

```bash
uv run videocaptioner subtitle next.srt \
  --translation-mode enhanced_llm \
  --target-language en \
  --glossary "【项目术语表】demo.vcglossary.json"
```

## 3. 字幕后处理

```bash
uv run videocaptioner postprocess demo-translated.srt \
  --profile balanced \
  --qa-report
```

该阶段会处理标点、短间隙、单句时长、阅读速度和结构问题。需要只看报告、不修改字幕时：

```bash
uv run videocaptioner postprocess demo-translated.srt \
  --profile balanced \
  --mode analyze \
  --qa-report
```

## 4. FFmpeg 合成

先预览命令：

```bash
uv run videocaptioner synthesize demo.mp4 \
  -s demo-translated.srt \
  --subtitle-mode hard \
  --video-encoder h264_nvenc \
  --cq 23 \
  --print-command
```

确认当前机器支持 NVENC 后，移除 `--print-command` 执行。没有可用硬件编码器时可改用
`x264` 或其他实际可用的 CPU 编码器。

## GUI 中的对应入口

1. 转录页：选择 MiMo 或 Qwen，并配置 Runtime / 模型。
2. 字幕优化与翻译页：选择增强型 LLM，绑定主翻译和高级校对方案。
3. 字幕后处理页：选择方案、主侧、分析/应用模式和可选媒体对齐。
4. 视频合成页：检查编码器能力、预览 FFmpeg 命令并运行任务。

---

- [快速开始](/guide/getting-started)
- [翻译模式与双角色校对](/config/translator)
- [字幕后处理](/guide/subtitle-postprocessing)
- [FFmpeg 合成导出](/guide/video-synthesis)
