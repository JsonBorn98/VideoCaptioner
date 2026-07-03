import subprocess
import sys

from videocaptioner.core.asr import qwen_runtime_manager as qrm


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
        if command[:3] == ["uv-bin", "pip", "install"]:
            package = qrm.qwen_site_packages(runtime)[0] / "qwen_asr"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="ok")

    status = qrm.install_qwen_runtime(runtime, progress=messages.append, runner=fake_runner)

    assert commands[0][:3] == ["uv-bin", "venv", "--python"]
    assert commands[1][:4] == ["uv-bin", "pip", "install", "--python"]
    assert "Creating Qwen runtime environment" in messages
    assert "Installing Qwen runtime dependencies" in messages
    assert status.ready is True


def test_install_qwen_runtime_fails_without_uv(tmp_path, monkeypatch):
    monkeypatch.setattr(qrm, "find_uv", lambda: "")

    try:
        qrm.install_qwen_runtime(tmp_path / "qwen")
    except RuntimeError as exc:
        assert "uv was not found" in str(exc)
    else:
        raise AssertionError("install_qwen_runtime should fail without uv")
