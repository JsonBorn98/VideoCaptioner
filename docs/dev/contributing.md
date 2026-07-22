# 开发说明

这是一个偏个人使用、按需更新的 fork。Issues 和 Pull Requests 可用于记录或参考，
但不保证回复、合并或排期。

## 开发环境设置

1. Fork 本仓库
2. 克隆你的 Fork
3. 安装开发依赖

```bash
git clone https://github.com/YOUR_USERNAME/VideoCaptioner.git
cd VideoCaptioner
uv sync --python 3.12
uv run videocaptioner doctor --profile gui
```

如需开发 Qwen 本地 ASR：

```bash
# 日常源码启动 GUI 时，优先通过 Qwen 组件管理安装独立 runtime
uv run videocaptioner
uv run videocaptioner doctor --profile qwen
```

只有在调试 `qwen-asr` 依赖本身、需要让当前 `.venv` 直接 import `qwen_asr` 时，才执行：

```bash
uv sync --python 3.12 --extra qwen
```

Qwen 组件管理会把运行时安装到 `AppData/runtimes/qwen`，并用 `uv --torch-backend cpu/cu128` 控制 PyTorch 版本。不要用普通 `uv pip install qwen-asr` 替代 CUDA 运行时安装，否则依赖解析可能回退到 CPU PyTorch。

## 代码规范

- 使用 `pyright` 进行类型检查
- 使用 `ruff` 进行代码格式化

```bash
# 类型检查
uv run pyright

# 代码格式化
uv run ruff check --select I --fix .
```

## 提交前检查

1. 创建新分支
2. 运行与改动相关的检查
3. 提交修改；需要时推送到自己的 Fork

## 注释要求

保持简洁清晰，只需要必要的注释即可。

---

相关文档：
- [MiMo and Qwen ASR Backend Lessons](/dev/asr-mimo-qwen-lessons)
- [ASR 配置指南](/config/asr)
