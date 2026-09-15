# Tickets 11–13 代码地图笔记

行号基准：主 checkout master `300f54a`。所有路径相对仓库根 `C:\Users\lijso\Software\VideoCaptioner\`。
（探索 worktree 曾落后 master 若干提交；`videocaptioner/core/llm/*`、`core/utils/cache.py`、`tests/conftest.py`、`tests/test_llm/*`、`ui/components/TranslationSettingWidget.py` 等文件的行号已用 `git show 300f54a:` 核对，以本文为准。）

## 0. 三张票各自要动的文件清单

**票 11（断句 + 字幕优化）**
- `videocaptioner/core/split/split_by_llm.py`（消费点本体）
- `videocaptioner/core/split/split.py`（SubtitleSplitter，构造入口）
- `videocaptioner/core/optimize/optimize.py`（消费点本体）
- `videocaptioner/core/entities.py`（SubtitleConfig 六标量删除 + 新增 profile 字段）
- `videocaptioner/ui/task_factory.py`（装配段 :378–449）
- `videocaptioner/ui/common/config.py`（新增 utility_llm_profile_id 绑定键）
- `videocaptioner/ui/thread/subtitle_thread.py`（need_legacy_llm :451、_setup_llm_config :133、两处环境变量写入 :149–150 与 :564–565）
- `videocaptioner/cli/commands/subtitle.py`（临时桥，:219–225、:298、:321、:452）
- `tests/conftest.py`（mock_llm_client 旧缝，@300f54a :198–246）、`tests/test_subtitle/test_subtitle_thread.py`、`tests/test_split/*`、`tests/test_optimize/*`、`tests/test_thread/*`

**票 12（字幕压缩 + 语义修复 + JSON Schema 升格）**
- `videocaptioner/core/postprocess/compress.py`（F5 压缩消费点）
- `videocaptioner/core/speed/semantic.py`（语义修复消费点 + 多形态解析）
- `videocaptioner/core/postprocess/config.py`（PostprocessConfig.llm_model :78 退役）
- `videocaptioner/core/postprocess/__init__.py`（run_post_stage :105、semantic_model 传递 :173）
- `videocaptioner/core/speed/pipeline.py`（optimize_speed :459、_apply_semantic_repairs :335）
- `videocaptioner/core/llm/adapters.py`（仅当分档行为需扩展时；现状已具备三档）
- `videocaptioner/cli/commands/postprocess.py`（临时桥 :159–160、:172–174）
- `videocaptioner/ui/task_factory.py`（create_postprocess_task :508，模型名填充 :538–539）
- `tests/test_llm/test_adapters.py`（请求体直测先例）、`tests/test_speed/test_semantic.py`、`tests/test_optimize/test_postprocess_compress.py`

**票 13（配音改写）**
- `videocaptioner/core/dubbing/rewriter.py`（消费点本体）
- `videocaptioner/core/dubbing/models.py`（DubbingConfig 三元组 :75–77）
- `videocaptioner/core/dubbing/pipeline.py`（调用点 :76）
- `videocaptioner/cli/commands/dub.py`（装配 :106–135，临时桥）
- `tests/test_dubbing/test_pipeline.py`（DubbingConfig 构造 :39、:86）
- **注意：GUI 侧无 dub 装配代码**（见「坑 1」）

---

## 1. 五个工具消费点现状

### 1.1 断句（split）
- 本体：`videocaptioner/core/split/split_by_llm.py`
  - `split_by_llm(text, model="gpt-4o-mini", max_word_count_cjk=18, max_word_count_english=12)` 模块级函数，:19–24。**model 是裸字符串参数**。
  - 请求点：`_split_with_agent_loop` 内 `call_llm(messages=messages, model=model, timeout=LLM_SPLIT_REQUEST_TIMEOUT_SECONDS)` :85–89。走旧 `client.py` 的 `call_llm`（import :6 `from ..llm import call_llm`）。
  - 超时常量：`LLM_SPLIT_REQUEST_TIMEOUT_SECONDS = 30.0` :16。
  - 自带外层重试：`LLM_SPLIT_MAX_ATTEMPTS = 2` :14，`_split_with_agent_loop` 内 agent loop `MAX_STEPS = 2` :13。
- 构造入口：`videocaptioner/core/split/split.py` `SubtitleSplitter.__init__(thread_num, model, max_word_count_cjk, max_word_count_english, use_llm=True, progress_callback=None)` :123–142；`self.model = model` :142；调用 `split_by_llm(text=txt, model=self.model, ...)` :357–361。
- 连接来源：无自有连接——`call_llm` → `get_llm_client()` 单例读环境变量（见 1.6）。

### 1.2 字幕优化（optimize）
- 本体：`videocaptioner/core/optimize/optimize.py`，类 `SubtitleOptimizer` :28。
- 构造签名：`__init__(thread_num, batch_num, model, custom_prompt, update_callback=None, extra_rules="")` :37–45。**model 裸字符串**。
- 请求点：`agent_loop` 内 `call_llm(messages=messages, model=self.model)` :245–248，**无 timeout 参数**（落 OpenAI SDK 默认 600 秒）。import :17。
- 解析：`json_repair.loads` + dict 类型检查 :248–256。
- 线程池并发：`ThreadPoolExecutor(max_workers=self.thread_num)` :78；统计 `failed_batches`/`maxed_batches` :71–73。

### 1.3 字幕压缩（后处理 F5）
- 本体：`videocaptioner/core/postprocess/compress.py`。
- `_agent_loop(candidates, cfg: PostprocessConfig)` :113；请求点 `call_llm(messages=messages, model=cfg.llm_model or "")` :135，**无 timeout**。import :18。
- `compress_fast_subtitles(asr_data, cfg, report, llm_ctx=None)` :156–161；`llm_ctx` 形参声明于 :160 但**函数体内零使用**（死参数，见「坑 4」）。
- 未配置即跳过：`if not cfg.llm_model: 跳过` :163–165。
- 解析：`json_repair.loads` + dict 检查 :136–140。
- 调用链：`videocaptioner/core/postprocess/__init__.py` `run_post_stage(asr_data, cfg, report=None, llm_ctx=None, layout=..., timing_windows=())` :105–112，在 :127–131 调 `compress_fast_subtitles`；模块 docstring :4 明言「F5 压缩重译除外，走现有 call_llm」。

### 1.4 语义修复（speed semantic repair）
- 本体：`videocaptioner/core/speed/semantic.py`。
- 默认 rewriter/reviewer 工厂：`_default_rewriter(model: str)` :279–297、`_default_reviewer(model: str)` :300–331。**model 裸字符串**，从 `PostprocessConfig.llm_model` 一路传入。
- 请求点：rewriter `call_llm(messages=[...], model=model, response_format={"type": "json_object"}, timeout=SEMANTIC_LLM_TIMEOUT_SECONDS)` :283–294；reviewer 同形 :320–328。
- 超时常量：`SEMANTIC_LLM_TIMEOUT_SECONDS = 60.0` :35。
- 入口：`repair_semantic_windows(cues, *, model, reviewer_model=None, rewriter=None, reviewer=None, cache=None, window_size=5, max_feedback_retries=2, minimum_literal_coverage=0.55)` :473–484。**rewriter/reviewer 可注入（callable 或带 rewrite/review 方法的对象）**——现有测试缝。
- 上游：`videocaptioner/core/speed/pipeline.py` `_apply_semantic_repairs(..., model: str, reviewer_model: str | None, ..., rewriter, reviewer)` :335–343，调 `repair_semantic_windows` :400–408；`optimize_speed(..., semantic_model: str | None, semantic_reviewer_model, semantic_window_size, semantic_uncertain_review, semantic_cache, semantic_rewriter, semantic_reviewer)` :459–478，门控 `if semantic_repair and semantic_model:` :530。
- 连接来源：同 1.1（环境变量经 call_llm）。

### 1.5 配音改写（dub rewrite）
- 本体：`videocaptioner/core/dubbing/rewriter.py`，`rewrite_segments_if_needed(segments: Iterable[DubbingSegment], config: DubbingConfig) -> None` :29。**模块级函数，非类**——票 13 说的「构造签名统一为 profile + gateway=None」目前没有类载体，需要引入或改函数签名。
- 连接：**直接构造裸 OpenAI 客户端** `client = OpenAI(api_key=config.llm_api_key, base_url=config.llm_api_base)` :41（`from openai import OpenAI` :6），不经过 client.py，也不读环境变量。
- 请求点：`client.chat.completions.create(model=config.llm_model, messages=messages, response_format={"type": "json_object"})` :69–73，**无 timeout**。
- 三标量缺失即抛错：`if not (config.llm_api_key and config.llm_api_base and config.llm_model): raise ValueError("Duration rewrite requires llm.api_key, llm.api_base, and llm.model")` :33–34。
- 解析：`json.loads(content)` + `result.get("items", [])` 手工提取 `{index, text}` :74–80。
- 调用点：`videocaptioner/core/dubbing/pipeline.py` :76 `rewrite_segments_if_needed(segments, self.config)`（在 `cb(8, "rewriting long lines")` 后）。

### 1.6 旧客户端（四个 call_llm 消费点的共同底座）
- `videocaptioner/core/llm/client.py`：
  - `get_llm_client()` :104–127：单例，读 `os.getenv("OPENAI_BASE_URL")` :111 与 `os.getenv("OPENAI_API_KEY")` :113，缺失抛 `ValueError("OPENAI_BASE_URL and OPENAI_API_KEY environment variables must be set")` :116–118；构造 `OpenAI(base_url, api_key, http_client=create_logging_http_client())` :120–125。
  - `_call_llm_api` :160–188：tenacity `@retry(stop=stop_after_attempt(10), wait=wait_random_exponential(multiplier=1, min=5, max=60), retry=retry_if_exception_type(openai.RateLimitError))` :154–159。
  - `call_llm(messages, model, temperature=1, **kwargs)` :191 起，装饰器 `@memoize(get_llm_cache(), expire=3600, typed=True)` :190——旧 memoize 磁盘缓存。
  - `normalize_base_url` :81。

---

## 2. SubtitleConfig（core/entities.py）

- 类定义 :723 `class SubtitleConfig`。
- 六个连接标量确切字段与行号：
  - `base_url: Optional[str] = None` :727
  - `api_key: Optional[str] = None` :728
  - `llm_model: Optional[str] = None` :729
  - `utility_llm_base_url: Optional[str] = None` :732
  - `utility_llm_api_key: Optional[str] = None` :733
  - `utility_llm_model: Optional[str] = None` :734
  - （:730–731 为保留注释，说明 utility 独立依赖的缘由）
- 既有 profile 字段（保留不动）：`main_llm_profile: Optional[LLMModelProfile]` :747、`review_llm_profile` :748；TYPE_CHECKING import `LLMModelProfile` :11。
- 六标量全仓读写消费点（grep 全仓结果）：
  - **写**：`videocaptioner/ui/task_factory.py` :443–448（`base_url=base_url, api_key=api_key, llm_model=llm_model, utility_llm_base_url=..., utility_llm_api_key=..., utility_llm_model=...`，位于 `config = SubtitleConfig(` :441 内）。
  - **读（utility 三元组回退）**：`videocaptioner/ui/thread/subtitle_thread.py` :138–140（`config.utility_llm_base_url or config.base_url` 等）、:331（splitter model）、:365（optimize model）。
  - **读（主三元组）**：`subtitle_thread.py` :564–565（RetranslateThread 环境变量写入）、`print_config` :840–842（打印 base_url/api_key/llm_model）、:828（optimize 段打印 `self.llm_model`）。
  - **测试写**：`tests/test_subtitle/test_subtitle_thread.py` :42–43、:192–193、:238–239、:260–261、:284–285、:354–355、:381–382、:404–405（`config.base_url = os.getenv(...)` / `config.api_key = os.getenv(...)` / `config.llm_model = get_test_model()` :191、:237、:259、:283、:353、:380、:403）。
  - `is_translation_mode_available` :786–800 与 `missing_translation_roles` :802–809 只读 profile 字段，不读六标量。
  - CLI 侧**不构造 SubtitleConfig**（见「坑 2」）。

---

## 3. DubbingConfig（core/dubbing/models.py）

- 类定义 :53 `class DubbingConfig`。
- 三字段确切名与行号：`llm_api_key: str = ""` :75、`llm_api_base: str = ""` :76、`llm_model: str = ""` :77。
- 相关字段：`rewrite_too_long: bool = False` :73、`rewrite_threshold: float = 1.15` :74、TTS 侧 `provider/api_key/base_url/model` :54–57、`timeout: int = 90` :65（**这是 TTS 超时，非 LLM 超时**）。
- 三字段全部消费点：
  - `videocaptioner/core/dubbing/rewriter.py` :33（缺失校验）、:41（OpenAI 构造）、:70（model 传请求）。
  - `videocaptioner/cli/commands/dub.py` :133–135（`llm_api_key=get(config, "llm.api_key", "")` 等，唯一装配点）。
  - `videocaptioner/cli/commands/config_cmd.py` :177–179（config 文档键映射表 `llm_api_key→llm.api_key` 等，仅文档用途）。
  - 测试：`tests/test_dubbing/test_pipeline.py` :39、:86 构造 DubbingConfig 但**未设置** rewrite/llm 字段（rewrite 路径无测试覆盖）。
- `rewrite_segments_if_needed` 现状见 1.5：裸 OpenAI :41，`response_format={"type": "json_object"}` :72。

---

## 4. 翻译路径既有先例（要照抄的签名模式）

- `videocaptioner/core/translate/llm_translator.py`：
  - `LLMTranslator.__init__(thread_num, batch_num, target_language, model, custom_prompt, is_reflect, update_callback, profile: Optional[LLMModelProfile] = None, gateway: Optional[LLMGateway] = None, source_language: str = "auto")` :28–40。
  - **惰性构造精确写法**：`self.profile = profile` :55；`self.gateway = gateway or (LLMGateway() if profile is not None else None)` :56。
  - `_call_text(messages)` :220–236：profile 分支 `self.gateway.complete(self.profile, LLMRequest(messages=tuple(LLMMessage(str(m["role"]), str(m["content"])) for m in messages), max_output_tokens=self.profile.max_output_tokens, metadata={"stage": "single_llm_translation", "role": "main"}))` :222–233，返回 `result.text.strip()` :234；**旧回退分支** `response = call_llm(messages=messages, model=self.model)` :235（import :15）。
  - **stage/role 标签的正确带法**：不是消费点直接调 `begin_gateway_request`——标签挂在 `LLMRequest.metadata={"stage": ..., "role": ...}`，由 gateway 内部调 `begin_gateway_request(profile, request, attempt=attempt)`（gateway.py @300f54a :120）统一落日志。
  - 另一先例：`videocaptioner/core/translate/enhanced/orchestrator.py` :457 `self.gateway = gateway or LLMGateway()`；:728 `metadata={"stage": stage, "role": role.role}`。
- 工厂透传：`videocaptioner/core/translate/factory.py` `TranslatorFactory.create_translator(..., profile: Optional[LLMModelProfile] = None, gateway: Optional[LLMGateway] = None, ...)` :21–38（profile :30、gateway :31 传入 LLMTranslator :50）。
- GUI 装配入口：`videocaptioner/ui/thread/subtitle_thread.py` `create_translator_from_config` :63。
- 探测函数的同款先例：`videocaptioner/core/llm/check_llm.py`（@300f54a）`probe_model_profile_capabilities(profile, *, gateway=None)` :173（`owns_gateway = gateway is None; runtime = gateway or LLMGateway()` :180–181，finally 关闭 :201–203）、`check_model_profile_connection(profile, *, gateway=None)` :206（同款 :218–219），二者请求均带 `metadata={"stage": ..., "role": "utility"}` :139/:230 且 `max_attempts=1, use_cache=False` :141–142/:232–233。

---

## 5. resolve_utility_profile 与 gateway/请求模型（@300f54a）

- `videocaptioner/core/llm/utility.py`：
  - `UTILITY_PROFILE_CARD = "翻译设置页·工具模型卡"` :21。
  - `class UtilityProfileError(ValueError)` :24。
  - `_stripped(profile)` :28–36：`dataclasses.replace(profile, openai_endpoint=OpenAIEndpoint.CHAT_COMPLETIONS, request_options={}, max_output_tokens=None)`——剥离的三字段即这三个。
  - `_lookup(store, profile_id, *, description)` :39–54：strip 后空则 None :45–47；`store.get` 抛 `LLMProfileNotFoundError` 时转 `UtilityProfileError(f"{description}模型配置方案「{identifier}」已不存在，请到{UTILITY_PROFILE_CARD}重新绑定或恢复该方案")` :48–54。
  - `resolve_utility_profile(store: LLMModelProfileStore, main_profile_id: Optional[str], utility_profile_id: Optional[str] = None) -> LLMModelProfile` :57–83。解析顺序：绑定优先 :72–74 → 主翻译派生 :76–78 → 双空抛 `UtilityProfileError("未找到可用的模型配置方案：主翻译方案与工具模型绑定均为空，请到{UTILITY_PROFILE_CARD}选择或创建模型配置方案")` :80–83。
  - `validate_utility_profile(profile)` :86–102：`validate_profile_request_options(profile)` :95 + 三字段断言（endpoint 必须 CHAT_COMPLETIONS :96–97、request_options 必须空 :98–99、max_output_tokens 必须 None :100–101）+ `validate_structured_output_compatibility(profile)` :102。纯本地、不发请求。
  - 导出（`core/llm/__init__.py` @300f54a :25–29、:50–53）：`UtilityProfileError`、`resolve_utility_profile`、`validate_utility_profile` 已进包级 `__all__`。
- `LLMGateway`（gateway.py @300f54a）：
  - 模块级共享缓存 `_shared_response_cache = GatewayResponseCache()` :39。
  - `__init__(adapter_factory=None, sleep=time.sleep, random_source=random.random, response_cache: Optional[GatewayResponseCache] = None)` :43–49；`response_cache is None` 时用共享单例 :53–55。
  - `complete(profile, request, *, max_attempts: int = 4, cancelled=None, use_cache: bool = True)` :95–103；缓存命中 `log_gateway_cache_hit(profile, request, cached)` :109；INVALID_RESPONSE 限重试 2 次 :140–141；输出上限耗尽不重试 :136–139。
- `LLMRequest`（models.py @300f54a :275–310）：`messages` :277、`temperature`（弃用兼容，adapter 不序列化）:279、`max_output_tokens` :280、**`response_schema: Optional[Mapping[str, Any]] = None` :281**、`cacheable_system_prefix: bool = True` :282、`metadata: Mapping[str, str]` :283、`request_options_override` :284、**`timeout: Optional[float] = None` :287**（None = 用 adapter 构造默认 120 秒；校验必须为正有限数 :292–298；response_schema 在 :305–310 冻结为 MappingProxyType）。
- `log_gateway_cache_hit(profile, request, result) -> None`（request_logger.py @300f54a :190–202）：`_base_entry` 骨架 + `status="cache_hit"` :198 + `duration_ms=0` :199 + include_content 时附 `response.text` :200–201。`_base_entry` :122–134 含 time/request_id/stage（metadata）/role（metadata）/profile{id, model}。`begin_gateway_request(profile, request, *, attempt)` :137–162。
- Profile 仓库：`videocaptioner/core/llm/profiles.py` `LLMModelProfileStore(path=None)` :78–79（默认 `DEFAULT_LLM_PROFILES_PATH`）、`list()` :133、`get(profile_id)` :141–145（不存在抛 `LLMProfileNotFoundError` :38）、`save` :148、`delete` :178。
- 缓存：`videocaptioner/core/utils/cache.py`（@300f54a）`is_cache_enabled()` :33、模块级 `_gateway_cache = Cache(str(CACHE_PATH / "llm_gateway"))` :40、`get_gateway_cache()` :52–54。

---

## 6. adapter 的 dialect 分档现状（@300f54a，`videocaptioner/core/llm/adapters.py`）

- `DEFAULT_TIMEOUT_SECONDS = 120.0` :42（注释 :39–41 说明 OpenAI SDK 自身默认为 `Timeout(connect=5.0, read=600, ...)`）。
- 档位选择 `_structured_chat_strategy()` :449–461：
  - `_NATIVE_SCHEMA_DIALECTS` :55–62 = {OPENAI, QWEN, GEMINI} → `"json_schema"` :457–458
  - `_FORCED_TOOL_SCHEMA_DIALECTS` :63–72 = {DEEPSEEK, KIMI, GLM, ANTHROPIC} → `"tool"` :459–460
  - 其余（GENERIC）→ `"json_object"` :461
- 三档请求体（`_complete_chat_once` :480–582）：
  - json_schema：`application_body["response_format"] = {"type": "json_schema", "json_schema": {"name": _STRUCTURED_RESPONSE_NAME, "strict": True, "schema": dict(request.response_schema or {})}}` :496–504
  - tool：`tools=[{"type": "function", "function": {"name": _STRUCTURED_RESPONSE_NAME, "description": "Return the requested structured response.", "parameters": dict(request.response_schema or {})}}]` + `tool_choice={"type": "function", "function": {"name": _STRUCTURED_RESPONSE_NAME}}` :505–519
  - json_object：`application_body["response_format"] = {"type": "json_object"}` :520–521
- 降级保护：`_complete_chat` :463–478——tool 档被服务商拒绝（`_rejects_forced_tool_request` :288–301，400/404/422 且非 context-limit）时 warning 一次并重发 json_object 档 :469–478。
- timeout 传递：`_transport_options(request)` :442–447——`request.timeout` 非 None 时 `{"timeout": request.timeout}` 进 SDK kwargs（不进 HTTP body）；`_effective_timeout` :318–323。
- schema 名/工具名统一常量 `_STRUCTURED_RESPONSE_NAME = "structured_response"` :47。
- **现有 response_format=json_object 的调用者（升格前的「现状请求形状」）**：
  - 语义修复：`semantic.py` :292 与 :326（`call_llm(..., response_format={"type": "json_object"}, timeout=60.0)`）——response_format 作为 call_llm 的 **kwargs 透传给 `client.chat.completions.create(**kwargs)`（client.py :175–180）。
  - 配音改写：`rewriter.py` :72（裸 OpenAI 客户端直传 `response_format={"type": "json_object"}`）。
  - generic 档等价性：升级后 GENERIC dialect 走 :520–521，与上述两处的请求体**逐字节一致**（`{"type": "json_object"}`），零回归成立。

---

## 7. GUI 装配

- **subtitle 装配段**（`videocaptioner/ui/task_factory.py`，`create_subtitle_task` :290）：
  - profile 绑定解析 :353–373：`main_profile_id = cfg.main_llm_profile_id.value` :354、`review_profile_id = cfg.review_llm_profile_id.value` :355、`needs_llm_profiles` 判定 :356–360、惰性 `profile_store = LLMModelProfileStore()` :361–365、`TaskFactory._resolve_llm_profile(store, id)` :366–373（helper 定义 :53–60）。
  - 六标量来源（要删的段）：`current_service = cfg.llm_service.value` :378；七组服务分支 `cfg.openai_api_base/openai_api_key/openai_model` 等 :379–410；**utility 回退三元组** `utility_base_url = base_url; utility_api_key = api_key; utility_llm_model = llm_model` :414–416（注释 :412–413 说明保留旧标量待迁移）；main profile 覆盖主三元组 :417–420。
  - `config = SubtitleConfig(...)` :441：六标量 :443–448、`main_llm_profile=main_profile` :459、`review_llm_profile=review_profile` :460。
- **postprocess 装配段**（同文件 `create_postprocess_task` :508）：`config_snapshot or profile_store.resolve_config(resolved_profile_id)` :521–522；模型名映射表 `model_items = {LLMServiceEnum.X: cfg.xxx_model, ...}` :524–532；**`if config.llm_model is None: config = replace(config, llm_model=model_item.value if model_item else None)`** :538–539——这是 PostprocessConfig.llm_model 的 GUI 填充点（票 12「模型名字段退役」要动的位置）。
- **dub 装配段**：不存在（见「坑 1」）。
- **绑定键先例**（`videocaptioner/ui/common/config.py`）：`main_llm_profile_id = ConfigItem("Translate", "MainLLMProfileId", "")` :178、`review_llm_profile_id = ConfigItem("Translate", "ReviewLLMProfileId", "")` :179——`utility_llm_profile_id` 照抄这两行。旧服务槽键在同文件：`llm_service` :112、`openai_model` :120、`openai_api_key` :121、`openai_api_base` :122、silicon_cloud/deepseek 等 :124–132 及后续。`need_optimize` :349、`need_split` :351。
- **subtitle_thread.py 关键位置**：
  - `_setup_llm_config` :133–152：utility 回退 :138–140、`check_llm_connection(base_url, api_key, model)` 三标量旧探测 :142–146（import :16）、**环境变量写入第一处** `os.environ["OPENAI_BASE_URL"] = base_url; os.environ["OPENAI_API_KEY"] = api_key` :149–150。
  - `run()` 任务上下文 `set_task_context(..., stage="subtitle")` :284–287；**`need_legacy_llm` 判定调用点** `if self.need_legacy_llm(subtitle_config, asr_data):` :316（fail-fast 预检放这里）。
  - 断句装配 `SubtitleSplitter(thread_num=..., model=subtitle_config.utility_llm_model or subtitle_config.llm_model, ...)` :329–339（model 在 :331）。
  - 优化装配 `utility_model = subtitle_config.utility_llm_model or subtitle_config.llm_model` :365，`SubtitleOptimizer(..., model=utility_model, ...)` :368–374。
  - `need_legacy_llm` 定义 :451–461：`need_optimize or (need_split and asr_data.is_word_timestamp()) or (need_translate and single_llm and main_llm_profile is None)`。兼容别名 `need_llm` :464–465。
  - **环境变量写入第二处**：`RetranslateThread`（class :521）`run()` 内 `os.environ["OPENAI_BASE_URL"] = config.base_url; os.environ["OPENAI_API_KEY"] = config.api_key` :564–565（分支 :558–565：main_llm_profile 非 None 时 pass :560–561，标量缺失抛错 :562–563）。
  - `_validate_enhanced_profile_compatibility` :158–174（增强翻译的本地预检先例，票 11 fail-fast 照此强度）。
- **PostprocessConfig 模型字段现状**：`videocaptioner/core/postprocess/config.py` `llm_model: Optional[str] = None` :78，docstring「压缩重译使用的 LLM 模型名（由调用方从各自配置注入）」。消费见 1.3/1.4；GUI 填充 task_factory.py :538–539；CLI 填充 postprocess.py :159–160。
- **工具模型卡落点先例**（TranslationSettingWidget.py @300f54a）：`_ProfileDialog` :184；dialect 下拉已有分档提示 tooltip :222–228（新写明「openai/qwen/gemini 用 json_schema，deepseek/kimi/glm/anthropic 用强制函数调用，generic 仅用 JSON 模式」）；`TranslationSettingWidget` :734；单模型页主卡 `self.singleMainProfileCard = self._profileCard(cfg.main_llm_profile_id, ...)` :809；增强页 :848/:854；`_profileCard(config_item, title, content, parent)` helper :915–925（接 `createRequested/editRequested/deleteRequested` 信号，handler `_createProfile` :969、`_editProfile` :982）。

---

## 8. CLI 装配

- **subtitle**（`videocaptioner/cli/commands/subtitle.py`）：**不构造 SubtitleConfig**。
  - `needs_llm = need_optimize or need_split or llm_translation` :127；注释 :124–125 明言「Legacy optimize/split calls intentionally keep using [llm]」。
  - `need_optimize or need_split` 时走 `validate_llm(config)` 门 :130–134。
  - 临时桥位置：读三标量 `llm_api_key = get(config, "llm.api_key", "")` :219、`llm_api_base` :220、`llm_model` :221；环境变量写入 `os.environ["OPENAI_API_KEY"]` :222–223、`os.environ["OPENAI_BASE_URL"]` :224–225。
  - `SubtitleSplitter(thread_num=..., model=llm_model, ...)` :298–305（model :300）；`SubtitleOptimizer(thread_num=..., batch_num=..., model=llm_model, ...)` :321–329（model :324）；LLM 翻译 `TranslatorFactory.create_translator(..., model=llm_model, ..., profile=profile)` :452–463（profile 变量 :440–442 已是 profile 化路径，model=llm_model 在 :458、profile=profile :462）。
- **postprocess**（`videocaptioner/cli/commands/postprocess.py`）：`llm_model = get(config, "llm.model", "") or None` :159；`resolved = replace(resolved, llm_model=llm_model, **overrides)` :160；环境变量写入 `os.environ["OPENAI_API_KEY"] = api_key` :172–173、`os.environ["OPENAI_BASE_URL"] = api_base` :174（读取 :170–171）。
- **dub**（`videocaptioner/cli/commands/dub.py`）：`_build_dubbing_config(config, speaker_profiles)` :106；`DubbingConfig(...)` :115；三标量 `llm_api_key=get(config, "llm.api_key", "")` :133、`llm_api_base` :134、`llm_model` :135。`rewrite_too_long` :131、`rewrite_threshold` :132。
- **process**（`videocaptioner/cli/commands/process.py`）：编排器——转 `subtitle.run` :166、postprocess 段 :171 起、`validate_dubbing(config, ..., rewrite=bool(get(config, "dubbing.rewrite_too_long", False)))` :64–67。自身不碰 LLM 标量。
- **配置底座**（`videocaptioner/cli/config.py`）：`ENV_MAP` 中 `"OPENAI_API_KEY": "llm.api_key"` :43、`"OPENAI_BASE_URL": "llm.api_base"` :44（`OPENAI_MODEL` :45，`VIDEOCAPTIONER_LLM_*` :46–48）；`DEFAULTS` 的 `"llm"` 节 :116；旧继承链 `translate.get("llm")` :303、`config.get("llm")` :616；`_legacy_llm_api_key_explicit` :33、:361、:614。
- 旗子先例：`cli/main.py` subtitle 组 `--api-key` :415、`--api-base` :417、`--model` :419（common 组 :50–53）。
- `cli/validators.py` `validate_llm(config)` :83，引用环境变量名 :92（`OPENAI_API_KEY`）、:100（`OPENAI_MODEL`）。

---

## 9. 测试缝先例

- **conftest 的 mock_llm_client**（`tests/conftest.py` @300f54a :198–246，即票要替换的旧缝）：
  - fixture 定义 :198；`monkeypatch.setenv("OPENAI_BASE_URL", "https://mock.local/v1")` :226、`OPENAI_API_KEY` :227、`OPENAI_MODEL` :228；
  - 五处符号打补丁：`videocaptioner.core.llm.call_llm` :230、`...llm.client.call_llm` :232、`...core.split.split_by_llm.call_llm` :236、`...core.translate.llm_translator.call_llm` :240、`...core.optimize.optimize.call_llm` :244；
  - 外加 `monkeypatch.setattr("videocaptioner.ui.thread.subtitle_thread.check_llm_connection", lambda *a, **k: (True, "ok"))` :244–246 区域。
  - 同文件：`cache.disable_cache()` :27；`pytest_unconfigure` 关闭缓存句柄（@300f54a 已含 `cache.get_gateway_cache`）:30–41。
- **fake gateway 注入先例**：
  - `tests/test_translate/test_llm_translator_unit.py` `_CapturingGateway` :50–56——`complete(self, profile, request, *, cancelled=None)` 记录 `(profile, request, cancelled)` 并返回 `LLMResult(text="  translated text  ")`；断言模式 `test_profile_single_llm_forwards_configured_max_output_tokens` :59–97：`assert used_profile is profile`、`assert request.max_output_tokens == 777`、`assert request.metadata == {"stage": "single_llm_translation", "role": "main"}`。这是票 11/12/13 消费点构造缝测试的直接模板。
  - `tests/test_translate/test_enhanced_orchestrator.py` `ScriptedGateway` :126–157——按 `request.metadata["stage"]` 派发脚本化响应，暴露 `.stages`/`.roles` 属性（:34–40 区域）。
  - `tests/test_translate/test_enhanced_context_fallback.py` `_FallbackGateway` :35。
- **请求形状断言先例**（`tests/test_llm/test_adapters.py` @300f54a）：`_profile(...)` helper :25、`_request()` :49、`_chat_adapter(dialect, completions)` :216；三档断言：tool 档 `"response_format" not in completions.kwargs` + `tool_choice` 形状 :236–258；generic 档 `response_format == {"type": "json_object"}` 且无 tool_choice :288–295；无 schema 时无任何结构化控制 :299–308；tool 被拒降级 json_object :336–344；timeout 断言（默认 120 秒/自定义保留/请求级覆盖）:84–115、:639–675。
- **gateway 行为先例**（`tests/test_llm/test_gateway.py`）：`_profile()` :17、`REQUEST = LLMRequest(messages=(LLMMessage("user", "hello"),))` :30、adapter 注入类 `_AlwaysFailAdapter` :33、`_SuccessAdapter` :149；重试语义测试 :52–148。
- **解析器/缓存契约先例**（@300f54a）：`tests/test_llm/test_utility.py`（解析优先级 :51–146、预检 :169–223）；`tests/test_llm/test_response_cache.py`（api_key 隔离 :115、开关关不读不写 :133、use_cache=False 双向旁路 :154、cache_hit 日志形状 :179、key_version bump :277）。
- **旧环境变量假值缝**（票 11 要改写的）：`tests/test_subtitle/test_subtitle_thread.py` :191–193 等七处 `config.llm_model = get_test_model()` + `config.base_url/api_key = os.getenv(...)`；`tests/test_split/test_split_by_llm.py` :4–5、:61、:90；`tests/test_optimize/test_optimize.py` :4–5、:61、:90（集成标记，读 OPENAI_* 环境变量）。
- **dubbing 测试现状**：`tests/test_dubbing/test_pipeline.py` monkeypatch `videocaptioner.core.dubbing.pipeline.create_speech_synthesizer` :36–40、:51–55；DubbingConfig 构造 :39、:86——**无任何 rewrite_segments_if_needed 的 LLM 路径测试**，票 13 的构造缝测试是全新覆盖。

---

## 10. 多形态响应解析现状（语义修复）

- **多形态读取链**（`videocaptioner/core/speed/semantic.py`）：
  - `_response_content(response)` :208–219：接受三种形态——纯 str :209–210、Mapping（json.dumps 回填）:211–212、SDK 响应对象 `response.choices[0].message.content` :213–216；空/非 str 抛 `ValueError("LLM response does not contain message content" / "LLM response content is empty")` :216–218。**这就是「手工校验降档读 content」的那段**——注入的 rewriter 可能返回 SemanticRewriteResponse、dict 或 SDK 对象，函数逐层兜。
  - `_load_json_object(response)` :222–229：`json.loads(_response_content(...))` + Mapping 检查。
  - `_parse_rewrite_response(response)` :232–251：先 short-circuit `isinstance(response, SemanticRewriteResponse)` :233–234，再手工校验 `segments` 是数组 :236–238、每项是对象且 `cue_id`/`text` 均为 str :240–247、`window_id` 是 str :248–250。
  - `_parse_review_response(response)` :254–276：同样 short-circuit :255–256，再校验 `decision` 可转 ReviewDecision :258–261、`changed_facts` 是 str 数组 :262–266、`window_id`/`explanation` 是 str :267–270。
  - **升格 schema 后可简化的部分**：`isinstance(response, SemanticRewriteResponse)` 之外的分支（`_load_json_object` 的 Mapping 容忍 + `_parse_*` 的逐字段类型断言）在 schema 档由 provider 侧保证，可收敛为纯 `json.loads` 文本解析；但**注入缝（rewriter/reviewer callable 返回原生对象）是公开契约**（Protocol `RewriterObject`/`ReviewerObject` :102–107，TypeAlias :110–111），简化时需保留对注入对象的 short-circuit。
  - 数据类：`SemanticRewriteResponse` :93–99（`as_mapping()` :98–99）；`SemanticReviewResponse` 在 `videocaptioner/core/speed/validation.py` :91（`SemanticReviewRequest` :76、`ReviewDecision` :39）。
- 另外两个「json 解析 + 手工 dict 校验」点（对照）：
  - `optimize.py` :248–256（`json_repair.loads` + isinstance dict + 键集校验 `_validate_optimization_result` :180 起）。
  - `compress.py` :136–140（`json_repair.loads` + dict 检查）+ `_validate` :81–110。
  - `rewriter.py` :74–80（`json.loads` + `result.get("items", [])`）。

---

## 附：ticket 描述与代码现状的出入（坑）

1. **票 13 的「GUI dub 装配」不存在**。`grep -rn "dub" videocaptioner/ui` 零命中——GUI 没有任何配音路径；DubbingConfig 全仓唯一装配点是 CLI（`cli/commands/dub.py` :115）。票 13 清单里「GUI dub 装配改调 resolve_utility_profile 解析方案填 DubbingConfig」没有对应的现有代码位置可改；真正要动的只有 CLI 装配 + DubbingConfig 实体 + rewriter 本体。
2. **CLI subtitle 不构造 SubtitleConfig**。`cli/commands/subtitle.py` 直接以 `[llm]` 标量构造 `SubtitleSplitter`（:298）与 `SubtitleOptimizer`（:321）并写环境变量（:222–225）。因此票 11 的「SubtitleConfig 六标量删除」波及面是 GUI 装配（task_factory.py :443–448）+ subtitle_thread 的读取（:138–140、:331、:365、:564–565）+ 测试直写（test_subtitle_thread.py 七处）；而 CLI 临时桥的位置是 splitter/optimizer/translator 的构造实参处，不是 SubtitleConfig 字段处。
3. **PostprocessConfig.llm_model 一字段喂两个消费点**。它同时驱动 F5 压缩（compress.py :135）与语义修复（postprocess/__init__.py :173 → speed/pipeline.py :530 的 `semantic_model`）。票 12 把「模型名字符串」换成「可选方案字段」时，两个消费点经同一字段取方案——与票的意图一致，但两张消费点的签名迁移共享这一个配置字段变更。
4. **`llm_ctx` 是死参数**。`run_post_stage(..., llm_ctx :109)` 与 `compress_fast_subtitles(..., llm_ctx :160)` 声明并透传（`__init__.py` :131），但 compress.py 函数体从不读取它，`runner.py` 调 `run_post_stage` 也不传。迁移时可顺带处理，但票文未提。
5. **配音改写的超时未被 spec/ticket 点名**。spec 只写「断句 30 秒、语义修复 60 秒保真；字幕优化与压缩落 120 秒」。dub rewriter 现状无 timeout（SDK 默认 600 秒，rewriter.py :69–73），迁到 gateway 后会**静默落到 120 秒默认**——行为变化与优化/压缩同类，但票 13 文中未列明，测试断言时需知晓。
6. **优化/断句当前走旧 memoize 磁盘缓存**。`call_llm` 带 `@memoize(get_llm_cache(), expire=3600)`（client.py :190）。迁移到 gateway 后缓存行为换成 GatewayResponseCache（`llm_gateway` 新目录，3600 过期）——这是 06 号票已落地的前置，票 11 实现时不需要自建，但「相同请求短时重跑不重复计费」的语义来源变了。
7. **语义修复有独立的进程内缓存 `_SEMANTIC_CACHE`**（speed/pipeline.py :74，dict 上限 256 :75，经 `repair_semantic_windows(cache=...)` :473 使用）。它与 gateway 磁盘缓存是两层东西，迁移后依然存在——fake gateway 测试若发现「没打到 gateway」可能是因为窗口命中了这层进程缓存。
8. **语义修复的默认 rewriter/reviewer 工厂目前以裸 model 字符串闭包构造**（semantic.py :279/:300），且 `repair_semantic_windows` 的注入缝（rewriter/reviewer 参数）是公开 API——迁移到 profile+gateway 时这两个工厂的签名变更会沿 `optimize_speed`（pipeline.py :459–478 的 `semantic_model`/`semantic_rewriter`/`semantic_reviewer` 参数）一路上传到 `run_post_stage`。
9. **断句/优化尚无任何 gateway stage 标签先例**。现有 utility 角色标签只有探测用的 `"connection_probe"`/`"connection_probe_text"`/`"connection_probe_structured"` + `role="utility"`（check_llm.py @300f54a :139/:230）；「split」「optimize」的 stage 字符串需要新定，翻译侧命名风格是 `"single_llm_translation"`（llm_translator.py :231）。
10. **conftest 行号偏移**。`mock_llm_client` 在主 checkout master（300f54a）是 `tests/conftest.py` :198。实现 subagent 应从 master 分支，本笔记行号以 master 为准。
