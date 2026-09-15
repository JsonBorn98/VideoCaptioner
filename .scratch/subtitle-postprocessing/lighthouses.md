## Lighthouses
- [Nimbalyst: worktree parallel agent guide](https://nimbalyst.com/blog/git-worktrees-for-ai-coding-agents-complete-guide) — 并行三步法：per-task worktree 隔离 → 共享产出经 PR/merge 汇流 → merge queue 防基线漂移；另给出 competing attempts 模式供不确定方案时对赌
- [Augment Code: git-worktree-per-task pattern](https://www.augmentcode.com/guides/git-worktrees-parallel-ai-agent-execution) — 业界默认并行隔离原语: 每票一 worktree+分支, agent 认领 worktree 而非永久拥有, incident.io 已生产验证; 对应 03/04 两票可各自开 worktree 并行
