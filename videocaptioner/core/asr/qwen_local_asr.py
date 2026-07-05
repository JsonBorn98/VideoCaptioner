"""Local Qwen3 ASR backend with Qwen3-ForcedAligner timestamps."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, List, Optional, Union

from ..utils.cache import is_cache_enabled
from ..utils.logger import setup_logger
from .anomaly import (
    MIN_ALIGNMENT_COVERAGE,
    alignment_coverage,
    alignment_problems,
    check_transcript_anomaly,
    clamp_segments_to_duration,
)
from .asr_data import ASRData, ASRDataSeg
from .base import ASRResultDegradedError, BaseASR
from .qwen_runtime import timestamp_items_to_segments
from .text_timing import make_timed_segments, split_transcript_text

logger = setup_logger("qwen_local_asr")

QWEN_WORKER_HEARTBEAT_SECONDS = 2.0
QWEN_WORKER_POLL_SECONDS = 0.25
QWEN_WORKER_MAX_RESTARTS = 1


class QwenWorkerError(RuntimeError):
    """Raised when the isolated Qwen worker process fails."""


class QwenWorkerProcessExited(QwenWorkerError):
    """Raised when the persistent Qwen worker exits before returning a response."""


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _worker_env() -> dict[str, str]:
    project_root = _project_root()
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PATH"] = _without_qt_dll_paths(env.get("PATH", ""))
    env["PYTHONPATH"] = (
        str(project_root)
        + os.pathsep
        + env["PYTHONPATH"]
        if env.get("PYTHONPATH")
        else str(project_root)
    )
    return env


def _worker_process_options() -> tuple[int, bool]:
    creationflags = 0
    start_new_session = False
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        start_new_session = True
    return creationflags, start_new_session


def _audio_suffix_from_bytes(audio_bytes: bytes) -> str:
    if audio_bytes.startswith(b"RIFF") and audio_bytes[8:12] == b"WAVE":
        return ".wav"
    if audio_bytes.startswith(b"fLaC"):
        return ".flac"
    if audio_bytes.startswith(b"ID3") or audio_bytes[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}:
        return ".mp3"
    return ".bin"


def _materialize_audio_input(
    audio_input: Union[str, bytes],
    temp_root: Path,
    *,
    prefix: str,
) -> tuple[str, Path | None]:
    if isinstance(audio_input, str):
        return audio_input, None

    suffix = _audio_suffix_from_bytes(audio_input)
    audio_path = temp_root / f"{prefix}-{uuid.uuid4().hex}{suffix}"
    audio_path.write_bytes(audio_input)
    return str(audio_path), audio_path


class QwenWorkerPool:
    """Single-process persistent Qwen worker using JSON Lines over stdio."""

    def __init__(
        self,
        *,
        command: list[str] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        stderr_path: Path | None = None,
    ):
        self.command = command
        self.cwd = cwd
        self.env = env
        self.stderr_path = stderr_path
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._responses: queue.Queue[dict[str, Any]] = queue.Queue()
        self._reader_thread: threading.Thread | None = None
        self._stderr_handle: Any = None
        self._started_at = 0.0

    def request(
        self,
        *,
        mode: str,
        request: dict[str, Any],
        callback: Optional[Callable[[int, str], None]] = None,
        heartbeat_progress: int = 10,
        heartbeat_message: str = "Qwen ASR worker is running",
    ) -> dict[str, Any]:
        with self._lock:
            for attempt in range(QWEN_WORKER_MAX_RESTARTS + 1):
                try:
                    self._start_locked()
                    self._discard_stale_responses()
                    request_id = uuid.uuid4().hex
                    self._send_locked(
                        {
                            "id": request_id,
                            "mode": mode,
                            "request": request,
                        }
                    )
                    response = self._wait_for_response_locked(
                        request_id,
                        callback=callback,
                        heartbeat_progress=heartbeat_progress,
                        heartbeat_message=heartbeat_message,
                    )
                except QwenWorkerProcessExited:
                    self._terminate_locked()
                    if attempt < QWEN_WORKER_MAX_RESTARTS:
                        logger.warning(
                            "Qwen worker exited during request; restarting once"
                        )
                        continue
                    raise
                except BaseException:
                    self._terminate_locked()
                    raise

                if "error" in response:
                    detail = response.get("traceback") or self._stderr_tail()
                    if detail:
                        logger.info("Qwen worker failure detail:\n%s", detail)
                    raise QwenWorkerError(str(response.get("error", "")).strip())

                result = response.get("result")
                if not isinstance(result, dict):
                    raise QwenWorkerError("Qwen worker returned an invalid result.")
                return result

        raise QwenWorkerError("Qwen worker request did not complete.")

    def close(self) -> None:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                self._terminate_locked()
                return
            try:
                self._send_locked({"id": uuid.uuid4().hex, "op": "shutdown"})
                process.wait(timeout=3)
            except Exception:
                self._terminate_locked()
            finally:
                self._close_stderr_locked()

    def terminate(self) -> None:
        with self._lock:
            self._terminate_locked()

    def _start_locked(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return

        self._close_stderr_locked()
        self._responses = queue.Queue()
        project_root = _project_root()
        command = self.command or [
            sys.executable,
            "-m",
            "videocaptioner.core.asr.qwen_worker",
            "--serve",
        ]
        cwd = self.cwd or str(project_root)
        env = self.env or _worker_env()
        stderr_path = self.stderr_path or (
            Path(tempfile.gettempdir()) / f"qwen-worker-{uuid.uuid4().hex}.stderr.log"
        )
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        self.stderr_path = stderr_path
        self._stderr_handle = stderr_path.open("a", encoding="utf-8")
        creationflags, start_new_session = _worker_process_options()

        logger.info("Qwen persistent worker 启动: command=%s", " ".join(command))
        self._process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr_handle,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
        self._started_at = time.perf_counter()
        self._reader_thread = threading.Thread(
            target=self._read_stdout,
            args=(self._process,),
            name="qwen-worker-stdout-reader",
            daemon=True,
        )
        self._reader_thread.start()

    def _send_locked(self, payload: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise QwenWorkerProcessExited("Qwen worker is not running.")
        try:
            self._process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise QwenWorkerProcessExited("Qwen worker pipe is closed.") from exc

    def _wait_for_response_locked(
        self,
        request_id: str,
        *,
        callback: Optional[Callable[[int, str], None]],
        heartbeat_progress: int,
        heartbeat_message: str,
    ) -> dict[str, Any]:
        last_heartbeat = 0.0
        while True:
            if self._process is None or self._process.poll() is not None:
                elapsed = time.perf_counter() - self._started_at
                raise QwenWorkerProcessExited(
                    f"Qwen worker exited before response after {elapsed:.2f}s."
                )

            now = time.monotonic()
            if callback and now - last_heartbeat >= QWEN_WORKER_HEARTBEAT_SECONDS:
                last_heartbeat = now
                callback(heartbeat_progress, heartbeat_message)

            try:
                response = self._responses.get(timeout=QWEN_WORKER_POLL_SECONDS)
            except queue.Empty:
                continue
            if response.get("id") == request_id:
                return response
            logger.debug("Ignoring stale Qwen worker response: %s", response.get("id"))

    def _read_stdout(self, process: subprocess.Popen) -> None:
        if process.stdout is None:
            return
        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                logger.info("Qwen worker stdout: %s", line)
                continue
            if isinstance(payload, dict):
                self._responses.put(payload)

    def _discard_stale_responses(self) -> None:
        while True:
            try:
                self._responses.get_nowait()
            except queue.Empty:
                return

    def _terminate_locked(self) -> None:
        process = self._process
        self._process = None
        if process is not None:
            _terminate_worker(process)
        self._close_stderr_locked()

    def _close_stderr_locked(self) -> None:
        if self._stderr_handle is not None:
            try:
                self._stderr_handle.close()
            except OSError:
                pass
            self._stderr_handle = None

    def _stderr_tail(self) -> str:
        if self.stderr_path is None:
            return ""
        return _read_tail(self.stderr_path)


_qwen_worker_pool: QwenWorkerPool | None = None
_qwen_worker_pool_lock = threading.Lock()


def get_qwen_worker_pool() -> QwenWorkerPool:
    global _qwen_worker_pool
    with _qwen_worker_pool_lock:
        if _qwen_worker_pool is None:
            _qwen_worker_pool = QwenWorkerPool()
        return _qwen_worker_pool


def close_qwen_worker_pool() -> None:
    with _qwen_worker_pool_lock:
        if _qwen_worker_pool is not None:
            _qwen_worker_pool.close()


def _cleanup_worker_temp_files(
    *,
    temp_root: Path,
    audio_path: Path | None,
    cleanup_temp_root: bool,
) -> None:
    if audio_path:
        audio_path.unlink(missing_ok=True)
    if cleanup_temp_root:
        shutil.rmtree(temp_root, ignore_errors=True)


def _run_qwen_worker_oneshot(
    *,
    audio_input: Union[str, bytes],
    language: str,
    asr_model: str,
    aligner_model: str,
    model_dir: str = "",
    device: str = "auto",
    dtype: str = "auto",
    max_new_tokens: int = 2048,
    max_inference_batch_size: int = 0,
    return_time_stamps: bool = True,
    temp_dir: str = "",
    clip_start_ms: int | None = None,
    clip_duration_ms: int | None = None,
    compile_aligner: bool = False,
    callback: Optional[Callable[[int, str], None]] = None,
) -> dict:
    """Run Qwen ASR in a child process to isolate torch/CUDA from the Qt process."""
    temp_root = Path(temp_dir).expanduser() if temp_dir else Path(tempfile.mkdtemp())
    temp_root.mkdir(parents=True, exist_ok=True)

    cleanup_temp_root = not temp_dir
    worker_audio_path, audio_path = _materialize_audio_input(
        audio_input,
        temp_root,
        prefix="qwen-worker-input",
    )

    request_path = temp_root / "qwen-worker-request.json"
    output_path = temp_root / "qwen-worker-output.json"
    stdout_path = temp_root / "qwen-worker-stdout.log"
    stderr_path = temp_root / "qwen-worker-stderr.log"

    request: dict[str, Any] = {
        "audio_path": worker_audio_path,
        "language": language,
        "asr_model": asr_model,
        "aligner_model": aligner_model,
        "model_dir": model_dir,
        "device": device,
        "dtype": dtype,
        "max_new_tokens": int(max_new_tokens),
        "max_inference_batch_size": int(max_inference_batch_size or 0),
        "return_time_stamps": bool(return_time_stamps),
        "compile_aligner": bool(compile_aligner),
        "temp_dir": str(temp_root),
    }
    if clip_start_ms is not None:
        request["clip_start_ms"] = int(clip_start_ms)
    if clip_duration_ms is not None:
        request["clip_duration_ms"] = int(clip_duration_ms)
    request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    output_path.unlink(missing_ok=True)

    project_root = Path(__file__).resolve().parents[3]
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PATH"] = _without_qt_dll_paths(env.get("PATH", ""))
    env["PYTHONPATH"] = (
        str(project_root)
        + os.pathsep
        + env["PYTHONPATH"]
        if env.get("PYTHONPATH")
        else str(project_root)
    )

    creationflags = 0
    start_new_session = False
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        start_new_session = True

    command = [
        sys.executable,
        "-m",
        "videocaptioner.core.asr.qwen_worker",
        "--request",
        str(request_path),
        "--output",
        str(output_path),
    ]

    worker_started = time.perf_counter()
    logger.info(
        "Qwen worker 启动: command=%s, request=%s, output=%s, stdout=%s, stderr=%s",
        " ".join(command),
        request_path,
        output_path,
        stdout_path,
        stderr_path,
    )
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
        "w",
        encoding="utf-8",
    ) as stderr:
        process = subprocess.Popen(
            command,
            cwd=str(project_root),
            env=env,
            stdout=stdout,
            stderr=stderr,
            text=True,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )

        last_heartbeat = 0.0
        try:
            while process.poll() is None:
                now = time.monotonic()
                if callback and now - last_heartbeat >= 2:
                    last_heartbeat = now
                    callback(10, "Qwen ASR worker is running")
                time.sleep(0.25)
        except BaseException:
            _terminate_worker(process)
            _cleanup_worker_temp_files(
                temp_root=temp_root,
                audio_path=audio_path,
                cleanup_temp_root=cleanup_temp_root,
            )
            raise

        return_code = process.wait()

    worker_elapsed = time.perf_counter() - worker_started
    logger.info(
        "Qwen worker 退出: return_code=%s, elapsed=%.2fs, output_exists=%s",
        return_code,
        worker_elapsed,
        output_path.exists(),
    )
    _log_worker_streams(stdout_path, stderr_path, return_code)

    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except Exception as exc:
        payload = {}
        if return_code == 0:
            _cleanup_worker_temp_files(
                temp_root=temp_root,
                audio_path=audio_path,
                cleanup_temp_root=cleanup_temp_root,
            )
            raise QwenWorkerError("Qwen worker finished without a valid result file.") from exc

    if return_code != 0 or "error" in payload:
        error = payload.get("error") or f"Qwen worker exited with code {return_code}"
        detail = payload.get("traceback") or _read_tail(stderr_path) or _read_tail(stdout_path)
        if detail:
            logger.info("Qwen worker failure detail:\n%s", detail)
        logger.error(
            "Qwen worker 失败: return_code=%s, elapsed=%.2fs, error=%s",
            return_code,
            worker_elapsed,
            error,
            extra={"suppress_console": True},
        )
        _cleanup_worker_temp_files(
            temp_root=temp_root,
            audio_path=audio_path,
            cleanup_temp_root=cleanup_temp_root,
        )
        raise QwenWorkerError(str(error).strip())

    result = payload.get("result")
    if not isinstance(result, dict):
        _cleanup_worker_temp_files(
            temp_root=temp_root,
            audio_path=audio_path,
            cleanup_temp_root=cleanup_temp_root,
        )
        raise QwenWorkerError("Qwen worker returned an invalid result.")

    _cleanup_worker_temp_files(
        temp_root=temp_root,
        audio_path=audio_path,
        cleanup_temp_root=cleanup_temp_root,
    )

    logger.info("Qwen worker 完成: elapsed=%.2fs", worker_elapsed)
    return result


def _run_qwen_alignment_worker_oneshot(
    *,
    audio_input: Union[str, bytes],
    transcript: str,
    language: str,
    aligner_model: str,
    model_dir: str = "",
    device: str = "auto",
    dtype: str = "auto",
    temp_dir: str = "",
    clip_start_ms: int | None = None,
    clip_duration_ms: int | None = None,
    compile_aligner: bool = False,
    callback: Optional[Callable[[int, str], None]] = None,
) -> list[dict[str, float | str]]:
    """Run Qwen3-ForcedAligner in a child process.

    MiMo ASR only returns transcript text. Keeping the aligner in the worker
    avoids importing torch/CUDA DLLs in the PyQt process.
    """
    temp_root = Path(temp_dir).expanduser() if temp_dir else Path(tempfile.mkdtemp())
    temp_root.mkdir(parents=True, exist_ok=True)

    cleanup_temp_root = not temp_dir
    worker_audio_path, audio_path = _materialize_audio_input(
        audio_input,
        temp_root,
        prefix="qwen-align-worker-input",
    )

    request_path = temp_root / "qwen-align-worker-request.json"
    output_path = temp_root / "qwen-align-worker-output.json"
    stdout_path = temp_root / "qwen-align-worker-stdout.log"
    stderr_path = temp_root / "qwen-align-worker-stderr.log"

    request: dict[str, Any] = {
        "audio_path": worker_audio_path,
        "transcript": transcript,
        "language": language,
        "aligner_model": aligner_model,
        "model_dir": model_dir,
        "device": device,
        "dtype": dtype,
        "compile_aligner": bool(compile_aligner),
        "temp_dir": str(temp_root),
    }
    if clip_start_ms is not None:
        request["clip_start_ms"] = int(clip_start_ms)
    if clip_duration_ms is not None:
        request["clip_duration_ms"] = int(clip_duration_ms)
    request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    output_path.unlink(missing_ok=True)

    project_root = Path(__file__).resolve().parents[3]
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PATH"] = _without_qt_dll_paths(env.get("PATH", ""))
    env["PYTHONPATH"] = (
        str(project_root)
        + os.pathsep
        + env["PYTHONPATH"]
        if env.get("PYTHONPATH")
        else str(project_root)
    )

    creationflags = 0
    start_new_session = False
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        start_new_session = True

    command = [
        sys.executable,
        "-m",
        "videocaptioner.core.asr.qwen_worker",
        "--mode",
        "align",
        "--request",
        str(request_path),
        "--output",
        str(output_path),
    ]

    worker_started = time.perf_counter()
    logger.info(
        "Qwen align worker 启动: command=%s, request=%s, output=%s, stdout=%s, stderr=%s",
        " ".join(command),
        request_path,
        output_path,
        stdout_path,
        stderr_path,
    )
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
        "w",
        encoding="utf-8",
    ) as stderr:
        process = subprocess.Popen(
            command,
            cwd=str(project_root),
            env=env,
            stdout=stdout,
            stderr=stderr,
            text=True,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )

        last_heartbeat = 0.0
        try:
            while process.poll() is None:
                now = time.monotonic()
                if callback and now - last_heartbeat >= 2:
                    last_heartbeat = now
                    callback(90, "Qwen align worker is running")
                time.sleep(0.25)
        except BaseException:
            _terminate_worker(process)
            _cleanup_worker_temp_files(
                temp_root=temp_root,
                audio_path=audio_path,
                cleanup_temp_root=cleanup_temp_root,
            )
            raise

        return_code = process.wait()

    worker_elapsed = time.perf_counter() - worker_started
    logger.info(
        "Qwen align worker 退出: return_code=%s, elapsed=%.2fs, output_exists=%s",
        return_code,
        worker_elapsed,
        output_path.exists(),
    )
    _log_worker_streams(stdout_path, stderr_path, return_code)

    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except Exception as exc:
        payload = {}
        if return_code == 0:
            _cleanup_worker_temp_files(
                temp_root=temp_root,
                audio_path=audio_path,
                cleanup_temp_root=cleanup_temp_root,
            )
            raise QwenWorkerError("Qwen align worker finished without a valid result file.") from exc

    if return_code != 0 or "error" in payload:
        error = payload.get("error") or f"Qwen align worker exited with code {return_code}"
        detail = payload.get("traceback") or _read_tail(stderr_path) or _read_tail(stdout_path)
        if detail:
            logger.info("Qwen align worker failure detail:\n%s", detail)
        logger.error(
            "Qwen align worker 失败: return_code=%s, elapsed=%.2fs, error=%s",
            return_code,
            worker_elapsed,
            error,
            extra={"suppress_console": True},
        )
        _cleanup_worker_temp_files(
            temp_root=temp_root,
            audio_path=audio_path,
            cleanup_temp_root=cleanup_temp_root,
        )
        raise QwenWorkerError(str(error).strip())

    result = payload.get("result")
    if not isinstance(result, dict):
        _cleanup_worker_temp_files(
            temp_root=temp_root,
            audio_path=audio_path,
            cleanup_temp_root=cleanup_temp_root,
        )
        raise QwenWorkerError("Qwen align worker returned an invalid result.")

    time_stamps = result.get("time_stamps")
    if not isinstance(time_stamps, list):
        _cleanup_worker_temp_files(
            temp_root=temp_root,
            audio_path=audio_path,
            cleanup_temp_root=cleanup_temp_root,
        )
        raise QwenWorkerError("Qwen align worker returned invalid timestamps.")

    _cleanup_worker_temp_files(
        temp_root=temp_root,
        audio_path=audio_path,
        cleanup_temp_root=cleanup_temp_root,
    )

    logger.info("Qwen align worker 完成: elapsed=%.2fs", worker_elapsed)
    return time_stamps


def run_qwen_worker(
    *,
    audio_input: Union[str, bytes],
    language: str,
    asr_model: str,
    aligner_model: str,
    model_dir: str = "",
    device: str = "auto",
    dtype: str = "auto",
    max_new_tokens: int = 2048,
    max_inference_batch_size: int = 0,
    return_time_stamps: bool = True,
    temp_dir: str = "",
    clip_start_ms: int | None = None,
    clip_duration_ms: int | None = None,
    compile_aligner: bool = False,
    callback: Optional[Callable[[int, str], None]] = None,
) -> dict:
    """Run Qwen ASR in the persistent isolated worker process."""
    if os.environ.get("VIDEOCAPTIONER_QWEN_WORKER_MODE", "").lower() == "oneshot":
        return _run_qwen_worker_oneshot(
            audio_input=audio_input,
            language=language,
            asr_model=asr_model,
            aligner_model=aligner_model,
            model_dir=model_dir,
            device=device,
            dtype=dtype,
            max_new_tokens=max_new_tokens,
            max_inference_batch_size=max_inference_batch_size,
            return_time_stamps=return_time_stamps,
            temp_dir=temp_dir,
            clip_start_ms=clip_start_ms,
            clip_duration_ms=clip_duration_ms,
            compile_aligner=compile_aligner,
            callback=callback,
        )

    temp_root = Path(temp_dir).expanduser() if temp_dir else Path(tempfile.mkdtemp())
    temp_root.mkdir(parents=True, exist_ok=True)
    cleanup_temp_root = not temp_dir
    worker_audio_path, audio_path = _materialize_audio_input(
        audio_input,
        temp_root,
        prefix="qwen-worker-input",
    )
    request: dict[str, Any] = {
        "audio_path": worker_audio_path,
        "language": language,
        "asr_model": asr_model,
        "aligner_model": aligner_model,
        "model_dir": model_dir,
        "device": device,
        "dtype": dtype,
        "max_new_tokens": int(max_new_tokens),
        "max_inference_batch_size": int(max_inference_batch_size or 0),
        "return_time_stamps": bool(return_time_stamps),
        "compile_aligner": bool(compile_aligner),
        "temp_dir": str(temp_root),
    }
    if clip_start_ms is not None:
        request["clip_start_ms"] = int(clip_start_ms)
    if clip_duration_ms is not None:
        request["clip_duration_ms"] = int(clip_duration_ms)
    started = time.perf_counter()
    try:
        result = get_qwen_worker_pool().request(
            mode="transcribe",
            request=request,
            callback=callback,
            heartbeat_progress=10,
            heartbeat_message="Qwen ASR worker is running",
        )
    except BaseException:
        get_qwen_worker_pool().terminate()
        raise
    finally:
        if audio_path:
            audio_path.unlink(missing_ok=True)
        if cleanup_temp_root:
            shutil.rmtree(temp_root, ignore_errors=True)

    logger.info("Qwen persistent worker 完成: elapsed=%.2fs", time.perf_counter() - started)
    return result


def _materialize_batch_audio_inputs(
    items: list[dict[str, Any]],
    temp_root: Path,
) -> tuple[list[dict[str, Any]], list[Path]]:
    worker_items: list[dict[str, Any]] = []
    cleanup_paths: list[Path] = []
    for index, item in enumerate(items):
        worker_audio_path, audio_path = _materialize_audio_input(
            item["audio_input"],
            temp_root,
            prefix=f"qwen-batch-input-{index}",
        )
        if audio_path is not None:
            cleanup_paths.append(audio_path)

        worker_item: dict[str, Any] = {"audio_path": worker_audio_path}
        if item.get("clip_start_ms") is not None:
            worker_item["clip_start_ms"] = int(item["clip_start_ms"])
        if item.get("clip_duration_ms") is not None:
            worker_item["clip_duration_ms"] = int(item["clip_duration_ms"])
        worker_items.append(worker_item)
    return worker_items, cleanup_paths


def run_qwen_batch_worker(
    *,
    items: list[dict[str, Any]],
    language: str,
    asr_model: str,
    aligner_model: str,
    model_dir: str = "",
    device: str = "auto",
    dtype: str = "auto",
    max_new_tokens: int = 2048,
    max_inference_batch_size: int = 0,
    return_time_stamps: bool = True,
    temp_dir: str = "",
    compile_aligner: bool = False,
    callback: Optional[Callable[[int, str], None]] = None,
) -> list[dict[str, Any]]:
    """Run a batch of Qwen ASR inputs through one persistent worker request."""
    if not items:
        return []

    if os.environ.get("VIDEOCAPTIONER_QWEN_WORKER_MODE", "").lower() == "oneshot":
        return [
            run_qwen_worker(
                audio_input=item["audio_input"],
                language=language,
                asr_model=asr_model,
                aligner_model=aligner_model,
                model_dir=model_dir,
                device=device,
                dtype=dtype,
                max_new_tokens=max_new_tokens,
                max_inference_batch_size=max_inference_batch_size,
                return_time_stamps=return_time_stamps,
                temp_dir=temp_dir,
                clip_start_ms=item.get("clip_start_ms"),
                clip_duration_ms=item.get("clip_duration_ms"),
                compile_aligner=compile_aligner,
                callback=callback,
            )
            for item in items
        ]

    temp_root = Path(temp_dir).expanduser() if temp_dir else Path(tempfile.mkdtemp())
    temp_root.mkdir(parents=True, exist_ok=True)
    cleanup_temp_root = not temp_dir
    worker_items, cleanup_paths = _materialize_batch_audio_inputs(items, temp_root)
    request: dict[str, Any] = {
        "items": worker_items,
        "language": language,
        "asr_model": asr_model,
        "aligner_model": aligner_model,
        "model_dir": model_dir,
        "device": device,
        "dtype": dtype,
        "max_new_tokens": int(max_new_tokens),
        "max_inference_batch_size": int(max_inference_batch_size or 0),
        "return_time_stamps": bool(return_time_stamps),
        "compile_aligner": bool(compile_aligner),
        "temp_dir": str(temp_root),
    }
    started = time.perf_counter()
    try:
        result = get_qwen_worker_pool().request(
            mode="transcribe_batch",
            request=request,
            callback=callback,
            heartbeat_progress=10,
            heartbeat_message="Qwen ASR batch worker is running",
        )
    except BaseException:
        get_qwen_worker_pool().terminate()
        raise
    finally:
        for audio_path in cleanup_paths:
            audio_path.unlink(missing_ok=True)
        if cleanup_temp_root:
            shutil.rmtree(temp_root, ignore_errors=True)

    raw_results = result.get("results")
    if not isinstance(raw_results, list):
        raise QwenWorkerError("Qwen batch worker returned invalid results.")
    if len(raw_results) != len(items):
        raise QwenWorkerError(
            "Qwen batch worker returned "
            f"{len(raw_results)} result(s) for {len(items)} input(s)."
        )

    logger.info(
        "Qwen persistent batch worker 完成: chunks=%s, elapsed=%.2fs",
        len(raw_results),
        time.perf_counter() - started,
    )
    return raw_results


def run_qwen_alignment_worker(
    *,
    audio_input: Union[str, bytes],
    transcript: str,
    language: str,
    aligner_model: str,
    model_dir: str = "",
    device: str = "auto",
    dtype: str = "auto",
    temp_dir: str = "",
    clip_start_ms: int | None = None,
    clip_duration_ms: int | None = None,
    compile_aligner: bool = False,
    callback: Optional[Callable[[int, str], None]] = None,
) -> list[dict[str, float | str]]:
    """Run Qwen3-ForcedAligner in the persistent isolated worker process."""
    if os.environ.get("VIDEOCAPTIONER_QWEN_WORKER_MODE", "").lower() == "oneshot":
        return _run_qwen_alignment_worker_oneshot(
            audio_input=audio_input,
            transcript=transcript,
            language=language,
            aligner_model=aligner_model,
            model_dir=model_dir,
            device=device,
            dtype=dtype,
            temp_dir=temp_dir,
            clip_start_ms=clip_start_ms,
            clip_duration_ms=clip_duration_ms,
            compile_aligner=compile_aligner,
            callback=callback,
        )

    temp_root = Path(temp_dir).expanduser() if temp_dir else Path(tempfile.mkdtemp())
    temp_root.mkdir(parents=True, exist_ok=True)
    cleanup_temp_root = not temp_dir
    worker_audio_path, audio_path = _materialize_audio_input(
        audio_input,
        temp_root,
        prefix="qwen-align-worker-input",
    )
    request: dict[str, Any] = {
        "audio_path": worker_audio_path,
        "transcript": transcript,
        "language": language,
        "aligner_model": aligner_model,
        "model_dir": model_dir,
        "device": device,
        "dtype": dtype,
        "compile_aligner": bool(compile_aligner),
        "temp_dir": str(temp_root),
    }
    if clip_start_ms is not None:
        request["clip_start_ms"] = int(clip_start_ms)
    if clip_duration_ms is not None:
        request["clip_duration_ms"] = int(clip_duration_ms)
    started = time.perf_counter()
    try:
        result = get_qwen_worker_pool().request(
            mode="align",
            request=request,
            callback=callback,
            heartbeat_progress=90,
            heartbeat_message="Qwen align worker is running",
        )
    except BaseException:
        get_qwen_worker_pool().terminate()
        raise
    finally:
        if audio_path:
            audio_path.unlink(missing_ok=True)
        if cleanup_temp_root:
            shutil.rmtree(temp_root, ignore_errors=True)

    time_stamps = result.get("time_stamps")
    if not isinstance(time_stamps, list):
        raise QwenWorkerError("Qwen align worker returned invalid timestamps.")

    logger.info(
        "Qwen persistent align worker 完成: elapsed=%.2fs",
        time.perf_counter() - started,
    )
    return time_stamps


def _terminate_worker(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    logger.warning("正在终止 Qwen worker 进程树: pid=%s", process.pid)
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    logger.warning("Qwen worker 已终止: pid=%s", process.pid)


def _read_tail(path: Path, limit: int = 4000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:]


def _log_worker_streams(stdout_path: Path, stderr_path: Path, return_code: int) -> None:
    stdout_tail = _read_tail(stdout_path).strip()
    stderr_tail = _read_tail(stderr_path).strip()
    if stdout_tail:
        logger.info("Qwen worker stdout tail:\n%s", stdout_tail)
    if stderr_tail:
        logger.info(
            "Qwen worker stderr tail%s:\n%s",
            " (failed)" if return_code else "",
            stderr_tail,
        )


def _without_qt_dll_paths(path_value: str) -> str:
    """Remove Qt DLL directories from the Qwen worker PATH.

    The worker does not use PyQt. Keeping PyQt's Qt5/bin directory in PATH can
    make torch/qwen native libraries resolve the Qt-bundled MSVC runtime first.
    """
    parts = []
    for item in path_value.split(os.pathsep):
        normalized = item.replace("/", "\\").lower()
        if "\\pyqt5\\qt5\\bin" in normalized:
            continue
        parts.append(item)
    return os.pathsep.join(parts)


class QwenLocalASR(BaseASR):
    """Local Qwen3 ASR backend.

    The backend uses the official qwen-asr runtime. When word timestamps are
    requested it loads Qwen3-ForcedAligner and returns true word/character
    timestamps instead of estimated timings.
    """

    def __init__(
        self,
        audio_input: Union[str, bytes],
        asr_model: str = "Qwen/Qwen3-ASR-1.7B",
        aligner_model: str = "Qwen/Qwen3-ForcedAligner-0.6B",
        model_dir: str = "",
        language: str = "",
        device: str = "auto",
        dtype: str = "auto",
        max_new_tokens: int = 2048,
        max_inference_batch_size: int = 0,
        compile_aligner: bool = False,
        temp_dir: str = "",
        use_cache: bool = False,
        need_word_time_stamp: bool = True,
        audio_duration: float | None = None,
        cache_identity: str | None = None,
        speech_ranges_ms: list[tuple[int, int]] | None = None,
        source_audio_path: str = "",
        source_start_ms: int | None = None,
        source_duration_ms: int | None = None,
    ):
        super().__init__(
            audio_input,
            use_cache=use_cache,
            need_word_time_stamp=need_word_time_stamp,
            audio_duration=audio_duration,
            cache_identity=cache_identity,
            speech_ranges_ms=speech_ranges_ms,
        )
        self.asr_model = asr_model.strip() or "Qwen/Qwen3-ASR-1.7B"
        self.aligner_model = aligner_model.strip() or "Qwen/Qwen3-ForcedAligner-0.6B"
        self.model_dir = model_dir
        self.language = language
        self.device = device or "auto"
        self.dtype = dtype or "auto"
        self.max_new_tokens = max_new_tokens
        self.max_inference_batch_size = max_inference_batch_size
        self.compile_aligner = bool(compile_aligner)
        self.temp_dir = temp_dir
        self.need_word_time_stamp = need_word_time_stamp
        self.source_audio_path = source_audio_path
        self.source_start_ms = source_start_ms
        self.source_duration_ms = source_duration_ms

    @classmethod
    def run_batch_instances(
        cls,
        instances: list["QwenLocalASR"],
        callback: Optional[Callable[[int, str], None]] = None,
        *,
        allow_degraded: bool = False,
    ) -> list[ASRData | BaseException]:
        """Run compatible Qwen ASR instances through batch worker requests."""
        results: list[ASRData | BaseException | None] = [None] * len(instances)
        pending_groups: dict[
            tuple[str, str, str, str, str, str, int, int, bool, bool, str],
            list[tuple[int, QwenLocalASR]],
        ] = {}

        for index, instance in enumerate(instances):
            cache_key = f"{instance.__class__.__name__}:{instance._get_key()}"
            if instance.use_cache and is_cache_enabled():
                cached_result = instance._cache.get(cache_key, default=None)
                if isinstance(cached_result, dict):
                    try:
                        segments = instance._make_segments(
                            cached_result,
                            _allow_degraded=allow_degraded,
                        )
                        results[index] = ASRData(segments)
                        continue
                    except BaseException as exc:
                        results[index] = exc
                        continue

            group_key = (
                instance.asr_model,
                instance.aligner_model,
                instance.model_dir,
                instance.language,
                instance.device,
                instance.dtype,
                int(instance.max_new_tokens),
                int(instance.max_inference_batch_size or 0),
                bool(instance.need_word_time_stamp),
                bool(instance.compile_aligner),
                instance.temp_dir,
            )
            pending_groups.setdefault(group_key, []).append((index, instance))

        for group in pending_groups.values():
            first = group[0][1]
            worker_items = [
                {
                    "audio_input": instance.source_audio_path
                    or instance.audio_input
                    or instance.file_binary
                    or b"",
                    "clip_start_ms": (
                        instance.source_start_ms
                        if instance.source_audio_path
                        else None
                    ),
                    "clip_duration_ms": (
                        instance.source_duration_ms
                        if instance.source_audio_path
                        else None
                    ),
                }
                for _, instance in group
            ]
            try:
                raw_results = run_qwen_batch_worker(
                    items=worker_items,
                    language=first.language,
                    asr_model=first.asr_model,
                    aligner_model=first.aligner_model,
                    model_dir=first.model_dir,
                    device=first.device,
                    dtype=first.dtype,
                    max_new_tokens=first.max_new_tokens,
                    max_inference_batch_size=first.max_inference_batch_size,
                    return_time_stamps=first.need_word_time_stamp,
                    temp_dir=first.temp_dir,
                    compile_aligner=first.compile_aligner,
                    callback=callback,
                )
            except BaseException as exc:
                for index, _ in group:
                    results[index] = exc
                continue

            for (index, instance), resp_data in zip(group, raw_results):
                try:
                    segments = instance._make_segments(
                        resp_data,
                        _allow_degraded=allow_degraded,
                    )
                    if (
                        instance.use_cache
                        and is_cache_enabled()
                        and instance._should_cache_response(resp_data, segments)
                    ):
                        cache_key = (
                            f"{instance.__class__.__name__}:{instance._get_key()}"
                        )
                        instance._cache.set(cache_key, resp_data, expire=86400 * 2)
                    results[index] = ASRData(segments)
                except BaseException as exc:
                    results[index] = exc

        return [
            result if result is not None else QwenWorkerError("Qwen batch missing result")
            for result in results
        ]

    def _boundary_ms(self, resp_data: dict) -> int:
        seconds = resp_data.get("seconds")
        if isinstance(seconds, (int, float)) and seconds > 0:
            return int(float(seconds) * 1000)
        return max(int(self.audio_duration * 1000), 1)

    def _make_estimated_segments(self, text: str, end_time: int) -> list[ASRDataSeg]:
        text_segments = split_transcript_text(text)
        logger.warning(
            "Qwen ASR response is using estimated timestamps; split transcript "
            "into %s cues",
            len(text_segments),
        )
        return make_timed_segments(text_segments, end_time, self.speech_ranges_ms)

    def _run(
        self, callback: Optional[Callable[[int, str], None]] = None, **kwargs: Any
    ) -> dict:
        if callback:
            callback(5, "Loading Qwen ASR model")

        audio_input = self.source_audio_path or self.audio_input or self.file_binary or b""
        result = run_qwen_worker(
            audio_input=audio_input,
            language=self.language,
            asr_model=self.asr_model,
            aligner_model=self.aligner_model,
            model_dir=self.model_dir,
            device=self.device,
            dtype=self.dtype,
            max_new_tokens=self.max_new_tokens,
            max_inference_batch_size=self.max_inference_batch_size,
            return_time_stamps=self.need_word_time_stamp,
            temp_dir=self.temp_dir,
            clip_start_ms=self.source_start_ms if self.source_audio_path else None,
            clip_duration_ms=self.source_duration_ms if self.source_audio_path else None,
            compile_aligner=self.compile_aligner,
            callback=callback,
        )

        if callback:
            callback(100, "Qwen ASR completed")

        return result

    def _make_segments(
        self, resp_data: dict, _allow_degraded: bool = False
    ) -> List[ASRDataSeg]:
        text = str(resp_data.get("text", "")).strip()
        end_time = self._boundary_ms(resp_data)
        segments = timestamp_items_to_segments(resp_data.get("time_stamps"))
        if segments:
            clamped, overflow_ratio = clamp_segments_to_duration(segments, end_time)
            problems = alignment_problems(text, clamped, end_time, overflow_ratio)
            anomaly = check_transcript_anomaly(text, end_time / 1000.0)
            if anomaly:
                problems.insert(0, f"transcript anomaly: {anomaly}")
            if problems:
                _, _, coverage = alignment_coverage(text, clamped)
                reason = "; ".join(problems)
                if _allow_degraded:
                    if clamped and coverage >= MIN_ALIGNMENT_COVERAGE:
                        logger.warning(
                            "Qwen ASR alignment degraded (%s); keeping clamped "
                            "aligned segments (text coverage %.0f%%)",
                            reason,
                            coverage * 100,
                        )
                        return clamped
                    logger.warning(
                        "Qwen ASR alignment degraded (%s); falling back to "
                        "estimated cue timings",
                        reason,
                    )
                    return self._make_estimated_segments(text, end_time)
                raise ASRResultDegradedError(reason, coverage=coverage)
            return clamped

        if not text:
            logger.warning(
                "Qwen ASR response returned empty text; treating this chunk as silence"
            )
            return []

        if self.need_word_time_stamp:
            if _allow_degraded:
                logger.warning(
                    "Qwen ASR response has no timestamps; falling back to "
                    "estimated cue timings"
                )
                return self._make_estimated_segments(text, end_time)
            raise ASRResultDegradedError(
                "Qwen ASR was requested to return timestamps, but no timestamps "
                "were returned. Check that Qwen3-ForcedAligner is available and "
                "the audio chunk is supported."
            )

        logger.warning("Qwen ASR response has no timestamps")
        return self._make_estimated_segments(text, end_time)

    def _get_key(self) -> str:
        return (
            f"v3-{self.cache_identity}-{self.asr_model}-{self.aligner_model}-"
            f"{self.language}-"
            f"{self.device}-{self.dtype}-{self.max_new_tokens}-"
            f"{self.max_inference_batch_size}-{self.compile_aligner}-"
            f"{self.need_word_time_stamp}"
        )

    def _should_cache_response(self, resp_data: dict, segments: list[ASRDataSeg]) -> bool:
        if not str(resp_data.get("text", "")).strip() and not segments:
            return False
        if self.need_word_time_stamp and not resp_data.get("time_stamps"):
            return False
        if self.need_word_time_stamp and resp_data.get("time_stamps") and not segments:
            return False
        if self.need_word_time_stamp and resp_data.get("time_stamps") and segments:
            text = str(resp_data.get("text", "")).strip()
            aligned_segments = timestamp_items_to_segments(resp_data.get("time_stamps"))
            boundary_ms = self._boundary_ms(resp_data)
            clamped, overflow_ratio = clamp_segments_to_duration(
                aligned_segments, boundary_ms
            )
            problems = alignment_problems(text, clamped, boundary_ms, overflow_ratio)
            anomaly = check_transcript_anomaly(text, boundary_ms / 1000.0)
            if anomaly:
                problems.insert(0, f"transcript anomaly: {anomaly}")
            if problems:
                logger.warning(
                    "Skip Qwen ASR cache because result looks degraded: %s",
                    "; ".join(problems),
                )
                return False
        return True
