"""Run repeatable ASR transcription benchmarks and write a JSON report.

Example:
    uv run python scripts/asr_benchmark.py \
        --case zh=tests/fixtures/audio/zh_20min.mp3 \
        --case en=tests/fixtures/audio/en_20min.mp3 \
        --asr qwen-local \
        --variant cuda="--word-timestamps --qwen-device cuda:0" \
        --variant cuda-compile="--word-timestamps --qwen-device cuda:0 --qwen-compile-aligner" \
        --required-label zh \
        --required-label en \
        --required-variant cuda \
        --required-variant cuda-compile \
        --min-duration-seconds 1200 \
        --check-qwen-cleanup

Arguments after "--" are shared arguments forwarded to every
"videocaptioner transcribe" run.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BenchmarkInput:
    label: str
    path: Path


@dataclass(frozen=True)
class BenchmarkVariant:
    name: str
    args: list[str]


DEFAULT_QWEN_CLEANUP_PATTERNS = [
    "qwen_worker.py",
    "videocaptioner.core.asr.qwen_worker",
]


def _is_qwen_worker_pattern(patterns: list[str]) -> bool:
    return any(
        "qwen_worker" in pattern.lower()
        or "videocaptioner.core.asr.qwen_worker" in pattern.lower()
        for pattern in patterns
    )


def _is_python_like_process(process: dict[str, Any]) -> bool:
    name = str(process.get("name") or "").lower()
    if not name:
        return True
    return name in {"python", "python.exe", "pythonw", "pythonw.exe", "uv", "uv.exe"}


def _timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return slug.strip("._-") or "case"


def _media_duration_seconds(path: Path) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None

    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except Exception:
        return None

    try:
        duration = float(result.stdout.strip())
    except ValueError:
        return None
    return duration if duration > 0 else None


def _count_srt_cues(path: Path) -> int | None:
    if not path.exists() or path.suffix.lower() != ".srt":
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return len(re.findall(r"(?m)^\d+\s*$", text))


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", errors="replace")


def _process_matches_patterns(process: dict[str, Any], patterns: list[str]) -> bool:
    haystack = " ".join(
        str(process.get(key, ""))
        for key in ("name", "command_line")
        if process.get(key)
    ).lower()
    matched = any(pattern.lower() in haystack for pattern in patterns)
    if matched and _is_qwen_worker_pattern(patterns):
        return _is_python_like_process(process)
    return matched


def _capture_windows_processes() -> list[dict[str, Any]]:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell:
        command = [
            powershell,
            "-NoProfile",
            "-Command",
            (
                "Get-CimInstance Win32_Process | "
                "Select-Object ProcessId,ParentProcessId,Name,CommandLine | "
                "ConvertTo-Json -Compress"
            ),
        ]
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            payload = json.loads(result.stdout)
            rows = payload if isinstance(payload, list) else [payload]
            return [
                {
                    "pid": int(row.get("ProcessId", 0)),
                    "parent_pid": int(row.get("ParentProcessId", 0)),
                    "name": str(row.get("Name") or ""),
                    "command_line": str(row.get("CommandLine") or ""),
                }
                for row in rows
                if row.get("ProcessId")
            ]

    tasklist = shutil.which("tasklist")
    if not tasklist:
        return []
    result = subprocess.run(
        [tasklist, "/fo", "csv", "/nh"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if result.returncode != 0:
        return []
    processes: list[dict[str, Any]] = []
    for row in csv.reader(result.stdout.splitlines()):
        if len(row) < 2:
            continue
        try:
            pid = int(row[1])
        except ValueError:
            continue
        processes.append(
            {
                "pid": pid,
                "parent_pid": None,
                "name": row[0],
                "command_line": "",
            }
        )
    return processes


def _capture_posix_processes() -> list[dict[str, Any]]:
    ps = shutil.which("ps")
    if not ps:
        return []
    result = subprocess.run(
        [ps, "-eo", "pid=,ppid=,comm=,args="],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if result.returncode != 0:
        return []

    processes: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            parent_pid = int(parts[1])
        except ValueError:
            continue
        processes.append(
            {
                "pid": pid,
                "parent_pid": parent_pid,
                "name": parts[2],
                "command_line": parts[3] if len(parts) > 3 else parts[2],
            }
        )
    return processes


def _capture_process_snapshot(patterns: list[str]) -> dict[str, Any]:
    if not patterns:
        return {
            "available": False,
            "patterns": [],
            "matches": [],
            "error": None,
        }

    try:
        processes = (
            _capture_windows_processes()
            if os.name == "nt"
            else _capture_posix_processes()
        )
    except Exception as exc:
        return {
            "available": False,
            "patterns": patterns,
            "matches": [],
            "error": f"{type(exc).__name__}: {exc}",
        }

    matches = [
        process
        for process in processes
        if _process_matches_patterns(process, patterns)
    ]
    return {
        "available": bool(processes),
        "patterns": patterns,
        "matches": matches,
        "error": None if processes else "process listing unavailable",
    }


def _new_process_matches(
    before: dict[str, Any],
    after: dict[str, Any],
) -> list[dict[str, Any]]:
    before_pids = {
        int(process["pid"])
        for process in before.get("matches", [])
        if isinstance(process, dict) and process.get("pid") is not None
    }
    return [
        process
        for process in after.get("matches", [])
        if isinstance(process, dict) and process.get("pid") not in before_pids
    ]


def _parse_input_value(value: str) -> BenchmarkInput:
    if "=" in value:
        label, path_value = value.split("=", 1)
        label = label.strip()
        path = Path(path_value).expanduser()
        return BenchmarkInput(label=label or path.stem, path=path)
    path = Path(value).expanduser()
    return BenchmarkInput(label=path.stem, path=path)


def _parse_variant_value(value: str) -> BenchmarkVariant:
    if "=" not in value:
        raise argparse.ArgumentTypeError(
            "--variant must use NAME=ARGS, for example "
            "--variant cuda=\"--word-timestamps --qwen-device cuda:0\""
        )
    name, raw_args = value.split("=", 1)
    name = name.strip()
    if not name:
        raise argparse.ArgumentTypeError("--variant name cannot be empty")
    return BenchmarkVariant(name=name, args=shlex.split(raw_args))


def _run_case(
    *,
    input_case: BenchmarkInput,
    variant: BenchmarkVariant,
    output_dir: Path,
    asr: str,
    shared_args: list[str],
    process_snapshot_patterns: list[str],
    process_snapshot_grace_seconds: float,
) -> dict[str, Any]:
    input_path = input_case.path
    case_slug = _safe_slug(input_case.label)
    variant_slug = _safe_slug(variant.name)
    subtitle_path = output_dir / f"{case_slug}.{variant_slug}.{asr}.srt"
    stdout_path = output_dir / f"{case_slug}.{variant_slug}.{asr}.stdout.log"
    stderr_path = output_dir / f"{case_slug}.{variant_slug}.{asr}.stderr.log"
    command = [
        sys.executable,
        "-m",
        "videocaptioner.cli.main",
        "transcribe",
        str(input_path),
        "--asr",
        asr,
        "--format",
        "srt",
        "--output",
        str(subtitle_path),
        *shared_args,
        *variant.args,
    ]

    process_before = _capture_process_snapshot(process_snapshot_patterns)
    started = time.perf_counter()
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    elapsed = time.perf_counter() - started
    if process_snapshot_patterns and process_snapshot_grace_seconds > 0:
        time.sleep(process_snapshot_grace_seconds)
    process_after = _capture_process_snapshot(process_snapshot_patterns)
    _write_text(stdout_path, result.stdout)
    _write_text(stderr_path, result.stderr)

    duration = _media_duration_seconds(input_path)
    case: dict[str, Any] = {
        "label": input_case.label,
        "variant": variant.name,
        "input": str(input_path),
        "asr": asr,
        "command": command,
        "return_code": result.returncode,
        "elapsed_seconds": round(elapsed, 3),
        "media_duration_seconds": round(duration, 3) if duration else None,
        "realtime_factor": round(elapsed / duration, 3) if duration else None,
        "subtitle_path": str(subtitle_path),
        "subtitle_cues": _count_srt_cues(subtitle_path),
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
    }
    if process_snapshot_patterns:
        case["process_snapshots"] = {
            "before": process_before,
            "after": process_after,
            "new_matches_after": _new_process_matches(process_before, process_after),
        }
    return case


def _acceptance_summary(
    cases: list[dict[str, Any]],
    *,
    required_labels: list[str],
    required_variants: list[str],
    min_duration_seconds: float,
    require_clean_processes: bool,
) -> dict[str, Any]:
    successful_cases = [case for case in cases if case.get("return_code") == 0]
    successful_labels = {str(case.get("label", "")) for case in successful_cases}
    successful_variants = {str(case.get("variant", "")) for case in successful_cases}
    successful_matrix = {
        (str(case.get("label", "")), str(case.get("variant", "")))
        for case in successful_cases
    }
    missing_labels = [
        label for label in required_labels if label not in successful_labels
    ]
    missing_variants = [
        variant for variant in required_variants if variant not in successful_variants
    ]
    failed_cases = [
        {
            "label": case.get("label"),
            "variant": case.get("variant"),
            "return_code": case.get("return_code"),
        }
        for case in cases
        if case.get("return_code") != 0
    ]
    missing_matrix_cases = [
        {
            "label": label,
            "variant": variant,
        }
        for label in required_labels
        for variant in required_variants
        if (label, variant) not in successful_matrix
    ]
    duration_failures: list[dict[str, Any]] = []
    if min_duration_seconds > 0:
        for case in successful_cases:
            duration = case.get("media_duration_seconds")
            if not isinstance(duration, (int, float)) or duration < min_duration_seconds:
                duration_failures.append(
                    {
                        "label": case.get("label"),
                        "variant": case.get("variant"),
                        "media_duration_seconds": duration,
                        "required_seconds": min_duration_seconds,
                    }
                )

    process_cleanup_failures: list[dict[str, Any]] = []
    if require_clean_processes:
        for case in cases:
            snapshots = case.get("process_snapshots")
            if not isinstance(snapshots, dict):
                process_cleanup_failures.append(
                    {
                        "label": case.get("label"),
                        "variant": case.get("variant"),
                        "reason": "missing process snapshots",
                        "new_matches_after": [],
                    }
                )
                continue
            after = snapshots.get("after")
            if not isinstance(after, dict) or not after.get("available"):
                process_cleanup_failures.append(
                    {
                        "label": case.get("label"),
                        "variant": case.get("variant"),
                        "reason": "process snapshot unavailable",
                        "new_matches_after": [],
                    }
                )
                continue
            new_matches_after = snapshots.get("new_matches_after")
            if isinstance(new_matches_after, list) and new_matches_after:
                process_cleanup_failures.append(
                    {
                        "label": case.get("label"),
                        "variant": case.get("variant"),
                        "reason": "matched process remained after run",
                        "new_matches_after": new_matches_after,
                    }
                )

    passed = not (
        missing_labels
        or missing_variants
        or missing_matrix_cases
        or failed_cases
        or duration_failures
        or process_cleanup_failures
    )
    return {
        "passed": passed,
        "required_labels": required_labels,
        "missing_labels": missing_labels,
        "required_variants": required_variants,
        "missing_variants": missing_variants,
        "missing_matrix_cases": missing_matrix_cases,
        "min_duration_seconds": min_duration_seconds or None,
        "duration_failures": duration_failures,
        "require_clean_processes": require_clean_processes,
        "process_cleanup_failures": process_cleanup_failures,
        "failed_cases": failed_cases,
    }


def parse_args(argv: list[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        action="append",
        metavar="PATH",
        help=(
            "Input media file. Repeat for benchmark sets. Use LABEL=PATH to "
            "make acceptance labels explicit."
        ),
    )
    parser.add_argument(
        "--case",
        action="append",
        metavar="LABEL=PATH",
        help="Named input case. Equivalent to --input LABEL=PATH.",
    )
    parser.add_argument(
        "--asr",
        default="qwen-local",
        choices=[
            "bijian",
            "jianying",
            "faster-whisper",
            "whisper-api",
            "whisper-cpp",
            "mimo-asr",
            "qwen-local",
        ],
        help="ASR engine passed to videocaptioner transcribe.",
    )
    parser.add_argument(
        "--output-dir",
        default="benchmark-output/asr",
        metavar="DIR",
        help="Directory where benchmark reports and logs are written.",
    )
    parser.add_argument(
        "--variant",
        action="append",
        type=_parse_variant_value,
        metavar="NAME=ARGS",
        help=(
            "Variant-specific transcribe args. Repeat to run a matrix, e.g. "
            "--variant cuda=\"--word-timestamps --qwen-device cuda:0\"."
        ),
    )
    parser.add_argument(
        "--required-label",
        action="append",
        default=[],
        metavar="LABEL",
        help="Acceptance label that must have at least one successful case.",
    )
    parser.add_argument(
        "--required-variant",
        action="append",
        default=[],
        metavar="NAME",
        help="Acceptance variant that must have at least one successful case.",
    )
    parser.add_argument(
        "--min-duration-seconds",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help=(
            "Acceptance minimum media duration for each successful case. "
            "Use 1200 for the 20 minute ASR plan gate."
        ),
    )
    parser.add_argument(
        "--process-snapshot-pattern",
        action="append",
        default=[],
        metavar="TEXT",
        help=(
            "Record before/after process snapshots for processes whose name or "
            "command line contains TEXT. Repeat for multiple patterns."
        ),
    )
    parser.add_argument(
        "--check-qwen-cleanup",
        action="store_true",
        help=(
            "Shortcut for Qwen worker cleanup validation. Adds common Qwen "
            "worker process patterns and fails acceptance if a new matching "
            "process remains after a case finishes."
        ),
    )
    parser.add_argument(
        "--process-snapshot-grace-seconds",
        type=float,
        default=1.0,
        metavar="SECONDS",
        help="Seconds to wait after each run before taking the after-process snapshot.",
    )
    args, forwarded_args = parser.parse_known_args(argv)
    if forwarded_args and forwarded_args[0] == "--":
        forwarded_args = forwarded_args[1:]
    if not args.input and not args.case:
        parser.error("at least one --input or --case is required")
    return args, forwarded_args


def main(argv: list[str] | None = None) -> int:
    args, forwarded_args = parse_args(argv)
    output_dir = Path(args.output_dir) / _timestamp_slug()
    output_dir.mkdir(parents=True, exist_ok=True)
    input_values = [*(args.input or []), *(args.case or [])]
    input_cases = [_parse_input_value(value) for value in input_values]
    variants = args.variant or [BenchmarkVariant(name="default", args=[])]
    process_snapshot_patterns = list(args.process_snapshot_pattern or [])
    if args.check_qwen_cleanup:
        process_snapshot_patterns.extend(
            pattern
            for pattern in DEFAULT_QWEN_CLEANUP_PATTERNS
            if pattern not in process_snapshot_patterns
        )

    report: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "asr": args.asr,
        "shared_forwarded_args": forwarded_args,
        "variants": [
            {
                "name": variant.name,
                "args": variant.args,
            }
            for variant in variants
        ],
        "output_dir": str(output_dir),
        "process_snapshot_patterns": process_snapshot_patterns,
        "check_qwen_cleanup": bool(args.check_qwen_cleanup),
        "cases": [],
    }

    exit_code = 0
    for input_case in input_cases:
        for variant in variants:
            case = _run_case(
                input_case=input_case,
                variant=variant,
                output_dir=output_dir,
                asr=args.asr,
                shared_args=forwarded_args,
                process_snapshot_patterns=process_snapshot_patterns,
                process_snapshot_grace_seconds=max(
                    0.0,
                    float(args.process_snapshot_grace_seconds or 0.0),
                ),
            )
            report["cases"].append(case)
            if case["return_code"] != 0:
                exit_code = int(case["return_code"])

    acceptance = _acceptance_summary(
        report["cases"],
        required_labels=args.required_label,
        required_variants=args.required_variant,
        min_duration_seconds=max(0.0, float(args.min_duration_seconds or 0.0)),
        require_clean_processes=bool(args.check_qwen_cleanup),
    )
    report["acceptance"] = acceptance
    if exit_code == 0 and not acceptance["passed"]:
        exit_code = 2

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report_path = output_dir / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(report_path)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
