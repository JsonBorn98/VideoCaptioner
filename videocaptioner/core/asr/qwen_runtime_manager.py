"""Managed runtime support for optional Qwen ASR dependencies."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Literal, Sequence

from videocaptioner.config import LOG_PATH, RUNTIME_PATH
from videocaptioner.core.utils.logger import setup_logger

QWEN_RUNTIME_NAME = "qwen"
QWEN_RUNTIME_REQUIREMENTS = ("qwen-asr",)
QWEN_TORCH_REQUIREMENTS = ("torch",)
QWEN_TORCH_CPU_INDEX_URL = "https://download.pytorch.org/whl/cpu"
QWEN_TORCH_CUDA_INDEX_URL = "https://download.pytorch.org/whl/cu128"
QWEN_TORCH_CPU_BACKEND = "cpu"
QWEN_TORCH_CUDA_BACKEND = "cu128"
DEFAULT_QWEN_PYTHON = "3.12"

QwenRuntimeProfile = Literal["cpu", "cuda"]
ProgressCallback = Callable[[str], None]
Runner = Callable[..., subprocess.CompletedProcess]
logger = setup_logger("qwen_runtime_manager")


@dataclass(frozen=True)
class QwenRuntimeStatus:
    runtime_dir: Path
    python_executable: Path
    site_packages: tuple[Path, ...]
    has_venv: bool
    importable: bool
    uv_executable: str
    torch_version: str = ""
    torch_cuda: str = ""
    torch_cuda_available: bool = False
    torch_error: str = ""

    @property
    def ready(self) -> bool:
        return self.importable

    @property
    def message(self) -> str:
        if self.has_venv and self.importable:
            return "Qwen runtime is ready"
        if self.importable:
            return "Qwen runtime is available from the current environment"
        if not self.uv_executable:
            return "uv was not found; install runtime from a release bundle or install uv first"
        if not self.has_venv:
            return "Qwen runtime is not installed"
        return "Qwen runtime exists but qwen-asr is not importable"

    @property
    def torch_message(self) -> str:
        if self.torch_error:
            return f"PyTorch unavailable: {self.torch_error}"
        if not self.torch_version:
            return "PyTorch not installed"
        flavor = f"CUDA {self.torch_cuda}" if self.torch_cuda else "CPU"
        available = "available" if self.torch_cuda_available else "unavailable"
        return f"PyTorch {self.torch_version} ({flavor}, CUDA {available})"


def qwen_runtime_dir(base_dir: Path | str | None = None) -> Path:
    root = Path(base_dir).expanduser() if base_dir else RUNTIME_PATH
    return root / QWEN_RUNTIME_NAME


def qwen_python_executable(runtime_dir: Path | str | None = None) -> Path:
    root = qwen_runtime_dir(runtime_dir) if runtime_dir is None else Path(runtime_dir).expanduser()
    if os.name == "nt":
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


def qwen_site_packages(runtime_dir: Path | str | None = None) -> tuple[Path, ...]:
    root = qwen_runtime_dir(runtime_dir) if runtime_dir is None else Path(runtime_dir).expanduser()
    candidates: list[Path] = []
    if os.name == "nt":
        candidates.append(root / "Lib" / "site-packages")
    else:
        lib_dir = root / "lib"
        candidates.extend(sorted(lib_dir.glob("python*/site-packages")))
        candidates.append(lib_dir / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages")
    return tuple(dict.fromkeys(candidates))


def find_uv() -> str:
    return shutil.which("uv") or ""


def ensure_qwen_runtime_on_path(runtime_dir: Path | str | None = None) -> None:
    root = qwen_runtime_dir(runtime_dir) if runtime_dir is None else Path(runtime_dir).expanduser()
    bin_dir = root / ("Scripts" if os.name == "nt" else "bin")
    if bin_dir.exists():
        os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")

    for site_package in reversed(qwen_site_packages(root)):
        if site_package.exists():
            site_package_str = str(site_package)
            if site_package_str not in sys.path:
                sys.path.insert(0, site_package_str)


def qwen_asr_importable(runtime_dir: Path | str | None = None) -> bool:
    importlib.invalidate_caches()
    root = qwen_runtime_dir(runtime_dir) if runtime_dir is None else Path(runtime_dir).expanduser()
    site_package_paths = [str(path) for path in qwen_site_packages(root) if path.exists()]
    if not site_package_paths:
        return False

    spec = importlib.machinery.PathFinder.find_spec("qwen_asr", site_package_paths)
    if spec is None:
        return False

    ensure_qwen_runtime_on_path(root)
    return True


def inspect_qwen_runtime(runtime_dir: Path | str | None = None) -> QwenRuntimeStatus:
    root = qwen_runtime_dir(runtime_dir) if runtime_dir is None else Path(runtime_dir).expanduser()
    python = qwen_python_executable(root)
    site_packages = qwen_site_packages(root)
    has_venv = python.exists()
    importable = qwen_asr_importable(root)
    torch_info = _inspect_torch_runtime(python) if has_venv else {}
    return QwenRuntimeStatus(
        runtime_dir=root,
        python_executable=python,
        site_packages=site_packages,
        has_venv=has_venv,
        importable=importable,
        uv_executable=find_uv(),
        torch_version=str(torch_info.get("version", "")),
        torch_cuda=str(torch_info.get("cuda", "")),
        torch_cuda_available=bool(torch_info.get("cuda_available", False)),
        torch_error=str(torch_info.get("error", "")),
    )


def create_runtime_command(
    runtime_dir: Path | str | None = None,
    *,
    python: str = DEFAULT_QWEN_PYTHON,
    uv: str = "uv",
) -> list[str]:
    root = qwen_runtime_dir(runtime_dir) if runtime_dir is None else Path(runtime_dir).expanduser()
    return [uv, "venv", "--python", python, str(root)]


def install_runtime_command(
    runtime_dir: Path | str | None = None,
    *,
    requirements: Sequence[str] = QWEN_RUNTIME_REQUIREMENTS,
    uv: str = "uv",
    torch_backend: str | None = None,
) -> list[str]:
    python = qwen_python_executable(runtime_dir)
    command = [uv, "pip", "install", "--python", str(python), "--upgrade"]
    if torch_backend:
        command.extend(["--torch-backend", torch_backend])
    command.extend(requirements)
    return command


def install_torch_runtime_command(
    runtime_dir: Path | str | None = None,
    *,
    profile: QwenRuntimeProfile = "cpu",
    requirements: Sequence[str] = QWEN_TORCH_REQUIREMENTS,
    uv: str = "uv",
    cuda_index_url: str | None = None,
    torch_backend: str | None = None,
) -> list[str]:
    python = qwen_python_executable(runtime_dir)
    backend = torch_backend or _torch_backend_for_profile(profile)
    if backend:
        return [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            "--upgrade",
            "--reinstall-package",
            "torch",
            "--torch-backend",
            backend,
            *requirements,
        ]

    if profile == "cuda":
        index_url = (
            cuda_index_url
            or os.environ.get("VIDEOCAPTIONER_QWEN_CUDA_TORCH_INDEX_URL")
            or QWEN_TORCH_CUDA_INDEX_URL
        )
    else:
        index_url = QWEN_TORCH_CPU_INDEX_URL
    return [
        uv,
        "pip",
        "install",
        "--python",
        str(python),
        "--upgrade",
        "--reinstall-package",
        "torch",
        "--index-url",
        index_url,
        *requirements,
    ]


def install_qwen_runtime(
    runtime_dir: Path | str | None = None,
    *,
    python: str = DEFAULT_QWEN_PYTHON,
    profile: QwenRuntimeProfile = "cpu",
    requirements: Sequence[str] = QWEN_RUNTIME_REQUIREMENTS,
    torch_requirements: Sequence[str] = QWEN_TORCH_REQUIREMENTS,
    cuda_torch_index_url: str | None = None,
    progress: ProgressCallback | None = None,
    runner: Runner = subprocess.run,
) -> QwenRuntimeStatus:
    uv = find_uv()
    if not uv:
        raise RuntimeError(
            "uv was not found. Use the desktop release bundle or install uv before installing Qwen runtime."
        )

    root = qwen_runtime_dir(runtime_dir) if runtime_dir is None else Path(runtime_dir).expanduser()
    root.parent.mkdir(parents=True, exist_ok=True)

    def emit(message: str) -> None:
        if progress:
            progress(message)

    if _runtime_needs_rebuild(root):
        emit("Detected incomplete Qwen runtime; rebuilding")
        _remove_runtime_dir(root)

    torch_backend = _torch_backend_for_profile(profile)

    if not qwen_python_executable(root).exists():
        emit("Creating Qwen runtime environment")
        _run_command(
            create_runtime_command(root, python=python, uv=uv),
            runner,
            progress=emit,
            label="Creating Qwen runtime environment",
        )

    emit("Installing Qwen runtime dependencies")
    _run_command(
        install_runtime_command(
            root,
            requirements=requirements,
            uv=uv,
            torch_backend=torch_backend,
        ),
        runner,
        progress=emit,
        label="Installing Qwen runtime dependencies",
    )

    # Install the selected PyTorch flavor last. qwen-asr dependencies such as
    # accelerate can otherwise resolve PyPI's CPU torch and overwrite CUDA torch.
    if profile == "cuda":
        emit("Installing CUDA PyTorch runtime")
    else:
        emit("Installing CPU PyTorch runtime")
    _run_command(
        install_torch_runtime_command(
            root,
            profile=profile,
            requirements=torch_requirements,
            uv=uv,
            cuda_index_url=cuda_torch_index_url,
            torch_backend=torch_backend,
        ),
        runner,
        progress=emit,
        label=(
            "Installing CUDA PyTorch runtime"
            if profile == "cuda"
            else "Installing CPU PyTorch runtime"
        ),
    )

    ensure_qwen_runtime_on_path(root)
    status = inspect_qwen_runtime(root)
    if not status.has_venv:
        raise RuntimeError(
            "Qwen runtime installation is incomplete: Python executable was not created. "
            "Please retry installation from Qwen component manager."
        )
    if not status.importable:
        raise RuntimeError(
            "Qwen runtime installation is incomplete: qwen-asr is not importable. "
            "Please retry installation from Qwen component manager."
        )
    _validate_installed_profile(status, profile)
    return status


def _run_command(
    command: Iterable[str],
    runner: Runner,
    *,
    progress: ProgressCallback | None = None,
    label: str = "",
) -> None:
    command_list = list(command)
    if progress and runner is subprocess.run:
        _run_command_streaming(command_list, progress, label)
        return

    completed = runner(
        command_list,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if completed.returncode != 0:
        output = (completed.stdout or "").strip()
        raise RuntimeError(output or f"Command failed with exit code {completed.returncode}")


def _run_command_streaming(
    command: Sequence[str],
    progress: ProgressCallback,
    label: str,
) -> None:
    started = time.monotonic()
    output_lines: list[str] = []
    output_queue: queue.Queue[str | None] = queue.Queue()
    progress(f"{label}: starting")
    logger.info("%s command: %s", label, " ".join(command))

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    process = subprocess.Popen(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creationflags,
        env=_install_subprocess_env(),
    )

    def read_output() -> None:
        try:
            if process.stdout:
                for line in process.stdout:
                    output_queue.put(line)
        finally:
            output_queue.put(None)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()

    reader_done = False
    last_progress_at = time.monotonic()
    last_ui_progress_at = time.monotonic()
    pending_ui_progress = ""

    def emit_ui(message: str, *, force: bool = False) -> None:
        nonlocal last_ui_progress_at, pending_ui_progress
        now = time.monotonic()
        if force or now - last_ui_progress_at >= 0.5:
            progress(message)
            last_ui_progress_at = now
            pending_ui_progress = ""
        else:
            pending_ui_progress = message

    while not reader_done or process.poll() is None or not output_queue.empty():
        try:
            line = output_queue.get(timeout=0.2)
        except queue.Empty:
            now = time.monotonic()
            if pending_ui_progress and now - last_ui_progress_at >= 0.5:
                emit_ui(pending_ui_progress, force=True)
            if now - last_progress_at >= 5:
                elapsed = int(now - started)
                emit_ui(f"{label}: still running, elapsed {elapsed}s", force=True)
                last_progress_at = now
            continue

        if line is None:
            reader_done = True
            continue

        for cleaned in _clean_progress_line(line):
            output_lines.append(cleaned)
            logger.info("%s: %s", label, cleaned)
            emit_ui(
                f"{label}: {cleaned}",
                force=_is_important_progress_line(cleaned),
            )
            last_progress_at = time.monotonic()

    return_code = process.wait()
    reader.join(timeout=1)
    elapsed = int(time.monotonic() - started)
    if pending_ui_progress:
        emit_ui(pending_ui_progress, force=True)
    if return_code != 0:
        tail = "\n".join(output_lines[-80:]).strip()
        logger.error(
            "%s failed: return_code=%s, elapsed=%ss\n%s",
            label,
            return_code,
            elapsed,
            tail,
        )
        raise RuntimeError(_format_command_error(label, return_code, tail))
    progress(f"{label}: finished in {elapsed}s")
    logger.info("%s finished in %ss", label, elapsed)


def _clean_progress_line(line: str) -> list[str]:
    cleaned_lines = []
    for part in line.replace("\r", "\n").splitlines():
        text = part.strip()
        if not text:
            continue
        if len(text) > 240:
            text = text[:237] + "..."
        cleaned_lines.append(text)
    return cleaned_lines


def _install_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    # On Windows, uv's default hardlink/cache strategy can fail when files are
    # locked by AV/indexers or when the cache crosses volumes. Copy is slower
    # for large torch wheels but much more stable for GUI-driven installs.
    env["UV_LINK_MODE"] = os.environ.get("VIDEOCAPTIONER_QWEN_UV_LINK_MODE", "copy")
    env["UV_NO_PROGRESS"] = os.environ.get("VIDEOCAPTIONER_QWEN_UV_NO_PROGRESS", "1")
    return env


def _is_important_progress_line(line: str) -> bool:
    lower = line.lower()
    return any(
        token in lower
        for token in [
            "error",
            "failed",
            "caused by",
            "拒绝访问",
            "access is denied",
            "installed",
            "uninstalled",
            "would install",
            "would uninstall",
        ]
    )


def _format_command_error(label: str, return_code: int, output: str) -> str:
    text = output.strip() or f"{label} failed with exit code {return_code}"
    lowered = text.lower()
    access_denied = (
        "拒绝访问" in text
        or "access is denied" in lowered
        or "os error 5" in lowered
        or "0x80070005" in lowered
        or "-2147024891" in lowered
    )
    if access_denied:
        return (
            f"{label} failed: Windows 拒绝访问运行时文件。请关闭正在转录的任务和残留 "
            "python/uv 进程后重试；如果仍失败，可能是杀毒/索引器锁住了 AppData\\runtimes\\qwen。"
            f"完整安装输出见 {LOG_PATH / 'app.log'}。"
        )
    if len(text) > 2000:
        text = text[-2000:]
    return text


def _runtime_needs_rebuild(root: Path) -> bool:
    if not root.exists():
        return False
    return not qwen_python_executable(root).exists()


def _torch_backend_for_profile(profile: QwenRuntimeProfile) -> str:
    if profile == "cuda":
        return os.environ.get("VIDEOCAPTIONER_QWEN_TORCH_BACKEND") or QWEN_TORCH_CUDA_BACKEND
    return QWEN_TORCH_CPU_BACKEND


def _remove_runtime_dir(root: Path) -> None:
    resolved_root = root.resolve()
    if resolved_root.name != QWEN_RUNTIME_NAME:
        raise RuntimeError(f"Refusing to rebuild unexpected runtime directory: {resolved_root}")
    if root.is_symlink():
        raise RuntimeError(f"Refusing to rebuild symlinked runtime directory: {root}")
    shutil.rmtree(root)


def _validate_installed_profile(status: QwenRuntimeStatus, profile: QwenRuntimeProfile) -> None:
    if profile != "cuda":
        return
    if status.torch_error:
        raise RuntimeError(
            "Qwen CUDA runtime installation could not verify PyTorch. "
            f"Inspection error: {status.torch_error}. Please retry CUDA runtime installation."
        )
    if not status.torch_version:
        raise RuntimeError(
            "Qwen CUDA runtime installation is incomplete: PyTorch is not installed. "
            "Please retry CUDA runtime installation."
        )
    if not status.torch_cuda:
        raise RuntimeError(
            "Qwen CUDA runtime installation installed CPU PyTorch instead of CUDA PyTorch. "
            f"Current runtime: {status.torch_message}. Please retry CUDA runtime installation."
        )


def _inspect_torch_runtime(python: Path) -> dict:
    command = [
        str(python),
        "-c",
        (
            "import json\n"
            "try:\n"
            "    import torch\n"
            "    print(json.dumps({"
            "'version': getattr(torch, '__version__', ''), "
            "'cuda': getattr(torch.version, 'cuda', None) or '', "
            "'cuda_available': bool(torch.cuda.is_available())"
            "}))\n"
            "except Exception as exc:\n"
            "    print(json.dumps({'error': str(exc)}))\n"
        ),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
        )
    except Exception as exc:
        return {"error": str(exc)}
    output = (completed.stdout or "").strip().splitlines()
    if not output:
        return {"error": "torch inspection produced no output"}
    try:
        return json.loads(output[-1])
    except json.JSONDecodeError:
        return {"error": output[-1][:500]}
