# 常见问题

常见问题解答。

## 安装问题

### Q: 如何安装依赖？

A: 参考[快速开始](/guide/getting-started)中的安装步骤。

### Q: Qwen CUDA 运行时安装后仍显示 `+cpu` 怎么办？

A:
- 确认你点击的是 **Qwen 组件管理 → 安装 CUDA 运行时**，不是 CPU 运行时。
- 关闭正在运行的转录任务，确认没有残留 `python` / `uv` 进程占用 `AppData/runtimes/qwen`。
- 重新点击 **安装 CUDA 运行时**。安装完成后应显示类似 `PyTorch 2.11.0+cu128 (CUDA 12.8, CUDA available)`。
- 如果仍失败，查看 `AppData/logs/app.log` 中 `Installing CUDA PyTorch runtime` 附近的日志。

### Q: Qwen 运行时安装时报“拒绝访问”怎么办？

A:
- 这是 Windows 文件访问错误，常见原因是运行时目录被转录任务、残留 Python 进程、杀毒软件或系统索引器占用。
- 先关闭 VideoCaptioner 中正在进行的转录/安装任务，等待数秒后重试。
- 如果仍失败，重启软件后直接进入 **Qwen 组件管理** 重新安装。
- 详细错误会记录在 `AppData/logs/app.log`。

### Q: Qwen 转录失败并提示 `No module named 'qwen_asr'` 或 `No module named 'nagisa'` 怎么办？

A:
- 说明 Qwen runtime 没有完整安装。
- 进入 **设置 → 转录配置 → Qwen 组件管理**，重新安装与设备匹配的运行时。
- CUDA 用户安装后要确认状态不是 `+cpu`，否则不要选择 `cuda:0` 转录。

## 使用问题

### Q: 转录时出现幻觉或重复怎么办？

A: 
- 启用 VAD 过滤
- 更换更大的模型
- 尝试 Large-v2 而不是 Large-v3
- 在嘈杂环境中启用音频分离

### Q: LLM 请求失败怎么办？

A:
- 检查 API Key 是否正确
- 检查 Base URL 是否正确
- 降低线程数
- 检查网络连接
- 查看日志文件获取详细错误信息

更多问题，请访问 [GitHub Issues](https://github.com/JsonBorn98/VideoCaptioner/issues)。

