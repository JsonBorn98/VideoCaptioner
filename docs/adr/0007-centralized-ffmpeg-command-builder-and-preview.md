# 集中化 ffmpeg 命令构建器与只读命令预览

## Status
accepted（命令预览：修订为只读；双向编辑列为后续）

历史上有三处硬编码的 ffmpeg 编码调用点（`video_utils.add_subtitles`、`ass_renderer.render_ass_video`、`rounded_renderer.render_rounded_video`）各自拼命令。本次收敛到单一 `build_ffmpeg_command()`：结构化的 `EncodeSettings` 是**唯一事实源**，构建器据此生成 argv，被软/copy、ASS 硬烧、圆角最终编码三条路共用。

GUI 展示**只读的实时命令预览**：由当前控件（含自定义参数框 `extra_args`）经 `build_ffmpeg_command` 实时生成，供用户核对与复制。用户手动介入编码参数的入口是独立的**自定义参数**输入框，预览会反映它。字幕滤镜、`scale` 与输入/输出/临时路径由系统托管。执行永远从结构化状态重建。CLI 侧对应 `--print-command`（打印含字幕滤镜的正确命令）+ `--raw-ffmpeg`（`shlex` 拆分、无 shell、强制 argv[0]=托管 ffmpeg）+ `--extra-args`（追加）。

## Considered Options
- **只读预览 + 独立自定义参数框**（采纳）：低风险、透明；用户仍可经自定义参数框与 CLI `--raw-ffmpeg` 手改命令。
- **白名单最佳努力双向**（后续优化方向）：识别常见标志回填控件、未识别归入自定义参数、往返需幂等。实现/UX 更复杂，暂缓。
- **完整双向反解析**（拒绝）：ffmpeg 命令空间过大、往返不幂等、UX 脆（UI 会与用户"打架"）；连 Handbrake 都不做。
