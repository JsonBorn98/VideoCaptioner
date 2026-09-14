## Shoals
- [Gemini 模型输出上限是半开区间](https://github.com/google-gemini/gemini-cli/issues/7578) — Gemini 输出侧超限 400 把上限写成 `from 1 (inclusive) to 65537 (exclusive)`；解析必须取 exclusive-1（65536）作为模型输出上限，直接用 65537 会让验证重试再次 400。
- [适配器优先用方案 max_output_tokens](videocaptioner/core/llm/adapters.py) — 三种接口的适配器都是 profile.max_output_tokens 优先于 request.max_output_tokens。输出上限探查要发工作上下文-1 / 建议值时必须 replace 临时 profile，只改 LLMRequest.max_output_tokens 在真实路径上不会生效。
