# 票01 修复回审（仅一轮）

- Spec轴：无实质发现；数值表待采样完填入，不等于后续优化已通过。
- Standards轴：其将Duplicated Code标为硬违例不符合技能『坏味永远判断题』规则，主流程已裁决。保留独立脚本的受控应答协议；修实际socket connect_ex保护覆盖漂移。
- 额外只读测量检查采纳：headline/profile诊断分离，第二拆分片段真实校订断言，CLI本地零模型请求断言。未采纳『单次worker失败毁掉已采数据』：逐项JSON已先落盘，可直接核实main中sample读取先于aggregate。
- 验证事实：headline `_measure_case(profiled=False)` 不enable cProfile；diagnostic单独目录与cache，输出SHA256必须一致。新的test_long_local_task断言profiler_enabled false/true并观察阶段调用。
- 第二拆分片段 fixture 文本确为 `The second sentence is here.`，返回译文需为 `您好`；run_case mixed/partial-failure测试已通过。
- UI socket guard已对connect/connect_ex保持相同loopback检查；最终完整Qt探针仍需通过。
- 本仓库为Python项目，无对应bun lint/test脚本；回审用uv ruff、uv pyright、全量离线pytest。没有产品UI修改，不适用bun build。
- 最终全量离线回归：`QT_QPA_PLATFORM=offscreen uv run --no-sync pytest -m 'not integration and not llm' -q` → 1472 passed、5 skipped、61 deselected、1 warning，254.98s；包含本票18个测试，全部通过。此前开发期采到子代理red的6项失败不是最终结果。
- 新增6个Python文件 ruff check 全通过；全项目 pyright 为0 errors / 20既有warnings；三个新增脚本单独pyright为0 errors / 6 warnings（duck-typed adapter/gateway/cache与Optional operand），如实保留告警，不称零告警。git diff --cached --check通过。
- 五次完整矩阵、warm独立五次、local-long整组重采均完成。最终主墙钟不启用profiler，diagnostic隔离并逐次断言输出指纹一致。三组的231个源码指纹及uv.lock一致，导出时再次核对。
- 原local-long无profiler组跨度19.57%，整组无效且保留；重采组跨度5.10%，median2.193482600s。slow-main median2.401658300s，对应后续门槛1.801243725s；local门槛2.522504990s，未放宽矩阵。
- 真实loopback三探针各五次全部ok/清理通过；Qt五次取消门禁全部通过、无过程成果。停止median queue1.360728s/network5.085254s/backoff0.900213s/ui1.202910s均是现状未达标，不是本票设施失败。
- 文档实测表与安全JSON已落`docs/dev/postprocess-performance-baseline{,-v1.json}`，脱除绝对路径与凭据，不包含字幕正文/tracker。JSON保留所有五次和无效组，没有删慢样本。
- Spec剩余实测表事项已补齐；缓慢分段/5xx和实际widget明确留给后续票。完成这一轮修复验证，不重启双轴审查循环。
