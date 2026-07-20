# Context Map

## Contexts

- [Subtitle Translation](./CONTEXT-TRANSLATION.md) — 从源字幕建立翻译上下文、统一疑难术语并生成初版字幕
- [Subtitle Postprocessing](./CONTEXT.md) — 字幕内容生成后的质量评估与改善（阅读速度、时间轴、语义修复）
- [Video Synthesis Encoding](./CONTEXT-VIDEO-SYNTHESIS.md) — 将成型字幕烧录/内嵌进视频，并按用户指定的编码格式与参数产出最终媒体文件

## Relationships

- **Subtitle Translation → Subtitle Postprocessing**：翻译阶段完成翻译质量审计并产生「初版字幕」；只有初版字幕进入后处理，术语表和翻译审计逻辑不向后处理模块延伸。
- **Subtitle Postprocessing → Video Synthesis Encoding**：后处理产生的「活动字幕输出」作为视频合成的字幕输入；两者通过 SRT 或内存字幕快照交接，不共享样式或编码语义。
