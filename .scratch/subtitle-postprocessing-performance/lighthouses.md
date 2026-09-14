## Lighthouses
- [既有并发请求数控制面（ADR-0018）](../../docs/adr/0018-thread-num.md) — 项目已决定 thread_num 直通每 profile 并发闸，max_concurrency 仅作显式 provider 夹钳；后处理提速应先验证是否接入既有控制面，避免再造隐藏限流或重复旋钮。
