"""Offscreen Qt presentation probe for the postprocess performance baseline.

Run with ``uv run python -m scripts.postprocess_ui_probe --output DIR`` (new
directory only).  The benchmark wrapper invokes it as ``run_ui_probe()``.

What this probe measures (ticket 01): the REAL ``PostprocessThread`` (QThread)
executes the REAL ``run_postprocess_task`` repair path against a controlled
synthetic adapter installed behind the REAL ``LLMGateway`` (gates, retries and
cache policy are the gateway's own).  An offscreen QApplication event loop,
driven by a QTimer heartbeat, receives the thread's real progress/cancelled
signals.  An event barrier (worker→GUI signal) fires when the first controlled
main repair request starts; the GUI loop then calls ``thread.stop()`` while
that request is still in flight.

Presentation scope is stated in every report: QThread signal delivery to the
event loop only — NOT widget refresh.  No page is constructed (minimum
integration); the worker's real progress signal plus a QTimer is the measured
presentation surface.

Current-behaviour gaps this probe deliberately exposes (never masked, never
faked by shortening the controlled delay): stop cannot preempt the in-flight
request — the terminal state waits for the request to return naturally
(``stop_during_inflight`` / ``terminal_after_inflight_end``), and no progress
signal is emitted while a model request is in flight
(``silent_during_inflight_request``).  Ticket 06 changes the behaviour and
updates these baselines; this module only measures.

No real provider is contacted: the offline socket guard rejects every
non-loopback connect, the adapter answers synthetically (protocol aligned with
``scripts/postprocess_benchmark`` synthetic-v1: main → one-to-one split with
``你好``, review → ``您好`` corrections), and the caller must isolate AppData
via ``VIDEOCAPTIONER_APPDATA_PATH``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import statistics
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

PROBE_ID = "postprocess-ui-probe-v1"
SCENARIO = "stop-during-first-main-repair"
PRESENTATION_SCOPE = (
    "real PostprocessThread signals delivered through the offscreen QApplication"
    " event loop (QTimer heartbeat + queued pyqtSignal); measures signal"
    " presentation only, NOT widget refresh"
)
# Controlled main request length: >= 1s so the 500ms refresh target is testable
# against a clearly observable window; never shortened to fake fast stop.
MAIN_DELAY_S = 1.2
REVIEW_DELAY_S = 0.05
GATE_CONCURRENCY = 4
HEARTBEAT_MS = 10
WATCHDOG_S = 30.0
THREAD_JOIN_S = 5.0
INPUT_SEGMENTS = 60
PROBLEM_STRIDE = 3
BOUNDARY_RADIUS = 2


@contextmanager
def _offline_network():
    """Reject non-loopback sockets; a probe must never reach a real provider."""

    original = socket.socket.connect
    original_ex = socket.socket.connect_ex

    def connect(sock, address):
        if not isinstance(address, tuple) or address[0] not in ("127.0.0.1", "::1"):
            raise RuntimeError("offline probe forbids non-loopback connections")
        return original(sock, address)

    def connect_ex(sock, address):
        if not isinstance(address, tuple) or address[0] not in ("127.0.0.1", "::1"):
            raise RuntimeError("offline probe forbids non-loopback connections")
        return original_ex(sock, address)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    try:
        yield
    finally:
        socket.socket.connect = original
        socket.socket.connect_ex = original_ex


def _fingerprint(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _decode_request(request):
    """Extract (role, payload) from the repair protocol's <input> JSON."""

    user = request.messages[-1].content
    match = re.search(r"<input>(.*?)</input>", user, re.S)
    if match is None:
        raise ValueError("unexpected probe request protocol")
    payload = json.loads(match.group(1))
    return ("review" if "review_subjects" in payload else "main"), payload


class _ColdCache:
    """No lookup, no store: the cancellation window must not replay a warm hit."""

    def lookup(self, *_args):
        return None

    def store(self, *_args):
        pass


class _BarrierSignal(QObject):
    """Worker→GUI event barrier object (queued pyqtSignal, thread-safe)."""

    reached = pyqtSignal()


class _ProbeGateway:
    """Controlled synthetic adapter installed behind the REAL ``LLMGateway``.

    Self-contained on purpose: the UI probe must not depend on benchmark
    internals.  The response protocol is aligned with the benchmark's
    synthetic-v1 (main: one-to-one split, translated ``你好``; review:
    ``您好`` corrections) so both baselines describe the same repair path.
    """

    def __init__(self, *, main_delay: float, review_delay: float):
        from videocaptioner.core.llm.gateway import LLMGateway

        self.main_delay = main_delay
        self.review_delay = review_delay
        self.calls: list[dict] = []
        self.attempts: list[dict] = []
        self.lock = threading.Lock()
        self.barrier = _BarrierSignal()
        self.barrier_emit_at: float | None = None
        self._barrier_fired = False
        self.runtime = LLMGateway(
            adapter_factory=self._adapter,
            max_concurrency=GATE_CONCURRENCY,
            response_cache=_ColdCache(),
            sleep=time.sleep,
            random_source=lambda: 0.5,
        )

    def _fire_barrier_once(self, role: str) -> None:
        with self.lock:
            if role != "main" or self._barrier_fired:
                return
            self._barrier_fired = True
            self.barrier_emit_at = time.perf_counter()
        self.barrier.reached.emit()

    def _adapter(self, profile):
        owner = self

        class Adapter:
            def __init__(self):
                self.profile = profile

            def complete(self, request):
                from videocaptioner.core.llm.models import LLMResult

                role, payload = _decode_request(request)
                owner._fire_barrier_once(role)
                start = time.perf_counter()
                try:
                    time.sleep(owner.main_delay if role == "main" else owner.review_delay)
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
                                repairs.append(
                                    {
                                        "problem_id": segment["problem_ids"][0],
                                        "output_index": 0,
                                        "original": segment["text"],
                                        "translated": "你好",
                                    }
                                )
                        response = {"repairs": repairs}
                    return LLMResult(text=json.dumps(response, ensure_ascii=False, sort_keys=True))
                finally:
                    with owner.lock:
                        owner.attempts.append(
                            {
                                "role": role,
                                "start": start,
                                "end": time.perf_counter(),
                            }
                        )

            def close(self):
                pass

        return Adapter()

    def complete(self, profile, request, **kwargs):
        role, _payload = _decode_request(request)
        entry = {"role": role, "start": time.perf_counter()}
        try:
            return self.runtime.complete(profile, request, **kwargs)
        finally:
            entry["end"] = time.perf_counter()
            with self.lock:
                self.calls.append(entry)

    def close(self) -> None:
        self.runtime.close()


def _synthetic_input():
    """60 bilingual segments; every 3rd carries an over-limit translation."""

    from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg

    segments = []
    for index in range(INPUT_SEGMENTS):
        original = f"Item {index}: hello."
        translated = "你好"
        if index % PROBLEM_STRIDE == 0:
            translated = "这是一个用于离线测试的非常冗长且需要修复的字幕翻译内容"
        segments.append(ASRDataSeg(original, index * 8000, index * 8000 + 6000, translated))
    return ASRData(segments)


def _guard_isolated_appdata() -> str:
    """The caller owns AppData isolation (spec: probe must not manage it)."""

    appdata = os.environ.get("VIDEOCAPTIONER_APPDATA_PATH", "").strip()
    repo_appdata = (Path(__file__).resolve().parents[1] / "AppData").resolve()
    if not appdata:
        raise RuntimeError(
            "VIDEOCAPTIONER_APPDATA_PATH must be set by the caller to an isolated"
            " AppData root before running the UI probe"
        )
    if Path(appdata).resolve() == repo_appdata:
        raise RuntimeError(
            "VIDEOCAPTIONER_APPDATA_PATH points at the repository AppData;"
            " the probe requires an isolated root"
        )
    return appdata


def run_ui_probe(directory: Path | None = None) -> dict:
    """Run the stop-during-first-main-repair presentation probe.

    Returns the baseline report dict.  ``directory`` (new only) holds the
    synthetic input and the cancelled output slot; ``None`` (benchmark wrapper
    mode) uses a fresh temporary directory with the caller's AppData.
    """

    appdata = _guard_isolated_appdata()
    if directory is None:
        directory = Path(tempfile.mkdtemp(prefix="postprocess-ui-probe-"))
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)

    from PyQt5.QtWidgets import QApplication

    from videocaptioner.core.entities import SubtitleLayoutEnum
    from videocaptioner.core.llm.models import LLMModelProfile, LLMTransport, ProviderDialect
    from videocaptioner.core.postprocess.config import PostprocessConfig, config_payload
    from videocaptioner.core.postprocess.models import PostprocessLayoutMode, PostprocessTask
    from videocaptioner.core.postprocess.translation import TranslationExecutionSnapshot
    from videocaptioner.core.subtitle.io import save_canonical_srt
    from videocaptioner.ui.thread.postprocess_thread import PostprocessThread

    app = QApplication.instance()
    if app is None:
        app = QApplication([])

    source, output = directory / "input.srt", directory / "result.srt"
    data = _synthetic_input()
    save_canonical_srt(data, source, layout=SubtitleLayoutEnum.ORIGINAL_ON_TOP)
    before = source.read_bytes()
    config = PostprocessConfig(
        trim_trailing_punct=False,
        qa_report=True,
        speed_optimize=False,
        speed_semantic_repair=False,
    )
    profiles = {
        role: LLMModelProfile(
            profile_id=f"ui-probe-{role}",
            name=f"ui-probe-{role}",
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
    gateway = _ProbeGateway(main_delay=MAIN_DELAY_S, review_delay=REVIEW_DELAY_S)
    task = PostprocessTask(
        str(source),
        postprocessed_subtitle_path=str(output),
        layout_mode=PostprocessLayoutMode.ORIGINAL_ON_TOP,
        config_snapshot=config,
        # 停止/静默探针固定并发 1（票 04）：本探针测量「在途请求期间停止
        # 的 GUI 呈现与响应」，窗口并发行为由修复并发测试与基准覆盖。
        thread_num=1,
        translation_snapshot=TranslationExecutionSnapshot(
            method="enhanced_llm",
            main_profile=profiles["main"],
            review_profile=profiles["review"],
            boundary_context_radius=BOUNDARY_RADIUS,
        ),
    )
    thread = PostprocessThread(task, gateway=gateway)

    class _Recorder:
        """GUI-side presentation recorder: heartbeat, signals, stop decision."""

        def __init__(self):
            self.progress: list[dict] = []
            self.heartbeats: list[float] = []
            self._last_tick: float | None = None
            self.stop_at: float | None = None
            self.barrier_recv_at: float | None = None
            self.terminal_at: float | None = None
            self.terminal_signal: str | None = None
            self.finished_events: list[tuple[str, str]] = []
            self.warnings: list[str] = []
            self.errors: list[str] = []
            self.hard_cap_exceeded = False
            self._tick = QTimer()
            self._tick.timeout.connect(self._on_tick)
            self._watchdog = QTimer()
            self._watchdog.setSingleShot(True)
            self._watchdog.timeout.connect(self._on_watchdog)

        def start(self) -> None:
            self._last_tick = time.perf_counter()
            self._tick.start(HEARTBEAT_MS)
            self._watchdog.start(int(WATCHDOG_S * 1000))

        def _on_tick(self) -> None:
            now = time.perf_counter()
            if self._last_tick is not None:
                self.heartbeats.append(now - self._last_tick)
            self._last_tick = now

        def _on_watchdog(self) -> None:
            self.hard_cap_exceeded = True
            app.quit()

        def on_progress(self, value, message) -> None:
            self.progress.append(
                {
                    "at_s_from_task_start": round(time.perf_counter() - started, 6),
                    "value": value,
                    "message": message,
                }
            )

        def on_barrier(self) -> None:
            # Event barrier: the first controlled main request just started.
            self.barrier_recv_at = time.perf_counter()
            self.stop_at = time.perf_counter()
            thread.stop()

        def on_cancelled(self) -> None:
            self.terminal_at = time.perf_counter()
            self.terminal_signal = "cancelled"
            app.quit()

        def on_finished(self, video, path) -> None:
            self.finished_events.append((video, path))
            if self.terminal_signal is None:
                self.terminal_at = time.perf_counter()
                self.terminal_signal = "finished"
                app.quit()

        def on_warning(self, message) -> None:
            self.warnings.append(message)

        def on_error(self, message) -> None:
            self.errors.append(message)
            if self.terminal_signal is None:
                self.terminal_at = time.perf_counter()
                self.terminal_signal = "error"
                app.quit()

    started = time.perf_counter()
    recorder = _Recorder()
    thread.progress.connect(recorder.on_progress)
    thread.warning.connect(recorder.on_warning)
    thread.error.connect(recorder.on_error)
    thread.finished.connect(recorder.on_finished)
    thread.cancelled.connect(recorder.on_cancelled)
    gateway.barrier.reached.connect(recorder.on_barrier)

    try:
        with _offline_network():
            thread.start()
            recorder.start()
            app.exec_()
    finally:
        gateway.close()
    thread_exited = thread.wait(int(THREAD_JOIN_S * 1000))
    wall = time.perf_counter() - started

    result = thread.result
    repair = getattr(getattr(result, "report", None), "viewing_repair", None)
    main_attempts = [entry for entry in gateway.attempts if entry["role"] == "main"]
    review_attempts = [entry for entry in gateway.attempts if entry["role"] == "review"]
    first = main_attempts[0] if main_attempts else None
    # Cancellation must not deliver downstream process assets (D28): the
    # workspace manifest from asset discovery may exist, but none of the
    # module-success outputs (qa_report / postprocess_state / speed_changes)
    # may. A missing output.srt alone must not mask this delivery gate.
    discovery = task.asset_discovery
    task_dir = getattr(discovery, "task_dir", None) if discovery is not None else None
    delivered_assets = {
        kind: bool(task_dir is not None and (task_dir / filename).is_file())
        for kind, filename in (
            ("qa_report", "qa-report.md"),
            ("postprocess_state", "postprocess-state.json"),
            ("speed_changes", "speed-changes.json"),
        )
    }

    stop_at, terminal_at = recorder.stop_at, recorder.terminal_at
    stop_during_inflight = bool(
        first is not None and stop_at is not None and first["start"] <= stop_at <= first["end"]
    )
    terminal_after_inflight_end = bool(
        first is not None and terminal_at is not None and terminal_at >= first["end"]
    )
    barrier_to_stop_s = (
        round(stop_at - first["start"], 6) if first is not None and stop_at is not None else None
    )
    stop_to_terminal_s = (
        round(terminal_at - stop_at, 6) if terminal_at is not None and stop_at is not None else None
    )
    receptions_after_stop = (
        sum(1 for entry in recorder.progress if entry["at_s_from_task_start"] + started > stop_at)
        if stop_at is not None
        else None
    )
    silent_during_inflight = (
        receptions_after_stop == 0 if receptions_after_stop is not None else None
    )
    progress_ats = [entry["at_s_from_task_start"] + started for entry in recorder.progress]
    last_emission_to_terminal_s = (
        round(terminal_at - progress_ats[-1], 6)
        if terminal_at is not None and progress_ats
        else None
    )
    progress_max_gap_s = (
        max(b - a for a, b in zip([started] + progress_ats, progress_ats)) if progress_ats else None
    )
    delivery_max_latency_ms = (
        round((recorder.barrier_recv_at - gateway.barrier_emit_at) * 1000, 3)
        if recorder.barrier_recv_at is not None and gateway.barrier_emit_at is not None
        else None
    )
    heartbeats = recorder.heartbeats
    # Heartbeat is in SECONDS here; consumers (report/matrix) read the
    # *_ms fields. Thresholds must never mix units.
    heartbeat = {
        "count": len(heartbeats),
        "median_s": round(statistics.median(heartbeats), 6) if heartbeats else None,
        "max_s": round(max(heartbeats), 6) if heartbeats else None,
        "median_ms": (round(statistics.median(heartbeats) * 1000, 3) if heartbeats else None),
        "max_ms": round(max(heartbeats) * 1000, 3) if heartbeats else None,
    }
    terminal_signal = recorder.terminal_signal
    if terminal_signal is None and recorder.hard_cap_exceeded:
        terminal_signal = "watchdog"
    # Current-behaviour gap classification (facts, not failures): ticket 06
    # changes the behaviour and updates these categories; this probe only
    # classifies. No narrow time-window assertions belong in consumers.
    baseline_gaps = []
    if stop_during_inflight and terminal_after_inflight_end:
        baseline_gaps.append(
            "stop does not preempt the in-flight model request: the terminal"
            " state waits for the request to return naturally"
        )
    if silent_during_inflight:
        baseline_gaps.append(
            "no progress signal is emitted while a model request is in flight"
            " (the last progress stays on the repair-round message)"
        )
    if terminal_signal != "cancelled":
        baseline_gaps.append(f"terminal signal was {terminal_signal!r}, expected cancelled")

    return {
        "baseline_gaps": baseline_gaps,
        "probe_id": PROBE_ID,
        "scenario": SCENARIO,
        "presentation_scope": PRESENTATION_SCOPE,
        "appdata": {"root": str(Path(appdata).resolve())},
        "input": {
            "sha256": _fingerprint(before),
            "bytes": len(before),
            "segments": len(data.segments),
            "problem_segments": sum(
                1 for index in range(INPUT_SEGMENTS) if index % PROBLEM_STRIDE == 0
            ),
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
        "controlled_response": {
            "socket": "none",
            "protocol": "synthetic-v1-aligned",
            "main_delay": MAIN_DELAY_S,
            "review_delay": REVIEW_DELAY_S,
            "heartbeat_ms": HEARTBEAT_MS,
            "note": "main request >= 1s on purpose; never shortened to fake fast stop",
        },
        "outcome": {
            "terminal_signal": terminal_signal,
            "task_status": task.status,
            "hard_cap_exceeded": recorder.hard_cap_exceeded,
            "repair_flow": {
                "mode": getattr(repair, "flow_mode", None),
                "requests": getattr(repair, "requests", None),
            },
            "gating": {
                "source_unchanged": source.read_bytes() == before,
                "no_postprocessed_output": not output.exists(),
                "active_is_initial": task.active_subtitle_path == task.initial_subtitle_path,
                "downstream_blocked": bool(
                    result is not None and result.continue_downstream is False
                ),
                "thread_exited": thread_exited,
                "terminal_signal_cancelled": terminal_signal == "cancelled"
                and not recorder.finished_events
                and not recorder.errors,
                # Cancellation delivers no process assets (D28): manifest may
                # exist from discovery, downstream outputs must not.
                "no_delivered_process_assets": not any(delivered_assets.values()),
                "workspace_manifest_allowed": True,
            },
            "delivered_process_assets": delivered_assets,
        },
        "gateway": {
            "requests_main": len([entry for entry in gateway.calls if entry["role"] == "main"]),
            "requests_review": len([entry for entry in gateway.calls if entry["role"] == "review"]),
            "attempts_main": len(main_attempts),
            "attempts_review": len(review_attempts),
        },
        "timeline": {
            "stop_during_inflight": stop_during_inflight,
            "inflight_role": first["role"] if first else None,
            "terminal_after_inflight_end": terminal_after_inflight_end,
            "stop_to_terminal_signal_s": stop_to_terminal_s,
            "barrier_to_stop_s": barrier_to_stop_s,
            "request_window_s": (round(first["end"] - first["start"], 6) if first else None),
        },
        "gui": {
            "progress_emission_count": len(recorder.progress),
            "delivery_pairing_complete": all(
                entry.get("value") is not None and entry.get("message") is not None
                for entry in recorder.progress
            ),
            "heartbeat": heartbeat,
            "delivery_max_latency_ms": delivery_max_latency_ms,
            "silent_during_inflight_request": silent_during_inflight,
            "last_emission_to_terminal_s": last_emission_to_terminal_s,
            "progress_max_gap_s": (
                round(progress_max_gap_s, 6) if progress_max_gap_s is not None else None
            ),
            "progress_events": recorder.progress,
            "warnings": recorder.warnings,
            "errors": recorder.errors,
        },
        "measurements": {
            "wall_seconds": round(wall, 6),
            "watchdog_seconds": WATCHDOG_S,
            "thread_join_timeout_seconds": THREAD_JOIN_S,
        },
        "real_model_validation": (
            "not executed; synthetic offline adapter behind the real gateway,"
            " non-loopback sockets rejected, no API key"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new directory only")
    args = parser.parse_args(argv)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    # AppData isolation is the caller's contract; setdefault never overrides an
    # already-isolated root (benchmark wrapper, tests, conftest).
    os.environ.setdefault("VIDEOCAPTIONER_APPDATA_PATH", str((output / "appdata").resolve()))
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    report = run_ui_probe(output / "probe")
    with (output / "report.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, default=str)
        stream.write("\n")
    print(
        json.dumps(
            {
                "probe_id": report["probe_id"],
                "scenario": report["scenario"],
                "terminal_signal": report["outcome"]["terminal_signal"],
                "stop_to_terminal_signal_s": report["timeline"]["stop_to_terminal_signal_s"],
                "silent_during_inflight_request": report["gui"]["silent_during_inflight_request"],
                "hard_cap_exceeded": report["outcome"]["hard_cap_exceeded"],
                "thread_exited": report["outcome"]["gating"]["thread_exited"],
            },
            ensure_ascii=False,
        )
    )
    # Hard gate: only delivery invariants fail the run. baseline_gaps are
    # observed current-behaviour facts (upgraded by ticket 06), never errors.
    gating_ok = (
        all(report["outcome"]["gating"].values())
        and report["outcome"]["terminal_signal"] == "cancelled"
        and report["outcome"]["task_status"] == "cancelled"
    )
    return 0 if gating_ok and not report["outcome"]["hard_cap_exceeded"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
