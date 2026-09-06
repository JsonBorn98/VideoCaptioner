"""Core postprocess task seam: snapshots, frozen config, and result statuses."""

from pathlib import Path

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.postprocess.config import PostprocessConfig
from videocaptioner.core.postprocess.models import PostprocessLayoutMode, PostprocessTask
from videocaptioner.core.postprocess.runner import run_postprocess_task


def _write_srt(path: Path, text: str = "翻译。") -> None:
    path.write_text(
        f"1\n00:00:00,000 --> 00:00:02,000\n{text}\n",
        encoding="utf-8",
    )


def test_successful_task_allows_downstream_to_continue(tmp_path):
    source = tmp_path / "input.srt"
    _write_srt(source)
    result = run_postprocess_task(
        PostprocessTask(
            str(source),
            postprocessed_subtitle_path=str(tmp_path / "result.srt"),
            config_snapshot=PostprocessConfig(trim_trailing_punct=True),
        )
    )

    assert result.succeeded
    assert result.task.status == "completed"
    assert result.continue_downstream is True


def test_module_failure_rolls_back_snapshot_and_blocks_downstream(tmp_path):
    source = tmp_path / "input.srt"
    _write_srt(source)
    original_bytes = source.read_bytes()
    # Force a module-level failure: output path must not overwrite the input.
    result = run_postprocess_task(
        PostprocessTask(
            str(source),
            postprocessed_subtitle_path=str(source),
            config_snapshot=PostprocessConfig(),
        )
    )

    assert not result.succeeded
    assert result.used_fallback
    assert result.task.status == "fallback"
    assert result.continue_downstream is False
    assert result.task.active_subtitle_path == str(source)
    assert source.read_bytes() == original_bytes
    assert any("回退到初版字幕" in warning for warning in result.warnings)


def test_invalid_initial_subtitle_blocks_downstream_without_fallback(tmp_path):
    source = tmp_path / "empty.srt"
    source.write_text("", encoding="utf-8")

    result = run_postprocess_task(
        PostprocessTask(str(source), config_snapshot=PostprocessConfig())
    )

    assert not result.succeeded
    assert not result.used_fallback
    assert result.task.status == "invalid_initial"
    assert result.continue_downstream is False
    assert result.task.active_subtitle_path == str(source)


def test_cancelled_task_blocks_downstream_and_preserves_input(tmp_path):
    source = tmp_path / "input.srt"
    _write_srt(source)
    original_bytes = source.read_bytes()
    output = tmp_path / "result.srt"

    result = run_postprocess_task(
        PostprocessTask(
            str(source),
            postprocessed_subtitle_path=str(output),
            config_snapshot=PostprocessConfig(trim_trailing_punct=True),
        ),
        cancelled=lambda: True,
    )

    assert not result.succeeded
    assert not result.used_fallback
    assert result.task.status == "cancelled"
    assert result.continue_downstream is False
    assert result.task.active_subtitle_path == str(source)
    assert source.read_bytes() == original_bytes
    assert not output.exists()


def test_interrupted_processing_is_cancellation_not_module_failure(tmp_path):
    source = tmp_path / "input.srt"
    _write_srt(source)
    original_bytes = source.read_bytes()

    def stop_resolver(*_args):
        raise InterruptedError("stop requested")

    result = run_postprocess_task(
        PostprocessTask(
            str(source),
            media_path=str(tmp_path / "media.mp4"),
            config_snapshot=PostprocessConfig(
                precise_timing=True,
                speed_optimize=False,
                trim_trailing_punct=False,
            ),
        ),
        timing_resolver=stop_resolver,
    )

    assert result.task.status == "cancelled"
    assert not result.used_fallback
    assert result.continue_downstream is False
    assert source.read_bytes() == original_bytes


def test_in_memory_input_is_left_unmodified(tmp_path):
    source = tmp_path / "memory.srt"
    incoming = ASRData([ASRDataSeg("原文。", 0, 2000, "译文。")])
    result = run_postprocess_task(
        PostprocessTask(
            str(source),
            input_data=incoming,
            layout_mode=PostprocessLayoutMode.ORIGINAL_ON_TOP,
            config_snapshot=PostprocessConfig(trim_trailing_punct=True),
        )
    )

    assert result.succeeded
    assert incoming.segments[0].text == "原文。"
    assert incoming.segments[0].translated_text == "译文。"
    assert incoming.segments[0] is not result.output_data.segments[0]
    assert incoming.segments[0] is not result.input_data.segments[0]


def test_live_config_mutation_does_not_affect_frozen_task(tmp_path):
    source = tmp_path / "input.srt"
    _write_srt(source, "译文。")
    live = PostprocessConfig(trim_trailing_punct=True, precise_timing=True)

    def mutate_live(*_args):
        live.trim_trailing_punct = False
        return ()

    result = run_postprocess_task(
        PostprocessTask(
            str(source),
            media_path=str(tmp_path / "media.mp4"),
            config_snapshot=live,
        ),
        timing_resolver=mutate_live,
    )

    assert result.succeeded
    assert result.output_data.segments[0].text == "译文"
    assert result.task.config_snapshot is not live
    assert result.task.config_snapshot.trim_trailing_punct is True
    assert live.trim_trailing_punct is False


class _FakeAssetAdapter:
    def __init__(self) -> None:
        self.tasks: list[PostprocessTask] = []
        self.published: list[tuple[PostprocessTask, dict]] = []

    def discover(self, task: PostprocessTask) -> None:
        self.tasks.append(task)

    def publish_downstream_outputs(self, task: PostprocessTask, outputs) -> None:
        self.published.append((task, dict(outputs)))


def test_task_seam_invokes_injected_asset_adapter(tmp_path):
    source = tmp_path / "input.srt"
    _write_srt(source)
    assets = _FakeAssetAdapter()

    result = run_postprocess_task(
        PostprocessTask(
            str(source),
            config_snapshot=PostprocessConfig(trim_trailing_punct=True),
        ),
        assets=assets,
    )

    assert result.succeeded
    assert assets.tasks == [result.task]
    assert assets.tasks[0].config_snapshot is not None
    assert assets.tasks[0].config_snapshot.trim_trailing_punct is True


def test_task_seam_publishes_module_outputs_through_injected_adapter(tmp_path):
    """模块成功后下游产物经同一注入接缝发布（票 08，D21/D28）。"""

    source = tmp_path / "input.srt"
    _write_srt(source)
    assets = _FakeAssetAdapter()

    result = run_postprocess_task(
        PostprocessTask(
            str(source),
            config_snapshot=PostprocessConfig(trim_trailing_punct=True, qa_report=True),
        ),
        assets=assets,
    )

    assert result.succeeded
    assert len(assets.published) == 1
    published_task, outputs = assets.published[0]
    assert published_task is result.task
    assert set(outputs) == {"qa_report", "postprocess_state"}
    assert "# 字幕质量 QA".encode("utf-8") in outputs["qa_report"]
    assert b'"videocaptioner.postprocess_state"' in outputs["postprocess_state"]
    assert result.task.persisted_outputs == {}  # fake 不落盘，不伪造位置


class _BrokenAssetAdapter:
    def discover(self, task: PostprocessTask) -> None:
        del task
        raise RuntimeError("asset adapter exploded")


def test_asset_adapter_exception_is_module_failure(tmp_path):
    source = tmp_path / "input.srt"
    _write_srt(source)
    original_bytes = source.read_bytes()

    result = run_postprocess_task(
        PostprocessTask(str(source), config_snapshot=PostprocessConfig()),
        assets=_BrokenAssetAdapter(),
    )

    assert result.task.status == "fallback"
    assert result.used_fallback
    assert result.continue_downstream is False
    assert source.read_bytes() == original_bytes
