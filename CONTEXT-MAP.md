# Context Map

## Contexts

- [Subtitle Translation](./CONTEXT-TRANSLATION.md) — 从源字幕建立翻译上下文、统一疑难术语并生成初版字幕
- [Subtitle Postprocessing](./CONTEXT.md) — 字幕内容生成后的质量评估与改善（阅读速度、时间轴、语义修复）
- [Video Synthesis Encoding](./CONTEXT-VIDEO-SYNTHESIS.md) — 将成型字幕烧录/内嵌进视频，并按用户指定的编码格式与参数产出最终媒体文件

## Relationships

- **Subtitle Translation → Subtitle Postprocessing**：翻译阶段完成内容翻译、校订与翻译质量审计并产生「初版字幕」，显示长度、观看分段和阅读节奏约束统一归属后处理。后处理的问题重译跟随原任务的翻译方式与对应校对流程，携带问题所在上下文；具体翻译资产的复用范围见下方设计记录中的待定项。
- **Subtitle Postprocessing → Video Synthesis Encoding**：后处理产生的「活动字幕输出」作为视频合成的字幕输入；两者通过 SRT 或内存字幕快照交接，不共享样式或编码语义。

## Active Design

- [字幕后处理职责与限长设计记录](./docs/dev/subtitle-postprocessing-design-record.md)：继续长度约束、原文保护、批量重译或失败回退讨论时先读；已确认方向与待定实现细节分别记录。
