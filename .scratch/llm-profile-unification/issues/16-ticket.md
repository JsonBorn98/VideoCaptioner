# 16 - 旧客户端与环境变量中继退役

**构建什么.** 旧客户端与全部隐式通道退役（spec 落地顺序硬依赖的最后一环：磁盘缓存先落地、消费点全迁后，旧客户端才退役）。client.py 整文件退役：get_llm_client/call_llm 及其全部私有辅助函数（含 tenacity RateLimitError 重试）随删。normalize_base_url 搬到 check_llm.py（测试函数的家），四个存活消费者改 import。check_llm_connection 三标量旧入口删除；profile 版连接测试保留给方案编辑器测试按钮。llm_translator 的 call_llm 回退分支删除（profile 存在唯一路径）；GUI 重翻译线程的环境变量写入分支删除；环境变量写入五处全删（GUI 两处 11 号票已删，CLI 三处 14 号票已删——本票做残留检查与收尾）。旧 memoize 的 get_llm_cache 在 src 零消费者后删除（memoize 装饰器本体保留——通用工具且有直测）；旧 llm_translation 缓存目录留盘成死运行数据。legacy 三日志函数（create_logging_http_client/discard_pending_legacy_request/log_llm_response 及其 httpx hooks 与 base 条目构造）随旧客户端退役删除——legacy 标签从源头消失。测试面：测旧客户端私有行为的用例随文件退役删除；测旧 UI 组存在性与旧键保存恢复的用例改写（15 号票已改部分，本票清尾）；tests/conftest.py 的 mock_llm_client fixture 旧缝（环境变量假值+符号打补丁）随消费点构造缝替换后同步退役。验证源：AppData/logs/llm_requests.jsonl 不再出现 legacy 标签条目。

**阻塞于.** 14

**状态.** resolved

- [ ] client.py 整文件退役：get_llm_client/call_llm 及其全部私有辅助函数（含 tenacity RateLimitError 重试）随删
- [ ] normalize_base_url 搬到 check_llm.py（测试函数的家），四个存活消费者改 import
- [ ] check_llm_connection 三标量旧入口删除；profile 版连接测试（check_model_profile_connection）保留给方案编辑器测试按钮
- [ ] llm_translator 的 call_llm 回退分支删除（profile 存在唯一路径）
- [ ] GUI 重翻译线程的环境变量写入分支删除
- [ ] 环境变量写入五处全删（GUI 任务线程两处 11 号票已删，本票删 CLI subtitle/postprocess/transcribe 三处 14 号票已删后的残留检查）
- [ ] 旧 memoize 的 get_llm_cache 在 src 零消费者后删除；memoize 装饰器本体保留（通用工具且有直测）；旧 llm_translation 缓存目录留盘成死运行数据
- [ ] legacy 三日志函数删除：create_logging_http_client、discard_pending_legacy_request、log_llm_response 及其 httpx hooks 与 _legacy_base_entry
- [ ] AppData/logs/llm_requests.jsonl 核验：不再出现 legacy 标签请求、profile.id="legacy" 条目为零
- [ ] 测旧客户端私有行为的用例随文件退役删除；测旧 UI 组存在性与旧键保存恢复的用例改写（15 号票已改部分，本票清尾）
- [ ] 全仓 grep call_llm/get_llm_client 零命中（测试 fixture 的 mock_llm_client 旧缝随消费点构造缝 11-13 号票替换后同步退役）
- [ ] ruff/pyright 干净，全量 -m "not integration" 测试绿
