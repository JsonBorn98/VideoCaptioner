# 打包不可变默认 FFmpeg，用户替换解锁完整编码器集

## Status
accepted

VideoCaptioner 以 GPL-3.0 发布，随包分发 gyan.dev "essentials"（GPLv3、FFmpeg 8.0）的 `ffmpeg.exe`/`ffprobe.exe`。二进制在**构建时**由 static-ffmpeg 拉取并打进冻结包（`resource/bin`、`AppData/` 均 git 忽略，**从不进仓库**）；作为独立可执行文件经 subprocess 调用——属 GNU 的"纯粹聚合"，应用非 FFmpeg 衍生作品，捆绑合规。

ffmpeg 核心有两个来源，并作为显式设置项 `ffmpeg_source ∈ {default, custom}`：

- **默认（内置）**：不可变、随版本固定、应用更新时被替换；追求**通用性**而非大而全或极致性能。含 x264/x265/libaom(慢)/VP9/NVENC/QSV/AMF/原生 AAC/Opus。
- **自定义**：用户放入专用 git 忽略目录（生产 `APPData/bin`，dev 用独立 `AppData/bin` 以免与默认目录塌缩），**跨版本更新保留**；用于解锁默认构建缺失的编码器（最典型是快速 AV1 的 **SVT-AV1**）与自建 fdk-aac 等。

`get_ffmpeg_path()` 依此设置解析，缺失/损坏回退默认并告警。测试只覆盖默认构建的能力集；自定义 ffmpeg 的 GPL 与专利合规由用户负责。

## 硬约束
- 项目发布的构建**永不** `--enable-nonfree`/`libfdk-aac`/decklink/libmpeghdec/libnpp；AAC 用原生 `aac`。
- 保持 exe 独立 + subprocess，绝不静态/动态链接 libav* 进程内。
- 不重命名/包装/混淆二进制、不剥离声明、不加限制 GPL 权利的 EULA。

## Consequences
每次发布需归档 `ffmpeg -version/-L/-buildconf`、附 GPLv3/LGPL 许可证文本、提供对应源码或 3 年书面 offer、关于页署名（详见 `docs/plans/video-synthesis-encoding-overhaul.md` §10.2）。专利（H.264/HEVC）与版权是两根轴，对零收入 FOSS 现实风险低但非零，AV1 免专利费——风险信息非法律意见。
