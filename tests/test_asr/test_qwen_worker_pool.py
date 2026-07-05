import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

import videocaptioner.core.asr.qwen_local_asr as qwen_local_module
from videocaptioner.core.asr.qwen_local_asr import (
    QwenWorkerError,
    QwenWorkerPool,
    _materialize_audio_input,
    _run_qwen_alignment_worker_oneshot,
    _run_qwen_worker_oneshot,
)


def _write_fake_worker(tmp_path, body: str):
    script = tmp_path / "fake_qwen_worker.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return script


def test_qwen_worker_pool_reuses_one_process_for_multiple_requests(tmp_path):
    script = _write_fake_worker(
        tmp_path,
        """
        import json
        import os
        import sys

        count = 0
        for line in sys.stdin:
            envelope = json.loads(line)
            if envelope.get("op") == "shutdown":
                print(json.dumps({"id": envelope.get("id"), "result": {"ok": True}}), flush=True)
                break
            count += 1
            print(
                json.dumps(
                    {
                        "id": envelope["id"],
                        "result": {
                            "pid": os.getpid(),
                            "count": count,
                            "mode": envelope.get("mode"),
                        },
                    }
                ),
                flush=True,
            )
        """,
    )
    pool = QwenWorkerPool(
        command=[sys.executable, str(script)],
        cwd=str(tmp_path),
        env=os.environ.copy(),
        stderr_path=tmp_path / "worker.stderr.log",
    )

    try:
        first = pool.request(mode="transcribe", request={})
        second = pool.request(mode="align", request={})
    finally:
        pool.close()

    assert first["pid"] == second["pid"]
    assert first["count"] == 1
    assert second["count"] == 2
    assert second["mode"] == "align"


def test_qwen_worker_pool_restarts_after_worker_crash(tmp_path):
    marker = tmp_path / "crashed.once"
    script = _write_fake_worker(
        tmp_path,
        f"""
        import json
        import os
        import pathlib
        import sys

        marker = pathlib.Path({str(marker)!r})
        for line in sys.stdin:
            envelope = json.loads(line)
            if envelope.get("request", {{}}).get("crash_once") and not marker.exists():
                marker.write_text("yes", encoding="utf-8")
                sys.exit(7)
            print(
                json.dumps(
                    {{"id": envelope["id"], "result": {{"pid": os.getpid(), "ok": True}}}}
                ),
                flush=True,
            )
        """,
    )
    pool = QwenWorkerPool(
        command=[sys.executable, str(script)],
        cwd=str(tmp_path),
        env=os.environ.copy(),
        stderr_path=tmp_path / "worker.stderr.log",
    )

    try:
        result = pool.request(mode="transcribe", request={"crash_once": True})
    finally:
        pool.close()

    assert result["ok"] is True
    assert marker.exists()


def test_qwen_worker_pool_terminates_when_callback_cancels(monkeypatch, tmp_path):
    script = _write_fake_worker(
        tmp_path,
        """
        import json
        import sys
        import time

        for line in sys.stdin:
            json.loads(line)
            time.sleep(30)
        """,
    )
    pool = QwenWorkerPool(
        command=[sys.executable, str(script)],
        cwd=str(tmp_path),
        env=os.environ.copy(),
        stderr_path=tmp_path / "worker.stderr.log",
    )

    class Cancelled(Exception):
        pass

    def cancel_callback(progress, message):
        raise Cancelled()

    monkeypatch.setattr(qwen_local_module, "QWEN_WORKER_HEARTBEAT_SECONDS", 0)

    try:
        with pytest.raises(Cancelled):
            pool.request(mode="transcribe", request={}, callback=cancel_callback)
        assert pool._process is None
    finally:
        pool.close()


def test_materialize_audio_input_preserves_wav_suffix(tmp_path):
    wav_bytes = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32

    path, cleanup_path = _materialize_audio_input(
        wav_bytes,
        tmp_path,
        prefix="chunk",
    )

    assert path.endswith(".wav")
    assert cleanup_path is not None
    assert cleanup_path.read_bytes() == wav_bytes


class _FinishedProcess:
    def __init__(self, return_code: int):
        self.return_code = return_code
        self.pid = 12345

    def poll(self):
        return self.return_code

    def wait(self, timeout=None):
        return self.return_code


def _fake_failed_popen(command, **kwargs):
    output_path = Path(command[command.index("--output") + 1])
    output_path.write_text(
        json.dumps({"error": "worker boom"}, ensure_ascii=False),
        encoding="utf-8",
    )
    return _FinishedProcess(1)


def test_qwen_oneshot_worker_cleans_auto_temp_dir_on_failure(monkeypatch, tmp_path):
    temp_root = tmp_path / "auto-qwen-worker"
    monkeypatch.setattr(qwen_local_module.tempfile, "mkdtemp", lambda: str(temp_root))
    monkeypatch.setattr(qwen_local_module.subprocess, "Popen", _fake_failed_popen)

    with pytest.raises(QwenWorkerError, match="worker boom"):
        _run_qwen_worker_oneshot(
            audio_input=b"fake wav",
            language="en",
            asr_model="Qwen/Qwen3-ASR-0.6B",
            aligner_model="Qwen/Qwen3-ForcedAligner-0.6B",
            device="cpu",
            return_time_stamps=False,
        )

    assert not temp_root.exists()


def test_qwen_oneshot_alignment_cleans_auto_temp_dir_on_failure(
    monkeypatch,
    tmp_path,
):
    temp_root = tmp_path / "auto-qwen-align-worker"
    monkeypatch.setattr(qwen_local_module.tempfile, "mkdtemp", lambda: str(temp_root))
    monkeypatch.setattr(qwen_local_module.subprocess, "Popen", _fake_failed_popen)

    with pytest.raises(QwenWorkerError, match="worker boom"):
        _run_qwen_alignment_worker_oneshot(
            audio_input=b"fake wav",
            transcript="hello",
            language="en",
            aligner_model="Qwen/Qwen3-ForcedAligner-0.6B",
            device="cpu",
        )

    assert not temp_root.exists()
