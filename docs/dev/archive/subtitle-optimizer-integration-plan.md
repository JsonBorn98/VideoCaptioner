# 字幕优化模块改造方案 —— 内化 ass-subtitle-optimizer 技能

> 状态：设计方案（待实施）｜日期：2026-07-06
> 本文档面向执行改造任务的开发者/Agent。所有 `file:line` 锚点基于编写时的工作区代码，行号可能漂移，**实施时请以符号搜索为准**。实施前请先阅读仓库根目录 `CLAUDE.md` 与 `AGENTS.md`。

---

## 1. 目标与原则

将用户个人技能 `ass-subtitle-optimizer`（ASS 字幕修复与视觉优化）中的优化策略**内化**为 VideoCaptioner 字幕优化与翻译模块的内置能力，使用户在软件工作流（CLI 与 GUI）中即可完成字幕的深度优化，而不必事后用外部脚本二次处理。

**硬性原则**（来自用户要求 + 仓库约定）：

1. **全部新功能均为可选、可调的优化选项**，默认关闭（或保持与现状完全一致的默认行为），由用户主动开启或调整参数。
2. 核心逻辑放 `core/`（禁止 Qt import），CLI 与 GUI 共用；配置需同时落 CLI（TOML/argparse）与 GUI（qconfig）两套系统。
3. 遵循现有代码惯例：LLM 步骤沿用 `agent_loop + 校验 + 规则回退` 三件套；规则步骤操作 `ASRData`，失败不阻断管线；时间戳单位毫秒。
4. 兼容 Python 3.10；ruff（E/F/I/W，行宽 100）；pyright basic。
5. 审计类功能"只报告不擅改"：不确定的问题进 QA 报告，交人工判断（技能的核心哲学）。

**非目标**（本次不做）：

- `repair_ass_format.py` 的"用参考文件修复损坏 ASS"能力（项目自身生成 ASS，不存在此场景）。
- `fc-match`/libass fontselect 字体诊断（可作为未来 `doctor` 命令增强，本次仅在文档提及）。
- 烧录后自动渲染样例帧比对（未来增强）。
- ASS 文件级的二次编辑工具（本方案全部在 `ASRData` 数据层实现，保存时生效）。

---

## 2. 现状与差距分析

### 2.1 现有字幕处理管线

数据模型：`ASRDataSeg`（`videocaptioner/core/asr/asr_data.py:52`，`text` 原文 + `translated_text` 译文并存，时间毫秒 int）；容器 `ASRData`（:106，**构造时丢弃空文本段并按 start_time 排序**——这是所有清理类功能必须小心的行为）。

两条调用链（阶段完全一致）：

```
CLI  videocaptioner/cli/commands/subtitle.py:run()
GUI  videocaptioner/ui/thread/subtitle_thread.py:SubtitleThread.run()

from_subtitle_file(input)
  → ①(词级时) SubtitleSplitter.split_subtitle      # LLM 断句，规则回退
  → ② need_optimize: SubtitleOptimizer             # LLM 纠错，批处理+相似度校验+对齐修复
        → asr_data.remove_punctuation()            # 硬编码：仅删 text/translated_text 末尾 ，。
  → ③ need_translate: Translator.translate_subtitle # 原地写 seg.translated_text
        → asr_data.remove_punctuation()
  → ④ asr_data.save(output, layout[, ass_style, w, h])
```

关键锚点：CLI 管线 `subtitle.py:192-254`；GUI 管线 `subtitle_thread.py:123-220`；全流程编排 `cli/commands/process.py`（链式调用各命令 `run`，无需单独改动）。

现有能力清单（与技能相关的部分）：

| 能力 | 现状 | 位置 |
|---|---|---|
| 闪烁/间隙处理 | 有雏形：`optimize_timing(threshold_ms=1000)`，gap<1s 时把边界吸附到间隙 3/4 处；**仅 ASR 转录阶段调用**（`core/asr/transcribe.py:73`），字幕管线不触发，阈值不可配 | `asr_data.py:495` |
| 阅读速度（CPS）审计 | **完全没有**（无时长下限/上限、无密度检查） | — |
| 文本规范化 | 零散硬编码：`remove_punctuation` 只删末尾 `，。`；`preprocess_segments` 删纯标点段（仅断句路径，`split.py:88`）；其余靠 LLM prompt（`optimize/subtitle.md` 要求删填充词/规范标点，不可配置） | — |
| 占位符清理（`[Music]` 等） | 无规则实现，仅依赖 LLM 且不可关 | — |
| 行长控制 | 有：`max_word_count_cjk/english` 贯穿断句校验；烧录期 `auto_wrap_ass_file` 用 PIL 实测宽度换行（95% 宽，贪心+均衡） | `split.py` / `core/subtitle/ass_utils.py:222` |
| 长时长异常审计 | 没有 | — |
| QA 报告 | 没有 | — |
| 样式系统 | `SubtitleStyle` dataclass + JSON 预设（`resource/subtitle_style/ass-*.json`），双样式 Default/Secondary，720p 基准 + 按目标高度等比缩放（`ass_renderer.py:54` 缩放 Fontsize/Spacing/Outline/MarginV）；**无 Shadow/MarginL/R/副样式独立 MarginV 字段，无 ScaledBorderAndShadow，WrapStyle 硬编码 1** | `core/subtitle/style_manager.py` / `ass_renderer.py` |

配置双轨（新增选项必须两边都落）：

- CLI：`cli/config.py:122-136` `[subtitle]`/`[translate]` 默认值；argparse 定义 `cli/main.py:204-267`（`_build_subtitle_parser`）；args→config 覆盖映射 `cli/main.py:643` 附近（`_set("subtitle.xxx", ...)` 模式）。
- GUI：`ui/common/config.py:272-289`（qconfig "Subtitle" 组）→ `ui/task_factory.py:244-271`（组装 `SubtitleConfig`，`core/entities.py:708`）→ `subtitle_thread.py`。
- ⚠️ 已知问题：`max_word_count_*` 的默认值在 CLI(18/12)、GUI(28/20)、`SubtitleConfig`(12/18)、`split.py` 常量(25/18) 四处不一致。**新增选项严禁再制造多套默认值**：规定唯一权威默认值位于 `core` 层新配置 dataclass（见 §3），CLI/GUI 默认值从它引用或保持字面一致并加注释互指。

### 2.2 技能能力目录（含精确算法参数）

技能位置 `C:\Users\lijso\.claude\skills\ass-subtitle-optimizer`（SKILL.md + references/*.md + scripts/*.py）。逐项提炼（参数为技能脚本/文档的默认值）：

| # | 能力 | 核心算法/规则 | 关键参数 |
|---|---|---|---|
| S1 | 占位符清理 | 整行匹配非语义提示行则删除；双语块两侧都是占位符→删整块；仅一侧→只删该侧；对可能有意义的括号内容（标题/作品名/引用/说话人标签）保守不动；**删除后必须重跑闪烁检测**（删除会产生新空隙） | 词表：`[Music] [Applause] [Laughter] [Laughs] [Sighs] [Silence] [Inaudible] [Foreign language] [Speaking ...] ♪ Music [音乐] [掌声] [笑声]` 等 |
| S2 | 中文标点/引号规范化 | 双引号→`「」`、单引号→`『』`（开闭状态**跨条持续**；英文词内撇号 `don't` 不动：前后均 ASCII 字母则跳过）；删中文行末弱标点（含"闭合符前"位置：`。」`→`」`）；保留语义标点 | 弱标点集 `，,。．.、；;：:`；强标点集 `？！?!…`；闭合符集 `」』）)]】》〉”’`；仅处理含 CJK 的行 |
| S3 | 闪烁修复 | 按（近似）同起止分组双语块；正向间隙在阈值内时**把前块结束时间延长到后块开始时间**（不动文本、不合并、不动后块） | max_gap 0.8s（音乐/节奏剪辑 0.5s）、min_gap 0.03s、分组容差 0.02s |
| S4 | 阅读速度审计 | CPS = 可见字符数/时长；中文按去空白字符数计，英文按含空格可见字符数计；区分**硬警告**与**舒适警告**；输出 JSON+MD，附 ±N 相邻块上下文供重译参考 | 中文硬 11 cps / 舒适 9；英文硬 20 / 舒适 16；块最短时长公式 `max(zh_chars/11, en_chars/20, 1.0)s`（舒适 `max(zh/9, en/16, 1.2)`）；技术下限 5/6s，感知下限 1.0–1.2s |
| S5 | 快速字幕重译压缩 | 对超硬限的中文行做**局部压缩重译**：删填充词（"也就是/其实/非常/令人意外的是"）、紧凑字幕化措辞、把语境挪到相邻更长条、<0.7s 碎片用关键词式中文；修复优先级：延时进静默/闭合闪隙 → 调整切分边界 → 缩短译文 → 语义断行；**禁止只靠缩字号解决** | 处理顺序：先硬警告清零，再可选舒适二次打磨 |
| S6 | 长时长异常审计 | 块时长 > 7s，或"短文本长显示"（主行 ≤12 字且 >4s）→ **只标记进 QA 报告**，不自动改（可能是标题卡/歌词/有意长显示） | 7s / 12 字 / 4s |
| S7 | 结构与交付校验 | 字段数、非正时长、未知样式、短时长(<0.4s)、Default/Secondary 数量配对、闪烁候选、引号/弱尾标点候选、CPS 超限、宽度溢出；警告分类计数 + 有界样本 | min-duration 0.4s |
| S8 | QA 报告 | 单一 Markdown 交付：文件信息、各阶段处理摘要（变更计数）、校验摘要、译者复查队列（长时长表 / 快速中文表 / 快速英文表）、人工 QA 注意事项 | max-samples 40 |
| S9 | 视觉样式（house 风格） | 双语一体化：同字体同填充色，**语言区分只放在 OutlineColour 的两种低饱和深色**上；单层样式（禁 DefaultEdge 双层）；层级靠字号/位置/描边强弱；720p 共享网格 + `ScaledBorderAndShadow: yes` 跨分辨率复用 | Default: LXGW WenKai 48, 填充 `&H00F6F8F8`(#F8F8F6), 描边 `&H00302018`(#182030) 3.0, Shadow 0, Spacing 3.2, Margin 10/10/30, Bold；Secondary: 32, 同填充, 描边 `&H00151E33`(#331E15) 2.6, Spacing 0.6, Margin 10/10/30 |
| S10 | 宽度测量与断行 | 实字体测量（Tk/回退启发式：CJK 0.95em、窄字符 0.28、宽字符 0.86、其他 0.55、空格 0.34）；断点评分 `score = max(左,右宽) + 0.18*|左-右| + 4*超限量 + 180*(右行以闭合标点开头) + 90*(左行以开括号结尾)`，取最小分；<12 可见字符不断行 | 1080p 安全宽 1680–1840（默认 1728=90%）；4K 无换行 3600（93.75%） |
| S11 | 无换行 \fscx 压缩 | 删手工 `\N`、剥离旧 `\fscx` 再实测；超宽行加 `{\fscxNN}`，`NN = max(min_scale, ⌊max_width/宽*100⌋-1)`；WrapStyle 2 | min-scale-x 75（4K 72）；压缩过强时宁可微降全局字号或改文本 |
| S12 | 中文行长指引 | 中文主行理想 20–24 字（26–28 需检查）；英文辅行 42–55 字符 | 字符数只是预警，最终以实测宽度为准 |

### 2.3 差距对照与内化策略

| 技能能力 | 项目现状 | 内化策略 |
|---|---|---|
| S1 占位符 | 无规则实现 | **新增规则步骤**（P0） |
| S2 规范化 | 只有硬编码删 `，。` | **泛化为参数化步骤**，现行为作为默认保持（P0） |
| S3 闪烁 | 有 `optimize_timing` 但不在字幕管线 | **提升为管线可选步骤**，语义改为技能的 extend-prev 模式，阈值可配（P0） |
| S4/S6 审计 | 无 | **新增只读审计**（P1） |
| S5 压缩重译 | 无 | **新增可选 LLM 步骤**，复用 agent_loop 模式（P2） |
| S7/S8 校验+QA | 无 | **新增 QA 报告生成**（P1） |
| S9 样式 | 有样式系统但字段不全、理念部分一致（现预设已是"同填充+双描边色"） | **扩展 SubtitleStyle 字段 + 新增 house 预设 + ScaledBorderAndShadow**（P2） |
| S10 断行 | 有 PIL 实测换行（贪心+均衡），无标点感知 | **可选增强**：断点评分引入标点惩罚（P3） |
| S11 \fscx | 无 | 合成期可选功能（P3，最低优先级） |
| S12 行长 | max_word_count 已覆盖思想 | 文档化即可，不改代码 |

优势：项目在 `ASRData` 数据层工作，一个 segment 天然就是技能里的"双语块"，**不需要**技能脚本里的同时轴分组/容差逻辑；文本是纯文本，**不需要** ASS tag 保护逻辑。移植的是算法与阈值，不是 ASS 解析代码。

---

## 3. 总体架构设计

### 3.1 新模块：`videocaptioner/core/postprocess/`

规则型后处理与审计统一收口（不放 `asr_data.py`，避免其继续膨胀；不放 `optimize/`，因为 optimize 语义是 LLM 纠错）。无 Qt、无网络（除 F5 压缩走现有 `call_llm`）。

```
videocaptioner/core/postprocess/
├── __init__.py          # 导出 run_postprocess / PostprocessConfig / QualityReport
├── config.py            # PostprocessConfig dataclass —— 全部新选项的唯一权威默认值
├── placeholders.py      # F1 占位符清理
├── normalize.py         # F2 中文文本规范化（引号 + 弱尾标点）
├── timing.py            # F3 时轴间隙闭合（闪烁修复）
├── audit.py             # F4/F6 阅读速度 + 时长异常审计（只读）
└── report.py            # F7 QA 报告（Markdown 生成）
```

统一入口（伪代码，供两条管线调用）：

```python
@dataclass
class PostprocessConfig:
    # 文本清理
    remove_placeholders: bool = False
    normalize_quotes: bool = False
    trim_trailing_punct: bool = True     # 对应现有 remove_punctuation 行为，保持默认开
    # 时轴
    fix_gaps: bool = False
    max_gap_ms: int = 800
    min_gap_ms: int = 30
    gap_mode: str = "extend"             # "extend"(技能语义) | "midpoint"(现 optimize_timing 语义)
    # 审计 / 报告
    audit_reading_speed: bool = False    # qa_report 开启时强制视为 True
    max_cps_cjk: float = 11.0
    max_cps_latin: float = 20.0
    comfort_cps_cjk: float = 9.0
    comfort_cps_latin: float = 16.0
    min_duration_ms: int = 1000
    max_duration_ms: int = 7000
    short_text_max_chars: int = 12
    short_text_max_duration_ms: int = 4000
    qa_report: bool = False
    # LLM 压缩重译（P2）
    compress_fast_subtitles: bool = False

def run_pre_stage(asr_data, cfg) -> tuple[ASRData, StageReport]:
    """加载后、断句/优化前：占位符清理。"""

def run_post_stage(asr_data, cfg, llm_ctx=None) -> tuple[ASRData, QualityReport]:
    """翻译后、保存前：规范化 → [压缩重译] → 闭合间隙 → 审计。返回报告对象。"""
```

设计要点：

- 每个步骤"输入 ASRData → 输出 ASRData + 变更计数"，可单测；任何步骤内部异常捕获后记 warning 并跳过（不阻断管线，与 optimizer 的三层回退精神一致）。
- 报告对象 `QualityReport`（dataclass）收集各步骤 `StageReport`（changed 计数 + 有界 samples ≤20）与审计结果，`report.py` 负责渲染 Markdown。
- **双字段纪律**：所有文本步骤必须同时考虑 `seg.text` 与 `seg.translated_text`（哪侧是中文用 `is_mainly_cjk` 判定，工具在 `core/utils/text_utils.py:26`）。
- **空段陷阱**：清理步骤把 `text` 清成空串后，一旦经 `ASRData(segments)` 重建该段即被静默丢弃（`asr_data.py:107-110`）。删除段必须显式进行并计数，禁止"置空即删"。

### 3.2 管线插入点（两条链路各 3 处，每处 1–3 行调用）

| 阶段 | CLI（`cli/commands/subtitle.py`） | GUI（`ui/thread/subtitle_thread.py`） |
|---|---|---|
| ① 加载后（断句前） | `:171` `from_subtitle_file` 之后 → `run_pre_stage` | `:123-126` 之后 |
| ② 优化后 / 翻译后 | 用 `run_post_stage` 中的 normalize 取代 `:223` 与 `:251` 两处 `remove_punctuation()`（`trim_trailing_punct=True` 时行为等价） | 同理取代 `:176`/`:196` |
| ③ 保存前 | `:254` `save` 之前 → `run_post_stage` 其余步骤 + 审计；save 之后写 QA 报告 | `:213` 之前；QA 路径经 `progress`/`error` 信号告知 UI |

`process.py`（全流程）通过链式调用 `subtitle.run` 自动获得全部能力，只需透传新 CLI flags（见 §5）。`optimize_timing` 在 ASR 阶段的现有调用（`transcribe.py:73`）**保持原样不动**，避免行为回归。

---

## 4. 分项功能设计

### F1 占位符清理（`placeholders.py`，P0）

- **默认词表**（模块常量 `DEFAULT_PLACEHOLDER_PATTERNS`，大小写不敏感、去 ASS 换行与空白后**整行匹配**）：
  - 括号式（支持 `[] () 【】 （）` 四种括号）：`music, applause, laughter, laughs, chuckles, sighs, silence, inaudible, foreign language, speaking foreign language, speaking [a-z ]+, 音乐, 掌声, 笑声, 笑, 叹气, 咳嗽, 沉默, 听不清`
  - 裸词/符号行：`music`、仅由 `♪ ♫ 空白` 构成的行
- **保守规则**：只有整行是占位符才处理；括号内含其他实义内容（标题、说话人、引用）一律不动 —— 与技能 S1 一致。
- **段级语义**（ASRDataSeg 双字段）：
  - `text` 与 `translated_text` 均为占位符（或另一侧为空）→ **删除整段**（显式从列表移除并计数）。
  - 仅 `translated_text` 是占位符 → 置 `translated_text = ""`。
  - 仅 `text` 是占位符而译文有实义（罕见）→ **不动**，加入 QA 复查队列（数据模型不支持"只留译文"，不做译文升格这种危险操作）。
- **顺序约束**：在管线①执行（省 LLM token）；若同时开启 F3，F3 在最后执行时自然覆盖删除产生的新间隙（对应技能"删除后重跑闪烁检测"）。
- 词表允许用户扩展：配置项 `extra_placeholder_patterns`（CLI TOML 字符串列表 / GUI 逗号分隔文本，P1 再做，P0 先内置词表）。
- 与 LLM 的关系：`optimize/subtitle.md` prompt 本就要求删除非语言声音——规则步骤是**确定性兜底**，两者不冲突；F1 关闭时行为与现状完全一致。

### F2 中文文本规范化（`normalize.py`，P0）

移植技能 S2 的两个纯函数（逐字符状态机，算法直接照搬 `normalize_chinese_subtitle_text.py:50-142`，去掉 ASS tag 处理）：

- `normalize_quotes(text, state) -> (text, count)`：`“”`→`「」`、`‘’`→`『』`、直引号 `"` `'` 按开闭状态翻转；**英文词内撇号跳过**（前后均 ASCII 字母）；`QuoteState` 由调用方持有并**跨段传递**（引号常常跨条开闭）。
- `trim_weak_trailing(text) -> (text, count)`：按 `\n` 分行处理行尾；先剥闭合符 `」』）)]】》〉”’`，再循环删弱标点 `，,。．.、；;：:`，遇强标点 `？！?!…` 停止，最后拼回闭合符。覆盖现有 `remove_punctuation` 的能力（其只处理 `，。` 且不管闭合符）。
- 应用范围：仅对 `is_mainly_cjk` 为真的字段（text 或 translated_text 中的中文侧）；`normalize_quotes` 受 `normalize_quotes` 开关控制（默认关），`trim_weak_trailing` 受 `trim_trailing_punct` 控制（**默认开**，保持现状兼容并小幅增强；如需逐字节等价的保守实现，可让默认路径仍走旧字符集 `，。`，将扩展字符集绑定到 `normalize_quotes` 开关——实施者二选一并在 PR 里说明，推荐前者+changelog 注明）。
- 落点：取代管线②两处 `asr_data.remove_punctuation()` 调用；`ASRData.remove_punctuation` 本体保留并委托新实现（别处可能引用）。
- 联动增强：`normalize_quotes` 开启时，向 `optimize/subtitle.md` 注入可选模板变量 `${extra_rules}`（prompt 加载器 `get_prompt` 用 `safe_substitute`，`core/prompts/__init__.py:70`，**不传则原样保留，零成本**），内容如"中文引号使用「」/『』"，让 LLM 输出与规则层一致，减少来回改写。

### F3 时轴间隙闭合 / 闪烁修复（`timing.py`，P0）

- `close_gaps(segments, max_gap_ms=800, min_gap_ms=30, mode="extend") -> (segments, count)`：
  - `extend`（默认，技能语义）：`min_gap_ms < gap ≤ max_gap_ms` 时 `prev.end_time = next.start_time`（前段延长到后段开始；后段与文本不动）。
  - `midpoint`（现 `optimize_timing` 语义）：边界吸附到间隙 3/4 点，供偏好旧行为的用户选择。
- 词级时间戳数据直接跳过（判定 `is_word_timestamp()`，与 `optimize_timing:507` 一致）。
- 只处理正间隙；重叠（负 gap）不动，仅计入审计警告。
- 落点：管线③，**所有文本步骤之后、审计之前**（顺序：placeholders → …… → close_gaps → audit，保证删除段产生的新间隙被处理，审计看到的是最终时轴）。
- 参数暴露：`fix_gaps`（bool，默认 False）、`max_gap_ms`（默认 800；文档提示音乐类内容用 500）、`gap_mode`。

### F4 阅读速度审计（`audit.py`，P1，只读）

- 字符计数（对每个非空字段独立计算）：
  - `is_mainly_cjk(field)` → `cjk_chars = len(去除全部空白后的字符串)`，上限用 `max_cps_cjk`/`comfort_cps_cjk`；
  - 否则 → `latin_chars = len(strip 后字符串)`（含词间空格，与技能/Netflix 口径一致），上限用 `max_cps_latin`/`comfort_cps_latin`。
- 段级判定（一个段 = 一个双语块，时长同一）：
  - `duration_s = (end-start)/1000`；每字段 `cps = chars/duration_s`。
  - `required_s = max(cjk_chars/max_cps_cjk, latin_chars/max_cps_latin, 1.0)`；`duration_s < required_s` 或任一字段 cps 超硬限 → **硬警告**；仅超舒适限（cjk>9 / latin>16）或 `duration_ms < min_duration_ms`（默认 1000，感知下限） → **舒适警告**。
- 时长异常（S6）：`duration_ms > max_duration_ms`(7000)，或中文侧 `chars ≤ short_text_max_chars`(12) 且 `duration_ms > short_text_max_duration_ms`(4000) → 长时长异常队列（只进报告）。
- 重叠时轴（`next.start < prev.end`）→ 结构警告。
- 输出 `AuditResult` dataclass：`hard: list[Warning]`、`comfort: list[...]`、`long_duration: list[...]`、`overlaps: list[...]`；每条含 segment 序号、起止（`to_srt_ts` 格式）、时长、双语文本、cps、超限量，以及 **±1 相邻段上下文**（供 F5 与人工重译参考，对应技能 `--context-blocks 1`）。
- 触发条件：`audit_reading_speed or qa_report or compress_fast_subtitles`。CLI 结束时打印计数摘要（沿用 `cli/output.py` 风格）；GUI 写日志 + 完成消息附带计数。

### F5 快速字幕 LLM 压缩重译（`compress` 步骤，P2，可选、需 LLM）

对 F4 硬警告中的**中文侧**文本做局部压缩（技能 S5 的自动化版本）：

- 新 prompt `core/prompts/optimize/compress.md`：角色为"字幕压缩编辑"；规则来自技能 workflow.md §Reading-Speed Retranslation：删除不改变语义的填充词（如"也就是""其实""非常""令人意外的是"）、重复完整称谓/已确立术语缩写化、解释性长语改紧凑字幕措辞、时长 <0.7s 的碎片用关键词式表达（语义由相邻条承接）；**禁止改变含义、禁止合并/拆分条目**；输入含每条的 `duration_s`、`target_max_chars` 与相邻条上下文；输出 JSON `{index: compressed_text}`。模板变量 `${max_cps_cjk}`。
- 执行模式：完全复用 `SubtitleOptimizer` 的 `agent_loop + 校验 + 回退` 骨架（`core/optimize/optimize.py:187-341`）——建议把该骨架抽出私有辅助或直接仿写（三处现有实现本就高度一致，抽象与否由实施者判断，勿过度设计）：
  - 校验①键集合一致；②每条 `压缩后中文字符数 ≤ ceil(duration_s × max_cps_cjk)`；③非空；④与原文 `SequenceMatcher` 相似度 ≥ 0.3（防止改写跑题，阈值同 optimize 短文本档）。
  - 校验失败反馈重试 ≤3 轮；仍失败**保留原文**并把该条记入 QA 报告"未能自动压缩"队列。
- 批量：仅送硬警告条目（含上下文），通常规模很小，单批即可（≤`batch_size`）。
- 落点：管线③中 normalize 之后、close_gaps 之前（压缩改变字符数，须在最终审计前完成）。写回：改的是"当前中文显示侧"字段（翻译开启且目标语言为中文 → `translated_text`；纯中文字幕 → `text`）。
- 温度 0.2；经 `call_llm`（自动获得缓存/重试/请求日志，`core/llm/client.py:112`）；GUI 侧 `update_stage("compress")` 记入 LLM 日志上下文。

### F6 长时长异常审计 —— 并入 F4（`audit.py`），只进 QA 报告，永不自动修改（技能 S6 原则）。

### F7 质量校验与 QA 报告（`report.py`，P1）

- `build_qa_report(quality_report, source_path, output_path) -> str(markdown)`，结构对齐技能 S8：
  1. 文件信息（输入/输出路径、段数、处理时间）；
  2. 处理摘要（每个启用步骤的变更计数：删除占位符 N 段/清理 N 侧、规范化 N 处、闭合间隙 N 处、压缩成功/失败 N 条）；
  3. 校验摘要（段数、剩余闪烁候选、硬/舒适警告计数按语言分列、长时长异常数、重叠数）;
  4. 译者复查队列（三张表：长时长/短文本长显示、快速中文行、快速英文行；每表 ≤40 行，超出注明省略数）；
  5. 人工 QA 注意事项（固定文案：优先看中文行；英文辅行可能因源时轴天然偏快；长时长行需人工判断是否有意为之）。
- 输出位置：CLI 写 `<output>.qa.md`（与输出字幕同目录）；GUI 写任务输出目录同名文件并在完成日志中给出路径。
- 开关 `qa_report`（默认 False）。开启时隐含执行 F4 审计。

### F8 ASS 样式系统增强（P2）

对齐技能 S9 的表达能力，同时保持向后兼容（旧 JSON 预设无新字段时行为不变）：

1. `SubtitleStyle`（`core/subtitle/style_manager.py:43`）新增可选字段：
   - `shadow: float = 0.0`、`margin_l: int = 10`、`margin_r: int = 10`；
   - `SecondaryStyle` 新增 `shadow: float = 0.0`、`margin_bottom: Optional[int] = None`（None → 沿用主样式 `margin_bottom`，保持现状）。
2. `to_ass_string()`（:84）输出上述字段（当前 Shadow 硬编码 0、Margin 硬编码 `10,10,{margin_bottom}`，改为取字段值）。
3. `from_json`/`_parse_ass_txt`/`to_json_dict` 同步支持新字段；`clamp` 不变。
4. **`ScaledBorderAndShadow: yes`** 写入两处 ASS 头模板：`asr_data.py:to_ass`（:363-371 的 Script Info 块）与 `ass_renderer.py:ASS_TEMPLATE`（:23）。这是技能"720p 共享网格跨分辨率复用"的关键（描边/阴影随帧缩放不变细）。加入后 `_scale_ass_style`（`ass_renderer.py:54`）的手动缩放依旧保留（两者兼容：libass 以 PlayRes 为基准缩放，项目又把样式数值缩放到实际分辨率、PlayRes 也写实际分辨率，净效果一致），但需补缩放 `Shadow`（parts[17]）与 `MarginL/R`（parts[19]/[20]）。
5. 新增内置预设 `resource/subtitle_style/ass-house.json`（内容见附录 B）：技能 house 风格 —— LXGW WenKai 48/32、同填充 `#F8F8F6`、双描边色 `#182030`/`#331E15`、无阴影、720p 基准。字体 `LXGWWenKai-Regular.ttf` 已随包内置（`resource/fonts/`），bold=-1 由 libass 合成加粗，可用。
6. `style_cmd.py` 的 `style list` 详情输出补充新字段；`docs` 同步（§9）。
7. WrapStyle：`to_ass` 与模板当前硬编码 `WrapStyle: 1`。技能推荐默认 0（均衡换行）——**本次不改默认**（避免既有输出回归），仅在样式 JSON 增加可选 `wrap_style` 字段（缺省 1），文档说明取值含义。

### F9 宽度/换行增强（P3，最低优先级，可裁剪）

1. `core/subtitle/text_utils.py` 的均衡换行在选断点时引入技能 S10 的标点惩罚项（避免行首闭合标点/行尾开括号）：在 `_wrap_cjk_balanced` 的 should_break 判定处增加对 `text[i+1]` 的惩罚检查即可，权重照搬（180/90 等比折算）。
2. 烧录期安全宽度 `auto_wrap_ass_file` 的 0.95 系数（`ass_utils.py:253`）参数化为合成配置 `safe_width_ratio`（默认 0.95 不变；文档给出技能建议值 0.90）。
3. 合成命令可选 `--no-wrap-fit`：删 `\N` + 超宽行 `{\fscxNN}` 局部压缩（`NN = max(75, ⌊max_width/宽×100⌋-1)`），实现参照技能 `fit_no_wrap_ass.py:97-263`（宽度测量复用项目 PIL 体系而非 Tk）。仅 hard 模式有效。

---

## 5. 配置总表（执行清单）

所有新增项默认值以 `PostprocessConfig`（§3.1）为唯一权威。五套载体映射：

| 功能 | TOML `[subtitle]` 键 | CLI flag（subtitle & process 子命令） | GUI qconfig（组 "Subtitle"） | `SubtitleConfig` 字段 | 默认 |
|---|---|---|---|---|---|
| F1 | `remove_placeholders` | `--remove-placeholders` | `need_remove_placeholders` (Bool) | `remove_placeholders` | False |
| F2 引号 | `normalize_quotes` | `--normalize-quotes` | `need_normalize_quotes` (Bool) | `normalize_quotes` | False |
| F2 尾标点 | `trim_trailing_punct` | `--keep-trailing-punct`（反向） | `trim_trailing_punct` (Bool) | `trim_trailing_punct` | True |
| F3 | `fix_gaps` / `max_gap_ms` / `gap_mode` | `--fix-gaps` / `--max-gap-ms N` | `need_fix_gaps` (Bool) / `max_gap_ms` (Range 100–2000) | 同名 | False / 800 / "extend" |
| F4 | `audit_reading_speed` / `max_cps_cjk` / `max_cps_latin` / `comfort_cps_cjk` / `comfort_cps_latin` / `min_duration_ms` / `max_duration_ms` | `--audit-speed` / `--max-cps-cjk` / `--max-cps-latin` | `need_audit_speed` (Bool) / `max_cps_cjk` (Range 5–30) / `max_cps_latin` (Range 8–40) | 同名 | False / 11 / 20 / 9 / 16 / 1000 / 7000 |
| F5 | `compress_fast_subtitles` | `--compress-fast` | `need_compress_fast` (Bool) | `compress_fast_subtitles` | False |
| F7 | `qa_report` | `--qa-report` | `need_qa_report` (Bool) | `qa_report` | False |

实施说明：

- **CLI**：`cli/config.py:122` `DEFAULTS["subtitle"]` 增键；`cli/main.py:_build_subtitle_parser`（:204）在 "Processing options" 组加 flags（`store_true` 风格、help 文案对齐既有简洁风格）；`cli/main.py:643` 附近仿 `_set("subtitle.optimize", ...)` 增加覆盖映射；`process` 子命令（:394 起）透传同名 flags。数值型 flag 用 `type=float/int, metavar="N"`。ENV 映射（`ENV_MAP`）本次不为每个键添加（现有 subtitle.* 亦无 env 映射，保持一致）；如需可后续补 `VIDEOCAPTIONER_SUBTITLE_*`。
- **GUI**：`ui/common/config.py` "Subtitle" 组新增 ConfigItem（Bool 用 `BoolValidator`，数值用 `RangeConfigItem`，参照 :273-289 现有写法；qconfig 对 settings.json 缺失键自动用默认值，无迁移问题）；`ui/task_factory.py:create_subtitle_task`（:244）透传；`core/entities.py:SubtitleConfig`（:708）加字段并更新 `print_config`（:741）；`subtitle_thread.py` 调用 §3.2 的两个入口，从 `SubtitleConfig` 构造 `PostprocessConfig`。
- **GUI 设置界面**：`ui/view/setting_interface.py` 字幕设置区（`subtitleCorrectCard` 一带，:105）新增卡片：`SwitchSettingCard` ×（占位符清理/引号规范化/闪烁修复/QA 报告/快速字幕压缩），`RangeSettingCard` ×（max_gap_ms、max_cps_cjk、max_cps_latin）。图标复用 FluentIcon 现有集合；文案中文为主（跟随现有 i18n 处理方式——若现有卡片用 `self.tr()` 则一致处理）。`ui/view/subtitle_interface.py` 顶部快捷开关区（:253-254 现有 optimize/translate SwitchButton）**本次不加**新快捷开关（避免拥挤），设置统一走设置页；如需可 P3 再评估。

---

## 6. Prompt 变更

| 文件 | 变更 | 说明 |
|---|---|---|
| `core/prompts/optimize/subtitle.md` | 末尾增加可选 `${extra_rules}` 插槽 | `safe_substitute` 不传则原样保留字面量——**注意**：不传时 `${extra_rules}` 字符串会残留在 prompt 中，因此实现上应始终传参（默认空串），由 `optimize.py:213` 的 `get_prompt("optimize/subtitle", extra_rules=...)` 统一注入；`call_llm` 磁盘缓存键随内容自动区分 |
| `core/prompts/optimize/compress.md` | 新增（F5） | 内容要点见 §4-F5；中英双示例，输出纯 JSON，风格对齐现有 prompt 文件 |

---

## 7. 实施阶段与验收标准

### Phase 0 —— 基础设施（0.5 天规模）
- 建 `core/postprocess/` 包 + `PostprocessConfig` + 空管线入口 + 报告 dataclass。
- 验收：`uv run pytest tests/test_optimize -q` 全绿（新包空跑不改变任何现有行为）；ruff/pyright 通过。

### Phase 1 —— 规则型后处理 F1/F2/F3 + 配置贯通（P0）
- 实现三个规则步骤；接入两条管线；CLI/GUI 配置五套载体全部落地。
- 验收：
  - 全部开关默认关时，`videocaptioner subtitle input.srt` 输出与改造前**逐字节一致**（用现有 fixture `tests/fixtures/subtitle/` 对比）；
  - `--remove-placeholders`：构造含 `[Music]`/`[音乐]`/`♪` 的用例，段被删除且计数正确；含实义括号（如 `[第3章] 内容`）不动；
  - `--normalize-quotes`：跨段引号状态正确（前段开 `「` 后段闭 `」`）；`don't` 撇号不动；`。」` → `」`；
  - `--fix-gaps`：0.5s 间隙被闭合（prev.end == next.start）、1.2s 间隙不动、词级数据跳过；
  - `uv run pytest tests/test_cli tests/test_optimize -m "not integration" -q` 通过（test_cli 在 CI 必跑）。

### Phase 2 —— 审计与 QA 报告 F4/F6/F7（P1）
- 验收：构造超速/超长/短文本长显示用例，硬/舒适/长时长分类与计数正确（对照附录 A 阈值手算）；`--qa-report` 生成 `<output>.qa.md`，四个章节齐全、表格行数受限、省略数注明；审计不修改任何段。

### Phase 3 —— LLM 压缩重译 F5（P2）
- 验收：用根 conftest 的 `mock_llm_client`（`tests/conftest.py:162`，需为新模块追加 `monkeypatch.setattr("videocaptioner.core.postprocess.<module>.call_llm", ...)`）：mock 返回合法压缩 → 写回正确字段且时间戳不变；mock 返回超长/缺键 → 重试后回退原文并进 QA 队列；段数永不变化。

### Phase 4 —— 样式增强 F8（P2）与换行增强 F9（P3）
- 验收（F8）：旧预设 JSON 加载行为不变（缺新字段走默认）；`ass-house.json` 经 `style list` 正确展示；`to_ass_string` 含 Shadow/MarginL/R；生成的 ASS 头含 `ScaledBorderAndShadow: yes`；`_scale_ass_style` 缩放 Shadow/MarginL/R；`tests/test_subtitle/test_style_manager.py` 补断言。F9 可独立裁剪，验收从简（断点不再出现行首闭合标点）。

各 Phase 独立成 PR（commit 风格见 `AGENTS.md`：祈使句 + 可选 Conventional 前缀，如 `feat: add rule-based subtitle postprocess pipeline`）。

---

## 8. 测试要求（放置与惯例）

- 纯算法单测 → `tests/test_optimize/test_postprocess_*.py`（placeholders / normalize / timing / audit / report 各一文件）。风格仿 `tests/test_translate/test_llm_translator_unit.py`（中文 docstring、无 marker、monkeypatch mock）。**不要放 `tests/test_subtitle/`**（该目录 conftest 有 autouse 的 Qt qapp fixture，会引入 PyQt5 依赖）。
- 样式相关 → `tests/test_subtitle/test_style_manager.py` 追加。
- CLI 参数/配置 → `tests/test_cli/test_parser.py`、`test_config.py` 追加（这两个在 CI 必跑；若希望 postprocess 单测进 CI，需在 `.github/workflows/ci.yml` 的 pytest 路径中追加 `tests/test_optimize`——建议做）。
- 共享 fixture：`sample_asr_data`（`tests/conftest.py:21`）；缺凭据跳过用 `check_env_vars` 模式；触网测试打 `@pytest.mark.integration`（strict-markers 已开，新 marker 须先注册）。
- 既有断言范式：优化后段数不变（除显式删除）、时间戳不变（除显式 fix_gaps）、text 非空。
- 验收命令：`uv run pytest tests/test_optimize tests/test_cli tests/test_subtitle -m "not integration" -q` + `uv run ruff check .` + `uv run pyright`。

---

## 9. 文档同步清单

- `docs/cli.md`：subtitle/process 新 flags。
- `docs/guide/configuration.md`（中英）：新 TOML 键与默认值表。
- `docs/en/guide/subtitle-style.md` 更新新样式字段与 house 预设；**中文 `docs/guide/subtitle-style.md` 缺失**（侧边栏 `docs/.vitepress/config.mts:269` 已有链接指向它）——本次顺手补齐。
- `docs/guide/workflow.md`：管线阶段图更新（加入可选后处理与 QA 报告）。
- 本方案实施完成后移入 `docs/dev/archive/`（仓库惯例）。

---

## 10. 风险与实现注意事项

1. **空段静默丢弃**：`ASRData` 构造过滤空 text（`asr_data.py:107`）。删除段必须显式操作；任何步骤不得把有译文的段的 text 置空。
2. **双字段一致性**：`remove_punctuation` 已示范双字段处理（:213-217）；新步骤照做。优化步骤 `_create_segments` 不携带 translated_text（`optimize.py:399`），因此**所有后处理必须放在翻译之后**（本方案的管线③满足）。
3. **`SubtitleAligner` 勿改**（`split/alignment.py`，脆弱状态机）；F5 不需要对齐（键校验足够）。
4. **LLM 缓存**：`call_llm` 磁盘缓存 1h（`utils/cache.py`）；调试 prompt 记得 `disable_cache()`；prompt 内容变化自动改变缓存键。
5. **索引约定**：批处理键用从 1 开始的字符串序号（与 optimize/translate/`to_json` 一致）。
6. **行为兼容红线**：所有开关默认关时输出必须与现状逐字节一致（Phase 1 验收第一条）；`trim_trailing_punct=True` 的默认增强（扩展弱标点集+闭合符处理）是唯一例外，若评审认为风险高，降级为"默认走旧字符集"。
7. **GUI 线程**：postprocess 在 `SubtitleThread.run` 内同步执行（规则步骤毫秒级；F5 走线程池内已有模式）；进度回调沿用 `SubtitleProcessData` 机制，不必为规则步骤加进度条，日志记录即可。
8. **默认值单一来源**：新选项默认值只写在 `PostprocessConfig`；CLI `DEFAULTS`/GUI ConfigItem 处加注释 `# keep in sync with core/postprocess/config.py`。不要顺手"修复"既有 max_word_count 不一致问题（超出本次范围，可另开 issue）。
9. **from_ass 往返**：`from_ass` 丢弃 override tag（`asr_data.py:889`）——本方案全部在数据层工作不受影响；但 F9-3 的 `\fscx` 注入必须发生在**合成期临时 ASS**上（如 `ass_renderer` 的 `auto_wrap_ass_file` 之后），不得写入用户保存的字幕文件。
10. **CI 覆盖**：CI 只跑部分测试目录（见 §8）；改 workflow 时注意 3.10–3.12 三版本兼容（`int | None` 联合类型注解在 3.10 需 `from __future__ import annotations` 或 Optional 写法，项目已有先例）。

---

## 附录 A：技能参数速查表（实现时直接取用）

| 参数 | 值 | 用途 |
|---|---|---|
| 中文硬 CPS / 舒适 CPS | 11 / 9 | F4 |
| 英文硬 CPS / 舒适 CPS | 20 / 16 | F4 |
| 块最短硬时长 | `max(zh/11, en/20, 1.0)` s | F4 |
| 块舒适时长 | `max(zh/9, en/16, 1.2)` s | F4（文档/注释用） |
| 技术最短显示 | 5/6 s ≈ 833ms | F4（注释） |
| 感知最短显示 | 1000–1200 ms（取 1000 为默认） | F4 |
| 普通字幕最长显示 | 7 s | F6 |
| 短文本长显示 | ≤12 中文字 且 >4 s | F6 |
| 闪烁闭合 max/min gap | 800 ms / 30 ms（音乐类建议 500） | F3 |
| 校验短时长阈值 | 400 ms | F4 结构警告 |
| 弱尾标点集 | `，,。．.、；;：:` | F2 |
| 强标点集（保留） | `？！?!…` | F2 |
| 闭合符集 | `」』）)]】》〉”’` | F2 |
| 断点评分 | `max_w + 0.18·|Δw| + 4·overflow + 180·坏行首 + 90·坏行尾`，<12 字不断 | F9 |
| \fscx 下限 | 75（4K 72） | F9 |
| 安全宽度 | 1080p 90–95%；4K 93.75% | F9 |
| 中文行长指引 | 理想 20–24 字，26–28 需复核 | 文档 |
| QA 报告样本上限 | 每表 40 行 | F7 |

## 附录 B：`resource/subtitle_style/ass-house.json` 草案（F8）

```json
{
  "name": "house",
  "description": "双语一体化：同近白填充，语言区分仅在描边色（中文深蓝 / 英文深棕），无阴影",
  "mode": "ass",
  "reference_width": 1280,
  "reference_height": 720,
  "font_name": "LXGW WenKai",
  "font_size": 48,
  "primary_color": "#f8f8f6",
  "outline_color": "#182030",
  "outline_width": 3.0,
  "bold": true,
  "spacing": 3.2,
  "margin_bottom": 30,
  "shadow": 0.0,
  "margin_l": 10,
  "margin_r": 10,
  "secondary": {
    "font_name": "LXGW WenKai",
    "font_size": 32,
    "color": "#f8f8f6",
    "outline_color": "#331e15",
    "outline_width": 2.6,
    "spacing": 0.6,
    "shadow": 0.0
  }
}
```

（色值换算依据：ASS `&HAABBGGRR` → 填充 `&H00F6F8F8` = #F8F8F6；中文描边 `&H00302018` = #182030；英文描边 `&H00151E33` = #331E15。）

## 附录 C：技能脚本 → 项目模块映射

| 技能脚本/规则 | 项目落点 | 移植方式 |
|---|---|---|
| SKILL.md 占位符规则（无脚本） | `core/postprocess/placeholders.py` | 规则新写，词表照搬 |
| `normalize_chinese_subtitle_text.py` | `core/postprocess/normalize.py` | 算法照搬（去 ASS tag 逻辑） |
| `fix_subtitle_blinks.py` | `core/postprocess/timing.py` | 语义照搬（无需块分组） |
| `audit_reading_speed.py` | `core/postprocess/audit.py` | 阈值/口径照搬，数据源换 ASRData |
| `build_translator_qa_report.py` | `core/postprocess/report.py` | 报告结构照搬 |
| `validate_ass_subtitle.py` | 分拆入 audit.py（时轴/密度部分）；ASS 结构校验不需要（自生成） | 部分移植 |
| `optimize_ass_subtitle.py` 样式预设 | `SubtitleStyle` 扩展 + `ass-house.json` | 数值照搬 |
| `optimize_ass_subtitle.py` choose_break | `core/subtitle/text_utils.py` 增强 | 评分项移植（P3） |
| `fit_no_wrap_ass.py` | 合成期可选 `--no-wrap-fit`（P3） | 算法照搬，测量换 PIL |
| `repair_ass_format.py` | 不移植（非目标） | — |
| references/visual-style.md 阅读速度/安全区指引 | 本文附录 A + docs | 文档化 |
| references/font-rendering.md | docs（字体选择说明）；未来 doctor 增强 | 文档化 |
