"""Offline complete-task benchmark. Run with uv run python -m scripts.postprocess_benchmark."""

from __future__ import annotations

import argparse
import cProfile
import hashlib
import json
import os
import platform
import pstats
import re
import socket
import statistics
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path

BASELINE_ID = "postprocess-v1"
CASES = ("local-long", "independent", "slow-main", "mixed", "partial-failure")


def fingerprint(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def distribution(values):
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "sum": 0, "min": None, "median": None, "p95": None, "max": None}
    return {
        "count": len(ordered),
        "sum": sum(ordered),
        "min": ordered[0],
        "median": statistics.median(ordered),
        "p95": ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)],
        "max": ordered[-1],
    }


@contextmanager
def offline_network():
    """Reject non-loopback sockets even if a future task accidentally creates a provider."""
    original = socket.socket.connect
    original_ex = socket.socket.connect_ex

    def connect(sock, address):
        if not isinstance(address, tuple) or address[0] not in ("127.0.0.1", "::1"):
            raise RuntimeError("offline benchmark forbids non-loopback connections")
        return original(sock, address)

    def connect_ex(sock, address):
        if not isinstance(address, tuple) or address[0] not in ("127.0.0.1", "::1"):
            raise RuntimeError("offline benchmark forbids non-loopback connections")
        return original_ex(sock, address)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    try:
        yield
    finally:
        socket.socket.connect = original
        socket.socket.connect_ex = original_ex


def _fixture(case):
    from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg

    segments = []
    count = 7257 if case == "local-long" else 120
    for index in range(count):
        original = f"Item {index}: hello."
        translated = "你好"
        if case != "local-long" and index % 3 == 0:
            translated = "这是一个用于离线测试的非常冗长且需要修复的字幕翻译内容"
        if case in ("mixed", "partial-failure") and index % 12 == 0:
            original = f"Item {index}: the first sentence. The second sentence is here."
        if case in ("mixed", "partial-failure") and index % 12 == 1:
            translated = "这是另一个相邻的问题字幕需要与前面的字幕一起修复"
        segments.append(ASRDataSeg(original, index * 8000, index * 8000 + 6000, translated))
    return ASRData(segments)


class ControlledGateway:
    """Observe the gateway seam; real gateway owns gates, retries and cache policy."""

    def __init__(self, case, main_delay, review_delay, concurrency, cache):
        from videocaptioner.core.llm.gateway import LLMGateway

        self.case = case
        self.delays = {"main": main_delay, "review": review_delay}
        self.calls = []
        self.attempts = []
        self.backoffs = []
        self.lock = threading.Lock()
        self.inflight = 0
        self.max_inflight = 0
        self.runtime = LLMGateway(
            adapter_factory=self.adapter,
            max_concurrency=concurrency,
            response_cache=cache,
            sleep=self.sleep,
            random_source=lambda: 0.5,
        )

    def sleep(self, seconds):
        start = time.perf_counter()
        time.sleep(seconds)
        self.backoffs.append(time.perf_counter() - start)

    def adapter(self, profile):
        owner = self

        class Adapter:
            def __init__(self):
                self.profile = profile

            def complete(self, request):
                from videocaptioner.core.llm.models import LLMResult

                start = time.perf_counter()
                role, payload = owner.decode(request)
                with owner.lock:
                    owner.inflight += 1
                    owner.max_inflight = max(owner.max_inflight, owner.inflight)
                try:
                    time.sleep(owner.delays[role])
                    if role == "review":
                        reviews = []
                        for subject in payload["review_subjects"]:
                            for segment in subject["segments"]:
                                for proposal in segment["proposals"]:
                                    reviews.append(
                                        {
                                            "problem_id": segment["problem_ids"][0],
                                            "output_index": proposal["output_index"],
                                            "translated": "您好",
                                        }
                                    )
                        response = {"reviews": reviews}
                    else:
                        repairs = []
                        for subject in payload["repair_subjects"]:
                            for segment in subject["segments"]:
                                if not segment["problem_ids"]:
                                    continue
                                text = segment["text"]
                                pieces = text.split(". ") if "second sentence" in text else [text]
                                if len(pieces) > 1:
                                    pieces[0] += "."
                                for index, piece in enumerate(pieces):
                                    if owner.case == "partial-failure" and text.startswith(
                                        "Item 0:"
                                    ):
                                        piece = "Illegal source replacement"
                                    repairs.append(
                                        {
                                            "problem_id": segment["problem_ids"][0],
                                            "output_index": index,
                                            "original": piece,
                                            "translated": "你好",
                                        }
                                    )
                        response = {"repairs": repairs}
                    text = json.dumps(response, ensure_ascii=False, sort_keys=True)
                    return LLMResult(text=text)
                finally:
                    with owner.lock:
                        owner.inflight -= 1
                        owner.attempts.append(
                            {"role": role, "start": start, "end": time.perf_counter()}
                        )

            def close(self):
                pass

        return Adapter()

    @staticmethod
    def decode(request):
        user = request.messages[-1].content
        match = re.search(r"<input>(.*?)</input>", user, re.S)
        if match is None:
            raise ValueError("unexpected benchmark request protocol")
        payload = json.loads(match.group(1))
        role = "review" if "review_subjects" in payload else "main"
        return role, payload

    def complete(self, profile, request, **kwargs):
        from videocaptioner.core.translate.enhanced.token_planner import estimate_tokens

        role, payload = self.decode(request)
        subjects = payload["review_subjects" if role == "review" else "repair_subjects"]
        serialized = json.dumps(
            [{"role": msg.role, "content": msg.content} for msg in request.messages],
            ensure_ascii=False,
            sort_keys=True,
        )
        entry = {
            "role": role,
            "subject_count": len(subjects),
            "segment_count": sum(len(item["segments"]) for item in subjects),
            "problem_count": sum(
                len(seg["problem_ids"]) for item in subjects for seg in item["segments"]
            ),
            "input_tokens_estimate": estimate_tokens(serialized),
            "request_sha256": fingerprint(serialized.encode()),
            "max_output_tokens": request.max_output_tokens,
            "timeout": request.timeout,
            "request_options_override": (
                dict(request.request_options_override) if request.request_options_override else None
            ),
            "profile": profile.profile_id,
            "subject_segment_counts": [len(item["segments"]) for item in subjects],
            "segment_source_sha256": [
                fingerprint(
                    (
                        seg["text"]
                        if role == "main"
                        else "".join(p["original"] for p in seg["proposals"])
                    ).encode()
                )
                for item in subjects
                for seg in item["segments"]
            ],
            "review_fragment_count": sum(
                len(seg.get("proposals", [])) for item in subjects for seg in item["segments"]
            ),
            "capacity": {
                "work_context_tokens": profile.work_context_tokens,
                "requested_output_cap": profile.max_output_tokens,
                "serialized_messages_tokens_estimate": estimate_tokens(serialized),
                "planning_enforced": False,
                "note": "observation only; current repair execution does not enforce token planning",
            },
        }
        start = time.perf_counter()
        try:
            result = self.runtime.complete(profile, request, **kwargs)
            entry["cache_hit"] = result.duration_ms is None
            entry["response_sha256"] = fingerprint(result.text.encode())
            entry["output_tokens_estimate"] = estimate_tokens(result.text)
            return result
        finally:
            entry.update(start=start, end=time.perf_counter())
            with self.lock:
                self.calls.append(entry)

    def close(self):
        self.runtime.close()


def _interval_union(intervals):
    end = total = 0.0
    for start, stop in sorted(intervals):
        total += max(0.0, stop - max(start, end))
        end = max(end, stop)
    return total


def run_case(
    case, directory, *, main_delay=0.05, review_delay=0.005, concurrency=4, cache_state="cold"
):
    """Measure one complete task with an isolated cold cache or a separate warm-up task."""
    from diskcache import Cache

    from videocaptioner.core.llm.response_cache import GatewayResponseCache
    from videocaptioner.core.utils import cache as cache_control

    if cache_state not in ("cold", "warm"):
        raise ValueError("cache_state must be cold or warm")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    was_enabled = cache_control.is_cache_enabled()
    cache_control.enable_cache()
    try:
        with Cache(str(directory / "cache")) as disk:
            response_cache = GatewayResponseCache(cache=disk)
            if cache_state == "warm":
                _measure_case(
                    case,
                    directory / "warmup",
                    main_delay,
                    review_delay,
                    concurrency,
                    response_cache,
                )
            result = _measure_case(
                case, directory / "task", main_delay, review_delay, concurrency, response_cache
            )
            result["cache"] = f"{cache_state}-isolated-disk"
        with Cache(str(directory / "diagnostic-cache")) as diagnostic_disk:
            diagnostic_cache = GatewayResponseCache(cache=diagnostic_disk)
            if cache_state == "warm":
                _measure_case(
                    case,
                    directory / "diagnostic-warmup",
                    main_delay,
                    review_delay,
                    concurrency,
                    diagnostic_cache,
                )
            diagnostic = _measure_case(
                case,
                directory / "diagnostic",
                main_delay,
                review_delay,
                concurrency,
                diagnostic_cache,
                profiled=True,
            )
            result["diagnostic_profile"] = diagnostic["measurements"]
            assert result["outcome"]["output_sha256"] == diagnostic["outcome"]["output_sha256"]
        return result
    finally:
        if not was_enabled:
            cache_control.disable_cache()


def _measure_case(
    case, directory, main_delay, review_delay, concurrency, response_cache, *, profiled=False
):
    """Create synthetic input, execute the real task, and assert delivery invariants."""
    from videocaptioner.core.entities import SubtitleLayoutEnum
    from videocaptioner.core.llm.models import LLMModelProfile, LLMTransport, ProviderDialect
    from videocaptioner.core.postprocess.config import PostprocessConfig, config_payload
    from videocaptioner.core.postprocess.models import PostprocessLayoutMode, PostprocessTask
    from videocaptioner.core.postprocess.runner import run_postprocess_task
    from videocaptioner.core.postprocess.translation import TranslationExecutionSnapshot
    from videocaptioner.core.subtitle.io import save_canonical_srt

    if case not in CASES:
        raise ValueError(f"unknown case: {case}")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    source, output = directory / "input.srt", directory / "result.srt"
    data = _fixture(case)
    save_canonical_srt(data, source, layout=SubtitleLayoutEnum.ORIGINAL_ON_TOP)
    before = source.read_bytes()
    config = PostprocessConfig(
        trim_trailing_punct=False,
        qa_report=True,
        speed_optimize=case == "local-long",
        speed_semantic_repair=False,
    )
    profiles = {
        role: LLMModelProfile(
            profile_id=f"baseline-{role}",
            name=f"baseline-{role}",
            transport=LLMTransport.OPENAI_COMPATIBLE,
            dialect=ProviderDialect.GENERIC,
            base_url="http://127.0.0.1:1/v1",
            api_key="offline-placeholder",
            model=f"controlled-{role}",
            max_output_tokens=8192,
            request_options={"temperature": 0},
        )
        for role in ("main", "review")
    }

    gateway = ControlledGateway(case, main_delay, review_delay, concurrency, response_cache)
    task = PostprocessTask(
        str(source),
        postprocessed_subtitle_path=str(output),
        layout_mode=PostprocessLayoutMode.ORIGINAL_ON_TOP,
        config_snapshot=config,
        thread_num=concurrency,
        translation_snapshot=TranslationExecutionSnapshot(
            method="non_llm" if case == "local-long" else "enhanced_llm",
            main_profile=profiles["main"],
            review_profile=profiles["review"],
            boundary_context_radius=2,
        ),
    )
    progress = []
    profiler = cProfile.Profile()
    cpu_start = time.process_time()
    start = time.perf_counter()
    try:
        with offline_network():
            if profiled:
                profiler.enable()
            result = run_postprocess_task(
                task,
                gateway=gateway,
                progress=lambda value, message: progress.append(
                    {
                        "elapsed": time.perf_counter() - start,
                        "value": value,
                        "message": message,
                    }
                ),
            )
            profiler.disable()
            wall = time.perf_counter() - start
            cpu = time.process_time() - cpu_start
    finally:
        profiler.disable()
        gateway.close()

    def compact(text):
        return "".join(text.split())

    assert source.read_bytes() == before, "baseline overwrote input"
    assert result.succeeded and result.continue_downstream, task.error
    assert output.is_file() and task.active_subtitle_path == str(output)
    assert task.asset_discovery is not None
    state_path = task.asset_discovery.asset_path("postprocess_state")
    qa_path = task.asset_discovery.asset_path("qa_report")
    assert state_path is not None and state_path.is_file(), "missing process state delivery"
    assert qa_path is not None and qa_path.is_file(), "missing QA delivery"
    delivered_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert delivered_state["status"] == task.status
    assert delivered_state["active_subtitle_path"] == str(output)
    assert compact("".join(seg.text for seg in data.segments)) == compact(
        "".join(seg.text for seg in result.output_data.segments)
    ), "source protection failed"
    assert all(seg.end_time > seg.start_time for seg in result.output_data.segments)
    expected_segments = (
        7257
        if case == "local-long"
        else 130
        if case == "mixed"
        else 129
        if case == "partial-failure"
        else 120
    )
    assert len(result.output_data.segments) == expected_segments, "missing repair/split coverage"
    if case == "local-long":
        assert not gateway.calls, "local benchmark unexpectedly called a model"
    if case != "local-long":
        assert sum(call["segment_count"] for call in gateway.calls if call["role"] == "review") >= (
            49 if case == "partial-failure" else 50 if case == "mixed" else 40
        ), "necessary review omitted"
        assert result.report.viewing_repair is not None
        assert result.report.viewing_repair.flow_mode == "main_review"
        for segment in result.output_data.segments:
            match = re.match(r"Item (\d+):", segment.text)
            if match:
                index = int(match.group(1))
                needs_repair = index % 3 == 0 or (
                    case in ("mixed", "partial-failure") and index % 12 == 1
                )
                if needs_repair and not (case == "partial-failure" and index == 0):
                    assert segment.translated_text == "您好", "review correction was not applied"
                elif not needs_repair:
                    assert segment.translated_text == "你好", "boundary context was modified"
            else:
                assert segment.text == "The second sentence is here.", "unexpected split fragment"
                assert segment.translated_text == "您好", "split fragment review correction omitted"
    unresolved = len(result.report.unresolved_viewing_problems())
    assert delivered_state["unresolved_viewing_problems"] == unresolved
    if case != "partial-failure":
        assert unresolved == 0, "necessary viewing repair was not completed"
    else:
        assert unresolved > 0 and result.report.viewing_repair.rollbacks
        assert any(seg.translated_text == "你好" for seg in result.output_data.segments)
    summaries = {}
    for role in ("main", "review"):
        calls = [entry for entry in gateway.calls if entry["role"] == role]
        attempts = [entry for entry in gateway.attempts if entry["role"] == role]
        summaries[role] = {
            "logical": len(calls),
            "attempts": len(attempts),
            "cache_hits": sum(entry.get("cache_hit", False) for entry in calls),
            "logical_seconds": distribution([entry["end"] - entry["start"] for entry in calls]),
            "attempt_seconds": distribution([entry["end"] - entry["start"] for entry in attempts]),
            "subject_counts": [entry["subject_count"] for entry in calls],
            "input_tokens_estimate": sum(entry["input_tokens_estimate"] for entry in calls),
            "output_tokens_estimate": sum(
                entry.get("output_tokens_estimate", 0) for entry in calls
            ),
        }
    selected = {
        "run_pre_stage",
        "run_post_stage",
        "plan_repair_batches",
        "scan_viewing_lengths",
        "_validate_output",
        "save_canonical_srt",
        "_publish_module_outputs",
        "_load_and_classify",
    }
    stages = {}
    stats = getattr(pstats.Stats(profiler), "stats") if profiled else {}
    for (_filename, _line, name), (_cc, nc, tt, ct, _callers) in stats.items():
        if name in selected:
            stages[name] = {"calls": nc, "self_seconds": tt, "inclusive_seconds": ct}
    for entry in gateway.calls + gateway.attempts:
        entry["start"] -= start
        entry["end"] -= start
    return {
        "baseline_id": BASELINE_ID,
        "case": case,
        "input": {
            "sha256": fingerprint(before),
            "bytes": len(before),
            "segments": len(data.segments),
        },
        "config": config_payload(config),
        "roles": {
            role: {
                key: value
                for key, value in profile.to_dict().items()
                if key not in ("api_key", "base_url")
            }
            for role, profile in profiles.items()
        },
        "translation_snapshot": task.translation_snapshot.to_persisted(),
        "concurrency": {
            "gateway_requested": concurrency,
            "profile_clamp": None,
            "task_thread_num": task.thread_num,
            "repair_summary": (
                {
                    key: getattr(result.report.viewing_repair, key)
                    for key in (
                        "thread_num",
                        "concurrency_gate",
                        "effective_concurrency",
                        "max_inflight",
                        "concurrent_rounds",
                    )
                }
                if result.report.viewing_repair is not None
                else None
            ),
        },
        "controlled_response": {
            "protocol": "synthetic-v1",
            "main_delay": main_delay,
            "review_delay": review_delay,
        },
        "outcome": {
            "status": task.status,
            "continue_downstream": result.continue_downstream,
            "source_unchanged": source.read_bytes() == before,
            "unresolved": unresolved,
            "output_segments": len(result.output_data.segments),
            "output_sha256": fingerprint(output.read_bytes()),
            "warnings": list(result.warnings),
            "process_state_delivered": True,
            "qa_delivered": True,
            "process_state_unresolved": delivered_state["unresolved_viewing_problems"],
            "repair": vars(result.report.viewing_repair) if result.report.viewing_repair else None,
        },
        "coverage": {
            "main_input_segments": sum(
                entry["segment_count"] for entry in gateway.calls if entry["role"] == "main"
            ),
            "review_input_segments": sum(
                entry["segment_count"] for entry in gateway.calls if entry["role"] == "review"
            ),
        },
        "requests": summaries,
        "request_events": gateway.calls,
        "attempt_events": gateway.attempts,
        "progress": progress,
        "measurements": {
            "profiler_enabled": profiled,
            "task_wall_seconds": wall,
            "process_cpu_seconds": cpu,
            "local_wall_excluding_gateway": wall
            - _interval_union([(entry["start"], entry["end"]) for entry in gateway.calls]),
            "max_inflight": gateway.max_inflight,
            "queue_seconds": None,
            "queue_note": "not instrumented in task; actual semaphore wait measured by transport queue probe",
            "gateway_wall_union_seconds": _interval_union(
                [(entry["start"], entry["end"]) for entry in gateway.calls]
            ),
            "backoff_seconds": sum(gateway.backoffs),
            "local_stages": stages,
            "stage_note": "cProfile inclusive walls overlap; not additive; no per-stage CPU measurement",
            "progress_max_gap_seconds": max(
                b - a
                for a, b in zip(
                    [0.0] + [event["elapsed"] for event in progress],
                    [event["elapsed"] for event in progress] + [wall],
                )
            ),
        },
    }


def _write_json(path, payload):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, default=lambda obj: vars(obj))
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new directory only")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--case", choices=CASES, action="append")
    parser.add_argument("--cache-state", choices=("cold", "warm"), default="cold")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--skip-probes", action="store_true", help="task-only diagnostic; not a full acceptance run"
    )
    parser.add_argument(
        "--probe-worker", choices=("queue", "network", "backoff", "ui"), help=argparse.SUPPRESS
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--worker-output", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    os.environ["PYTHONIOENCODING"] = "utf-8"
    args.output = args.output.resolve()
    if args.repeats < 1 or args.concurrency < 1:
        parser.error("repeats and concurrency must be positive")
    if args.probe_worker:
        os.environ["VIDEOCAPTIONER_APPDATA_PATH"] = str((args.output / "appdata").resolve())
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        with offline_network():
            if args.probe_worker == "ui":
                from scripts.postprocess_ui_probe import run_ui_probe

                report = run_ui_probe(args.output / "task")
                if report["outcome"]["hard_cap_exceeded"] or not all(
                    report["outcome"]["gating"].values()
                ):
                    raise RuntimeError("UI probe failed cancellation/delivery/cleanup invariants")
            else:
                from scripts.postprocess_transport_probe import run_probe

                report = run_probe(args.probe_worker)
                if (
                    not report["ok"]
                    or not report["cleanup"]["server_thread_exited"]
                    or not report["cleanup"]["handler_answers_drained"]
                    or not all(item["joined"] for item in report["cleanup"]["worker_joins"])
                ):
                    raise RuntimeError(f"transport probe infrastructure failed: {report['errors']}")
        _write_json(args.worker_output, report)
        return
    if args.worker:
        os.environ["VIDEOCAPTIONER_APPDATA_PATH"] = str(
            (args.output.parent / (args.output.name + "-appdata")).resolve()
        )
        report = run_case(
            args.case[0],
            args.output,
            cache_state=args.cache_state,
            concurrency=args.concurrency,
            main_delay=0.4 if args.case[0] == "slow-main" else 0.05,
        )
        _write_json(args.worker_output, report)
        return
    args.output.mkdir(parents=True, exist_ok=False)
    # Must precede all project imports, including logger/cache module initialization.
    os.environ["VIDEOCAPTIONER_APPDATA_PATH"] = str((args.output / "appdata").resolve())
    root = Path(__file__).resolve().parents[1]
    source_fingerprints = {
        str(path.relative_to(root)): fingerprint(path.read_bytes())
        for folder in (root / "videocaptioner", root / "scripts")
        for path in sorted(folder.rglob("*.py"))
        if path.name != "_version.py"
    }
    reports = []
    try:
        for case in args.case or CASES:
            for repeat in range(args.repeats):
                sample = args.output / f"{case}-{repeat + 1}.json"
                command = [
                    sys.executable,
                    "-m",
                    "scripts.postprocess_benchmark",
                    "--worker",
                    "--output",
                    str(args.output / f"{case}-{repeat + 1}"),
                    "--worker-output",
                    str(sample),
                    "--case",
                    case,
                    "--cache-state",
                    args.cache_state,
                    "--concurrency",
                    str(args.concurrency),
                ]
                # Infrastructure watchdog only; the product task receives no whole-task deadline.
                with (args.output / f"{case}-{repeat + 1}.log").open("x", encoding="utf-8") as log:
                    subprocess.run(
                        command, cwd=root, check=True, timeout=180, stdout=log, stderr=log
                    )
                reports.append(json.loads(sample.read_text(encoding="utf-8")))
        probes = []
        if not args.skip_probes:
            for kind in ("queue", "network", "backoff", "ui"):
                for repeat in range(args.repeats):
                    sample = args.output / f"probe-{kind}-{repeat + 1}.json"
                    command = [
                        sys.executable,
                        "-m",
                        "scripts.postprocess_benchmark",
                        "--probe-worker",
                        kind,
                        "--output",
                        str(args.output / f"probe-{kind}-{repeat + 1}"),
                        "--worker-output",
                        str(sample),
                    ]
                    with sample.with_suffix(".log").open("x", encoding="utf-8") as log:
                        subprocess.run(
                            command, cwd=root, check=True, timeout=60, stdout=log, stderr=log
                        )
                    probes.append(
                        {"kind": kind, "result": json.loads(sample.read_text(encoding="utf-8"))}
                    )
        for relative, digest in source_fingerprints.items():
            if fingerprint((root / relative).read_bytes()) != digest:
                raise RuntimeError(
                    f"source changed during measurement: {relative}; rerun in a stable tree"
                )
        artifact = {
            "probes": probes,
            "probes_skipped": args.skip_probes,
            "baseline_id": BASELINE_ID,
            "code_revision": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True, encoding="utf-8"
            ).strip(),
            "working_tree": subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=root, text=True, encoding="utf-8"
            ),
            "source_fingerprints": source_fingerprints,
            "dependency_lock_sha256": fingerprint((root / "uv.lock").read_bytes()),
            "environment": {
                "python": sys.version,
                "platform": platform.platform(),
                "cpu_count": os.cpu_count(),
                "dependencies": {
                    name: version(name)
                    for name in ("openai", "httpx", "diskcache", "PyQt5", "pytest")
                },
                "uv": subprocess.check_output(["uv", "--version"], text=True).strip(),
            },
            "runs": reports,
            "summary": {
                case: distribution(
                    [r["measurements"]["task_wall_seconds"] for r in reports if r["case"] == case]
                )
                for case in args.case or CASES
            },
            "real_model_validation": "not executed; separate scope and cost approval required",
            "historical_silence": "not attributed",
        }
        _write_json(args.output / "report.json", artifact)
        print(json.dumps(artifact["summary"], indent=2))
    finally:
        from videocaptioner.core.utils import cache

        for getter in (
            cache.get_gateway_cache,
            cache.get_asr_cache,
            cache.get_translate_cache,
            cache.get_tts_cache,
            cache.get_timing_cache,
        ):
            getter().close()


if __name__ == "__main__":
    main()
