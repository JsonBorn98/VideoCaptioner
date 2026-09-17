"""批量任务行恢复续跑标注（票 09）。

批量任务对翻译与后处理都不接恢复决定回调（默认继续）；任一模块
从检查点继续时线程结果带 recovery_summary，完成包装器把续跑事实
经 task_completed 信号透传，视图层渲染「已从恢复检查点继续」。
纯 Python 包装器逻辑，不需要 Qt 事件循环。
"""

from videocaptioner.core.entities import BatchTaskStatus, BatchTaskType
from videocaptioner.ui.thread import batch_process_thread as batch_module
from videocaptioner.ui.thread.batch_process_thread import BatchProcessThread, BatchTask


class _ThreadWithSummary:
    """带 recovery_summary 的最小线程替身：返回非 None 摘要。"""

    def recovery_summary(self):
        return object()


def test_batch_row_annotates_resumed_checkpoint(tmp_path, monkeypatch):
    """续跑行带标注事实，未续跑行不带。"""

    # 本测试只测行标注包装器：绕过方案库解析（无种子 LLM 方案会 fail-fast）。
    monkeypatch.setattr(
        batch_module.TaskFactory,
        "create_postprocess_task",
        lambda *args, **kwargs: type("Post", (), {})(),
    )
    thread = BatchProcessThread()
    completed: list[tuple[str, bool]] = []
    thread.task_completed.connect(lambda path, resumed: completed.append((path, resumed)))

    resumed = BatchTask("a.wav", BatchTaskType.SUBTITLE)
    resumed.status = BatchTaskStatus.RUNNING
    resumed.resumed_from_checkpoint = True
    thread.current_tasks = {resumed.file_path: resumed}
    thread._on_finished_wrapper(resumed)
    assert completed == [(resumed.file_path, True)]

    # 未续跑行：False，视图层不拼标注。
    completed.clear()
    plain = BatchTask("b.wav", BatchTaskType.SUBTITLE)
    plain.status = BatchTaskStatus.RUNNING
    thread.current_tasks = {plain.file_path: plain}
    thread._on_finished_wrapper(plain)
    assert completed == [(plain.file_path, False)]

    # 标注检测：线程带 recovery_summary → 标注置位；无摘要方法 → 不置。
    thread._mark_resumed_from_checkpoint(resumed, _ThreadWithSummary())
    assert resumed.resumed_from_checkpoint is True
    plain.resumed_from_checkpoint = False
    thread._mark_resumed_from_checkpoint(plain, object())  # 无 recovery_summary
    assert plain.resumed_from_checkpoint is False
