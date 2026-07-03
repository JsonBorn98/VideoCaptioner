"""Managed runtime support for optional Qwen ASR dependencies."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from videocaptioner.config import RUNTIME_PATH

QWEN_RUNTIME_NAME = "qwen"
QWEN_RUNTIME_REQUIREMENTS = ("qwen-asr",)
DEFAULT_QWEN_PYTHON = "3.12"

ProgressCallback = Callable[[str], None]
Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class QwenRuntimeStatus:
    runtime_dir: Path
    python_executable: Path
    site_packages: tuple[Path, ...]
    has_venv: bool
    importable: bool
    uv_executable: str

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
    ensure_qwen_runtime_on_path(runtime_dir)
    return importlib.util.find_spec("qwen_asr") is not None


def inspect_qwen_runtime(runtime_dir: Path | str | None = None) -> QwenRuntimeStatus:
    root = qwen_runtime_dir(runtime_dir) if runtime_dir is None else Path(runtime_dir).expanduser()
    python = qwen_python_executable(root)
    site_packages = qwen_site_packages(root)
    has_venv = python.exists()
    importable = qwen_asr_importable(root)
    return QwenRuntimeStatus(
        runtime_dir=root,
        python_executable=python,
        site_packages=site_packages,
        has_venv=has_venv,
        importable=importable,
        uv_executable=find_uv(),
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
) -> list[str]:
    python = qwen_python_executable(runtime_dir)
    return [uv, "pip", "install", "--python", str(python), *requirements]


def install_qwen_runtime(
    runtime_dir: Path | str | None = None,
    *,
    python: str = DEFAULT_QWEN_PYTHON,
    requirements: Sequence[str] = QWEN_RUNTIME_REQUIREMENTS,
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

    if not qwen_python_executable(root).exists():
        emit("Creating Qwen runtime environment")
        _run_command(create_runtime_command(root, python=python, uv=uv), runner)

    emit("Installing Qwen runtime dependencies")
    _run_command(install_runtime_command(root, requirements=requirements, uv=uv), runner)
    ensure_qwen_runtime_on_path(root)
    return inspect_qwen_runtime(root)


def _run_command(command: Iterable[str], runner: Runner) -> None:
    completed = runner(
        list(command),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if completed.returncode != 0:
        output = (completed.stdout or "").strip()
        raise RuntimeError(output or f"Command failed with exit code {completed.returncode}")
