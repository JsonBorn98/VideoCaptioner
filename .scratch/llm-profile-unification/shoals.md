## Shoals
- [fork 里 gh pr create 默认指向上游（PR #1189/#1190 误发）](https://github.com/WEIFENG2333/VideoCaptioner/pull/1190) — 在 fork 里跑 gh pr create 不带 --repo 时默认目标是上游 WEIFENG2333/VideoCaptioner（GitHub 对 fork 的设计意图），本仓库为私用维护、永不向上游发 PR；规矩已写进 AGENTS.md 的 Commit & Pull Request Guidelines，gh 默认仓库已 set-default 为 JsonBorn98/VideoCaptioner，创建 PR 时必须显式传 --repo JsonBorn98/VideoCaptioner
