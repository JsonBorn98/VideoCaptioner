# 任务方案：字幕视频合成模块重构（Handbrake 式编码控制）

> 领域词汇见 [`CONTEXT-VIDEO-SYNTHESIS.md`](../../CONTEXT-VIDEO-SYNTHESIS.md)；关键决策见 `docs/adr/0006~0008`。
> 本文是需求 grilling 后形成的实施蓝图，所有设计点均有对应决策记录。

## 1. 背景与现状

字幕视频合成（`VideoSynthesisInterface` → `VideoSynthesisThread` → 核心；CLI `synthesize` 共用 `SynthesisConfig`）当前实现：

- **三处硬编码 ffmpeg 编码调用点**：`video_utils.add_subtitles`（软/SRT 硬字幕）、`ass_renderer.render_ass_video`（ASS 硬烧）、`rounded_renderer.render_rounded_video`（圆角背景，分批链式重编码）。
- 视频编码器**写死 `libx264`**（webm 走 `libvpx-vp9`）；`-hwaccel cuda` 仅**解码加速**，编码仍走 CPU。
- 质量 = `VideoQualityEnum` 四档黑盒 → `(crf, preset)`；无码率模式。
- 音频 `-acodec copy` **从不重编码**；无分辨率/色彩/帧率/faststart/元数据控制；无自定义参数/命令预览。
- 输出命名硬编码 `【卡卡】{stem}.mp4`（GUI）；ffmpeg 走裸 PATH 调用。
- 打包：`build_desktop.py` 构建时用 static-ffmpeg 拉 **gyan essentials（GPLv3、FFmpeg 8.0）** 进冻结包；`resource/bin`、`AppData/` 均 **git 忽略**，二进制不进仓库。

## 2. 目标与范围

**做（本次）**：核心集中命令构建器 + ffprobe 探测；完整编码器/质量/分辨率/音频/杂项控制（对齐 Handbrake）；真正的硬件**编码**加速；GUI 页面重构；CLI 完整编码 flag + 命令透传闭环；ffmpeg 来源管理 + 许可证合规；新输出命名。

**不做（后续）**：命名编码预设/模板（ADR 0003 范式，deferred）；完整命令双向反解析；默认打包升级到含 SVT-AV1 的大构建；多音轨处理；圆角模式的分辨率缩放/HDR 保真。

## 3. 架构设计

### 3.1 集中命令构建器（唯一事实源）

新增 `core/synthesis/` 包：

- `EncodeSettings`（dataclass）：结构化编码设置，**唯一事实源**。
- `command_builder.py`：`build_ffmpeg_command(inputs, subtitle_filter, settings, probe) -> list[str]`，产出 argv；被**软/copy、ASS 硬烧、圆角最终编码**三条路共用（ADR 0007）。
- `ffmpeg_env.py`：`get_ffmpeg_path()` / `get_ffprobe_path()` 解析器（见 §10）。
- `media_probe.py`：`probe(path) -> MediaProbe`，`ffprobe -show_streams -show_format -of json`，拿宽高比/帧率/`color_range/primaries/transfer/space`/像素位深/音频编码/时长；ffprobe 缺失回退现有正则。
- `encoder_catalog.py`：编码器目录（§12）+ 能力探测（ADR 0008）。
- `command_parse.py`：命令预览白名单反解析（§8）。
- `naming.py`：输出命名（§9），GUI/CLI 共用。

三处旧编码点改为：生成字幕滤镜/输入 → 调 `build_ffmpeg_command` → 执行 + 进度解析（进度解析逻辑抽公共函数）。

### 3.2 数据模型

`SynthesisConfig` 内嵌 `EncodeSettings`（保留 `need_video/soft_subtitle/render_mode/subtitle_layout` 及样式字段）：

```
video_encoder: str            # 目录键，如 'libx264' / 'hevc_nvenc' / 'copy' / 自定义原名
encode_mode: 'cq' | 'abr'
quality: int                  # 固定品质数值（按编码器原生刻度）
bitrate_kbps: int             # 平均码率
two_pass: bool                # 仅 CPU
turbo_first_pass: bool        # 仅 CPU 2-pass
enc_preset / enc_tune / enc_profile / enc_level: str | None  # 按目录，auto=None
fast_decode: bool             # 仅 x264/x265
target_height: int | None     # None=与源相同、不放大
fps: float | None             # None=与源相同
vfr: bool = True
audio_encoder: str            # 'copy'（默认）/ 'aac' / 'libopus' / ...
audio_bitrate_kbps: int
container: 'mp4' | 'mkv'
faststart / keep_metadata / preserve_color / start_zero: bool
extra_args: str               # 追加参数
raw_command: str | None       # 逐字执行（CLI/预览）
ffmpeg_source: 'default' | 'custom'
```

旧 `VideoQualityEnum` 迁移：`ULTRA_HIGH→cq18 / HIGH→cq23 / MEDIUM→cq28 / LOW→cq32`（x264），首启一次性迁移到新模型；无旧值则用 §11 默认。

## 4. 功能规格（对应 10 条需求）

| # | 需求 | 决策 |
|---|---|---|
| 1 | ffmpeg 替换/升级/开目录/架构标注/可用性测试 | §10；来源=默认(内置)/自定义(git 忽略目录)；纯手动替换、不联网更新 |
| 2 | 编码格式 × 三厂商 + 自定义 | 单一分组下拉列出具体编码器 + 自定义 + copy（ADR 0008/§12） |
| 3 | 去黑盒档位 → 固定品质(RF) / 平均码率(kbps, CPU 2-pass+首遍加速) | §12；固定品质用编码器**原生刻度** |
| 4 | 编码器预设/微调/快速解码/配置/级别 | 策划目录，只显示当前编码器适用项，profile/level 默认 auto |
| 5 | 分辨率预设 + 自定义 + 禁变形 | 高度模板、自定义只填高度、宽按 AR 取偶、**不放大** |
| 6 | faststart/对齐开头/元数据/色彩/帧率/VFR/mp4 | 见 §11；"对齐开头"=良性起始归零 `-avoid_negative_ts make_zero` |
| 7 | 音频编码格式 + 码率 + 采样率 auto | 音频编码器下拉，**默认直通 copy**；原生 aac（非 fdk）；容器不兼容回退 aac |
| 8 | 自定义 ffmpeg 参数框 | `extra_args` 追加在末段 |
| 9 | 完整命令展示 + 可编辑 + 双向同步 + 校验 | 白名单最佳努力双向（ADR 0007/§8） |
| 10 | 前缀 `【视频合成】` + 后缀 | §9 |

## 5. GUI 页面设计

Fluent 可展开设置卡，整页滚动（渐进式披露）：

- 顶部：字幕/视频文件输入 + 工具条 `[软/硬字幕] [渲染模式] [⚙ ffmpeg 核心] [▶ 开始合成]`；底部进度条（保留现状）。
- **【视频编码】**（常驻）：编码器（分组下拉 + 置灰不可用 + "检测到 NVENC 可加速"chip）→ 编码方式（固定品质/平均码率 分段）→ 质量（RF/CQ 拉条含"越小越好 + 映射参数"提示 ↔ 码率输入 + `2-pass`/`首遍加速` 仅 CPU 亮）。
- **【编码器选项】**（默认折叠）：预设/微调/快速解码/配置/级别，**仅显示当前编码器支持项**。
- **【分辨率与帧率】**（折叠）：分辨率（与源相同/720/1080/1440/4K/自定义高度）+ 帧率（与源相同/自定义 + VFR/CFR）。
- **【音频】**（折叠）：音频编码器 + 码率（重编码才亮）。
- **【其他·高级】**（折叠）：faststart/元数据/起始归零/色彩透传/容器 + 自定义参数框。
- **【命令预览】**（本页唯一重点亮点）：可编辑、实时双向、带校验状态行。

圆角背景模式下，分辨率/色彩相关控件显示"此模式下不适用"。

## 6. CLI 设计

`synthesize` 暴露完整编码配置为 flag：`--video-encoder --encode-mode {cq,abr} --quality N --bitrate K --preset --tune --profile --level --fast-decode --height N --fps --cfr/--vfr --audio-encoder --audio-bitrate --container {mp4,mkv} --[no-]faststart --[no-]keep-metadata --ffmpeg PATH --extra-args "..."`。

透传闭环（ADR 0007）：`--print-command`（模块拼好含字幕滤镜的正确命令并打印）→ agent 改编码尾巴 → `--raw-ffmpeg "<cmd>"`（`shlex` 拆分、**无 shell**、**强制 argv[0]=托管 ffmpeg**、拒绝非 ffmpeg 可执行）。GUI 专属（动态 UI/双向联动/核心管理面板/探测置灰）不进 CLI。

## 7. 编码方式与参数

- **固定品质**：数值按编码器原生刻度映射（§12），拉条范围/默认随编码器变。
- **平均码率**：`-b:v {k}k`；`two_pass` 仅 CPU（`-pass 1`（`-an -f null`）+ `-pass 2`，`-passlogfile`）；`turbo_first_pass` 给首遍加速设置，默认开、仅 CPU 2-pass 时出现。硬件编码器 ABR 走单遍。
- copy/直通：`-c:v copy`（硬烧录时不可选，派生规则）。

## 8. 命令预览与双向同步（ADR 0007）

- 结构化 `EncodeSettings` 为唯一事实源；预览由 `build_ffmpeg_command` 实时生成。
- 编辑应用时白名单反解析：识别 `-c:v/-crf/-cq/-qp/-b:v/-preset/-tune/-profile:v/-level(x265 特例见 §16.13)/-r/-c:a/-b:a/-movflags/color*` → 回填控件；未识别 token 经**幂等去重**后进 `extra_args`（见 §16.4）；随后从状态重建（归一化）。
- 字幕滤镜与分辨率共用的 `-vf`（`ass` + `scale`，`ass` 在前）**整体系统托管**，`scale` **不可在命令文本里编辑**——分辨率只经高度控件改（见 §16.4 / §16.9）；输入/输出/临时路径亦系统托管。
- 校验：引号/括号配平 + 基本结构（含输入/输出）+ 可选 dry-run；错误以界面口吻提示。

## 9. 输出命名

- 前缀 `【视频合成】`（替换 `【卡卡】`）。
- 重编码：`【视频合成】{原名}_{高度}p_{编码器}_{编码格式}_{质量}.{容器}`
  - 编码器 token：`x264/x265/svt/aom/nvenc/qsv/amf/vpx`（自定义用原名，backend token 全保留）
  - 编码格式：`h264/h265/av1/vp9`
  - 质量：固定品质 `{n}Q`；码率 `{kbps}k`
- 直通/软字幕：`【视频合成】{原名}_{高度}p_copy.{容器}`
- 已存在则覆盖（`-y`，保持现状）；GUI/CLI 共用 `naming.py`，CLI `-o` 可覆盖。
- 示例：`【视频合成】旅行vlog_1080p_nvenc_h264_34Q.mp4`

## 10. ffmpeg 核心管理 + 许可证合规

### 10.1 来源模型（Q13 精化）

| 来源 | 位置 | 性质 |
|---|---|---|
| 默认（内置） | `resource/bin`（dev）/ 冻结包内（生产） | 不可变、随版本固定、更新时替换；构建时注入、git 忽略 |
| 自定义 | `APPData/bin`（生产）/ `AppData/bin`（dev，修暗坑用独立 git 忽略目录） | 跨更新保留；"打开核心目录"开此处 |

- `ffmpeg_source` 设置 + `get_ffmpeg_path()` 解析；缺失/损坏回退默认并告警。
- "可用性测试"= `ffmpeg -version`+`-encoders`+`-hwaccels`+ 硬件编码器真实初始化探测，结果缓存并驱动编码器置灰（即 ADR 0008 的能力探测触发口）。
- 架构：显示应用期望架构（x64/x86）；尽力解析 PE 头得当前 ffmpeg 实际架构、不匹配告警。
- 无联网自动更新。

### 10.2 许可证合规（GPL-3.0，经对抗性核查确认）

应用为 GPL-3.0；ffmpeg/ffprobe 作独立 exe 经 subprocess 调用 = **纯粹聚合**，非衍生作品，捆绑合规。**红线：永不发布 `--enable-nonfree`/`libfdk-aac`/decklink/libmpeghdec/libnpp 构建；AAC 用原生 `aac`。** 发布清单：

1. 每次发布记录并归档 `ffmpeg -version`/`-L`/`-buildconf`/`-encoders`，确认含 `--enable-gpl --enable-version3` 且**不含** `--enable-nonfree`/`libfdk-aac`。
2. 随包附 `COPYING.GPLv3`（+`COPYING.LGPLv2.1`）与 FFmpeg `LICENSE.md`。
3. 为所发布的 ffmpeg 二进制提供对应源码或 3 年书面 offer（同版本 + 构建配置），与二进制同处托管、版本锁定。
4. 覆盖编进二进制的 GPL 库（x264/x265/xvid）对应源码。
5. 保持 exe 独立 + subprocess，绝不链接 libav* 进程内。
6. 不重命名/包装/混淆二进制、不剥离声明、不加限制 GPL 权利的 EULA。
7. 关于页/文档/安装器署名 "This product bundles FFmpeg (GPLv3); source at <link>"。
8. 确保本仓库（发布 tag）对应源码公开。
9. 若做"下载/替换 ffmpeg"，项目绝不去拉 nonfree；替换件的 GPL 与专利合规由用户负责。

**专利提醒**（风险信息非法律意见）：版权与专利两轴分离；H.264/HEVC 专利池对零收入 FOSS 现实风险低但非零，AV1 免专利费。

## 11. 默认值汇总（裸调用即可跑通）

| 项 | 默认 |
|---|---|
| 视频编码器 / 格式 / 容器 | `libx264` / H.264 / mp4 |
| 编码方式 / 质量 | 固定品质 / `23`（各编码器给各自合理默认，见 §12） |
| 分辨率 / 帧率 | 与源相同、不放大 / 与源相同 + VFR |
| 音频 | 直通 copy |
| 色彩 | 全套标签 + 位深透传 |
| 杂项 | faststart 开、元数据 开、起始归零 开 |
| ffmpeg 来源 | 默认（内置） |
| 硬件编码 | 不自动启用；检测到时显眼提示可切换 |

## 12. 编码器 → ffmpeg 参数映射表（实现权威）

> ✓=开箱（essentials）可用；⚠=需替换 ffmpeg。质量数值越小越好。

| 编码器（目录键） | 格式 | 开箱 | 固定品质 | 默认 | 码率/2-pass | preset | 微调/快速解码 | profile/level |
|---|---|---|---|---|---|---|---|---|
| libx264 | h264 | ✓ | `-crf 0–51` | 23 | `-b:v`+2pass✓ | ultrafast…veryslow(medium) | film/animation/grain/…; fastdecode✓ | baseline/main/high/high10…; 3.0–6.2 |
| libx265 | h265 | ✓ | `-crf 0–51` | 28 | `-b:v`+2pass✓ | ultrafast…veryslow | psnr/ssim/grain/animation; fastdecode✓ | main/main10/…; `-x265-params level-idc=` |
| libsvtav1 | av1 | ⚠ | `-crf 0–63`(+`-b:v 0`) | 30 | 单遍 | `-preset 0–13`(8) | `-svtav1-params tune=` | — |
| libaom-av1 | av1 | ✓(慢) | `-crf 0–63`(+`-b:v 0`) | 30 | `-cpu-used 0–8`(6);2pass✓ | — | — | — |
| h264_nvenc | h264 | ✓* | `-rc vbr -cq 0–51` | 23 | `-b:v`+`-multipass` | p1–p7(p5) | `-tune hq/ll/ull/lossless` | baseline/main/high; level |
| hevc_nvenc | h265 | ✓* | `-rc vbr -cq 0–51` | 26 | 同上 | p1–p7 | 同上 | main/main10 |
| av1_nvenc | av1 | ✓* | `-rc vbr -cq 0–51` | 30 | 同上 | p1–p7 | 同上 | — |
| h264_qsv / hevc_qsv / av1_qsv | h264/h265/av1 | ✓* | `-global_quality N` | 23/26/30 | `-b:v` | veryfast…veryslow | — | profile/level |
| h264_amf / hevc_amf / av1_amf | h264/h265/av1 | ✓* | `-rc cqp -qp_i/-qp_p N` | 23/26/30 | `-b:v` | speed/balanced/quality | — | profile/level |
| libvpx-vp9 | vp9 | ✓ | `-crf 0–63`(+`-b:v 0`) | 31 | 2pass✓ | — | — | — |
| copy | — | ✓ | — | — | — | — | — | — |
| 自定义 | — | 探测 | 通用文本 | — | — | 自由 | — | — |

\* 硬件编码器另需匹配 GPU + 当前驱动；由能力探测判定。

**通用参数**：色彩 `-color_range/-color_primaries/-color_trc/-colorspace`（探测源）+ 保留 `pix_fmt`/位深；faststart `-movflags +faststart`（mp4/mov）；元数据 `-map_metadata 0 -map_chapters 0`；帧率默认不设 + `-fps_mode vfr`；起始归零 `-avoid_negative_ts make_zero -fflags +genpts`；音频 `-c:a copy`(默认) / `aac`(原生) / `libopus`(mp4 提示走 mkv) / `ac3`/`libmp3lame`/`flac`，采样率不设(auto)。

## 13. 分阶段实施

1. **核心底座**：`EncodeSettings` + `command_builder` + `ffmpeg_env`(来源模型/dev 暗坑修复) + `media_probe`(ffprobe) + `naming`；三处编码点收敛到构建器（软/copy、ASS 硬烧全量；圆角仅最终编码接入）。含 `VideoQualityEnum` 迁移。
2. **编码器目录 + 能力探测**：`encoder_catalog` + 探测缓存；置灰逻辑。
3. **CLI**：完整 flag + `--ffmpeg` + `--print-command`/`--raw-ffmpeg`/`--extra-args`。
4. **GUI 重构**：分区折叠卡 + 动态按编码器披露 + ffmpeg 核心管理面板 + 命令预览（白名单双向 + 校验）。
5. **合规**：发布流程接 §10.2 清单（构建脚本自动导出 `-version/-L/-buildconf`、附许可证文本与源码 offer）。

## 14. 测试策略

- **仅覆盖默认（内置）ffmpeg** 的能力集；自定义 ffmpeg 仅最佳努力探测、不入测试。
- 命令构建器：纯函数单测（各编码器/模式/分辨率/音频/色彩组合 → 期望 argv），无需真实 ffmpeg。
- 命名/迁移/白名单反解析：纯逻辑单测。
- 集成（标记 `integration`，无 ffmpeg 时跳过）：对内置构建实测 x264/libaom + 一条软字幕 + 命名/探测；硬件编码器测试按 GPU 存在与否跳过。
- 目录断言：内置构建**应有** x264/x265/libaom/vp9/aac/opus，**应无** svt-av1（作为探测置灰的回归基线）。

## 15. 风险与未决

- 硬件编码器探测跨机型抖动 → 缓存 + 手动"可用性测试"重探。
- 白名单反解析非幂等边角 → 以"重建归一化"兜底，未识别一律进 `extra_args`。
- 圆角管线链式重编码低效/8-bit → 列为后续优化（单遍 filter_complex 或转 ass）。
- 色彩/HDR：SDR 字幕烧 HDR 画面偏暗为 ffmpeg 通病，本次 HDR 透传、字幕颜色尽力。
- deferred：命名编码模板、多音轨、圆角分辨率/HDR、默认打包升级。

## 16. 完备性审查修订（实现细则）

对抗性完备性审查发现的 13 项缺口/矛盾（对着 10 条需求 + 真实代码），逐条给出实现级结论。以下为规范性要求。

1. **输出命名依赖探测后的有效高度（§9/§3.1）**：有效高度 = `min(请求高度或源高度, 源高度)`（承接"不放大"）。**探测在 worker 内执行一次**（`VideoSynthesisThread.run` / CLI `run`），`naming.py` 消费探测结果得出最终 `output_path` 并**回传 GUI**（信号）供 `open_video_folder`/`process` 使用；`task_factory.create_synthesis_task` 只设临时占位路径。探测失败 → 回退 `get_video_info` 正则；仍未知则高度 token 用 `src`。copy/软字幕同样按此拿高度。
2. **圆角最终编码接入构建器（§3.1/§15）**：构建器提供第二入口 `build_ffmpeg_command_multi(inputs, filter_complex, maps, settings, probe, extra)` 承载 `filter_complex`/多 PNG 输入/`-map [vN]`/`-map 0:a?`/`-t {duration}`。圆角中间批仍 `libx264 -crf 0 -pix_fmt yuv420p`，故 `preserve_color`/`keep_metadata` 在圆角路**数据模型层即置为 inert**（非仅 GUI 隐藏），并在文档标注"圆角不支持色彩/元数据保真"。
3. **硬件解码 + 软件 `ass` + 硬件编码（Req 2/§12）**：存在烧录字幕滤镜时，解码加速**只用 `-hwaccel <api>`、不加 `-hwaccel_output_format`**（帧落系统内存，CPU `ass` 滤镜才能工作），再由硬件编码器上传编码；不走全 GPU 管线。`check_cuda_available` 及所有探测改走 `get_ffmpeg_path()`（见 §16.6）。
4. **命令预览：`scale` 移出可编辑白名单 + `extra_args` 幂等（Req 9/§8）**：`-vf` = 系统托管的 `ass,scale`（`ass` 在前）；分辨率只经高度控件编辑，不在命令文本里改，避开与 `ass='<转义路径>':fontsdir=...` 同参数拆分的脆点。`extra_args` 往返幂等：反解析时把未识别 token 与"当前构建器输出 token 集"做差集，只存真正额外的，重建时构建器输出 token 不重复计入 `extra_args`。
5. **CLI `--quality` 冲突迁移（Req 3/§6）**：旧 `--quality {ultra,high,medium,low}` 与 TOML `synthesize.quality="medium"`（`main.py:505`、`cli/config.py:148`、`synthesize.py:32`）与新的整数 `--quality N` 冲突。**弃用旧档位**：`--quality` 改表整数 CQ/RF；旧字符串值自动迁移（`ultra→18/high→23/medium→28/low→32` + `encode_mode=cq`）并打弃用提示；新增 `synthesize.encode_mode/bitrate/video_encoder/...` TOML 键。
6. **全部 ffmpeg/ffprobe 调用点收敛 + 修 PATH bug（Req 1/§10.1/§13）**：除三处编码点外，`video2audio`、`check_cuda_available`、`get_video_info`、`_extract_thumbnail`、`_get_video_resolution`(ass)、`render_ass_preview`、rounded `_get_video_info`、`render_preview` 默认背景生成等**全部**改走 `get_ffmpeg_path()/get_ffprobe_path()`，否则 `ffmpeg_source=custom` 时探测/预览/缩略图用内置、编码用自定义，不一致。**同时修 `config.py:116-118` 的 PATH 优先级 bug**：现循环 `[FASTER_WHISPER, BIN, BUNDLED]` 逐个前插，导致 `BUNDLED_BIN_PATH` 最终排在最前、内置反而盖过用户 `BIN_PATH`，与注释"用户优先"相反（dev 下 BIN==BUNDLED 无影响，生产下有）。修正前插顺序使用户目录真正优先。
7. **软字幕编码格式随容器 + copy 回退归属（Req 6/7/§12）**：软字幕字幕编码按容器选择——mp4→`mov_text`，mkv→`srt`（`video_utils.py:233` 现写死 `mov_text`，mkv 会失败）。音频 copy 与容器不兼容 → 回退 aac 的判断由**调用方**用 `probe.audio_codec` vs 容器算好后传入 `settings`（构建器保持纯函数）。
8. **多阶段/多批的首遍与进度（Req 3/§7/§3）**：2-pass 的 **pass1 必须携带同一 `-vf ass=` 滤镜**（否则码率控制统计与烧字幕输出不符）；`-passlogfile` 落临时目录、`finally` 清理。统一单调进度模型 `ProgressAggregator`：2-pass 按 pass1 0–50%/pass2 50–100%；圆角按批次映射 30–100%；对外始终 0–100 单调。
9. **`scale` 与 ASS 样式缩放的顺序（Req 5/§5）**：滤镜链**强制 `ass` 在 `scale` 之前**（libass 按源分辨率渲染、整帧再缩放）；`_scale_ass_style` 与 `PlayResX/Y` **仍按源高度**计算，不改用目标高度，避免双重缩放/字号错误。
10. **copy 直通抑制色彩/像素格式标志（Req 6/§11）**：`video_encoder == copy`（直通/软字幕）时，**跳过** `-color_*`/`-pix_fmt`/编码器侧标志（copy 下会被忽略或报错）；音频 copy 时同样跳过码率。
11. **自定义编码器的质量与命名（Req 2/3/10）**：自定义（未列出）编码器**禁用固定品质拉条**（质量经 `extra_args`/`raw` 提供）；命名 token：编码器 = 净化后的自定义名，编码格式 = `custom`（或省略），质量 token 省略（ABR 则用 `{kbps}k`）。
12. **架构标注按平台（Req 1/§10.1）**：PE 头解析与 x64/x86 措辞**限 Windows**；macOS/Linux 用 Mach-O/ELF 或 `platform.machine()`，提示文案按平台给（`build_desktop.py` 也产 darwin/linux 包）。
13. **x265 级别经 `-x265-params level-idc=`（Req 4/9/§12）**：x265 的 level 用 `-x265-params level-idc=` 而非 `-level`；构建器按此发、预览反解析特例识别。x265/svt-av1 的其余 `-x265-params`/`-svtav1-params` 同理整体识别。
14. **能力回归基线补充（§14/ADR 0006，minor）**：内置 essentials 的 `-encoders` **应列出** `h264_nvenc/hevc_nvenc/h264_qsv/...`（wrapper 已编译，无 GPU 也列出）——将其纳入回归断言；而硬件编码器的**功能性探测**需真实 GPU，CI 无 GPU 时跳过。二者分开：编译存在（可断言）vs 运行可用（探测/跳过）。
