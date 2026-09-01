# 并发控制面收敛到 thread_num 直通

背景：同一 provider 并发资源被 thread_num（任务线程池）与 profile.max_concurrency（gateway 信号量）两个旋钮分持，UI 调大 thread_num 被隐藏的信号量静默压到 4，旋钮失效。决定：thread_num 直通每 profile 并发闸（每 profile 一闸，main/review 各自吃满）；profile.max_concurrency 降为可选夹钳，默认 None 不夹，旧 profile 存的 4 迁移时清除为默认（无法区分显式与默认，需要限流的用户重设），仅显式设置时作为防 429 的 provider 保护上限；429/5xx 由既有退避重试（遵循 Retry-After）兜底。GUI/CLI thread_num 统一默认 10，文案改「并发请求数」。被否决：彻底退役该字段（丢失按 provider 手动限流的主动权）、min 两者+警告（双旋钮语义保留、静默不再但混乱依旧）。
