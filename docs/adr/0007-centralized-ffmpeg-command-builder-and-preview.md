# 集中化 ffmpeg 命令构建器与可编辑命令预览

## Status
accepted

历史上有三处硬编码的 ffmpeg 编码调用点（`video_utils.add_subtitles`、`ass_renderer.render_ass_video`、`rounded_renderer.render_rounded_video`）各自拼命令。本次收敛到单一 `build_ffmpeg_command()`：结构化的 `EncodeSettings` 是**唯一事实源**，构建器据此生成 argv，被软/copy、ASS 硬烧、圆角最终编码三条路共用。

GUI 展示**可编辑的实时命令预览**，采用**白名单最佳努力双向同步**：识别的标志（`-c:v/-crf/-cq/-qp/-b:v/-preset/-tune/-profile:v/-level/-r/scale/-c:a/-b:a/-movflags/color*`）解析回控件，其余 token 落入自定义参数框；随后从结构化状态重建命令归一化。字幕滤镜 `-vf ass=...` 与输入/输出/临时路径由系统托管、不参与用户编辑。执行永远从结构化状态重建。CLI 侧对应 `--print-command`（打印含字幕滤镜的正确命令）+ `--raw-ffmpeg`（`shlex` 拆分、无 shell、强制 argv[0]=托管 ffmpeg）+ `--extra-args`（追加）。

## Considered Options
- **完整双向反解析**（拒绝）：ffmpeg 命令空间过大、往返不幂等、UX 脆（UI 会与用户"打架"）；连 Handbrake 都不做。
- **只读预览**（拒绝）：不满足"编辑同步回选项"的诉求。
- **白名单最佳努力双向**（采纳）：覆盖绝大多数价值，未识别部分安全降级到自定义参数。
