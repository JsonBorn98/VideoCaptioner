"""后处理范围内的轻量结构化进度 / 诊断事件（票 07，spec「进度与诊断」）。

沿既有进度回调与 Qt 信号旁路一条**可选**的事件通道：``execute_viewing_repair``
/ ``run_postprocess_task`` 接受 ``on_event`` 回调，按阶段发出携带口径的
字典事件（轮次分母 / 归并验收通过 / 在途排队 / 等待期限 / 终态），
GUI 与 CLI 从同一事实渲染摘要与展开详情——不建设全应用重量级进度
协议（ADR-0009：控制台由前端拥有，这里只提供结构化事实）。

事件 ``kind``：
- ``stage``：任务级本地阶段（读取 / 规划 / 规范化 / 保存）。
- ``round``：修复轮次开始——分母（未解决问题数）逐轮显式，不藏在
  伪单调百分比里（spec：重规划 / 拆分 / 回退的分母变化有明确口径）。
- ``waiting``：模型请求在途等待——长调用期间持续刷新等待时长，
  携带窗口 / 在途 / 排队与单次尝试窗口（等待期限）。
- ``batch``：一批归并验收完成——模型返回不等于问题解决，只有验收
  通过的计数进入「已通过」（本批 / 累计分列）。
- ``retry``：传输 / 业务失败的可观察原因（重试与限流退避归网关日志）。
- ``terminal``：修复循环终态——completed / cancelled / failed /
  report_only，恰发一次（无重复完成）。

``at_s`` 是 ``time.perf_counter()`` 进程单调时钟（跨线程可比，只用于
间隔诊断，不跨进程比较）。事件由多个线程（修复协调线程、等待刷新
线程、校对窗口线程）并发发射：``on_event`` 回调必须线程安全
（Qt ``pyqtSignal.emit`` 排队送达；CLI 侧由输出锁串行化）。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional

# 等待事件节流间隔（秒）：01 冻结「等待中可见状态间隔 ≤0.50s」门槛的
# 2.5 倍余量——刷新足够密，又不至于逐 token 刷屏成为新瓶颈（spec：
# 日志与进度避免大量事件）。
WAITING_REFRESH_INTERVAL_S = 0.2

# 等待刷新线程停止时的 join 上界（秒）：刷新线程只睡 / 发事件，
# 无锁竞争；超时即放弃等待（daemon 线程自然回收）。
_REFRESHER_JOIN_TIMEOUT_S = 1.0

_ROLE_LABELS = {"main": "主修复", "review": "高级校对"}

_TERMINAL_LABELS = {
    "completed": "修复完成",
    "cancelled": "修复已停止",
    "failed": "修复失败",
    "report_only": "仅报告（未发起模型请求）",
}


def role_label(role: str) -> str:
    """请求角色的人类可读标签（事件消息 / 详情渲染共用）。"""
    return _ROLE_LABELS.get(role, role)


def waiting_event(
    *,
    round_index: int,
    wait_elapsed_ms: float,
    inflight: int,
    queued: int,
    window: int,
    request_window_s: float,
    role: str,
    percent: Optional[int] = None,
    at_s: float = time.perf_counter(),
) -> Dict[str, Any]:
    """构造一个在途等待刷新事件（长模型调用期间持续更新等待时长）。"""
    elapsed_s = wait_elapsed_ms / 1000.0
    message = (
        f"正在等待{role_label(role)}模型返回：已等待 {elapsed_s:.1f}s"
        f"（在途 {inflight} / 排队 {queued}）"
    )
    return {
        "kind": "waiting",
        "round": round_index,
        "role": role,
        "message": message,
        "counts": {
            "window": window,
            "inflight": inflight,
            "queued": queued,
            "wait_elapsed_ms": round(wait_elapsed_ms),
        },
        "request_window_s": request_window_s,
        "percent": percent,
        "at_s": at_s,
    }


def round_event(
    *,
    round_index: int,
    open_problems: int,
    batches: int,
    window: int,
    concurrency_gate: Optional[int] = None,
    percent: Optional[int] = None,
    at_s: float = time.perf_counter(),
) -> Dict[str, Any]:
    """构造修复轮次事件：分母逐轮显式（进入下一轮 / 重规划时更新口径）。

    ``concurrency_gate`` 是本轮实际生效并发（P5 修复：spec 决策 4
    「显示实际生效值」——冻结值与钳制闸进轮次事件，前端默认摘要
    可渲染，不再只进状态载荷 / 日志）。
    """
    message = f"正在修复观看问题（第 {round_index} 轮，{open_problems} 个未解决）"
    if concurrency_gate is not None:
        message += f"（本轮并发 {concurrency_gate}）"
    return {
        "kind": "round",
        "round": round_index,
        "message": message,
        "counts": {
            "open_problems": open_problems,
            "batches": batches,
            "window": window,
            "concurrency_gate": concurrency_gate,
        },
        "percent": percent,
        "at_s": at_s,
    }


def batch_event(
    *,
    round_index: int,
    batch_index: int,
    accepted_in_batch: int,
    accepted_total: int,
    subjects: int,
    at_s: float = time.perf_counter(),
) -> Dict[str, Any]:
    """构造归并验收事件：只有验收通过的计数进入「已通过」（返回 ≠ 解决）。"""
    return {
        "kind": "batch",
        "round": round_index,
        "batch": batch_index,
        "message": (
            f"第 {batch_index} 批归并验收：本批通过 {accepted_in_batch} · 累计 {accepted_total}"
        ),
        "counts": {
            "accepted_in_batch": accepted_in_batch,
            "accepted_total": accepted_total,
            "subjects": subjects,
        },
        "at_s": at_s,
    }


def retry_event(
    *,
    round_index: int,
    batch_index: Optional[int],
    category: str,
    reason: str,
    attempt_count: Optional[int] = None,
    at_s: float = time.perf_counter(),
) -> Dict[str, Any]:
    """构造重试 / 失败原因事件（传输级不消耗业务重试，业务级进入反馈）。

    ``attempt_count`` 是覆盖问题的已发生业务请求次数（P2 修复：用户
    故事 28——重试次数可见，不只最近一次原因）；传输级传 ``None``
    （传输重试归网关日志，与业务重试分开统计）。
    """
    label = "传输失败（不消耗业务重试）" if category == "transport" else "业务失败"
    message = f"{label}：{reason[:200]}"
    if attempt_count is not None:
        message += f"（该批问题已请求 {attempt_count} 次）"
    return {
        "kind": "retry",
        "round": round_index,
        "batch": batch_index,
        "message": message,
        "category": category,
        "attempt_count": attempt_count,
        "at_s": at_s,
    }


def stage_event(
    *,
    stage: str,
    message: str,
    percent: Optional[int] = None,
    at_s: float = time.perf_counter(),
) -> Dict[str, Any]:
    """构造任务级本地阶段事件（规划 / 本地处理 / 保存）。"""
    return {
        "kind": "stage",
        "stage": stage,
        "message": message,
        "percent": percent,
        "at_s": at_s,
    }


def terminal_event(
    *,
    status: str,
    counts: Dict[str, int],
    wall_seconds: Optional[float] = None,
    at_s: float = time.perf_counter(),
) -> Dict[str, Any]:
    """构造修复循环终态事件（恰发一次；成功 / 停止 / 失败均有明确终态）。"""
    label = _TERMINAL_LABELS.get(status, status)
    message = f"{label}：" + " · ".join(f"{key} {value}" for key, value in counts.items())
    if wall_seconds is not None:
        message += f" · 耗时 {wall_seconds:.1f}s"
    return {
        "kind": "terminal",
        "status": status,
        "message": message,
        "counts": dict(counts),
        "wall_seconds": wall_seconds,
        "at_s": at_s,
    }


def render_event_line(event: Dict[str, Any]) -> str:
    """把一个事件渲染成单行（CLI 详细模式 / 简单日志消费者）。"""
    parts = [str(event.get("kind", ""))]
    if event.get("round") is not None:
        parts.append(f"round={event['round']}")
    if event.get("batch") is not None:
        parts.append(f"batch={event['batch']}")
    message = event.get("message")
    if message:
        parts.append(str(message))
    return " · ".join(part for part in parts if part)


def merge_waiting_event(fields: Dict[str, Any], event: Dict[str, Any]) -> Dict[str, Any]:
    """把一个 waiting 事件并入按角色分槽的最新事实（票 08 点验修复）。

    并发等待（票 04/05 后的常态）：主修复与高级校对各在途请求的
    ``WaitRefresher`` 并发发射 waiting 事件（0.2s 各自节流）。单槽
    「最新一条」存储让两类消息交替覆盖（实机闪烁：主修复 230.3s
    与校对 12.0s 互相刷掉）。``waiting_slots`` 按角色分槽存储最新
    口径；非等待字段照旧平铺（round / batch 事实与角色无关）。
    返回新 dict，``fields`` 不被就地修改（GUI 累积口径可安全复用）。
    """
    if event.get("kind") != "waiting":
        return dict(fields)
    merged = dict(fields)
    counts = event.get("counts") or {}
    merged["waiting_slots"] = {
        **{str(role): slot for role, slot in (fields.get("waiting_slots") or {}).items()},
        str(event.get("role")): {
            "message": event.get("message"),
            "window": counts.get("window"),
            "inflight": counts.get("inflight"),
            "queued": counts.get("queued"),
            "wait_elapsed_ms": counts.get("wait_elapsed_ms"),
            "request_window_s": event.get("request_window_s"),
            "round": event.get("round"),
        },
    }
    return merged


def drop_waiting_role(fields: Dict[str, Any], role: str) -> Dict[str, Any]:
    """移除一个角色的等待槽（角色请求已返回 / 轮次推进）。

    单槽口径下这一步隐含在「下一条事件覆盖」里；分槽后旧槽不再
    被新事件覆盖，必须显式清理，否则已返回角色陈旧闪烁。
    返回新 dict，``fields`` 不被就地修改。
    """
    slots = {str(key): slot for key, slot in (fields.get("waiting_slots") or {}).items()}
    slots.pop(role, None)
    merged = dict(fields)
    merged["waiting_slots"] = slots
    return merged


def consume_event(fields: Dict[str, Any], event: Dict[str, Any]) -> Dict[str, Any]:
    """把一个事件并入跨事件累积口径（GUI / CLI 共享，标准轴 #2 修复）。

    CLI ``on_event`` 与 GUI ``_on_progress_event`` 原各留一份 ``kind``
    分支（round 丢 main+review 槽、batch 丢 main 槽、retry 记原因）；
    这里是单一实现，前端只保留各自的呈现策略（CLI 单行 vs GUI 多行）：
    - ``waiting``：按角色分槽并入（``merge_waiting_event``）。
    - ``round``：新一轮清全部等待槽，更新分母 / 窗口 / 本轮并发。
    - ``batch``：本批主修复已返回，清 main 槽，更新验收累计。
    - ``retry``：记录最近重试原因与重试次数口径（``attempt_count``）。
    返回新 dict，``fields`` 不被就地修改；``message`` 按最新事件更新。
    """
    kind = event.get("kind")
    merged = dict(fields)
    merged["message"] = event.get("message")
    counts = event.get("counts") or {}
    if kind == "waiting":
        return merge_waiting_event(merged, event)
    if kind == "round":
        merged = drop_waiting_role(merged, "main")
        merged = drop_waiting_role(merged, "review")
        merged["open_problems"] = counts.get("open_problems")
        merged["window"] = counts.get("window")
        if counts.get("concurrency_gate") is not None:
            merged["concurrency_gate"] = counts.get("concurrency_gate")
    elif kind == "batch":
        merged = drop_waiting_role(merged, "main")
        merged["accepted_total"] = counts.get("accepted_total")
        # 轮次事实与角色无关：批事件不带分母时不回退已有口径。
        if counts.get("open_problems") is not None:
            merged["open_problems"] = counts.get("open_problems")
    elif kind == "retry":
        merged["retry_reason"] = event.get("message")
        if event.get("attempt_count") is not None:
            merged["retry_attempt_count"] = event.get("attempt_count")
    return merged


def _render_waiting_slot(role: str, slot: Dict[str, Any]) -> list[str]:
    wait_ms = slot.get("wait_elapsed_ms")
    wait_s = f"{wait_ms / 1000.0:.1f}s" if wait_ms is not None else "?"
    lines = [
        "{label}等待中：已等待 {wait}（在途 {inflight} / 排队 {queued}）".format(
            label=role_label(role),
            wait=wait_s,
            inflight=slot.get("inflight") if slot.get("inflight") is not None else 0,
            queued=slot.get("queued") if slot.get("queued") is not None else 0,
        )
    ]
    request_window = slot.get("request_window_s")
    if request_window:
        lines.append(f"单次请求窗口（等待期限）{request_window:.0f}s")
    return lines


def render_detail(fields: Dict[str, Any]) -> str:
    """把最新事实渲染成展开详情多行文本（GUI 展开区 / 诊断输出）。

    ``fields`` 是跨事件累积的最新口径：分槽后的并发等待
    （``waiting_slots``，主修复与高级校对并列，不互相覆盖），
    已通过 / 未解决来自 batch / round。
    """
    lines: list[str] = []
    slots = fields.get("waiting_slots") or {}
    if slots:
        # 并发等待按固定角色序并列（先主修复后校对）：单角色场景
        # 与单槽口径呈现一致；并发双角色同屏（票 08 点验修复）。
        for role in ("main", "review"):
            if role in slots:
                lines.extend(_render_waiting_slot(role, slots[role]))
        for role, slot in slots.items():
            if role not in ("main", "review"):
                lines.extend(_render_waiting_slot(role, slot))
    else:
        # 无并发等待（旧单事件 / 离线渲染路径）：单槽等待口径。
        message = fields.get("message")
        if message:
            lines.append(str(message))
        window = fields.get("window")
        if window is not None:
            lines.append(
                "有效并发窗口 {window} · 在途 {inflight} · 排队 {queued}".format(
                    window=window,
                    inflight=fields.get("inflight", 0),
                    queued=fields.get("queued", 0),
                )
            )
        wait_ms = fields.get("wait_elapsed_ms")
        if wait_ms is not None:
            lines.append(f"本次等待时长 {wait_ms / 1000.0:.1f}s")
        request_window = fields.get("request_window_s")
        if request_window:
            lines.append(f"单次请求窗口（等待期限）{request_window:.0f}s")
    accepted = fields.get("accepted_total")
    unresolved = fields.get("open_problems")
    if accepted is not None or unresolved is not None:
        lines.append(
            f"验收已通过 {accepted if accepted is not None else 0}"
            f" / 未解决 {unresolved if unresolved is not None else 0}"
        )
    retry_reason = fields.get("retry_reason")
    if retry_reason:
        retry_line = f"最近重试原因：{retry_reason}"
        retry_count = fields.get("retry_attempt_count")
        if retry_count is not None:
            retry_line += f"（已请求 {retry_count} 次）"
        lines.append(retry_line)
    gate = fields.get("concurrency_gate")
    if gate is not None:
        lines.append(f"本轮实际并发 {gate}")
    return "\n".join(lines)


class WaitRefresher:
    """模型请求在途期间的等待刷新线程（票 07 等待事件来源）。

    以 ``interval_s`` 周期发射 ``waiting_event``（快照由 ``snapshot``
    回调读取当前窗口 / 在途 / 排队计数），请求返回 / 异常 / 取消时由
    调用方 ``stop``：先置停止事件再 join——刷新线程在下一轮等待处
    退出，至多多发一次在途事件（取消后由前端按取消状态抑制）。
    """

    def __init__(
        self,
        emit: Callable[[Dict[str, Any]], None],
        snapshot: Callable[[], Dict[str, int]],
        *,
        round_index: int,
        role: str,
        request_window_s: float,
        percent_provider: Callable[[], Optional[int]],
        interval_s: float = WAITING_REFRESH_INTERVAL_S,
    ) -> None:
        self._emit = emit
        self._snapshot = snapshot
        self._round_index = round_index
        self._role = role
        self._request_window_s = request_window_s
        self._percent_provider = percent_provider
        self._interval_s = interval_s
        self._stop_event = threading.Event()
        self._started_at = time.perf_counter()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "WaitRefresher":
        self._thread = threading.Thread(
            target=self._loop, name=f"postprocess-wait-refresh-{self._role}", daemon=True
        )
        self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop_event.wait(self._interval_s):
            if self._stop_event.is_set():
                return
            counts = self._snapshot()
            self._emit(
                waiting_event(
                    round_index=self._round_index,
                    wait_elapsed_ms=(time.perf_counter() - self._started_at) * 1000.0,
                    inflight=counts.get("inflight", 0),
                    queued=counts.get("queued", 0),
                    window=counts.get("window", 0),
                    request_window_s=self._request_window_s,
                    role=self._role,
                    percent=self._percent_provider(),
                )
            )

    def stop(self) -> None:
        """请求收尾时停止刷新（daemon 线程 join 有界，不阻塞取消路径）。"""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=_REFRESHER_JOIN_TIMEOUT_S)


__all__ = [
    "WAITING_REFRESH_INTERVAL_S",
    "WaitRefresher",
    "batch_event",
    "consume_event",
    "drop_waiting_role",
    "merge_waiting_event",
    "render_detail",
    "render_event_line",
    "retry_event",
    "role_label",
    "round_event",
    "stage_event",
    "terminal_event",
    "waiting_event",
]
