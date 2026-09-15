# 06 - gateway 磁盘缓存设计（发现文件 / 设计方案）

输入：ticket 02 Answer 决策 12-14（覆盖面全量含翻译、cache_hit 写日志、本票阻塞 03）+ ADR-0014。所有结论带 file:line，均来自代码实证。落点归 ticket 03（缓存落地先于 client.py 退役）。

---

## 一、现状事实

| 事实 | 位置 | 对设计的含义 |
|---|---|---|
| 旧缓存是 `@memoize` 装饰 `call_llm`，键=调用参数（messages/model/temperature/kwargs），**不含 base_url/api_key** | client.py:190, cache.py:72-105 | 换 provider/key 不换模型名 → 命中别的账号/provider 的旧响应。本设计必须修 |
| memoize 语义：expire=3600、typed=True、全局开关关闭时完全旁路（不读不写）、异常不缓存 | cache.py:86-105, client.py:190 | 新缓存对齐这四条作为平价基线 |
| 翻译路径今天**无** gateway 级磁盘缓存（`LLMGateway.complete` 直连 adapter，无任何缓存层） | gateway.py:82-139 | 缓存是新增能力，不是平价迁移 |
| `LLMResult = {text, usage, raw}`；`raw` 在 src 与 tests **零消费者**（grep `\.raw\b` 无匹配）；`usage` 被增强翻译计费报告消费（orchestrator.py:813 → report.py:92-102） | models.py:326-330 | 序列化面可收窄 |
| `LLMRequest.temperature` 是废弃兼容字段，adapter 从不序列化它 | models.py:278-279 | 键构造不能用 asdict()——会把无语义的 temperature 打进键 |
| `cacheable_system_prefix` 会改变请求体（Anthropic 包 cache_control 块 / Gemini 用 cachedContent） | adapters.py:665-668, 889-893 | 必须进键 |
| 探测（连接/能力）走 gateway 但语义是「验证当下真实链路」 | check_llm.py:172-240 | 不该读缓存（见 §五） |
| 全局开关三处拨动：GUI 启动、设置页切换、tests conftest 全局关 | ui/main.py:71-74, setting_interface.py:851-868, tests/conftest.py:27 | 新缓存复用同一开关，不新增面 |
| cache.py 五个 Cache 实例模块级创建；conftest `pytest_unconfigure` 逐个 close（Windows 句柄） | cache.py:39-43, tests/conftest.py:33-41 | 新实例必须照抄注册 |
| 增强翻译的 cap 阶梯/reasoning 降级会**逐次改变请求**（max_output_tokens / request_options_override 变） | orchestrator.py:715-795 | 重试请求天然不同键，不会误命中；跨 run 命中仅当 cap 阶梯重现首个 cap（可接受，见 §七） |
| llm_translator 另有**应用层** chunk 翻译缓存（`_get_cache_key`，含完整 profile dict） | llm_translator.py:263-278 | 与 gateway 缓存分层正交：app 层缓存最终译文，gateway 缓存原始响应；并存不冲突 |

---

## 二、设计总览

新模块 `videocaptioner/core/llm/response_cache.py`，类 `GatewayResponseCache`，`LLMGateway.__init__` 增可选参 `response_cache=None`（默认模块级单例，共享同一 diskcache 目录——跨 gateway 实例/跨 run 去重是目标本身）。`complete()` 插入点：

```
complete(profile, request, *, max_attempts=4, cancelled=None, use_cache=True):
    if use_cache:
        hit = response_cache.lookup(profile, request)     # 开关关/未命中/反序列化失败 → None
        if hit is not None:
            log_gateway_cache_hit(profile, request)        # request_logger 新函数
            return hit                                     # 不进信号量、不建 adapter
    ...现有重试循环不动...
    成功: if use_cache: response_cache.store(profile, request, result)
    return result
```

**fail-open 铁律**：缓存是优化不是依赖。lookup/store 内部一切异常（磁盘满、反序列化失败、损坏条目）捕获 → log debug → 当 miss / 放弃写入，绝不让缓存故障打断请求路径。旧 memoize 无此保护（diskcache 内部错误会冒泡），这是有意超越平价的一处。

---

## 三、逐点裁决

### 1. 缓存键——内容寻址，连接字段全量入键

**键 = sha256(规范 JSON(键素材))**，hex digest 做 diskcache 键（同 `generate_cache_key` cache.py:108-131 与 `_get_cache_key` llm_translator.py:275-277 的既有做法）。**明文永不落盘**，包括 api_key。

键素材 = 显式 allowlist，两级（**不要用 asdict()**——temperature/metadata 会混进来）：

| 层 | 进键字段 | 排除字段及理由 |
|---|---|---|
| profile（请求塑形字段） | `transport`、`dialect`、`base_url`、`api_key`、`model`、`openai_endpoint`、`request_options`（thaw 后） | `profile_id`/`name`——纯标识/显示，入了会把改名变缓存失效；`work_context_tokens`/`max_concurrency`——不进请求体（cap 规划产物走 request.max_output_tokens，已捕获） |
| request | `messages`（role+content 列表）、`max_output_tokens`、`response_schema`、`request_options_override`、`cacheable_system_prefix` | `temperature`——废弃字段，adapter 不序列化（models.py:278-279）；`metadata`——stage/role 是观测标签不是语义，同文同答跨 stage 去重是特性（cache_hit 日志记**当次**请求的 metadata，观测正确） |

- **api_key 必须全量入键**：同 provider 同模型不同账号若共享缓存 → 跨账号响应复用，计费归因错乱。隐私论证：digest 是单向哈希，明文不进缓存目录；能摸到缓存目录的人本来就摸得到 `llm_model_profiles.json` 明文（AppData）。无新增暴露面。
- **`dialect` 保守入键**：它只影响结构化输出策略（adapters.py:420-432），纯文本请求下多半不改变请求体——入键代价只是罕见场景多 miss 一次，换正确性兜底。
- **版本前缀**：键素材里加 `"key_version": "gateway-cache-v1"`。凡请求构造逻辑变更（adapter 行为修 bug、字段增删），bump 此值即可全量作废旧条目——这是「同键不同请求形态」跨版本陈旧响应的唯一逃生门。

### 2. LLMResult 序列化——只存 text，命中回放零 usage

落盘值 = JSON dict：`{"schema": "gateway-cache-v1", "text": <str>}`。

- **`raw` 丢弃**：src/tests 零消费者（§一），且是 SDK 对象，pickle 会把缓存条目耦合到 SDK 版本与 models.py 代码布局。不存。
- **`usage` 不存、命中回放全 None**：`LLMResult(text=payload["text"])` 默认 `LLMUsage()` 全 None。理由：usage 报告的语义是**真实花费**（orchestrator.py:813 累加），缓存命中零成本，回放 usage 会把首次调用的花费重复计入。`LLMUsage.__add__` 对全 None 是恒等元（models.py:311-323），orchestrator 累加无害。与「cache_hit 日志无 usage」（决策 13）口径一致。
- 值级 `schema` 字段与键级 version 双保险：读侧校验 payload 形状（dict + str text），不符即当 miss 覆写——防键 version 忘 bump 时的脏读。
- 用 JSON dict 而非 pickle LLMResult：解耦代码布局，损坏可检测。

### 3. profile 编辑失效——内容寻址即构造性正确，无主动失效

- **正确性零成本**：键含全部请求塑形字段 → 编辑 base_url/model/api_key/request_options 自动换 digest，旧条目**不可达**（不是错命中）。编辑 `name`/`max_concurrency` 等非键字段不换 digest——也不该换（不影响响应）。1 小时内改回原值 → 旧条目重新可达且语义仍正确（同配置生成的响应），是特性。
- **卫生靠 expire**：不可达条目 ≤1h 自然死亡（diskcache 过期惰性回收 + 默认 size_limit≈1GB 兜底）。**不做 tag_index / 不做按 profile_id evict / 不在 profile store 挂钩**——收益只剩提前回收不到 1 小时的垃圾，代价是 store↔cache 新耦合 + tag 索引开销。否决记录见 §六。
- 唯一需要人记住的纪律：请求构造逻辑变更时 bump `key_version`（§三.1）。

### 4. expire 对齐 + 失败语义

- **expire=3600**，与 memoize 完全一致（client.py:190）——平价锚点，同时界定 provider 侧行为漂移的陈旧窗口。
- **只缓存 `complete()` 正常返回的結果**：adapter 对空/坏响应抛 `LLMCallError(INVALID_RESPONSE)`（gateway.py:119-120 拦截重试），走不到 store；`LLMCallError`/`InterruptedError` 一律不缓存。与 memoize「异常不缓存」平价。
- **app 层失败语义（接受并记录）**：gateway 缓存的是 provider 成功，不是 app 成功。single-LLM 翻译 json_repair 解析失败 / enhanced 校验不过时，响应已在缓存——同请求重试会命中同一条坏文本，机械重试**快速失败**（省了钱）而非烧钱重跑；cap 升级 / reasoning 降级会改请求 → 不同键 → 真实重试（§一）。旧 memoize 同此行为，平价非回归。不做「app 层失败驱逐缓存」——需要跨层驱逐 API，耦合大于收益。

### 5. cache_hit 日志

`request_logger.py` 新函数 `log_gateway_cache_hit(profile, request)`（复用 `_write_log`，保持私有）。条目字段：

```json
{"time": ..., "request_id": "<新 uuid>", "stage": "<当次 metadata.stage>", "role": "<当次 metadata.role>",
 "status": "cache_hit", "profile": {"id": ..., "model": ...}, "duration_ms": 0}
```

- stage/role 照常、无 usage、无 attempt（决策 13；命中不是一次尝试）——与 begin/finish_gateway_request 的成功条目并排可 grep。
- `include_content` 开启时附 `"response": {"text": <缓存文本>}`，与成功条目对称（finish_gateway_request 同款，request_logger.py:174-175）。
- 目的地对齐：地图 Destination「配置漂移在第一个请求即可见」——cache_hit 条目带 profile.id/model，漂移后同请求不再命中，日志链自然可见。

### 6. 全局开关联动 + 缓存目录

- 开关：复用 `is_cache_enabled()`（cache.py:33）。**lookup 与 store 双侧都判**，关=完全不读不写（照抄 memoize wrapper cache.py:99-101）。GUI 拨点沿用现有一处不新增；CLI 无拨点（默认开）——现状即如此，不变。
- 目录：**新实例** `_gateway_cache = Cache(str(CACHE_PATH / "llm_gateway"))` + `get_gateway_cache()`（cache.py:39-43 旁照抄）。不复用 `llm_translation` 目录：旧条目键是 memoize 键（无连接信息，正是被修的洞），混目录只会污染排障。旧目录随 client.py 退役成为死运行数据，留盘不清理。
- conftest：`pytest_unconfigure` close 列表加 `cache.get_gateway_cache`（tests/conftest.py:33-41）；conftest:27 已全局 disable，测试默认零缓存副作用。03 顺带清理：client.py 删除后 `get_llm_cache` 在 src 零消费者，连同 conftest:35 引用一起删（`memoize` 本体保留——通用工具且 test_cache_validation.py 直测）。

---

## 四、探测语义——use_cache=False 双向旁路

`complete()` 增 keyword-only 参 `use_cache: bool = True`。两处探测路径传 `False`（check_llm.py:172-202 能力探测、:205-240 连接探测）：

- **不读**：探测的目的是「验证当下链路真实可用」。读缓存会让「测试连接」拿到 59 分钟前的 OK——provider 已宕/凭证已失效时给假信号。探测就几个 token，信号正确性 > 省这点钱。
- **不写**：缓存只沉淀真实任务流量，排障时目录里的条目即真实请求史。
- 不用 metadata 标记旁路（metadata 是观测标签不是控制通道）、不用构造器开关（探测是逐调用语义，per-call 参数最小）。

---

## 五、ticket 03 落地清单

1. `core/utils/cache.py`：`_gateway_cache` 实例 + `get_gateway_cache()`；conftest close 列表注册；随后删 `get_llm_cache`（含 conftest:35）。
2. 新 `core/llm/response_cache.py`：`GatewayResponseCache`（allowlist 键构造 / 序列化 / fail-open / expire=3600 / is_cache_enabled 双侧门）。
3. `core/llm/request_logger.py`：`log_gateway_cache_hit()`。
4. `core/llm/gateway.py`：`complete(..., use_cache=True)`；lookup 在信号量与 adapter 之前；成功后 store。
5. `core/llm/check_llm.py`：两处探测传 `use_cache=False`。
6. 测试要点：键含 base_url/api_key（两 profile 只差 api_key → 互不命中，**洞的回归测试**）；开关关=不读不写；探测旁路；cache_hit 日志行形状；命中回放 usage 全 None；坏 payload 当 miss；`key_version` bump 作废旧条目。

## 六、否决的备选方案

| 备选 | 否决理由 |
|---|---|
| 键用 profile_id 标识（非内容寻址） | 编辑同 id profile 需主动失效，且跨 profile 同配置无法去重；内容寻址让失效问题消失 |
| tag_index + 按 profile_id evict（store 挂钩） | 收益 <1h 提前回收垃圾，代价是新耦合 + 索引开销；expire 已兜底 |
| 序列化整个 LLMResult（pickle） | raw 是 SDK 对象且零消费者；pickle 耦合代码布局与 SDK 版本 |
| 命中回放 usage | 计费报告语义是真实花费；会双重计数 |
| api_key 不入键 / 只入前缀 | 同 provider 换账号跨账号复用响应，计费归因错乱 |
| 探测也读缓存 | 「测试连接」变成「测试 1 小时内的连接」，假信号 |
| app 层解析失败驱逐缓存 | 跨层驱逐 API，耦合大于收益；快速失败语义可接受（§三.4） |

## 七、遗留未确认（低风险，不阻塞）

- 增强翻译跨 run 命中率：cap 阶梯由 runtime budget 推导（orchestrator.py:715-717），两次 run 若首个 cap 不同则不命中。无正确性问题，只是省钱率不可承诺——留观测数据（llm_requests.jsonl 的 cache_hit 行天然就是度量）。
- diskcache 多进程并发（GUI 与 CLI 同时跑）：diskcache 自带文件锁，理论安全，Windows 句柄模式已有 conftest 先例；如出问题属 03 实现阶段发现项。
