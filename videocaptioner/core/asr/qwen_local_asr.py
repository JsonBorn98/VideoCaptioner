"""Local Qwen3 ASR backend with Qwen3-ForcedAligner timestamps."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, List, Optional, Union

from ..utils.logger import setup_logger
from .asr_data import ASRDataSeg
from .base import BaseASR
from .qwen_runtime import timestamp_items_to_segments
from .text_timing import make_timed_segments, split_transcript_text

logger = setup_logger("qwen_local_asr")


class QwenWorkerError(RuntimeError):
    """Raised when the isolated Qwen worker process fails."""


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
    return_time_stamps: bool = True,
    temp_dir: str = "",
    callback: Optional[Callable[[int, str], None]] = None,
) -> dict:
    """Run Qwen ASR in a child process to isolate torch/CUDA from the Qt process."""
    temp_root = Path(temp_dir).expanduser() if temp_dir else Path(tempfile.mkdtemp())
    temp_root.mkdir(parents=True, exist_ok=True)

    cleanup_temp_root = not temp_dir
    audio_path: Path | None = None
    if isinstance(audio_input, bytes):
        audio_path = temp_root / "qwen-worker-input.mp3"
        audio_path.write_bytes(audio_input)
        worker_audio_path = str(audio_path)
    else:
        worker_audio_path = audio_input

    request_path = temp_root / "qwen-worker-request.json"
    output_path = temp_root / "qwen-worker-output.json"
    stdout_path = temp_root / "qwen-worker-stdout.log"
    stderr_path = temp_root / "qwen-worker-stderr.log"

    request = {
        "audio_path": worker_audio_path,
        "language": language,
        "asr_model": asr_model,
        "aligner_model": aligner_model,
        "model_dir": model_dir,
        "device": device,
        "dtype": dtype,
        "max_new_tokens": int(max_new_tokens),
        "return_time_stamps": bool(return_time_stamps),
        "temp_dir": str(temp_root),
    }
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
        raise QwenWorkerError(str(error).strip())

    result = payload.get("result")
    if not isinstance(result, dict):
        raise QwenWorkerError("Qwen worker returned an invalid result.")

    if audio_path:
        audio_path.unlink(missing_ok=True)
    if cleanup_temp_root:
        shutil.rmtree(temp_root, ignore_errors=True)

    logger.info("Qwen worker 完成: elapsed=%.2fs", worker_elapsed)
    return result


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
        temp_dir: str = "",
        use_cache: bool = False,
        need_word_time_stamp: bool = True,
    ):
        super().__init__(audio_input, use_cache, need_word_time_stamp)
        self.asr_model = asr_model.strip() or "Qwen/Qwen3-ASR-1.7B"
        self.aligner_model = aligner_model.strip() or "Qwen/Qwen3-ForcedAligner-0.6B"
        self.model_dir = model_dir
        self.language = language
        self.device = device or "auto"
        self.dtype = dtype or "auto"
        self.max_new_tokens = max_new_tokens
        self.temp_dir = temp_dir
        self.need_word_time_stamp = need_word_time_stamp

    def _run(
        self, callback: Optional[Callable[[int, str], None]] = None, **kwargs: Any
    ) -> dict:
        if callback:
            callback(5, "Loading Qwen ASR model")

        result = run_qwen_worker(
            audio_input=self.audio_input or self.file_binary or b"",
            language=self.language,
            asr_model=self.asr_model,
            aligner_model=self.aligner_model,
            model_dir=self.model_dir,
            device=self.device,
            dtype=self.dtype,
            max_new_tokens=self.max_new_tokens,
            return_time_stamps=self.need_word_time_stamp,
            temp_dir=self.temp_dir,
            callback=callback,
        )

        if callback:
            callback(100, "Qwen ASR completed")

        return result

    def _make_segments(self, resp_data: dict) -> List[ASRDataSeg]:
        segments = timestamp_items_to_segments(resp_data.get("time_stamps"))
        if segments:
            return segments

        text = str(resp_data.get("text", "")).strip()
        if not text:
            logger.warning(
                "Qwen ASR response returned empty text; treating this chunk as silence"
            )
            return []

        if self.need_word_time_stamp:
            raise RuntimeError(
                "Qwen ASR was requested to return timestamps, but no timestamps were returned. "
                "Check that Qwen3-ForcedAligner is available and the audio chunk is supported."
            )

        end_time = max(int(self.audio_duration * 1000), 1)
        text_segments = split_transcript_text(text)
        logger.warning(
            "Qwen ASR response has no timestamps; split transcript into %s estimated cues",
            len(text_segments),
        )
        return make_timed_segments(text_segments, end_time)

    def _get_key(self) -> str:
        return (
            f"v2-{self.crc32_hex}-{self.asr_model}-{self.aligner_model}-{self.language}-"
            f"{self.device}-{self.dtype}-{self.max_new_tokens}-{self.need_word_time_stamp}"
        )

    def _should_cache_response(self, resp_data: dict, segments: list[ASRDataSeg]) -> bool:
        if not str(resp_data.get("text", "")).strip() and not segments:
            return False
        return True
