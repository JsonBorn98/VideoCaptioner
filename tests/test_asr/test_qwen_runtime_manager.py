import subprocess
import sys

import pytest

from videocaptioner.core.asr import qwen_runtime_manager as qrm


@pytest.fixture(autouse=True)
def restore_sys_path(monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))


def test_qwen_runtime_dir_uses_named_child(tmp_path):
    assert qrm.qwen_runtime_dir(tmp_path) == tmp_path / "qwen"


def test_create_and_install_commands_use_uv_and_runtime_python(tmp_path):
    runtime = tmp_path / "qwen"

    assert qrm.create_runtime_command(runtime, python="3.12", uv="uv-bin") == [
        "uv-bin",
        "venv",
        "--python",
        "3.12",
        str(runtime),
    ]

    install_command = qrm.install_runtime_command(runtime, requirements=("qwen-asr",), uv="uv-bin")
    assert install_command[:4] == ["uv-bin", "pip", "install", "--python"]
    assert install_command[-1] == "qwen-asr"
    assert str(qrm.qwen_python_executable(runtime)) in install_command

    cuda_torch_command = qrm.install_torch_runtime_command(
        runtime,
        profile="cuda",
        requirements=("torch",),
        uv="uv-bin",
        cuda_index_url="https://example.invalid/cu",
    )
    assert cuda_torch_command[:4] == ["uv-bin", "pip", "install", "--python"]
    assert "--reinstall-package" in cuda_torch_command
    assert "--torch-backend" in cuda_torch_command
    assert "cu128" in cuda_torch_command
    assert cuda_torch_command[-1] == "torch"


def test_ensure_qwen_runtime_on_path_makes_managed_package_importable(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    runtime = tmp_path / "qwen"
    site_packages = qrm.qwen_site_packages(runtime)[0]
    package = site_packages / "qwen_asr"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")

    assert qrm.qwen_asr_importable(runtime) is True
    assert str(site_packages) in sys.path


def test_install_qwen_runtime_runs_venv_then_pip_and_reports_ready(tmp_path, monkeypatch):
    runtime = tmp_path / "qwen"
    commands = []
    messages = []

    monkeypatch.setattr(qrm, "find_uv", lambda: "uv-bin")

    def fake_runner(command, **kwargs):
        commands.append(command)
        if command[:2] == ["uv-bin", "venv"]:
            python = qrm.qwen_python_executable(runtime)
            python.parent.mkdir(parents=True)
            python.write_text("", encoding="utf-8")
        if command[:3] == ["uv-bin", "pip", "install"] and command[-1] == "qwen-asr":
            package = qrm.qwen_site_packages(runtime)[0] / "qwen_asr"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
        if command[:3] == ["uv-bin", "pip", "install"] and command[-1] == "torch":
            package = qrm.qwen_site_packages(runtime)[0] / "torch"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("__version__ = '2.0.0+cpu'\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="ok")

    status = qrm.install_qwen_runtime(runtime, progress=messages.append, runner=fake_runner)

    assert commands[0][:3] == ["uv-bin", "venv", "--python"]
    assert commands[1][:4] == ["uv-bin", "pip", "install", "--python"]
    assert commands[1][-1] == "qwen-asr"
    assert "--torch-backend" in commands[1]
    assert "cpu" in commands[1]
    assert commands[2][:4] == ["uv-bin", "pip", "install", "--python"]
    assert commands[2][-1] == "torch"
    assert "--torch-backend" in commands[2]
    assert "cpu" in commands[2]
    assert "Creating Qwen runtime environment" in messages
    assert "Installing CPU PyTorch runtime" in messages
    assert "Installing Qwen runtime dependencies" in messages
    assert status.ready is True


def test_install_qwen_runtime_cuda_installs_cuda_torch_last(tmp_path, monkeypatch):
    runtime = tmp_path / "qwen"
    commands = []

    monkeypatch.setattr(qrm, "find_uv", lambda: "uv-bin")
    monkeypatch.setattr(
        qrm,
        "_inspect_torch_runtime",
        lambda python: {"version": "2.11.0+cu128", "cuda": "12.8", "cuda_available": True},
    )

    def fake_runner(command, **kwargs):
        commands.append(command)
        if command[:2] == ["uv-bin", "venv"]:
            python = qrm.qwen_python_executable(runtime)
            python.parent.mkdir(parents=True)
            python.write_text("", encoding="utf-8")
        if command[:3] == ["uv-bin", "pip", "install"] and command[-1] == "qwen-asr":
            package = qrm.qwen_site_packages(runtime)[0] / "qwen_asr"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="ok")

    status = qrm.install_qwen_runtime(
        runtime,
        profile="cuda",
        cuda_torch_index_url="https://example.invalid/cu",
        runner=fake_runner,
    )

    assert commands[1][-1] == "qwen-asr"
    assert "--torch-backend" in commands[1]
    assert "cu128" in commands[1]
    assert commands[2][-1] == "torch"
    assert "--torch-backend" in commands[2]
    assert "cu128" in commands[2]
    assert status.ready is True
    assert status.torch_cuda == "12.8"


def test_install_qwen_runtime_cuda_fails_if_cpu_torch_was_installed(tmp_path, monkeypatch):
    runtime = tmp_path / "qwen"

    monkeypatch.setattr(qrm, "find_uv", lambda: "uv-bin")
    monkeypatch.setattr(
        qrm,
        "_inspect_torch_runtime",
        lambda python: {"version": "2.12.1+cpu", "cuda": "", "cuda_available": False},
    )

    def fake_runner(command, **kwargs):
        if command[:2] == ["uv-bin", "venv"]:
            python = qrm.qwen_python_executable(runtime)
            python.parent.mkdir(parents=True)
            python.write_text("", encoding="utf-8")
        if command[:3] == ["uv-bin", "pip", "install"] and command[-1] == "qwen-asr":
            package = qrm.qwen_site_packages(runtime)[0] / "qwen_asr"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="ok")

    try:
        qrm.install_qwen_runtime(runtime, profile="cuda", runner=fake_runner)
    except RuntimeError as exc:
        assert "CPU PyTorch instead of CUDA PyTorch" in str(exc)
    else:
        raise AssertionError("CUDA runtime install should fail when CPU torch is installed")


def test_cuda_profile_validation_reports_torch_inspection_error(tmp_path):
    status = qrm.QwenRuntimeStatus(
        runtime_dir=tmp_path / "qwen",
        python_executable=tmp_path / "qwen" / "Scripts" / "python.exe",
        site_packages=(),
        has_venv=True,
        importable=True,
        uv_executable="uv",
        torch_error="timed out",
    )

    try:
        qrm._validate_installed_profile(status, "cuda")
    except RuntimeError as exc:
        assert "could not verify PyTorch" in str(exc)
        assert "timed out" in str(exc)
    else:
        raise AssertionError("CUDA validation should fail with the inspection error")


def test_install_qwen_runtime_rebuilds_incomplete_runtime(tmp_path, monkeypatch):
    runtime = tmp_path / "qwen"
    stale_file = runtime / "Lib" / "site-packages" / "torch" / "__init__.py"
    stale_file.parent.mkdir(parents=True)
    stale_file.write_text("__version__ = 'stale'\n", encoding="utf-8")
    commands = []
    messages = []

    monkeypatch.setattr(qrm, "find_uv", lambda: "uv-bin")

    def fake_runner(command, **kwargs):
        commands.append(command)
        if command[:2] == ["uv-bin", "venv"]:
            python = qrm.qwen_python_executable(runtime)
            python.parent.mkdir(parents=True)
            python.write_text("", encoding="utf-8")
        if command[:3] == ["uv-bin", "pip", "install"] and command[-1] == "qwen-asr":
            package = qrm.qwen_site_packages(runtime)[0] / "qwen_asr"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="ok")

    status = qrm.install_qwen_runtime(runtime, progress=messages.append, runner=fake_runner)

    assert not stale_file.exists()
    assert commands[0][:2] == ["uv-bin", "venv"]
    assert "Detected incomplete Qwen runtime; rebuilding" in messages
    assert status.ready is True


def test_install_qwen_runtime_fails_if_qwen_asr_is_still_missing(tmp_path, monkeypatch):
    runtime = tmp_path / "qwen"

    monkeypatch.setattr(qrm, "find_uv", lambda: "uv-bin")

    def fake_runner(command, **kwargs):
        if command[:2] == ["uv-bin", "venv"]:
            python = qrm.qwen_python_executable(runtime)
            python.parent.mkdir(parents=True)
            python.write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="ok")

    try:
        qrm.install_qwen_runtime(runtime, runner=fake_runner)
    except RuntimeError as exc:
        assert "qwen-asr is not importable" in str(exc)
    else:
        raise AssertionError("install_qwen_runtime should fail if qwen-asr is missing")


def test_install_qwen_runtime_fails_without_uv(tmp_path, monkeypatch):
    monkeypatch.setattr(qrm, "find_uv", lambda: "")

    try:
        qrm.install_qwen_runtime(tmp_path / "qwen")
    except RuntimeError as exc:
        assert "uv was not found" in str(exc)
    else:
        raise AssertionError("install_qwen_runtime should fail without uv")


def test_clean_progress_line_splits_carriage_return_progress():
    assert qrm._clean_progress_line("Downloading torch 1%\rDownloading torch 2%\n") == [
        "Downloading torch 1%",
        "Downloading torch 2%",
    ]

    long_line = "x" * 300
    cleaned = qrm._clean_progress_line(long_line)
    assert len(cleaned[0]) == 240
    assert cleaned[0].endswith("...")


def test_streaming_command_reports_output_and_finish():
    messages = []

    qrm._run_command_streaming(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "print('step one')\n"
                "sys.stderr.write('step two\\n')\n"
            ),
        ],
        messages.append,
        "Installing test runtime",
    )

    assert "Installing test runtime: starting" in messages
    assert any(message in {
        "Installing test runtime: step one",
        "Installing test runtime: step two",
    } for message in messages)
    assert any(message.startswith("Installing test runtime: finished in ") for message in messages)


def test_install_subprocess_env_prefers_copy_link_mode(monkeypatch):
    monkeypatch.delenv("VIDEOCAPTIONER_QWEN_UV_LINK_MODE", raising=False)
    monkeypatch.delenv("VIDEOCAPTIONER_QWEN_UV_NO_PROGRESS", raising=False)

    env = qrm._install_subprocess_env()

    assert env["UV_LINK_MODE"] == "copy"
    assert env["UV_NO_PROGRESS"] == "1"
