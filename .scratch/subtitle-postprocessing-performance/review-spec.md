# Spec 轴审查结论 — 票 01（后处理性能基准基线）

- 审查对象：`git diff f13e8c464699c52705491aff71044bd2aba115e1 --`（未提交新增，`git add -N`）
- 范围：仅票 01 基线；spec 来源 `spec.md` + `issues/01-postprocess-performance-baseline.md`
- 审查者：matt-flow spec 轴；日期 2026-09-11

## 结论：符合 spec，无范围蔓延

Diff 仅新增 `docs/dev/postprocess-performance-baseline.md`、`scripts/postprocess_benchmark.py`、`scripts/postprocess_transport_probe.py`、`scripts/postprocess_ui_probe.py` 与三个测试文件；未改动 `videocaptioner/` 产品码，符合票 L5（只测量现状，不实现并发/批量校对/主动取消，不改用户模型方案，不发真实请求）。

## 冻结验收矩阵（postprocess-acceptance-v1）核对

覆盖票 L21 全项：版本化、复跑命令、5 次重复、并发改善 ×0.75（且 ≥2 请求重叠 / peak ≤4）、主请求 ≤4、校对 ≤8（较现状 40 次减 ≥80%）、五类停止响应各 ≤0.30s（排队/在途/退避/SDK 重试/完整任务）、状态刷新 ≤0.50s / Qt 信号 ≤0.10s / 心跳 ≤0.10s、本地回退双门槛 ≤15% 且 ≤0.75s。每行均有依据与适用条件，与 spec L112（每批最多 10 主体起点）、L133（不得忽略 Retry-After 提前重发达标）、L166（先于优化冻结、不得见结果降门槛）一致。防作弊条款（不调延迟/缓存/角色参数伪造提速）符合票 L22。UI 行正确标注仅测 worker 信号/事件循环，不宣称已验 widget。

## 已验证合规项（引用票行号）

- L17：uv 工具链离线入口；无需 API key；DHH 缺失可运行；AppData/缓存/输入/输出隔离。
- L18：`report.json` 记录 revision、工作区状态、源码/uv.lock 指纹、环境、逐次原始数据；拒绝覆盖既有目录（测试断言）。
- L19：三类传输探针（queue/network/backoff）+ UI；取消锚定在真实进入事件；清理有界且被证明（join/is_alive/handler drain）。
- L20：logical/attempts/cache_hits 分列；冷热不混算；任务信号量排队未埋点如实报 null。
- L22：原文保护断言、`您好` 校对实际应用断言、130/129 段输出、局部回退下游继续；区分现状缺陷（baseline_gaps）与设施失败（ok=False）。
- L23：未执行真实验证与历史静默未归因均已显式声明。

## local-long 噪声 21.9% 处置

超 15% 即判整组无效并另跑整组 5 次、不删慢样本、不放宽阈值——正是矩阵自身规则（min–max 超 median 15% 整组重采）的正确执行，合规。建议该 21.9% 组作为噪声证据保留在报告，勿事后混入有效组。

## 附录：主流程采纳的额外测量检查（2026-09-11 追加，未重审）

主流程本轮采纳以下调整，经核对均与 spec 一致，不改变「符合」结论：

- **headline 去 cProfile**：cProfile 观测开销移出主测量（原 docs 口径 L33 已注明含观测开销）。独立 diagnostic 任务承担本地阶段成本记录（spec L165「报告…本地阶段成本」），并断言输出指纹一致，保证观测手段不改变被测行为。
- **全矩阵重采 5 次**：沿用噪声处置规则（min–max 超 median 15% 整组重采、不删慢样本、不降门槛），符合票 L21。
- **第二拆分片段校订断言**：强化 spec L185（一对多拆分）与 L14（校对绑定主修复候选、不漏一对多拆分结果）的覆盖。
- **local-long CLI 零请求断言**：强化票 L17/L20（本地处理不调用模型、逻辑请求数如实分列）。

无需双轴重审；修复回审以 lint/types/全套 tests 验证，实测值由最终文档填写。

## 遗留（非本票缺陷）

1. 实测基线数值表待采样完成填写（须先于优化票开始，票 L21）。
2. 传输探针未含 spec L189 的缓慢分段数据/5xx 场景；票 L19 仅要求排队/网络/退避三类，已满足。建议将此缺口显式记入后续票（如 06），勿误读为矩阵「各等待阶段停止响应」已全覆盖。
