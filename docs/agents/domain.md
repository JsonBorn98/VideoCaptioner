# 领域文档

工程技能在探索代码库时该如何消费这个仓库的领域文档。

## 探索前先读这些

- 仓库根的 **`CONTEXT-MAP.md`**，它指向三个上下文文档：`CONTEXT-TRANSLATION.md`（字幕翻译）、`CONTEXT.md`（字幕后处理）、`CONTEXT-VIDEO-SYNTHESIS.md`（视频合成）。读跟即将改动的区域相关的那些。
- **`docs/adr/`**（根目录集中存放）。本仓库所有 ADR——包括上下文专属决策——都集中在这里，没有 `src/<context>/docs/adr/` 式的上下文专属目录。读跟改动区域相关的 ADR。

这些文件如果有不存在的，**默默继续**。不要特意指出它们不存在，也不要建议提前创建。`matt-flow:domain-modeling`（经 `matt-flow:grill-with-docs` 和 `matt-flow:improve-codebase-architecture` 到达）会在术语或决策真正被解决时惰性创建它们。

## 文件结构（本仓库实际布局，multi-context）

```
/
├── CONTEXT-MAP.md                    ← 指向三个上下文文档
├── CONTEXT-TRANSLATION.md            ← 字幕翻译上下文
├── CONTEXT.md                        ← 字幕后处理上下文
├── CONTEXT-VIDEO-SYNTHESIS.md        ← 视频合成上下文
├── docs/adr/                         ← 所有 ADR 集中存放（系统级 + 上下文决策）
└── videocaptioner/
```

注意：`commit_glossary_term` 等术语工具写入的是**根 `CONTEXT.md`**（字幕后处理上下文），跨上下文的系统级术语也落在那里。

## 用词汇表里的词

当你的输出要命名一个领域概念（issue 标题、重构提案、假设、测试名）时，用 `CONTEXT.md` 里定义的术语。不要漂移到词汇表明确避开的同义词。

如果你需要的概念还不在词汇表里，那是个信号，要么你在生造项目里不用的说法（重新考虑），要么真有个缺口（记下来给 `matt-flow:domain-modeling`）。

## 标记 ADR 冲突

如果你的输出和某个已有 ADR 矛盾，显式指出，而不是悄悄覆盖。

> _与 ADR-0007（事件溯源订单）冲突，但值得重开，因为……_
