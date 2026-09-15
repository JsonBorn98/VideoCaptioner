# Tickets 14–15 代码地图笔记

行号基准：主 checkout master `3028505`（`git rev-parse --short HEAD`，即 11-13 合并提交）。所有路径相对仓库根 `C:\Users\lijso\Software\VideoCaptioner\`。
工作区有 3 个 `resource/subtitle_style/*.json` 未提交改动，与本题无关；本笔记全部以已提交的 HEAD 为准。
上一轮笔记 `research/tickets-11-13-code-map.md` 的行号基于 `300f54a`，**已过时**（11-13 合并改动了其中大量文件），只当索引用。

基线（HEAD `3028505`，`uv run pytest -m "not integration" -q`）：**1207 passed, 5 skipped, 62 deselected, 1 warning, 125.84s**。

---

## 0. 两张票各自要动的文件清单

**票 14（CLI 配置面坍缩，动 `cli/*`）**
- `videocaptioner/cli/config.py`（ENV_MAP 硬切 + DEFAULTS [llm] 三键 + 整条继承链删除，约 250 行）
- `videocaptioner/cli/main.py`（三面旗子 + 顶层 profile 命令组 + `_build_cli_overrides` 的 [llm] 段）
- `videocaptioner/cli/validators.py`（validate_llm / validate_translation_llm 改走方案库）
- `videocaptioner/cli/commands/subtitle.py`（临时桥 :219-232 删除 + 三键装配 + fail-fast）
- `videocaptioner/cli/commands/postprocess.py`（临时桥 :159-167 + 环境变量写入 :177-182）
- `videocaptioner/cli/commands/dub.py`（`_legacy_rewrite_profile` :143-160 改 resolve_utility_profile）
- `videocaptioner/cli/commands/transcribe.py`（环境变量写入 :61-68——whisper-api 的，见坑 5）
- `videocaptioner/cli/commands/doctor.py`（`_check_subtitle` :200-203 的 llm.api_key/model 检查）
- `videocaptioner/cli/commands/config_cmd.py`（init 的 LLM 三问 :143-146、映射表 :177-179、模板 llm 块）
- 新文件：`videocaptioner/cli/commands/profile_cmd.py`（profile list/show/set-default）
- `videocaptioner/core/llm/utility.py`（CLI 包装错误文案时用；本体不动）
- `videocaptioner/core/llm/request_logger.py`（`key_source=env_override` 日志字段——建议方案见 §7）
- 测试：`tests/test_cli/*`（见 §12）

**票 15（GUI 旧服务页移除 + 工具模型卡，动 `ui/*`）**
- **`videocaptioner/ui/view/setting_interface.py`**（注意：在 `ui/view/` 下，不是 `ui/` 直下——见坑 1）
- `videocaptioner/ui/common/config.py`（22 个旧键 :111-158 删除）
- `videocaptioner/ui/components/TranslationSettingWidget.py`（工具模型卡 + 模型框升级可编辑下拉 + 拉取按钮）
- `videocaptioner/ui/task_factory.py`（**11-13 已把旧装配段全删干净，本票装配侧基本不动**，见坑 7）
- 测试：`tests/test_ui/test_translation_setting_widget.py:496-510` 一条用例改写
- 可能新增：`tests/test_ui/` 工具模型卡用例

---

## 1. Ticket 14：`cli/config.py` 全貌（@3028505，720 行）

### 1.1 ENV_MAP（:39-84）——要硬切的条目

- :43 `"OPENAI_API_KEY": "llm.api_key"` —— **删除**（OPENAI_* 事实标准名不认）
- :44 `"OPENAI_BASE_URL": "llm.api_base"` —— 删除
- :45 `"OPENAI_MODEL": "llm.model"` —— 删除
- :47 `"VIDEOCAPTIONER_LLM_API_KEY": "llm.api_key"` —— **语义改变**：不再映射进 TOML 树，改为「只覆盖已解析方案凭证」的窄覆盖（见 §7；`OPENAI_API_KEY` 不映射但 `VIDEOCAPTIONER_LLM_API_KEY` 保留）
- :48 `"VIDEOCAPTIONER_LLM_API_BASE": "llm.api_base"` —— 删除
- :49 `"VIDEOCAPTIONER_LLM_MODEL": "llm.model"` —— 删除
- 新增三个键：`"VIDEOCAPTIONER_LLM_PROFILE_ID": "llm.profile_id"`、`"VIDEOCAPTIONER_LLM_REVIEW_PROFILE_ID": "llm.review_profile_id"`、`"VIDEOCAPTIONER_LLM_UTILITY_PROFILE_ID": "llm.utility_profile_id"`
- 其余条目（whisper_api/transcribe/dubbing/qwen 等 :50-83）**全部保留**

### 1.2 `_TRANSLATION_LLM_ENV_FIELDS` 循环（:86-113）——整段删除

```python
92	_TRANSLATION_LLM_ENV_FIELDS = { "API_KEY": "api_key", "API_BASE": "api_base", "BASE_URL": "api_base",
    "MODEL": "model", "TRANSPORT": "transport", "DIALECT": "dialect", "WORK_CONTEXT_TOKENS": ...,
    "MAX_CONCURRENCY": ..., "OPENAI_ENDPOINT": ..., "ENDPOINT": ..., "MAX_OUTPUT_TOKENS": ...,
    "REQUEST_OPTIONS_JSON": ... }
106	for _role in ("main", "review"):
107	    for _env_suffix, _field in _TRANSLATION_LLM_ENV_FIELDS.items():
108	        ENV_MAP[f"VIDEOCAPTIONER_LLM_{_role.upper()}_{_env_suffix}"] = f"translate.llm.{_role}.{_field}"
111	        ENV_MAP[f"VIDEOCAPTIONER_TRANSLATE_LLM_{_role.upper()}_{_env_suffix}"] = f"translate.llm.{_role}.{_field}"
```
这产生 `VIDEOCAPTIONER_LLM_MAIN_*`/`VIDEOCAPTIONER_LLM_REVIEW_*`/`VIDEOCAPTIONER_TRANSLATE_LLM_MAIN_*`/`VIDEOCAPTIONER_TRANSLATE_LLM_REVIEW_*` 共 2×12×2=48 个映射，全部随 TRANSLATE_LLM_* 全套环境变量硬切删除。

### 1.3 DEFAULTS 的 [llm] 节（:115-122）——换三键

现五键：
```python
116	    "llm": {
117	        "api_key": "",
118	        "api_base": "https://api.openai.com/v1",
119	        "model": "gpt-4o-mini",
120	        "work_context_tokens": 65536,
121	        "max_concurrency": 4,
122	    },
```
终局只剩（命名跟随 GUI cfg 先例 `main/review/utility_llm_profile_id`）：
```python
    "llm": {
        "profile_id": "",
        "review_profile_id": "",
        "utility_profile_id": "",
    },
```

### 1.4 `_normalize_translation_llm_aliases`（:298-322）——删除

被 build_config :332/:349/:355 三处调用，处理 `translate.llm.{role}.base_url→api_base` / `endpoint→openai_endpoint` 别名归一与冲突报错。inline 表全删后无存在意义。

### 1.5 build_config（:325-365）

- :332-334 file 层、:349-351 env 层、:355-357 CLI 层的三处 `_normalize_translation_llm_aliases` 调用删除。
- :335-346 「旧 translate.service→mode 迁移」逻辑与 LLM 无关，**保留**。
- `_BuiltConfig`（:30-33）与 `result._legacy_llm_api_key_explicit`（:360-364）删除——`_legacy_llm_api_key_explicit` 的三个消费点：`config.py:33`（声明）、`:361`（写入）、`:614`（读，在 translation_llm_role_allows_empty_api_key 内）。
- **新增**：build_config 里做「旧键残留一次性 stderr 警告」——检测 file_config/env_config/cli_overrides 三层里是否残留 `llm.api_key`/`llm.api_base`/`llm.model`/`llm.work_context_tokens`/`llm.max_concurrency`/`translate.llm.main.*`/`translate.llm.review.*`（以及 OPENAI_*/TRANSLATE_LLM_*/VIDEOCAPTIONER_LLM_{API_BASE,MODEL,MAIN_*,REVIEW_*} 环境变量），命中即 `print(..., file=sys.stderr)` 警告 + 迁移指引（指向 `llm.profile_id` 三键与方案库文件 `DEFAULT_LLM_PROFILES_PATH`）+ 可用方案 id 列表（`LLMModelProfileStore().list()`）。死数据容忍不迁移（不删除不报错退出）。

### 1.6 整条 [llm]→main→review 继承链（:373-623）——全删（约 250 行）

| 函数 | 行界 | 说明 |
|---|---|---|
| `build_legacy_llm_profile(config)` | :373-396 | 从 [llm] 标量构建 `profile_id="cli-legacy"` 的 profile。**消费者**：validators.py:105-107、subtitle.py:225、postprocess.py:166、dub.py:160 |
| `_TRANSLATION_LLM_PROFILE_FIELDS` | :399-414 | role 表字段白名单 frozenset |
| `_translation_llm_role_config(config, role)` | :417-441 | 读 `translate.llm.{role}` 显式字段（无继承）+ 别名归一 |
| `_parse_translation_request_options(raw, *, role)` | :444-457 | request_options_json 解析 |
| `_parse_translation_max_output_tokens(raw, *, role)` | :460-479 | max_output_tokens 'auto'/int 解析 |
| `_parse_translation_integer(raw, *, role, field)` | :482-491 | work_context_tokens/max_concurrency 解析 |
| `_enum_config_value(raw)` | :494-496 | 枚举值归一 |
| `_build_translation_llm_profile(values, *, role)` | :499-549 | 从 values dict 构建 LLMModelProfile + validate_profile_request_options |
| `_resolve_translation_llm_main(config)` | :552-574 | legacy→main 继承解析，返回 (profile, values) |
| `build_translation_llm_profiles(config)` | :577-593 | main/review 双 profile（[llm]→main→review） |
| `build_translation_llm_profile(config, role)` | :596-608 | 单 role 解析（review 缺省回退 main） |
| `translation_llm_role_allows_empty_api_key(config, role)` | :611-623 | 显式空 key 判定（读 `_legacy_llm_api_key_explicit`） |

**保留的通用设施**（这条链删除后仍被别处使用）：`_deep_merge` :232-240、`_set_nested` :243-248、`_get_nested` :251-260、`load_config_file` :263-276、`load_env_overrides` :279-295、`get` :368-370、`ensure_config_dir` :626-629、`_parse_value` :632-652、`save_config_value` :655-669、`_write_toml` :672-684、`_toml_value` :687-702、`format_config` :705-719（:715 有现成掩码格式，见 §8）。

### 1.7 翻译三键解析的新形态（票 14 要新建的）

三键从 config 读出后，翻译侧走 `LLMModelProfileStore`：
- 主翻译：`store.get(get(config, "llm.profile_id", ""))` —— 空/不存在即 fail-fast 指引（错误文案指向方案库文件与字段形状，故事 23；必要时包装 `MAIN_LLM_PROFILE_MISSING_MESSAGE`）
- 校对：`llm.review_profile_id` —— **增强型翻译方式下为空即 fail-fast**（不静默回退主翻译）；单模型 LLM 翻译不需要校对（语义对齐 GUI：`core/entities.py:779-802` 的 `is_translation_mode_available`/`missing_translation_roles`，见 §9）
- 工具：`llm.utility_profile_id` 空 = 从主翻译派生 → `resolve_utility_profile(store, profile_id, utility_profile_id)`
- 优先级：旗标 > 环境变量（VIDEOCAPTIONER_LLM_PROFILE_ID 及 _REVIEW/_UTILITY）> TOML —— build_config 的三层 merge 天然给出 file<env<cli，旗子在 `_build_cli_overrides` 写入 `llm.profile_id` 等三键即可复用该顺序

---

## 2. Ticket 14：11-13 留下的临时桥（三处，全带 TODO 标记）

### 2.1 `cli/commands/subtitle.py:218-232`

```python
219	    llm_api_key = get(config, "llm.api_key", "")
220	    llm_api_base = get(config, "llm.api_base", "")
221	    llm_model = get(config, "llm.model", "")
222	    # TODO(ticket-14): 临时桥——[llm] 三标量构 profile 喂 split/optimize 的新签名。
223	    # 14 号票把 CLI 的 LLM 配置面坍缩进方案库后一次性删除此桥。
224	    utility_profile = (
225	        build_legacy_llm_profile(config)
226	        if (need_optimize or need_split) and llm_api_key and llm_model
227	        else None
228	    )
229	    if llm_api_key:
230	        os.environ["OPENAI_API_KEY"] = llm_api_key
231	    if llm_api_base:
232	        os.environ["OPENAI_BASE_URL"] = llm_api_base
```
- import 行 :9 `from videocaptioner.cli.config import build_legacy_llm_profile, get` 要改。
- `utility_profile` 的两个消费点 :308（SubtitleSplitter 的 `profile=` 实参）、:333（SubtitleOptimizer 的 `profile=` 实参）。
- `llm_model` 消费点：:300（splitter model=）、:324（optimizer model=）、:458（TranslatorFactory 的 model=）、:252（verbose 打印）。
- 环境变量写入 :229-232 删除（写 OPENAI_* 的中继，16 号票核验源）。
- LLM 翻译 profile 解析段 :135-157（调 build_translation_llm_profiles/build_translation_llm_profile + validate_translation_llm）改为三键 + store 解析。
- `needs_llm`/`validate_llm` 门 :127-134 改走方案库校验。

### 2.2 `cli/commands/postprocess.py:159-167`

```python
159	    # TODO(ticket-14): temporary bridge — the compress/semantic-repair consumers
160	    # now take a utility model profile instead of a model-name string, so the
161	    # legacy [llm] scalars are bridged into a profile here until [llm] collapses
162	    # to profile-id keys.
163	    llm_model = get(config, "llm.model", "")
164	    if llm_model and resolved.utility_llm_profile is None:
165	        resolved = replace(
166	            resolved, utility_llm_profile=build_legacy_llm_profile(config)
167	        )
```
- 环境变量写入 :177-182（`os.environ["OPENAI_API_KEY"]/["OPENAI_BASE_URL"]`）删除。
- import :12 改。
- 替换形态：`resolve_utility_profile(store, main_profile_id, utility_profile_id)` 填 `resolved.utility_llm_profile`（无可用方案时是否 fail-fast 需与 GUI 对齐——GUI 侧 task_factory :514 只对 `needs_utility_llm()` 且 enabled 的任务解析）。

### 2.3 `cli/commands/dub.py:143-160`

```python
143	def _legacy_rewrite_profile(config: dict) -> Optional[LLMModelProfile]:
144	    """Bridge the legacy ``[llm]`` table into the rewrite profile.
145	
146	    TODO(ticket-14): the legacy [llm] table is removed wholesale there; this
147	    bridge (and the validate_llm gate) goes with it. A blank api_key/model
148	    yields no profile, so a disabled rewrite stays a silent no-op — matching
149	        ... (docstring 继续到 :154)
155	    api_key = str(get(config, "llm.api_key", ""))
156	    api_base = str(get(config, "llm.api_base", ""))
157	    model = str(get(config, "llm.model", ""))
158	    if not api_key or not api_base or not model:
159	        return None
160	    return build_legacy_llm_profile(config)
```
- 唯一调用点 :135 `llm_profile=_legacy_rewrite_profile(config),`（在 `_build_dubbing_config` :108-140 内）。
- 改为调 `resolve_utility_profile(store, main_profile_id, utility_profile_id)` 填 DubbingConfig.llm_profile（`core/dubbing/models.py:83`，`Optional[LLMModelProfile]`，None = rewrite 无法运行，由 rewriter fail-fast）。
- dub 命令本体无 rewrite 的独立旗子面（`--adapt-length` → dubbing.rewrite_too_long），三面旗子照 ticket 挂在 dub 上（`_add_hidden_llm_options` :58-62 / `_build_dub_parser` :722 现状挂的是 --api-key/--api-base/--model）。
- import :9 改；`validate_dubbing` 的 `rewrite and not validate_llm(config)` 门（validators.py:310-311）随 validate_llm 一起改走方案库。

---

## 3. Ticket 14：环境变量全部读取/写入点

### 3.1 读取（经 ENV_MAP，config.py:279-295 `load_env_overrides`）
- 所有 OPENAI_*/TRANSLATE_LLM_*/VIDEOCAPTIONER_LLM_{API_KEY,API_BASE,MODEL,MAIN_*,REVIEW_*} 读取都集中在 ENV_MAP（§1.1/§1.2），删映射即删读取。
- 唯一保留读取：`VIDEOCAPTIONER_LLM_API_KEY`（窄覆盖，见 §7）与新增 `VIDEOCAPTIONER_LLM_PROFILE_ID`/`_REVIEW_PROFILE_ID`/`_UTILITY_PROFILE_ID`。

### 3.2 写入（`os.environ[...]`，CLI 侧共 4 处，全删）
| 位置 | 内容 | 说明 |
|---|---|---|
| `subtitle.py:229-232` | `OPENAI_API_KEY`/`OPENAI_BASE_URL` ← llm.api_key/api_base | ticket 点名删除 |
| `postprocess.py:179-182` | 同上 | ticket 点名删除 |
| `transcribe.py:65-68` | `OPENAI_API_KEY`/`OPENAI_BASE_URL` ← **whisper_api**.api_key/api_base | **不是 llm.*！见坑 5** |
| `subtitle.py:460` | `DEEPLX_ENDPOINT` ← translate.deeplx_endpoint | 与 LLM 无关，**保留** |

`transcribe.py:61-68` 全文：
```python
61	    # Setup environment for Whisper API
62	    if asr_engine == "whisper-api":
63	        whisper_key = get(config, "whisper_api.api_key", "")
64	        whisper_base = get(config, "whisper_api.api_base", "")
65	        if whisper_key:
66	            os.environ["OPENAI_API_KEY"] = whisper_key
67	        if whisper_base:
68	            os.environ["OPENAI_BASE_URL"] = whisper_base
```
消费端 `core/asr/whisper_api.py`（`WhisperAPI.__init__` :20-64）**不读环境变量**——它从构造参数收 `base_url`/`api_key`（:27-28），CLI 侧经 `TranscribeConfig.whisper_api_key/whisper_api_base`（transcribe.py:119-120）显式传入。所以这两行环境写入确实是**零读取者的死代码**，ticket 判断成立，直接删。

### 3.3 core 侧残留读取（票 16 的事，票 14 不动）
`core/llm/client.py:111/:113` 仍 `os.getenv("OPENAI_BASE_URL")/("OPENAI_API_KEY")`（get_llm_client 单例）；消费端 `optimize.py:267`、`split_by_llm.py:114`、`llm_translator.py:232` 的 `call_llm` 回退分支在 profile=None 时走它。票 14 删掉 CLI 的环境写入后这些回退分支自然失效，但**文件本身归 16 号票退役**。

---

## 4. Ticket 14：`cli/main.py` 命令组结构

### 4.1 子命令定义位置（`_build_*_parser`，注册在 `build_parser` :1039-1062）
| 命令 | builder | 行 |
|---|---|---|
| transcribe | `_build_transcribe_parser` | :333-384 |
| gui | `_build_gui_parser` | :387-393 |
| subtitle | `_build_subtitle_parser` | :396-500 |
| postprocess | `_build_postprocess_parser` | :503-528 |
| dub | `_build_dub_parser` | :614-723 |
| synthesize | `_build_synthesize_parser` | :531-611 |
| process | `_build_process_parser` | :726-902 |
| download | `_build_download_parser` | :920-931 |
| config | `_build_config_parser` | :934-1016 |
| doctor | `_build_doctor_parser` | :1019-1036 |
| style | `_build_style_parser` | :905-917 |

`build_parser` :1050-1060 逐个调用。**新 profile 组落点**：新建 `_build_profile_parser(subparsers)`（照 `_build_doctor_parser` 的形态——无位置参数、`p.set_defaults(func=_run_profile)`），在 :1060 附近注册；runner `_run_profile(args)` 照 :1342-1346 `_run_doctor` 模式（`_load_config(args)` + `commands/profile_cmd.run(args, config)`）。

### 4.2 现有 LLM 旗子先例（要改造成三面旗子）

- **共享组** `_add_llm_options(parser)` :46-55：`--api-key`（help 提 OPENAI_API_KEY env）、`--api-base`、`--model`。消费者：postprocess（:526）、process（:737）。
- **隐藏版** `_add_hidden_llm_options(parser)` :58-62：同三旗 `argparse.SUPPRESS`。消费者：dub（:722）。
- **subtitle 自带组** :414-419（`llm = p.add_argument_group("LLM options")` + 三旗）。
- `_add_translation_llm_profile_options(group)` :65-84：现有 `--main-llm-endpoint`/`--main-llm-max-output-tokens`/`--main-llm-request-options-json` 与 review 对应——**这组随 inline 表硬切整体删除**（消费者 subtitle :477、process :807；`_build_cli_overrides` :1092-1104 的写入段一并删）。
- **三面旗子** `--profile` / `--review-profile` / `--utility-profile` 挂 subtitle/process/dub/postprocess 四个消费子命令。注意：postprocess 现有 `--profile`/`--speed-profile`（:234-240，dest=`speed_profile`，后处理模板 id）**已占用 `--profile` 名字**——同组里 argparse 同名冲突会直接崩，必须改名或换 dest（见坑 6）。
- `_build_cli_overrides`（:1078-1256）的 LLM 段 :1088-1091（`_set("llm.api_key", ...)` 等三行）换成 `llm.profile_id`/`llm.review_profile_id`/`llm.utility_profile_id`；:1092-1104 的 role 段删。

### 4.3 process.py 的旗子透传（三面旗子必须穿透）

process 用 `Namespace(...)` 手工转装子命令参数，三处都要加新旗子字段：
- sub_args :136-165（subtitle 段，现有 `api_key/api_base/model` 在 :148-150）
- post_args :185-206（postprocess 段，现有 :199-201）
- dub_args :241-279（dub 段，现有 :273-275）

---

## 5. Ticket 14：validators 与 doctor

### 5.1 `cli/validators.py`
- `validate_llm(config)` :83-112：读 `llm.api_key`/`llm.model`，缺失走 `output.config_missing_error(...)`（引用 OPENAI_API_KEY/OPENAI_MODEL env 名），再 `build_legacy_llm_profile` 试构建。**整体重写**：改走方案库（库非空 + `llm.profile_id` 有效），`utility_profile_id` 空 = 派生合法。错误文案指向方案库文件与字段形状（`DEFAULT_LLM_PROFILES_PATH` + 三键名 + 可用 id 列表）。
- `validate_translation_llm(config, mode)` :115-148：调 `build_translation_llm_profile` ×2 + `translation_llm_role_allows_empty_api_key`。**重写**：main = `store.get(llm.profile_id)`；enhanced 模式下 `llm.review_profile_id` 为空即 fail（输出指引自建校对方案）；single_llm 不校验 review。`translation_llm_role_allows_empty_api_key` 的「显式空 key 容忍」语义随 [llm] 删除而消亡（方案库里 key 就是字段值，空即空）。
- `validate_subtitle(config)` :234-250：`legacy_llm_needed`（:239-241，读 `subtitle.optimize` + `subtitle.compress_fast_subtitles`）门调 `validate_llm`；`validate_dubbing` :258-312 的 `rewrite and not validate_llm(config)`（:310-311）。这两处的 validate_llm 语义随重写自动跟新。
- `validate_process` :315-323 链式调用，无需单独改。

### 5.2 `cli/commands/doctor.py` 的 LLM 检查
全部在 `_check_subtitle(config)` :182-204：
```python
200	    if needs_llm and not get(config, "llm.api_key", ""):
201	        checks.append(Check("llm.api_key", "warn", "LLM API key is missing; ...", "Run 'videocaptioner config set llm.api_key <key>' ..."))
202	    if needs_llm and not get(config, "llm.model", ""):
203	        checks.append(Check("llm.model", "error", "LLM model is missing", "Run 'videocaptioner config set llm.model <model>'"))
```
- `needs_llm` 判定 :189-191（`optimize or split or (translate and mode in {single_llm, enhanced_llm})`）。
- 改为：方案库非空（`LLMModelProfileStore().list()`）+ `llm.profile_id`（及 enhanced 模式的 `llm.review_profile_id`）有效。Check 名建议 `llm.profile_id`/`llm.review_profile_id`，fix 文案指向 `videocaptioner profile list`。
- `--check-api` 的 `_check_api` :243-248 只看 dubbing，不动。

---

## 6. Ticket 14：`cli/commands/config_cmd.py`

- **init 的 LLM 三问**（`_interactive_init` :143-146）：
  ```python
  143	        print("LLM config is used for AI subtitle polish, LLM translation, and --adapt-length.")
  144	        _set_nested(config_data, "llm.api_key", _prompt("LLM API key [skip]: "))
  145	        _set_nested(config_data, "llm.api_base", _prompt(f"LLM API base [{DEFAULTS['llm']['api_base']}]: ", DEFAULTS["llm"]["api_base"]))
  146	        _set_nested(config_data, "llm.model", _prompt(f"LLM model [{DEFAULTS['llm']['model']}]: ", DEFAULTS["llm"]["model"]))
  ```
  缩为 profile_id 占位 + 注释指引（一个问题 + 指向方案库文件与 `videocaptioner profile list`）。
- **非交互映射表**（`_build_onboarding_config` :176-188）：`"llm_api_key": "llm.api_key"` :177、`"llm_api_base": "llm.api_base"` :178、`"llm_model": "llm.model"` :179 三行换 `llm.profile_id`（对应的 argparse 旗子在 main.py `_build_config_parser` :964-966 `--llm-api-key/--llm-api-base/--llm-model`，缩为 `--llm-profile`）。
- **模板**（`_render_onboarding_template` :207-227）：:216 的 llm 用途注释行改写；`_user_facing_config` :230-281 的 `"llm"` 块 :233-239（api_key/api_base/model/work_context_tokens/max_concurrency 五键）缩为 `{"profile_id": ..., "review_profile_id": ..., "utility_profile_id": ...}`。
- **set/get**（`_set` :57-75、`_get` :78-91）：用 `get(DEFAULTS, key)` 校验键存在（:59），DEFAULTS 换三键后 `llm.profile_id` 自然可 set/get，旧键自然拒绝（`Unknown config key`）——无需改逻辑。注意 :73/:87 的掩码逻辑（`"key" in key` 判断）对 `profile_id` 不触发，profile show 的掩码另做（§8）。
- **show**（`_show` :44-46）走 `format_config`（config.py:705-719），DEFAULTS 变化后自动跟新。

---

## 7. Ticket 14：VIDEOCAPTIONER_LLM_API_KEY 窄覆盖的落点

**要求**：只换已解析方案的凭证、不动 base_url/model，请求日志记 `key_source=env_override`。

**现状机制**（`core/llm/request_logger.py`）：
- `_base_entry(profile, request)` :122-134 是全部日志条目的骨架：`time`/`request_id`/`stage`（metadata）/`role`（metadata）/`profile: {"id": profile.profile_id, "model": profile.model}`。**没有任何 api_key 或 key 来源字段**。
- `begin_gateway_request` :137-162（gateway 每次 attempt 调，gateway.py:120）、`log_gateway_cache_hit` :190-202（gateway.py:109 缓存命中调）。二者都以 `_base_entry` 起底。
- gateway 调用链：`LLMGateway.complete(profile, request, ...)`（gateway.py:95-160）→ `begin_gateway_request(profile, request, attempt=...)`。profile 是**每次 complete 调用传入的对象**，gateway 内部不缓存它（`_resources` :70-83 只按 profile_id 缓存 adapter/信号量）。

**最顺的落点：CLI 装配时 `dataclasses.replace(profile, api_key=env_value)`**，理由：
1. `LLMModelProfile` 是 frozen dataclass（models.py:121-190），`replace()` 是既有惯例（`utility.py:45-50` 的 `_stripped` 正是这么剥三字段的）。
2. 不动 gateway/request_logger 的核心路径——`_base_entry` 只透传 profile.id/model，replace 后 id/model 不变，日志天然正确。
3. `key_source=env_override` 的落法有两个选项，推荐 **(b)**：
   - (a) 在 `LLMRequest.metadata` 里带上（`metadata={"key_source": "env_override"}`）——但 metadata 现在只装 stage/role，且 `log_gateway_cache_hit`/`begin_gateway_request` 只取 stage/role 两个键，其余被丢弃；除非同时改 `_base_entry`。
   - (b) 在 `begin_gateway_request`/`log_gateway_cache_hit`/`_base_entry` 加一个显式来源判断——但 logger 无法从 profile 对象上看出 key 来自 env。
   - **干净做法**：在 CLI 装配处（resolve 完 profile 之后）对每个 profile 做 `profile = replace(profile, api_key=os.environ["VIDEOCAPTIONER_LLM_API_KEY"])`，并往 `LLMRequest.metadata` 塞 `key_source=env_override`；同时把 `_base_entry` :128-129 扩为 `"key_source": str(request.metadata.get("key_source", ""))` 一行（或只在该键存在时写入，避免所有存量条目多一个空字段——测试 `test_request_logger.py` 断言条目形状，加空字段会破坏既有断言，**建议仅存在时写入**）。
4. 缓存键天然正确：`response_cache._cache_key`（response_cache.py:58-98）把 `profile.api_key` 全量入键（:72），replace 后的 key 自动换摘要，不会误命中 store 原key 的缓存条目——正是「从 CI 注入 key」想要的行为。

**建议装配函数形态**（新，放 `cli/config.py` 或命令侧 helper）：
```python
def apply_env_api_key_override(profile: LLMModelProfile) -> LLMModelProfile:
    key = os.environ.get("VIDEOCAPTIONER_LLM_API_KEY", "").strip()
    return replace(profile, api_key=key) if key else profile
```
三处消费（subtitle 的 main/review/utility、postprocess 的 utility、dub 的 utility）装配时统一过一遍。`OPENAI_API_KEY` 不做任何映射（ENV_MAP 删干净后自然不认）。

---

## 8. Ticket 14：profile list / show / set-default 命令组

- **新文件** `videocaptioner/cli/commands/profile_cmd.py`，照 `style_cmd.py`（:1-88）的形态：模块级 `run(args, config)` + 私有 `_list/_show/_set_default` + `__all__`。命令结构先例也参考 `config_cmd.py` 的 `config_action` 分发（:24-41）——但 ticket 明确**不嵌进 config 子命令**（store 与 config.toml 各管各的文件）。
- argparse 侧：`_build_profile_parser` + `p.add_subparsers(dest="profile_action")`，三个子命令 `list` / `show <id>` / `set-default <id>`。
- **store API**（`core/llm/profiles.py`）：`LLMModelProfileStore(path=None)` :78-81（默认 `DEFAULT_LLM_PROFILES_PATH` :19 = `APPDATA_PATH / "llm_model_profiles.json"`）；`list() -> tuple[LLMModelProfile, ...]` :133-139（按 name 排序、返回深拷贝）；`get(profile_id)` :141-146（不存在抛 `LLMProfileNotFoundError` :38）；`save(profile)` :148-172（含 request_options 校验与重名冲突检查，返回落盘后副本）；`create(**values)` :174-176；`delete` :178-185。
- **LLMModelProfile 全字段**（models.py:121-136）：`profile_id`/`name`/`transport`/`dialect`/`base_url`/`api_key`/`model`/`work_context_tokens`(65536)/`max_concurrency`(4)/`openai_endpoint`(CHAT_COMPLETIONS)/`request_options`({})/`max_output_tokens`(None)；`to_dict()` :192-206（键名 `id` 不是 `profile_id`）。show 命令直接渲染 `to_dict()`（除掩码外）。
- **set-default 语义**：写 `llm.profile_id` 进 TOML（复用 `save_config_value("llm.profile_id", id)`，config.py:655-669，自带 0600 权限），先 `store.get(id)` 校验存在，失败列 `store.list()` 的可用 id。
- **掩码格式先例**（repo 已有三处同款 `f"{value[:4]}...{value[-4:]}"` if len>8 else `"****"`）：
  - `cli/config.py:715`（format_config）
  - `cli/commands/config_cmd.py:87`（_get）
  - `core/entities.py:652-656`（`TranscribeConfig._mask_key`——独立方法，最接近 profile show 想要的形态）
  - `config_cmd.py:73`（_set 的回显，`f"{value[:4]}...{value[-4:]}"` 同款）
  - profile show 的 api_key 掩码直接照抄此格式；「原文直接读方案库文件」= 提示 `DEFAULT_LLM_PROFILES_PATH` 路径（show 输出末尾 hint 一行即可）。
- 测试先例：`tests/test_llm/test_profiles.py`（store 契约直测）、`tests/test_cli/test_parser.py` 的 `TestConfigParser` :689-727（`main(["config", ...])` 级命令测试）。

---

## 9. Ticket 14：增强型翻译 review 为空即 fail-fast（GUI 语义对齐）

GUI 侧先例（**CLI 侧要对齐的语义**）：
- `core/entities.py:779-793` `is_translation_mode_available(mode=None)`：`single_llm` → `main_llm_profile is not None`；`enhanced_llm` → main 与 review **都**非 None；`non_llm` → 服务枚举合法。
- `core/entities.py:795-802` `missing_translation_roles(mode=None)`：`enhanced_llm` 缺 review 也算 missing（返回 `("review",)`）——即 **review 为空不静默回退 main**。
- `ui/task_factory.py` 装配段（GUI 不 fail-fast 在工厂层，而是把 None 传下去）：main/review 经 `_resolve_llm_profile`（:52-62，**注意：NotFound 时返回 None 而非抛错**——宽松契约）；subtitle 装配 :353-393。
- 真正的 fail-fast 在任务线程：`ui/thread/subtitle_thread.py` `_validate_utility_profile` :143-157（utility 双空抛 `UtilityProfileError(UTILITY_PROFILE_UNRESOLVED_MESSAGE)`）；`_validate_enhanced_profile_compatibility` :164-179。

**CLI 侧落法**：`validate_translation_llm` 重写时，`translation_mode == "enhanced_llm"` 且 `llm.review_profile_id` 为空 → `output.error(...)` + hint 指引「在 TOML 设 llm.review_profile_id 或用 --review-profile 指定一个已有方案」+ 列可用 id；`single_llm` 不查 review。`utility_profile_id` 空 = 派生，合法。

**包装 resolve_utility_profile 的 GUI 卡片文案**（ticket 原文「必要时包装」）：`utility.py` 的报错指向 `UTILITY_PROFILE_CARD = "翻译设置页·工具模型卡"`（:24）与 `UTILITY_PROFILE_UNRESOLVED_MESSAGE`（:27-30）。CLI 是纯 Agent 面，应 catch `UtilityProfileError` 后改写为 CLI 语境（指向 `llm.profile_id`/`llm.utility_profile_id`/`videocaptioner profile list`），不要把 GUI 卡片文案直接吐给 agent。15 号票验证「文案指向工具模型卡」用的是 GUI 路径，两侧不冲突。

---

## 10. Ticket 14：`core/llm/utility.py` 当前 API（@3028505，146 行）

- `UTILITY_PROFILE_CARD = "翻译设置页·工具模型卡"` :24
- `UTILITY_PROFILE_UNRESOLVED_MESSAGE` :27-30（「未找到可用的模型配置方案：主翻译方案与工具模型绑定均为空，请到{卡片}选择或创建模型配置方案」）
- `MAIN_LLM_PROFILE_MISSING_MESSAGE` :32-35（「未配置主翻译模型配置方案，无法使用 LLM 翻译；请到翻译设置页选择或创建模型配置方案」——11 号票为 GUI 加的，**CLI fail-fast 指引要另写或包装它**）
- `class UtilityProfileError(ValueError)` :38-39
- `_stripped(profile)` :42-50：`replace(profile, openai_endpoint=CHAT_COMPLETIONS, request_options={}, max_output_tokens=None)`
- `_lookup(store, profile_id, *, description)` :53-68：空 id → None；NotFound → `UtilityProfileError("「{id}」已不存在，请到{卡片}重新绑定或恢复该方案")`
- `resolve_utility_profile(store, main_profile_id, utility_profile_id=None) -> LLMModelProfile` :71-94：绑定优先 :86-88 → 主翻译派生 :90-92 → 双空抛 :94
- `validate_utility_profile(profile)` :97-113：`validate_profile_request_options` + 三字段断言（endpoint 必须 CHAT_COMPLETIONS :107-108、request_options 必须空 :109-110、max_output_tokens 必须 None :111-112）+ `validate_structured_output_compatibility` :113。纯本地。
- `borrow_utility_gateway(gateway)` :116-135：函数型消费点（dub rewrite/compress/semantic）的 gateway 惰性借用 seam（注入不关、自建 finally close）。
- 包级导出（`core/llm/__init__.py`）：`UtilityProfileError`/`resolve_utility_profile`/`validate_utility_profile` :26-30、:51/:53/:55；`MAIN_LLM_PROFILE_MISSING_MESSAGE` **未导出**（GUI 内部 import）。

---

## 11. Ticket 14：三键解析要用的 profile store / models 要点

- `LLMModelProfileStore.list()` 返回**按 name 排序**的深拷贝元组；`get()` NotFound 抛 `LLMProfileNotFoundError`（KeyError 子类）——CLI 装配 catch 它转 fail-fast 输出（列可用 id）。
- `LLMModelProfile.__post_init__`（models.py:138-190）的校验：profile_id 必须 1-64 小写 ASCII（`_PROFILE_ID_RE`）、base_url/model 非空、work_context_tokens ≥16384、max_concurrency 1-50。**store 里已存在的方案不会再触发这些**（load 时已验），但 CLI 若要构造新 profile（set-default 不需要）会走。
- `DEFAULT_LLM_PROFILES_PATH` :19 与 `SETTINGS_PATH`（`videocaptioner/config.py:76`）都在 `APPDATA_PATH` 下；`APPDATA_PATH` 可被 `VIDEOCAPTIONER_APPDATA_PATH` 覆盖（config.py:53-55，测试 conftest 正是这么隔离的，见 tests/conftest.py:16-19）。**CLI profile 命令的测试用同一覆盖机制即可，不要写真用户目录。**

---

## 12. Ticket 14：现有 CLI 测试布局（硬切后会删/改写的用例）

### 12.1 `tests/test_cli/test_config.py`（516 行）——重灾区
| 用例 | 行 | 处置 |
|---|---|---|
| `TestParseValue.test_string` :93-94（`_parse_value("gpt-4o", "llm.model")`） | 改键 |
| `TestConfigRoundtrip.test_save_and_load` :122-133（`save_config_value("llm.model", "gpt-4o")` :126） | 改键 |
| `TestBuildConfig.test_defaults_only` :137-139（`config["llm"]["model"]`） | 改断言 |
| `TestBuildConfig.test_cli_overrides` :141-143（`{"llm": {"model": "custom"}}`） | 改键 |
| `TestBuildConfig.test_env_overrides` :145-148（`VIDEOCAPTIONER_LLM_MODEL` env） | **删/改**（env 映射没了） |
| `TestBuildConfig.test_priority_cli_over_env` :150-153（同上） | **删/改**（可换成 PROFILE_ID 版本） |
| `TestBuildConfig.test_compress_fast_requires_llm_validation` :176-191（`llm.api_key` 空 → validate_subtitle False） | 改写（方案库语义） |
| `TestTranslationLLMProfiles` **整个类** :194-515（15 个用例全测 [llm]→main→review 继承链：`test_missing_new_sections_is_exact_legacy_fallback` :206、`test_role_fields_inherit_by_presence...` :222、`test_source_priority_keeps_role_layers...` :265、`test_lower_priority_role_aliases...` :313、`test_conflicting_aliases...` :360、`test_role_environment_supports_complete_connection_fields` :374、`test_request_options_json_must_be_a_valid_object` :410、`test_max_output_tokens_rejects_invalid_values` :424、`test_protected_request_option_is_rejected...` :435、`test_native_transport_rejects_responses_endpoint` :450、`test_explicit_empty_key_is_valid_for_keyless_translation_profile` :469、`test_explicit_empty_legacy_key_is_valid_without_role_sections` :481、`test_single_mode_does_not_validate_unused_review_profile` :500） | **整类删除**（继承链没了），换三键 + store 的新用例 |
| 顶部 import :5-18（`build_legacy_llm_profile`、`build_translation_llm_profiles`） | 删 |

### 12.2 `tests/test_cli/test_translation_modes.py`（323 行）
- `test_cli_enhanced_translation_is_automatic_and_persists_artifacts` :69-122：config 里 `"llm": {"api_key": "test-key", "model": "test-model"}`（:96）→ 改三键 + 临时 store monkeypatch；断言 `main_role.profile == review_role.profile` :116（inheritance 语义变）。
- `test_enhanced_translation_uses_independent_profiles_and_warns_once_for_store` :125-183：`"llm": {"api_key": "legacy-key", ...}` :151 + `translate.llm.main/review` 表 :155-169 → **整体改写**（inline 表没了，store 版本要复刻 store=true 警告断言 :183）。
- `test_single_llm_keeps_reflection_and_uses_profile` :226-270：`"llm": {...}` :247 + `translate.llm.main/review` :253-257 → 同上改写。
- 其余用例（:37-66、:186-223、:273-322）不碰 [llm]，保留。

### 12.3 `tests/test_cli/test_parser.py`（740 行）
- `TestSubtitleParser.test_translation_role_options_build_partial_overrides` :340-368（`--main-llm-endpoint` 等旗子）→ **删**（旗子组删除），换 --profile/--review-profile/--utility-profile 解析用例。
- `TestSubtitleParser.test_translation_role_flags_reject_unknown_endpoint` :370-375 → 删。
- `TestConfigParser.test_show` :708-713（断言 `"llm:"` 与 `"api_key"` 在输出）→ 改断言（DEFAULTS 换三键后 `api_key` 消失，`profile_id` 出现）。
- `TestProcessParser`/`TestDubParser` 若加三面旗子需补 parse 用例（现有 :428-475、:628-688 的形态可参考）。

### 12.4 其它
- `tests/test_cli/test_subtitle_command.py`（:7-45）：两条用例都 `--no-optimize --no-translate --no-split`，不触发 LLM 路径，**应保持绿**（validate_llm 只在 optimize/split/llm_translation 时走）。改名/加用例时保这条不变式。
- `tests/test_cli/test_postprocess_command.py`（:9-118）：四条用例都不配 [llm]，`postprocess.py` 的临时桥在 `llm_model` 空时不触发——改写后注意「无方案时 postprocess 是否 fail-fast」的语义选择（GUI 侧 `needs_utility_llm()` 门先例，`core/postprocess/config.py:213-222`）。
- `tests/conftest.py` 的 `mock_llm_client`（:220-287）已含 `_FakeGatewayFromCall` 桥（:197-217）与 `LLMGateway` 四处 monkeypatch（:271-286），**票 14 不动它**（16 号票退役）。

---

## 13. Ticket 15：`ui/view/setting_interface.py`（注意路径在 `ui/view/` 下）

### 13.1 llmGroup 创建（:74-78）
```python
75	        # 旧通用 LLM 工具仍服务于断句、校正；翻译角色在独立分页中配置。
76	        self.llmGroup = SettingCardGroup(
77	            self.tr("通用 LLM 工具配置"), self.scrollWidget
78	        )
```

### 13.2 `__createLLMServiceCards`（:293-447）
- 服务选择卡 :296-304（`ComboBoxSettingCard(cfg.llm_service, ...)`，texts 来自 `cfg.llm_service.validator.options`）。
- 七服务配置 dict :307-377：OPENAI :308-321（`cfg.openai_api_key/api_base/model`）、SILICON_CLOUD :322-332、DEEPSEEK :333-340、OLLAMA :341-348、LM_STUDIO :349-356（prefix 是带空格的 `"LM Studio"`，setattr 出来的属性名带空格——无害）、GEMINI :357-368、CHATGLM :369-376。共 21 个凭证 cfg 键。
- 卡片创建循环 :379-435：`self.llm_service_configs = {}` :380；每服务 `LineEditSettingCard` ×2（api_key :387-394、api_base :398-405，OPENAI/OLLAMA/LM_STUDIO 之外只读 :409-414）+ `EditComboBoxSettingCard`（model :417-424）；存 `self.llm_service_configs[service]` :430-435（键 `cards`/`api_base`/`api_key`/`model`）。
- **检查连接按钮** :438-444：`PushSettingCard(self.tr("检查连接"), FIF.LINK, self.tr("检查 LLM 连接"), self.tr("点击检查 API 连接是否正常，并获取模型列表"), self.llmGroup)`。
- 初始状态调用 :447：`self.__onLLMServiceChanged(self.llmServiceCard.comboBox.currentText())`。

### 13.3 检查连接回调 + LLMConnectionThread
| 成员 | 行 | 行为 |
|---|---|---|
| `checkLLMConnection` | :870-910 | 读 `LLMServiceEnum(currentText())` :876 → `llm_service_configs` :879 → 从卡提取 api_base/api_key/model :883-897 → 禁按钮 :900-901 → `LLMConnectionThread(api_base, api_key, model)` :907，connect finished/error :908-910 |
| `onConnectionCheckError` | :912-921 | 复位按钮 + `InfoBar.error("LLM 连接测试错误", ...)` |
| `onConnectionCheckFinished` | :923-958 | 复位 :925-926；models 非空则 `service_config["model"].setItems(models)` 保当前文本 :931-937 + InfoBar 成功 :939-944；连接成败 InfoBar :945-958 |
| `LLMConnectionThread`（类） | :1256-1275 | `finished = pyqtSignal(bool, str, list)` :1257、`error = pyqtSignal(str)` :1258；`__init__(api_base, api_key, model)` :1260-1264；`run()` :1266-1275 调 `check_llm_connection(...)` + `get_available_models(self.api_base, self.api_key)` |

### 13.4 信号连接（`__connectSignalToSlot` :736-810 内）
- :741-743 `self.llmServiceCard.comboBox.currentTextChanged.connect(self.__onLLMServiceChanged)`
- :751 `self.checkLLMConnectionCard.clicked.connect(self.checkLLMConnection)`
（llmGroup 仅此两条连接；:738 cfg.appRestartSig 与 :745 起属其它组。）

### 13.5 入组/布局段
- `__initLayout` :688-734；LLM 块 **:716-721**：`self.llmGroup.addSettingCard(self.llmServiceCard)` :717 + 循环加 21 卡 :718-720 + `addSettingCard(self.checkLLMConnectionCard)` :721。
- 组进页面 :727 `self.expandLayout.addWidget(self.llmGroup)`（:723-734 的组顺序：transcribeGroup :726、llmGroup :727、translationSettingsWidget :728、translateGroup :729、postprocessGroup :730、subtitleGroup :731、saveGroup :732、personalGroup :733、aboutGroup :734）。
- `__onLLMServiceChanged` :963-993：隐藏全部服务卡 :968-970 → 显示选中服务的卡 :973-975 → 注入 ollama/lm-studio 默认 key :977-989 → `adjustSize` :992 + `update` :993。

### 13.6 imports 与「共用控件类」清单（**不能删的**）
qfluentwidgets 块 :6-21、FIF :22、本地 import :24-46。**被转录组共用的四个类，删除后仍须保留 import**：
| import 行 | llm 用点 | 其它组用点（保留依据） |
|---|---|---|
| `ComboBoxSettingCard` :7 | :296 | :126/:175/:234/:452/:561/:569/:585/:593 |
| `PushSettingCard` :14 | :438 | :185/:503/:553/:626 |
| `LineEditSettingCard` :43 | :387/:398 | :470/:480/:511/:519/:577（全转录组） |
| `EditComboBoxSettingCard` :42 | :417 | :490/:527（转录组） |
| `SpinBoxSettingCard` :45 / `SwitchSettingCard` :17 / `OptionsSettingCard` :12 / `PrimaryPushSettingCard` :13 / `HyperlinkCard` :10 / `SettingCardGroup` :16 / `ExpandLayout` :9 / `InfoBar` :11 | — | 广泛使用，全保留 |

**llmGroup 删除后变死、可删的 import**：
- :36 `from videocaptioner.core.llm.check_llm import check_llm_connection, get_available_models` —— 仅 `LLMConnectionThread.run`（:1269/:1272）使用。注意：`get_available_models` 在 `videocaptioner/` 内的**其余调用者为零**，但 15 号票要在方案编辑器新增「获取模型列表」按钮复用它——所以 **import 会搬家到 TranslationSettingWidget.py，函数本体保留**（`check_llm_connection` 三标量入口则由 16 号票删）。
- :32 `LLMServiceEnum`（from core.entities）—— 仅 :308-377/:876/:929/:965/:979/:984 使用，可删。同 import 行的 `LANGUAGES`（:31，用于 :1063 MiMo）与 `TranscribeModelEnum`（:33，:1024-1026）**保留**。

### 13.7 本文件内其它 llm 属性引用（删除时全要清）
`self.llm_service_configs`：:380/:430/:718/:879/:933/:968/:973/:974/:978；`self.llmGroup`：:76/:302/:393/:404/:423/:443/:717/:720/:721/:727/:992；`self.llmServiceCard`：:296/:304/:447/:717/:741/:876/:929；`self.checkLLMConnectionCard`：:438/:721/:751/:900-901/:914-915/:925-926；`self.connection_thread` :907；`LLMConnectionThread` :907/:1256；`cfg.llm_service` :297/:301。

**同文件 :201-210 的 `llmContentLoggingCard`（saveGroup，:282 入组）用的 `cfg.llm_content_logging` 不在删除范围**——见坑 2。

---

## 14. Ticket 15：`ui/common/config.py`

### 14.1 22 个旧键（:111-158，连续块）
- :111 注释、:112-118 `llm_service`（OptionsConfigItem("LLM", "LLMService", LLMServiceEnum.OPENAI, ...)）
- 七组三元组：openai :120-122、silicon_cloud :124-128、deepseek :130-134、ollama :136-138、lm_studio :140-144、gemini :146-152、chatglm :154-158
- 共 1+21=22 项，**整块删除**。

### 14.2 `utility_llm_profile_id`——**已存在**（ticket 11 已加），:182
```python
178	    main_llm_profile_id = ConfigItem("Translate", "MainLLMProfileId", "")
179	    review_llm_profile_id = ConfigItem("Translate", "ReviewLLMProfileId", "")
180	    # 工具角色（断句/字幕优化/后处理/配音改写）的独立方案绑定；
181	    # 空 = 跟随主翻译方案派生（解析器派生路径，ADR-0014）。
182	    utility_llm_profile_id = ConfigItem("Translate", "UtilityLLMProfileId", "")
```

### 14.3 **紧邻其后的 `llm_content_logging`（:159-161）不能删**
```python
159	    llm_content_logging = ConfigItem(
160	        "LLMLog", "ContentLogging", False, BoolValidator()
161	    )
```
组名是 `"LLMLog"` 不是 `"LLM"`，且被 `setting_interface.py:201-210` 的 llmContentLoggingCard、模块尾 :736-737 的 `set_llm_content_logging` wiring 依赖。

### 14.4 模块尾（:731-737）
`cfg = Config()` :731 → `migrate_legacy_translation_settings(SETTINGS_PATH)` :734 → `qconfig.load(SETTINGS_PATH, cfg)` :735 → llm_content_logging wiring :736-737。删 22 键不影响这段（migrate 读的是原始 JSON 不是 cfg 项，见 §17）。

---

## 15. Ticket 15：`ui/task_factory.py`（@3028505，696 行）——11-13 后的现状

- **旧 7 服务 if/elif 分支已删干净**：`cfg.llm_service`/`cfg.openai_*`/`cfg.silicon_cloud_*`/`cfg.deepseek_*`/`cfg.gemini_*`/`cfg.chatglm_*`/`cfg.ollama_*`/`cfg.lm_studio_*` 在 task_factory.py **零命中**（全 `videocaptioner/` 范围内只剩 setting_interface.py 一处）。
- **utility 三标量传递也已删**：SubtitleConfig 六标量（base_url/api_key/llm_model/utility_llm_*）在 `core/entities.py` 已不存在，只剩 `utility_llm_profile: Optional[LLMModelProfile]`（:729）。
- `_resolve_llm_profile(store, profile_id)` helper :52-62：空 id → None；NotFound → **None（不抛）**——宽松契约，fail-fast 留给任务线程。
- `create_subtitle_task` :289-473：profile 解析 :353-393（main/review 走 `_resolve_llm_profile` :366-375；utility 解析 :382-393——门 `cfg.need_split or cfg.need_optimize` 且 `main or utility id 非空` 时才 resolve，否则 None）；`SubtitleConfig(...)` :414-459，首个实参 `utility_llm_profile=utility_profile` :416。
- `create_postprocess_task` :475-551：**旧 model_items 映射已删**；模型注入现为 :511-522（`resolved_enabled and config.needs_utility_llm() and config.utility_llm_profile is None` 时 `replace(config, utility_llm_profile=resolve_utility_profile(LLMModelProfileStore(), main_id, utility_id))`）。
- `needs_utility_llm()` 定义在 `core/postprocess/config.py:213-222`；`utility_llm_profile` 是运行期注入字段（`RUNTIME_INJECTED_FIELDS = frozenset({"utility_llm_profile"})` :239，字段 :81）。
- **结论：票 15 的「task_factory 装配段旧逻辑」一条实际上已被 11-13 做掉**，task_factory.py 本票大概率零改动（除非工具模型卡的绑定键读取路径需要动——不需要，:382/:519-520 已在读 `cfg.utility_llm_profile_id`）。

---

## 16. Ticket 15：`ui/components/TranslationSettingWidget.py`（@3028505，1078 行）

### 16.1 ProfileSelectionCard（**同文件定义**，:616-683）
- 构造 :621 `def __init__(self, config_item, title: str, content: str, parent=None)`；信号 :617-619 `createRequested/editRequested/deleteRequested = pyqtSignal(object)`。
- `self.comboBox` :625（minWidth 170）、create/edit/delete 三按钮 :627-629。
- `refresh(profiles)` :654-677：填下拉（含「未配置」与「缺失方案」项）、按选中态切按钮、空时 contentLabel 显示「未配置，相关 LLM 翻译模式不可用」。
- `selectedProfileId()` :679-680；`_onSelectionChanged` :682-683 经 `cfg.set(self.configItem, ...)` 写回。

### 16.2 `_profileCard` helper（:915-928）
```python
915	    def _profileCard(self, config_item, title: str, content: str, parent) -> ProfileSelectionCard:
918	        card = ProfileSelectionCard(config_item, title, content, parent)
924	        card.createRequested.connect(self._createProfile)
925	        card.editRequested.connect(self._editProfile)
926	        card.deleteRequested.connect(self._deleteProfile)
927	        self.profileCards.append(card)
928	        return card
```
`self.profileCards` 声明 :756，由 `refreshProfiles` :955-958 消费。**工具模型卡照抄此形态 + `cfg.utility_llm_profile_id`**。

### 16.3 pivot 结构与「页签区上方」落点
`__init__` :739-772：`titleLabel` :747（StrongBodyLabel("翻译设置")）、`subtitleLabel` :748-751、`self.pivot = SegmentedWidget(self)` :752、`self.stackedWidget` :754、`self.pages` :755、`self.profileCards` :756、`self._probeThreads` :757。布局 :760-772：
```python
760	        self.rootLayout = QVBoxLayout(self)
763	        self.rootLayout.addWidget(self.titleLabel)
764	        self.rootLayout.addWidget(self.subtitleLabel)
765	        self.rootLayout.addWidget(self.pivot, 0, Qt.AlignLeft)  # type: ignore
766	        self.rootLayout.addWidget(self.stackedWidget)
```
**顶层共享卡插在 :764 与 :765 之间**（subtitleLabel 之下、pivot 之上）——`rootLayout.insertWidget(2, card)` 或在 :764 后 addWidget。`_syncContentHeight` :936-953 会用 `rootLayout.sizeHint().height()` 自动量高，rootLayout 子项自动被计入。`_addPage` :774-789（pivot.addItem + QStackedWidget 页）；`_buildPages` :791-913 建三页（non-llm :792、single-llm :807、enhanced-llm :844）。

### 16.4 既有方案卡创建行（照抄对象）
- `singleMainProfileCard` :809-814（single-llm 页，组「模型与翻译」:808）
- `enhancedMainProfileCard` :848-853、`reviewProfileCard` :854-859（enhanced 页，组「模型、术语与审计」:847）
- `_deleteProfile` :1001-1023：只在 :1020-1022 清 main/review 两个绑定键——**新增工具模型卡后要在这里加 `cfg.utility_llm_profile_id` 的清理**（否则删方案后 utility 绑定残留）。

### 16.5 `_ProfileDialog`（:184-598）与模型框升级
- **modelEdit 现状**：:203 `self.modelEdit = LineEdit(self)`（qfluentwidgets 纯 LineEdit，import :25；placeholder「模型名称」:239，无 completer/combo）。
- 其它字段：nameEdit :197、interfaceCombo :198、dialectCombo :199、baseUrlEdit :200、apiKeyEdit :201（PasswordLineEdit）、contextSpin :204、concurrencySpin :205、outputModeCombo :206、outputTokensSpin :207、advancedButton :208、templateCombo :209、applyTemplateButton :210、templateHint :211、requestOptionsEdit :212（TextEdit）、probeButton :213、probeResultLabel :214。
- `values()` :481-500（键：name/transport/dialect/base_url/api_key/model/work_context_tokens/max_concurrency/openai_endpoint/request_options/max_output_tokens）；`temporaryProfile()` :502-507。
- **升级形态**：modelEdit 换可编辑下拉。参考 `EditComboBoxSettingCard`（`ui/components/EditComboBoxSettingCard.py` :15-23 签名：`__init__(configItem, icon, title, content=None, items=None, parent=None)`；`currentTextChanged = pyqtSignal(str)` :13；`addItem/addItems/setItems/clear` :83-108）。dialog 内部不是 SettingCard 体系，**直接用 `EditableComboBox`（qfluentwidgets）** 更贴近现状（LineEdit→EditableComboBox 一换一），旁边加「获取模型列表」`PushButton`。
- **拉取按钮**：复用 `get_available_models(base_url, api_key)`（check_llm.py:272-327，签名见 §16.7），线程模式照 `ModelContextProbeThread` 先例（:601-613）：`completed = pyqtSignal(object)` / `failed = pyqtSignal(str)`，run() 里调外部函数；wiring 照 `_startProbe` :1030-1070（`self._probeThreads` 追踪、completed/failed handler、deleteLater 清理 :1034-1036、InfoBar 成败提示 :1038-1066）。拉取失败只 InfoBar 提示不阻塞保存。
- **探测按钮承接**：probeButton（:213「测试文本与结构化能力」）+ `_requestProbe` :564-579 + `showProbeResult` :581-598 + `ModelContextProbeThread` :601-613 + `_connectProbe` :1025-1028 + `_startProbe` :1030-1070 已完整覆盖旧「检查连接」能力，**无需新增**。

### 16.6 `get_available_models`（`core/llm/check_llm.py:272-327`）
```python
272	def get_available_models(base_url: str, api_key: str) -> list[str]:
```
normalize base_url :283 → `openai.OpenAI(base_url, api_key, timeout=5).models.list()` :285-287 → 过滤非文本模型（tts/transcribe/realtime/embedding/vision/audio/search/text-/image/whisper/gpt-3.5/gpt-4- 黑名单 :289-309）→ 权重排序（gpt-5/claude-4/gemini-2/gemini-3→10、gpt-4→5、deepseek/glm/qwen/doubao→3 :312-320）→ 名称排序 :322-325。**异常行为：`except Exception: return []`**（:326-327）——永不抛、失败返回空列表。UI 侧「失败只提示」直接判空列表即可。

### 16.7 其它相关
- `TranslationSettingWidget.__init__` :739-772 中 `self._profile_store`（在 :746 构造，`main_window.py:27/:40` 实例化 `SettingInterface(self)` 时不传 store——默认构造）。
- `setting_interface.py:728` 把 `translationSettingsWidget` 加进 expandLayout。

---

## 17. Ticket 15：qconfig 对未知键的容忍机制（死数据不迁移的依据）

- settings.json 路径：`videocaptioner/config.py:76` `SETTINGS_PATH = APPDATA_PATH / "settings.json"`。
- 加载：`ui/common/config.py:735` `qconfig.load(SETTINGS_PATH, cfg)`。qfluentwidgets 1.8.4 的 `QConfig.load`（`.venv/Lib/site-packages/qfluentwidgets/common/config.py:347-389`）：
  ```python
  372	        items = {}
  373	        for name in dir(self._cfg.__class__):
  374	            item = getattr(self._cfg.__class__, name)
  75	            if isinstance(item, ConfigItem):
  376	                items[item.key] = item
  380	        for k, v in cfg.items():
  381	            if not isinstance(v, dict) and items.get(k) is not None:
  382	                items[k].deserializeFrom(v)
  383	            elif isinstance(v, dict):
  384	                for key, value in v.items():
  385	                    key = k + "." + key
  386	                    if items.get(key) is not None:
  387	                        items[key].deserializeFrom(value)
  ```
  **未知 JSON 键过不了 `items.get(key) is not None` 门，被静默跳过——无 schema 校验、无报错、无警告**。且整个方法裹 `@exceptionHandler()`（:347，`exception_handler.py:6-31` 吞一切 BaseException 返回 None）。`deserializeFrom` 内 `validator.correct` 也会把坏枚举值纠回默认而非抛错。
- 保存：`QConfig.save`/`toDict`（:321-345）只写类上声明的 ConfigItem——**下一次 save 时陈旧键（如 `LLM.OpenAI_API_Key`）自动从 settings.json 掉落**。
- 预迁移：`migrate_legacy_translation_settings(SETTINGS_PATH)` 在 `ui/common/config.py:734` 于 load 之前跑（`ui/common/translation_migration.py:67-154`），它读**原始 JSON**（自己的 `_LEGACY_PROVIDER_FIELDS` :29-42），以 `Translate.TranslationMigrationVersion >= 2` :92 为幂等门——**删除 Config 类上的 22 个属性不影响它**（不经过 cfg 项）。票 15 不需要为它做任何事。

---

## 18. Ticket 15：现有 UI 测试

### 18.1 `tests/test_ui/test_translation_setting_widget.py`
- **`test_setting_interface_embeds_translation_widget_and_relabels_legacy_llm`（:496-510）**——唯一断言 llmGroup 的用例：
  - :502 `assert widget.llmGroup.titleLabel.text() == "通用 LLM 工具配置"` —— **随组删除而改写/删除**
  - :504 `assert widget.llmContentLoggingCard is not None` —— **必须保住**（该卡不在删除范围）
  - :501/:503/:505-509 关于 translationSettingsWidget 的断言 —— 保留
- 全 tests/ 树 grep `cfg.llm_service`/`openai_api_key`/`LLMConnectionThread`/`llmServiceCard` **零命中**——旧键没有其它测试负担。
- 该文件其余用例测 TranslationSettingWidget/_ProfileDialog/singleMainProfileCard（:100/:478），不动。

### 18.2 `tests/test_postprocess/test_task_factory.py`（145 行）
- `_seed_utility_profiles` autouse fixture :11-34：tmp store 种 `main-profile`、monkeypatch `videocaptioner.core.llm.profiles.DEFAULT_LLM_PROFILES_PATH`（:28-30）、save/restore `cfg.main_llm_profile_id`（:31-34）。**这是 11-13 已改写过的形态，不是旧键 save/restore**——ticket 写的「:49-67 保存/恢复旧键的用例」已被 11-13 提前处理（见坑 8）。
- `test_workflow_postprocess_task_prefers_independent_utility_binding` :90-114（:105 save `cfg.utility_llm_profile_id`、:106 set、:112 restore、:114 断言派生 id）。
- 其余：`test_workflow_postprocess_task_derives_utility_profile_from_main_binding` :76-87、`test_disabled_postprocess_task_needs_no_utility_profile` :117-123、`test_snapshot_profile_is_preserved_not_re_resolved` :126-144。
- **没有任何测试 save/restore 旧 LLM.* 凭证键**（grep 零命中）。

### 18.3 其它
- `tests/test_ui/test_postprocess_interface.py:134`：`_run_qt_script` 字符串里 import SettingInterface（theme 测试，只碰 settingLabel，不碰 llmGroup）——删除组后应仍绿，但**值得跑一遍确认**（实例化 SettingInterface 会走 `__createLLMServiceCards`，删干净后这段脚本反而更轻）。
- `tests/test_ui/test_translation_task_modes.py`、`tests/test_thread/test_batch_translation_workflow.py`：只用 main/review/utility_llm_profile_id 新键，不动。
- `tests/test_subtitle/test_subtitle_thread.py` :181/:225/:245/:267/:361/:383 设 `config.utility_llm_profile`（实体字段），不动。

---

## 19. Ticket 15：`core/entities.py` 现状（@3028505）

- `SubtitleConfig` :722-764。**六个旧标量（base_url/api_key/llm_model/utility_llm_base_url/utility_llm_api_key/utility_llm_model）已全部删除**；现存唯一工具字段 `utility_llm_profile: Optional[LLMModelProfile] = None` :729（注释 :727-728 引 ADR-0014）。
- `is_translation_mode_available` :779-793 与 `missing_translation_roles` :795-802 全文见 §9。
- `print_config` :804+ 在 :815-817 读 `utility_llm_profile`（不再有旧标量打印）。
- entities.py 里仅剩的 api_key/base_url 形状字段全是 **ASR** 的（whisper_api_key :620、mimo_asr_api_key :625）。

---

## 20. Ticket 15：`videocaptioner/ui/` 内旧键全量引用表（grep 结果）

除 §13/§14 所列外，`ui/` 下对旧键的引用**只剩**：
- `ui/components/TranslationModeSelector.py:271-272/:446/:545/:553` —— 是 `non_llm_service_label`/`cfg.translator_service`（**非 LLM 翻译服务选择器，另一个功能，不要碰**）。
- `ui/view/main_window.py:27/:40` —— 只 import/实例化 `SettingInterface(self)`。
- `ui/thread/subtitle_thread.py` —— **旧键零引用**；全走 profile 对象（import `UTILITY_PROFILE_UNRESOLVED_MESSAGE` :28；`_validate_utility_profile` :143-157；`missing_translation_roles()` 用于 :221；`need_legacy_llm` :459-470；`need_utility_llm` :472-480；profile 传给 split/optimize :336-339/:378-379）。
- `ui/components/WhisperAPISettingWidget.py:18`、`ui/view/setting_interface.py:35` —— `check_whisper_connection`（ASR，不碰）。

---

## 附：ticket 文字与代码现状的出入（坑）

1. **setting_interface.py 的真实路径是 `videocaptioner/ui/view/setting_interface.py`**，不是 ticket/决策票写的 `ui/setting_interface.py`（决策票 04 的行号如 :75-78/:293-447 都对，但路径前缀少了一级 `view/`）。探索与实现都要按 `ui/view/` 找。
2. **`llm_content_logging`（`ui/common/config.py:159-161`）紧贴 22 键删除块的下一行**，组名是 `"LLMLog"`，被 llmContentLoggingCard（setting_interface.py:201-210、saveGroup :282）和模块尾 wiring（:736-737）依赖——**绝不能连带删**。同样 :178-182 的 main/review/utility_llm_profile_id 三个新键在删除块下方，注意别误伤。
3. **决策票 04 给的 setting_interface.py 行号已漂移**（那是 11-13 之前的勘察）：组创建现 :74-78（票写 :75-78，基本一致）；`__createLLMServiceCards` 现 :293-447（票写 :293-447，一致）；但入组段现 :716-721（票写 :716-721，一致）、信号连接现 :741-743 与 :751（票写 :741-751，一致）、`__onLLMServiceChanged` 现 :963-993（票写 :963-993，一致）、LLMConnectionThread 现 :1256-1275（票写 :1256-1275，一致）——**04 票行号大体仍然有效**，但 import 行号（票写 :32/:35-36/:42-43）与 :470/:511/:577 的转录组共用行号需按本笔记 §13.6 重新核对（11-13 改过此文件吗？没有——但 ticket 11 改的是 task_factory/subtitle_thread，setting_interface 只在 04/15 范围内）。实测 import 区 :1-46，转录组 LineEditSettingCard 用点 :470/:480/:511/:519/:577。
4. **决策票 04 说「llmGroup import 清理 :32/:35-36/:42-43」**——实测 :32 是 `LLMServiceEnum`（from core.entities，可删）、:36 是 `check_llm_connection, get_available_models`（可删/搬家）、:42 是 `EditComboBoxSettingCard`、:43 是 `LineEditSettingCard`（**这两个共用，保留**）、:35 需现场核对（在 qfluentwidgets 块 :6-21 之外，实测 import 区里 :35 附近是本地 import）。实现时以 grep 实测为准，不要照票号盲删。
5. **`transcribe.py:65-68` 的环境变量写入读的是 `whisper_api.*` 不是 `llm.*`**——ticket 14 说「transcribe 的写入是零读取者的死代码」结论**正确**（消费端 `core/asr/whisper_api.py` 从构造参数收 base_url/api_key，不读环境变量），但删除理由要说清是 whisper-api 中继而非 LLM 中继；同理 doctor/validators 的 whisper_api 检查（`_check_transcribe` :135-146、`validate_whisper_api` :151-162）**不动**。
6. **postprocess 命令已占用 `--profile` 旗名**（main.py :234-240，`--profile`/`--speed-profile` → dest `speed_profile`，后处理模板 id）。ticket 14 要挂 `--profile`（模型方案 id）到 postprocess 子命令会与之**同名冲突**（argparse 同 parser 重复旗名直接崩）。必须换名（如 `--llm-profile`）或给模型方案旗子用别的 dest/别名——这是 ticket 文字没预见的硬冲突。subtitle/process/dub 无此冲突。
7. **票 15 的「task_factory 装配段旧逻辑（7 服务 if/elif 分支与 utility 三标量传递）」已被 11-13 提前做掉**：`task_factory.py` 对旧键零引用、`SubtitleConfig` 六标量已删、postprocess 的 model_items 映射已删。票 15 清单第 1 条里「task_factory 装配段旧逻辑」与「utility 三标量传递」**没有对应代码可删**——实际要动的只剩 setting_interface 的 UI 四层 + cfg 22 键 + 测试断言。
8. **票 15 写的「tests/test_postprocess/test_task_factory.py:49-67 保存/恢复旧键的用例」不存在**——该文件 :11-34 是 `_seed_utility_profiles` fixture（save/restore `cfg.main_llm_profile_id`，新键），:90-114 是 utility 绑定用例（新键）。旧键 save/restore 用例已被 11-13 改写。真正要改的测试只有 `test_translation_setting_widget.py:496-510` 一条。
9. **`tests/test_ui/test_translation_setting_widget.py:504` 的 llmContentLoggingCard 断言必须保住**——删 llmGroup 时若把 llmContentLoggingCard（setting_interface.py:201-210，saveGroup）误删会破这条。测试改写时保留该断言即可防回归。
10. **`_deleteProfile`（TranslationSettingWidget.py:1020-1022）只清 main/review 绑定**——新增工具模型卡后若不在 :1020-1022 补 `cfg.utility_llm_profile_id`，删除方案会留下悬空 utility 绑定（resolver 会对 NotFound 报错，但用户要自己找到哪里绑的）。建议顺手补。
11. **`resolve_utility_profile` 的错误文案指向 GUI 卡片**（`UTILITY_PROFILE_CARD = "翻译设置页·工具模型卡"`，utility.py:24）——CLI 装配（票 14）必须 catch `UtilityProfileError` 改写文案，否则 agent 在终端看到「请到翻译设置页·工具模型卡」的指引无法执行。ticket 14 自己也写了「必要时包装」，此处给出落点：subtitle.py :130-157 / postprocess.py 装配段 / dub.py `_build_dubbing_config`。
12. **11-13 的临时桥注释语言不统一**：subtitle.py :222-223 是中文「TODO(ticket-14): 临时桥」，postprocess.py :159-162 是英文「TODO(ticket-14): temporary bridge」，dub.py :146-147 是英文「TODO(ticket-14): the legacy [llm] table is removed wholesale there」。grep 时用 `TODO(ticket-14)` 能全中（3 处），不要只搜中文「临时」。
13. **`validators.py` 的 `validate_translation_llm` 现依赖 `translation_llm_role_allows_empty_api_key`**（:139）——「显式空 key 容忍」语义（keyless 本地服务）随 [llm] 与 inline 表删除而**没有自然替代**：方案库里 api_key 就是空串，validate 侧要么一律拒绝空 key（会破坏 keyless Ollama 用户），要么在方案库语境重定义（如 store 校验时允许空 key + 请求时报 provider 错）。ticket 14 文字没提这个语义收口，实现时要决定（建议：解析时不查 key 非空，fail-fast 只管「方案存在」——与 GUI 侧 `_resolve_llm_profile` 宽松契约 + 任务期真实报错对齐）。
14. **`build_config` 的旧键警告要小心 DEFAULTS 顺序**：`_deep_merge(config, file_config)` 把 file 层整个并进 defaults，检测「残留旧键」必须对 **merge 前的 file_config/env_config/cli_overrides 三层原文**做（`_get_nested(layer, "llm.api_key", missing)` 的写法在 :361-364 已有先例——`_legacy_llm_api_key_explicit` 正是这么探三层的，可照抄这个模式后删掉它）。
15. **CLI 测试硬切面比 ticket 写的大**：`tests/test_cli/test_config.py` 的 `TestTranslationLLMProfiles` **整个类 15 个用例**（:194-515）全部测继承链，硬切后整类删除；`test_translation_modes.py` 三条用例（:69/:125/:226）的 config 构造都含 `"llm": {...}` 或 `translate.llm.*` 表，要改成「monkeypatch 一个 tmp LLMModelProfileStore + 三键」形态（monkeypatch 点：`videocaptioner.core.llm.profiles.DEFAULT_LLM_PROFILES_PATH`，先例在 `tests/test_ui/test_postprocess_interface.py:25-33` 与 `tests/test_postprocess/test_task_factory.py:28-30`——注意 CLI 侧 store 是在命令函数内构造的，路径 monkeypatch 对 CLI 同样生效，因为 `LLMModelProfileStore()` 默认读 `DEFAULT_LLM_PROFILES_PATH` 模块级常量）。
16. **`_check_subtitle`（doctor.py:200-203）的 `needs_llm` 判定含 `subtitle.compress_fast_subtitles`**（validators.py:239-241 有，doctor 侧 :189-191 没有）——两处判定不一致是既有差异，重写 doctor 检查时对齐 validators 的判定即可。
17. **决策票 05 说「dub.py:133-135 三元组装配」**——现状三元组在 `_legacy_rewrite_profile` :155-157，`_build_dubbing_config` :135 已是 `llm_profile=_legacy_rewrite_profile(config)`。行号漂移，按本笔记 §2.3 为准。
