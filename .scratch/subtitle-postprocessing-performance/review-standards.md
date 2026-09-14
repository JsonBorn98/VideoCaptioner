# 规范轴审查结论 — 票 01（后处理性能基准基线）

- 审查对象：`git diff f13e8c464699c52705491aff71044bd2aba115e1 --`（未提交新增，`git add -N`）
- 范围：编码规范轴；规范来源 CONTEXT.md、docs/adr/0018–0021、docs/agents/issue-tracker.md、pyproject.toml；不评 spec 轴、不评冻结验收矩阵未填
- 审查者：matt-flow 规范轴；日期 2026-09-11；lint/type 类问题按票务口径忽略

## 结论：重复代码判断题；一处守卫覆盖差异已修，其余无仓库标准违背

主流程裁决：Fowler 坏味按技能规则永远是判断题，不能标为硬违例。修正下文「硬违例」定性：保留脚本各自的最小受控协议，不引入新的共享基准框架；connect_ex 守卫覆盖差异具有实际外网保护后果，已在 UI 独立入口补齐。其余重复/命名建议本轮保留，不无限循环重审。

Diff 仅新增 7 个文件（docs/dev/postprocess-performance-baseline.md、scripts/postprocess_{benchmark,transport_probe,ui_probe}.py、tests/test_postprocess/test_{performance,transport,ui}_baseline.py），未触碰产品码；CONTEXT.md 术语（后处理性能基准、批并发执行、初版快照等）与 ADR-0018（并发注入走 gateway 构造缝、thread_num 语义不受影响）、ADR-0021（冻结快照口径，基准只测量不改行为）均无背离；issue-tracker 与 pyproject 约定无违例。

## 硬违例：Duplicated Code（多个文件/hunk 同逻辑且已漂移）→ 应共享形状

1. **离线 loopback socket 守卫两份且行为漂移**：`scripts/postprocess_benchmark.py` `offline_network()` 同时补丁 `socket.socket.connect` 与 `connect_ex`；`scripts/postprocess_ui_probe.py` `_offline_network()` 只补 `connect`。同名守卫在两处覆盖面不同——走 `connect_ex` 路径的调用在 UI probe 下不被拦截。ui_probe 文档 "Self-contained on purpose: the UI probe must not depend on benchmark internals" 属有据偏离，但守卫漂移无据：应抽共享守卫模块或对齐两份实现。
2. **请求协议解码两份**：`ControlledGateway.decode`（benchmark）与 `_decode_request`（ui_probe）均为 `re.search(r"<input>(.*?)</input>", user, re.S)` + role 判别；transport probe 不需要，但两份副本后续改协议（如 synthetic-v2）会只改一处。
3. **合成应答协议两份**：benchmark 与 ui_probe 的 adapter 均实现 main→`你好`/review→`您好` 应答构造，文档自认"protocol aligned with synthetic-v1"——对齐靠人肉同步，属典型 Duplicated Code。

**轻微 Duplicated Code（同文件内）**：

- `scripts/postprocess_benchmark.py` `main()` finally 的五个 cache getter 关闭清单与 `tests/conftest.py` `pytest_unconfigure` 逐字重复（新增缓存项时两处漂移）；返回 dict 中 `local_wall_excluding_gateway` 与 `gateway_wall_union_seconds` 对同一 calls 列表各调一次 `_interval_union`。

## 轻微坏味（判断：不构成违例，仅记录）

- **Message Chains**：`scripts/postprocess_ui_probe.py` `getattr(getattr(result, "report", None), "viewing_repair", None)`；两层导航可封装。
- **Mysterious Name**：`scripts/postprocess_transport_probe.py` `CLI_ARG_OUTPUT = "--output"` 名称复述值，同侪参数均内联，该常量反增间接层。

## 判不违例项（对照基线逐条）

- **Primitive Obsession**：报告用嵌套 dict + 字符串键——序列化 JSON 边界（report.json），领域豁免。
- **Middle Man**：`ControlledGateway.complete`/`_ProbeGateway.complete` 转发 `runtime.complete` 但附观测记录与指纹，非纯转发。
- **Feature Envy / Shotgon Surgery / Divergent Change / Speculative Generality / Repeated Switches / Data Clumps / Refused Bequest**：未检出。

## 一致性冗余（可删）

`tests/test_postprocess/test_transport_baseline.py` 手写 `sys.path.insert(0, REPO_ROOT)` 垫片；同票 `test_performance_baseline.py` 与 `tests/test_utils/test_asr_benchmark.py` 均直接 `from scripts... import` 可用，垫片冗余且不一致。
