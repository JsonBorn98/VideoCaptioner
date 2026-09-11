"""Real-transport boundary probes for the postprocess performance baseline (ticket 01).

Run with ``uv run python -m scripts.postprocess_transport_probe --kind queue --output FILE``.

These probes drive the REAL transport stack — ``LLMGateway`` with its default
adapter factory → real ``OpenAICompatibleAdapter`` → real openai SDK/httpx →
a loopback-only TCP HTTP server — and measure how the current baseline
responds to cancellation. A fake gateway cannot answer HTTP in-flight
cancellation, adapter-level retries or semaphore waits; only the real
transport can (spec: 真实传输边界).

Counter semantics (kept distinct on purpose):
- ``adapter_attempts`` — one per ``adapter.complete`` call, counted from the
  gateway's own request log (``llm_requests.jsonl``) under this probe's stage.
- ``http_attempts`` — one per real HTTP request, counted by the loopback
  server. When the SDK retries implicitly these two diverge; the measured
  difference is reported, never assumed in advance. A missing request log is
  an infrastructure error (``ok=False``), never faked as zero attempts.

Probe kinds (three, deliberately not more):
- ``queue`` — four logical requests against an explicit gate of two: two real
  in-flight HTTP hangs (read timeout 30s never fires; a planned release at
  2.0s is the stimulus) plus two workers queued on the gate that entered
  their gateway calls but sent no HTTP. This is the scenario the future
  network-stop threshold (ticket 06) is measured against.
- ``network`` — one request with a short read timeout (1.0s): annotates the
  SDK timeout behaviour and the implicit-retry multiplication inside one
  adapter attempt, plus the gateway backoff boundary (stop observed before
  the backoff even starts).
- ``backoff`` — persistent 429 + ``Retry-After``: stop lands inside the
  gateway's own backoff sleep once that sleep has actually started.

Every ``baseline_gaps`` entry is computed from the measured counters of the
run that produced it. Gaps are observed baseline facts, never masked and
never reported as infrastructure ``errors`` (an ``InterruptedError`` outcome
is the expected business response to stop, not a failure). Ticket 06 changes
the behaviour and updates these baselines; this module only measures it.
Forward-looking gates are reported per run: ``stop.prompt`` (everything ended
within a short grace of stop) and ``stop.stopped_between_cancel_and_release``
(every worker reached a terminal state after stop but before the planned
release stimulus) — both False on the current baseline.

No real provider is contacted and no API key is needed: the server binds
127.0.0.1 only and ``hold`` responses never return naturally — a response
exists only when the probe's controlled release fires (or the bounded cap).
Every measured wait is >= 0.3s so real timing, not sleep granularity, is
observed. Cleanup is fully bounded and verified, never assumed:
``Thread.join()`` returns None, so exit is proven with ``is_alive()``;
``gateway.close()`` is always called; after the release the probe waits for
every server handler to finish its answer (an unfinished attempt keeps
``ended_s: null`` — never faked from its start time) and a worker still
alive after the release is an infrastructure failure (``ok=False``).
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROBE_SCHEMA = "transport-probe-v1"
PROBE_KINDS = ("queue", "network", "backoff")

# Watchdog bounds for every probe run. These guard against hangs (bounded
# cleanup); they are NOT performance gates. Observed latencies go in the report.
SERVER_SHUTDOWN_TIMEOUT = 10.0
HANDLER_DRAIN_TIMEOUT = 10.0  # every started answer must finish after release
WORKER_JOIN_TIMEOUT = 20.0
VERIFICATION_JOIN_TIMEOUT = 10.0  # workers must exit after the release
HOLD_RELEASE_CAP = 12.0  # absolute cap on any single held response
ENTRY_WAIT_TIMEOUT = 20.0  # waiting for an observed wait-phase entry event
POST_ENTRY_DELAY = 0.3  # measured wait offset after entry, before cancelling
CLI_ARG_OUTPUT = "--output"


def _completion_body(text: str) -> dict:
    return {
        "id": "chatcmpl-probe",
        "object": "chat.completion",
        "created": 0,
        "model": "probe-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
    }


class LoopbackProbeService:
    """Loopback-only HTTP server whose ``/v1/chat/completions`` never answers naturally.

    ``hold`` mode blocks each response until :meth:`release` (bounded by
    ``HOLD_RELEASE_CAP``); ``reject`` mode answers HTTP 429 with ``Retry-After``.
    One entry is recorded per REAL HTTP attempt, with server-side start/end
    times so in-flight concurrency is observed, not inferred from client logs.
    Every response carries ``Connection: close`` so the handler thread exits
    with its answer instead of idling on a keep-alive loop.
    """

    def __init__(self) -> None:
        self.mode = "hold"
        self.retry_after: str | None = None
        self._attempts: list[dict] = []
        self._natural_returns = 0
        self._released_writes = 0
        self._lock = threading.Lock()
        self._hold_added = threading.Condition(self._lock)
        self._released = False
        self.first_release_raw: float | None = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self.server.daemon_threads = True
        self._thread = threading.Thread(
            target=self.server.serve_forever, name="transport-probe-server", daemon=True
        )
        self._thread.start()

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:  # keep probe output clean
                del args

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                began = time.perf_counter()
                with owner._lock:
                    previous = owner._attempts[-1]["started_raw"] if owner._attempts else None
                    # Index captured under the lock: concurrent handlers must
                    # never overwrite each other's end times ([-1] would race).
                    owner._attempts.append(
                        {
                            "mode": owner.mode,
                            "started_raw": began,
                            "gap_since_previous_s": (
                                round(began - previous, 6) if previous is not None else None
                            ),
                            "ended_raw": None,
                        }
                    )
                    index = len(owner._attempts) - 1
                ended = self._answer(began)
                with owner._lock:
                    owner._attempts[index]["ended_raw"] = ended

            def _answer(self, began: float) -> float:
                if owner.mode == "reject":
                    body = json.dumps(
                        {
                            "error": {
                                "message": "probe rate limit",
                                "type": "rate_limit_error",
                                "code": "rate_limit_exceeded",
                            }
                        }
                    ).encode("utf-8")
                    self._write(
                        429,
                        body,
                        {"Retry-After": owner.retry_after} if owner.retry_after else {},
                    )
                    return time.perf_counter()
                # hold: wait for the explicit release, bounded by the cap.
                with owner._lock:
                    owner._hold_added.notify_all()
                    while (
                        not owner._released
                        and time.perf_counter() - began < HOLD_RELEASE_CAP
                    ):
                        owner._hold_added.wait(timeout=0.05)
                    natural = not owner._released
                body = json.dumps(_completion_body("probe-ok")).encode("utf-8")
                self._write(200, body, {})
                with owner._lock:
                    if natural:
                        owner._natural_returns += 1
                    else:
                        owner._released_writes += 1
                return time.perf_counter()

            def _write(self, status: int, body: bytes, headers: dict[str, str]) -> None:
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    for name, value in headers.items():
                        self.send_header(name, value)
                    # Ends the keep-alive loop with this answer: the handler
                    # thread exits instead of idling on the next readline.
                    self.send_header("Connection", "close")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass  # the client already timed out and dropped the socket

        return Handler

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def set_mode(self, mode: str, *, retry_after: str | None = None) -> None:
        with self._lock:
            self.mode = mode
            self.retry_after = retry_after

    def attempt_count(self) -> int:
        with self._lock:
            return len(self._attempts)

    def first_attempt_started_raw(self) -> float | None:
        with self._lock:
            return self._attempts[0]["started_raw"] if self._attempts else None

    def unfinished_attempts(self) -> int:
        with self._lock:
            return sum(1 for entry in self._attempts if entry["ended_raw"] is None)

    def wait_for_attempts(self, count: int, timeout: float = ENTRY_WAIT_TIMEOUT) -> int:
        """Wait until the server has observed ``count`` real HTTP attempts."""
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if self.attempt_count() >= count:
                break
            time.sleep(0.01)
        return self.attempt_count()

    def release(self) -> None:
        """Force-release every held response; the cap keeps this bounded anyway."""
        with self._lock:
            if self.first_release_raw is None:
                self.first_release_raw = time.perf_counter()
            self._released = True
            self._hold_added.notify_all()

    def release_delayed(self, delay: float) -> threading.Timer:
        timer = threading.Timer(delay, self.release)
        timer.daemon = True
        timer.start()
        return timer

    def close(self, started: float) -> dict:
        """Shut down and PROVE cleanup: join by ``is_alive``, drain handlers."""
        self.release()
        self.server.shutdown()
        # serve_forever returning does not mean the per-request handler
        # threads finished; wait until every started answer has ended.
        deadline = time.perf_counter() + HANDLER_DRAIN_TIMEOUT
        while time.perf_counter() < deadline and self.unfinished_attempts():
            time.sleep(0.01)
        handlers_drained = self.unfinished_attempts() == 0
        self.server.server_close()
        self._thread.join(timeout=SERVER_SHUTDOWN_TIMEOUT)
        # Thread.join() always returns None; exit is proven via is_alive().
        server_thread_exited = not self._thread.is_alive()
        with self._lock:
            attempts = [
                {
                    "mode": entry["mode"],
                    "status": 429 if entry["mode"] == "reject" else 200,
                    "gap_since_previous_s": entry["gap_since_previous_s"],
                    "started_s": round(entry["started_raw"] - started, 6),
                    # An unfinished answer keeps null here; never faked.
                    "ended_s": (
                        None
                        if entry["ended_raw"] is None
                        else round(entry["ended_raw"] - started, 6)
                    ),
                }
                for entry in self._attempts
            ]
            return {
                "server_thread_exited": server_thread_exited,
                "handler_answers_drained": handlers_drained,
                "attempts": attempts,
                "inflight_peak": _peak_concurrency(attempts),
                "natural_returns": self._natural_returns,
                "released_writes": self._released_writes,
                "first_release_s": (
                    None
                    if self.first_release_raw is None
                    else round(self.first_release_raw - started, 6)
                ),
            }


def _peak_concurrency(attempts: list[dict]) -> int:
    """Peak concurrent in-flight HTTP attempts, from server-side intervals."""
    events: list[tuple[float, int]] = []
    for entry in attempts:
        events.append((entry["started_s"], 1))
        if entry["ended_s"] is not None:
            events.append((entry["ended_s"] + 1e-9, -1))
    peak = current = 0
    for _at, delta in sorted(events):
        current += delta
        peak = max(peak, current)
    return peak


def _plan(kind: str) -> dict:
    """Per-kind plan; entry events (not fixed sleeps) anchor the cancellation."""
    if kind == "queue":
        # Gate of two against four logical requests: two go in flight and are
        # held (read timeout 30s never fires; the planned release at 2.0s is
        # the stimulus); two block on the gate without sending any HTTP.
        return {
            "logical": 4,
            "gate": 2,
            "entry_attempts": 2,
            "release_after": 2.0,
            "read_timeout": 30.0,
        }
    if kind == "network":
        # Short read timeout: annotates the SDK timeout behaviour and the
        # implicit-retry multiplication inside one adapter attempt, plus the
        # gateway backoff boundary (stop observed before the backoff starts).
        return {
            "logical": 1,
            "gate": None,
            "entry_attempts": 1,
            "release_after": None,
            "read_timeout": 1.0,
        }
    # Persistent 429 + Retry-After: cancellation lands inside the gateway's
    # own backoff sleep, once that sleep has actually started.
    return {
        "logical": 1,
        "gate": None,
        "entry_attempts": None,  # entry event is the gateway backoff start
        "release_after": None,
        "read_timeout": 10.0,
    }


def _read_adapter_attempt_log(stage_nonce: str) -> tuple[list[dict], str | None]:
    """Read the gateway's real per-attempt request log under this run's AppData.

    One log line is written per ``adapter.complete`` call, so this counts
    adapter attempts — deliberately distinct from the server-side HTTP count.
    Returns ``(entries, error)``: a missing log or an empty match is an error,
    never faked as zero attempts.
    """

    try:
        from videocaptioner.config import APPDATA_PATH
    except Exception as exc:  # noqa: BLE001 —— 读不到配置就是设施失败
        return [], f"could not import APPDATA_PATH for the request log: {exc}"
    log_file = APPDATA_PATH / "logs" / "llm_requests.jsonl"
    if not log_file.exists():
        return [], (
            f"gateway request log missing at {log_file}; refusing to fake zero "
            "adapter attempts"
        )
    entries = []
    try:
        lines = log_file.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [], f"could not read the gateway request log: {exc}"
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn line from a concurrent write, not a missing log
        if entry.get("stage") != stage_nonce:
            continue
        error = entry.get("error") or {}
        entries.append(
            {
                "attempt": entry.get("attempt"),
                "status": entry.get("status"),
                "category": error.get("category"),
                "status_code": error.get("status_code"),
                "duration_ms": entry.get("duration_ms"),
            }
        )
    if not entries:
        return [], (
            f"no adapter attempt entries matched probe stage {stage_nonce!r}; "
            "refusing to fake zero adapter attempts"
        )
    return entries, None


def _baseline_gaps(report: dict) -> list[str]:
    """Describe the observed current-behaviour gaps (facts, never failures)."""

    gaps: list[str] = []
    probe = report["probe"]
    requests = report["requests"]
    service_attempts = report["http_attempt_log"]
    adapter_count = report["adapter_attempts"]
    sleeps = report["sleeps"]
    cancel_at = report["stop"]["cancel_at_s"]
    evidence = report["wait_entry_evidence"]
    interrupted = [entry for entry in requests if entry["outcome"] == "interrupted"]
    if interrupted:
        gaps.append(
            "cancellation surfaces as InterruptedError only at gateway attempt "
            "boundaries: queued and in-flight waits keep blocking until they end "
            f"({len(interrupted)} request(s) interrupted, none promptly)"
        )
    if service_attempts and len(service_attempts) > adapter_count:
        gaps.append(
            "implicit retries inside one adapter attempt multiply transport "
            f"attempts: {adapter_count} adapter attempt(s) produced "
            f"{len(service_attempts)} real HTTP requests"
        )
    if sleeps and cancel_at is not None:
        first = sleeps[0]
        if cancel_at < first["started_at_s"]:
            gaps.append(
                "gateway backoff ignores an already-observed stop: cancellation "
                f"was observed at {round(cancel_at, 3)}s, yet the gateway still "
                f"entered a fresh {first['requested_seconds']}s backoff sleep at "
                f"{first['started_at_s']}s and the worker waited it out in full"
            )
        else:
            gaps.append(
                "gateway backoff sleeps are not interruptible: a "
                f"{first['requested_seconds']}s backoff sleep was running when "
                f"stop arrived at {round(cancel_at, 3)}s and ran to completion "
                f"({first['ended_at_s']}s) instead of being cut short"
            )
    if probe == "backoff":
        honoured = [
            entry["gap_since_previous_s"]
            for entry in service_attempts[1:]
            if entry["gap_since_previous_s"] is not None
        ]
        if honoured and all(gap >= 1.0 for gap in honoured):
            gaps.append("Retry-After is honoured in full; the wait is not shortened by stop")
    if probe == "queue":
        queued = evidence.get("queued_waiting_for_gate")
        gaps.append(
            "semaphore queue and in-flight HTTP waits do not respond to stop: "
            f"{queued} queued worker(s) entered their gateway calls but sent no "
            "HTTP (blocked on the gate), stayed blocked until the in-flight "
            "requests finished, and in-flight requests delivered their late "
            "responses normally"
        )
    return gaps


def run_probe(kind: str) -> dict:
    """Run one real-transport cancellation probe and return its baseline report."""

    if kind not in PROBE_KINDS:
        raise ValueError(f"kind must be one of {PROBE_KINDS}, got {kind!r}")
    plan = _plan(kind)

    # Imports stay inside: VIDEOCAPTIONER_APPDATA_PATH must be settled first
    # (the gateway request log derives its path from it, and the probe reads
    # that log back as per-attempt evidence).
    from videocaptioner.core.llm import (
        LLMGateway,
        LLMMessage,
        LLMModelProfile,
        LLMRequest,
        LLMTransport,
        ProviderDialect,
    )

    service = LoopbackProbeService()
    if kind == "backoff":
        service.set_mode("reject", retry_after="1.2")
    started = time.perf_counter()
    cancel_event = threading.Event()
    profile = LLMModelProfile(
        profile_id=f"transport-probe-{kind}",
        name=f"transport-probe-{kind}",
        transport=LLMTransport.OPENAI_COMPATIBLE,
        dialect=ProviderDialect.GENERIC,
        base_url=service.base_url,
        api_key="offline-placeholder",
        model="probe-model",
        max_concurrency=plan["gate"],
    )

    sleeps: list[dict] = []
    first_backoff_started = threading.Event()

    def observed_sleep(seconds: float) -> None:
        # The ONLY injection point: the gateway's public constructor seam.
        # time.sleep stays real; every backoff wait is observed live, and the
        # record exists BEFORE the sleep so the probe can anchor on its start.
        began = time.perf_counter()
        record = {
            "requested_seconds": seconds,
            "started_at_s": round(began - started, 6),
            "ended_at_s": None,
            "actual_seconds": None,
        }
        sleeps.append(record)
        first_backoff_started.set()
        time.sleep(seconds)
        record["ended_at_s"] = round(time.perf_counter() - started, 6)
        record["actual_seconds"] = round(time.perf_counter() - began, 6)

    gateway = LLMGateway(
        sleep=observed_sleep,
        # 0.75 + 0.5 * 0.5 = 1.0 → deterministic exponential backoff base.
        random_source=lambda: 0.5,
    )
    stage_nonce = f"transport-probe/{kind}/{uuid.uuid4().hex[:8]}"
    requests: list[dict] = []
    responses_before: list[int] = []
    responses_after: list[int] = []
    worker_joins: list[dict] = []
    errors: list[str] = []
    threads: list[threading.Thread] = []
    entry_evidence: dict = {
        "kind": kind,
        "entry_observed": False,
        "read_timeout_seconds": plan["read_timeout"],
        "planned_release_s": plan["release_after"],
    }

    def worker(request_id: int) -> None:
        entry = {
            "request": request_id,
            "call_started_at_s": round(time.perf_counter() - started, 6),
            "outcome": "error",
            "ended_at_s": None,
            "result_text": None,
            "error": None,
        }
        requests.append(entry)
        try:
            result = gateway.complete(
                profile,
                LLMRequest(
                    messages=(LLMMessage("user", f"probe request {request_id}"),),
                    metadata={"stage": stage_nonce, "role": "utility"},
                    timeout=plan["read_timeout"],
                ),
                cancelled=cancel_event.is_set,
                # The probe measures transport, not the response cache; cache
                # hits must never masquerade as transport observations.
                use_cache=False,
            )
            entry["outcome"] = "success"
            entry["result_text"] = result.text
            (responses_after if cancel_event.is_set() else responses_before).append(request_id)
        except InterruptedError:
            # The expected business response to stop, never a probe failure.
            entry["outcome"] = "interrupted"
        except Exception as exc:  # noqa: BLE001 —— 基准必须报告而不是挂掉
            entry["error"] = f"{type(exc).__name__}: {exc}"
            errors.append(f"request {request_id}: {entry['error']}")
        finally:
            entry["ended_at_s"] = round(time.perf_counter() - started, 6)

    release_timer = None
    forced_reason = "probe force-release after workers stopped waiting"
    cancel_at: float | None = None
    try:
        threads = [
            threading.Thread(
                target=worker,
                args=(request_id,),
                name=f"probe-worker-{request_id}",
                daemon=True,
            )
            for request_id in range(1, plan["logical"] + 1)
        ]
        for thread in threads:
            thread.start()
        if plan["release_after"] is not None:
            release_timer = service.release_delayed(plan["release_after"])

        # Anchor the cancellation on an OBSERVED entry into the wait phase.
        if plan["entry_attempts"] is not None:
            observed = service.wait_for_attempts(plan["entry_attempts"])
            entry_evidence.update(
                entry_observed=observed >= (plan["entry_attempts"] or 0),
                entry_condition=(
                    f"server saw {plan['entry_attempts']} in-flight HTTP attempts"
                ),
                http_attempts_at_entry=observed,
            )
            if service.first_attempt_started_raw() is not None:
                entry_evidence["first_http_at_s"] = round(
                    service.first_attempt_started_raw() - started, 6
                )
        else:
            # backoff: the gateway's own backoff sleep is the wait under test.
            entered = first_backoff_started.wait(ENTRY_WAIT_TIMEOUT)
            entry_evidence.update(
                entry_observed=entered,
                entry_condition="gateway backoff sleep started",
                entered_gateway_backoff=entered,
            )
            if entered and sleeps:
                entry_evidence["backoff_started_at_s"] = sleeps[0]["started_at_s"]
        if not entry_evidence["entry_observed"]:
            errors.append(
                f"{kind} probe never observed entry into its wait phase "
                f"(evidence: {entry_evidence})"
            )
        time.sleep(POST_ENTRY_DELAY)
        # Queue evidence: every worker entered its call; the ones the gate
        # held back sent no HTTP at all.
        entry_evidence["workers_entered_calls"] = len(requests)
        entry_evidence["http_attempts_at_cancel"] = service.attempt_count()
        if plan["gate"] is not None:
            entry_evidence["queued_waiting_for_gate"] = max(
                0, len(requests) - service.attempt_count()
            )
        cancel_at = time.perf_counter() - started
        cancel_event.set()
        entry_evidence["cancel_at_s"] = round(cancel_at, 6)

        for thread in threads:
            thread.join(timeout=WORKER_JOIN_TIMEOUT)
            worker_joins.append(
                {
                    "worker": thread.name,
                    "joined_before_release": not thread.is_alive(),
                    "joined": None,
                }
            )
    except Exception:  # noqa: BLE001 —— 基准设施失败必须可观察
        errors.append(traceback.format_exc(limit=3))
        cancel_at = None
    finally:
        if release_timer is not None:
            release_timer.cancel()
        # Emergency release: frees anything still held after workers stopped.
        forced_at = time.perf_counter() - started
        service.release()
        # Verification join: after the release every worker must exit; a
        # thread still alive here is an infrastructure failure, not a gap.
        joined_by_name = {item["worker"]: item for item in worker_joins}
        for thread in threads:
            thread.join(timeout=VERIFICATION_JOIN_TIMEOUT)
            alive = thread.is_alive()
            matching = joined_by_name.get(thread.name)
            if matching is not None:
                matching["joined"] = not alive
            else:
                worker_joins.append(
                    {
                        "worker": thread.name,
                        "joined_before_release": None,
                        "joined": not alive,
                    }
                )
            if alive:
                errors.append(f"worker {thread.name} did not exit after the release")
        # The gateway owns its adapters; closing it is part of cleanup, and a
        # failure here is an infrastructure error, not a baseline gap.
        try:
            gateway.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"gateway.close() failed: {exc}")

    adapter_log, log_error = _read_adapter_attempt_log(stage_nonce)
    if log_error is not None:
        errors.append(log_error)
    service_summary = service.close(started)
    if not service_summary["server_thread_exited"]:
        errors.append("loopback server thread did not exit within its bound")
    if not service_summary["handler_answers_drained"]:
        errors.append("loopback server handlers did not finish after the release")
    report_requests = sorted(requests, key=lambda entry: entry["request"])
    ended = [entry for entry in report_requests if entry["ended_at_s"] is not None]

    max_stop_responsiveness = None
    prompt = None
    if cancel_at is not None and ended:
        # Stop responsiveness: how long the worst worker kept waiting past stop.
        max_stop_responsiveness = max(entry["ended_at_s"] - cancel_at for entry in ended)
        prompt = max_stop_responsiveness <= 0.3
    first_release = service_summary["first_release_s"]
    completed_without_release = None
    stopped_between = None
    if first_release is not None and ended:
        # True when every worker reached a terminal state before any release
        # action was needed: the local stop completed through its bounded
        # transport paths alone (queue baseline: False).
        completed_without_release = all(entry["ended_at_s"] < first_release for entry in ended)
        if plan["release_after"] is not None and cancel_at is not None:
            # Forward-looking gate for ticket 06: did stop take effect after
            # cancellation but before the planned release stimulus?
            stopped_between = all(
                cancel_at < entry["ended_at_s"] < first_release for entry in ended
            )

    report = {
        "schema": PROBE_SCHEMA,
        "probe": kind,
        "ok": not errors,
        "errors": errors,
        "logical_requests": len(report_requests),
        "http_attempts": len(service_summary["attempts"]),
        "http_attempt_log": service_summary["attempts"],
        "adapter_attempts": len(adapter_log),
        "adapter_attempt_log": adapter_log,
        "sleeps": sleeps,
        "inflight_peak": service_summary["inflight_peak"],
        "requests": report_requests,
        "responses": {
            "before_cancel": sorted(responses_before),
            "after_cancel": sorted(responses_after),
            "service_natural_returns": service_summary["natural_returns"],
            "service_released_writes": service_summary["released_writes"],
        },
        "stop": {
            "cancel_at_s": None if cancel_at is None else round(cancel_at, 6),
            "prompt": prompt,
            "max_stop_responsiveness_seconds": (
                None if max_stop_responsiveness is None else round(max_stop_responsiveness, 6)
            ),
            "completed_without_release": completed_without_release,
            "stopped_between_cancel_and_release": stopped_between,
        },
        "wait_entry_evidence": entry_evidence,
        "controlled_release": {
            "at_s": first_release,
            "reason": (
                "planned release stimulus"
                if plan["release_after"] is not None
                else "none planned; emergency release only"
            ),
        },
        "forced_release": {"at_s": round(forced_at, 6), "reason": forced_reason},
        "cleanup": {
            "server_thread_exited": service_summary["server_thread_exited"],
            "handler_answers_drained": service_summary["handler_answers_drained"],
            "worker_joins": worker_joins,
        },
        "baseline_gaps": None,  # filled below (needs the assembled report)
        "duration_seconds": round(time.perf_counter() - started, 6),
        "real_model_validation": "not executed; loopback-only, no API key",
    }
    report["baseline_gaps"] = _baseline_gaps(report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=PROBE_KINDS, required=True)
    parser.add_argument(CLI_ARG_OUTPUT, type=Path, required=True, help="new report file only")
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"refusing to overwrite existing report: {args.output}")
    # Must precede the project imports inside run_probe: the gateway request
    # log derives its path from APPDATA_PATH, and the probe reads that log
    # back as per-attempt evidence. A caller-provided isolation is respected;
    # without one, each output directory gets its own isolated AppData.
    os.environ.setdefault(
        "VIDEOCAPTIONER_APPDATA_PATH", str((args.output.parent / "appdata").resolve())
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = run_probe(args.kind)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "probe",
                    "ok",
                    "logical_requests",
                    "http_attempts",
                    "adapter_attempts",
                    "stop",
                    "duration_seconds",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
