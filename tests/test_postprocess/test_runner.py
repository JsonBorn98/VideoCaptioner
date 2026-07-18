from pathlib import Path

import pytest

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.entities import SubtitleLayoutEnum
from videocaptioner.core.postprocess import run_post_stage
from videocaptioner.core.postprocess.config import PostprocessConfig
from videocaptioner.core.postprocess.models import PostprocessLayoutMode, PostprocessTask
from videocaptioner.core.postprocess.runner import run_postprocess_task
from videocaptioner.core.speed.timing_evidence import (
    TimingEvidenceWindow,
    TimingGranularity,
    TimingOperation,
    TimingProvenance,
    TimingQualityGrade,
)


def _write_srt(path: Path, text: str = "翻译。") -> bytes:
    content = f"1\n00:00:00,000 --> 00:00:02,000\n{text}\n"
    path.write_text(content, encoding="utf-8")
    return path.read_bytes()


def _timing_window(
    cue_id: str,
    start_ms: int,
    end_ms: int,
    *,
    fallback: bool,
) -> TimingEvidenceWindow:
    return TimingEvidenceWindow.create(
        cue_ids=(cue_id,),
        start_ms=start_ms,
        end_ms=end_ms,
        provenance=(
            TimingProvenance.SUBTITLE_INPUT if fallback else TimingProvenance.FORCED_ALIGNER
        ),
        granularity=TimingGranularity.CUE,
        coverage=1.0,
        quality_grade=TimingQualityGrade.LOW if fallback else TimingQualityGrade.HIGH,
        allowed_operations=frozenset({TimingOperation.USE_SAFE_GAP}),
        quality_metrics={"fallback": True} if fallback else {},
    )


def test_standalone_runner_preserves_input_and_writes_separate_active_output(tmp_path):
    source = tmp_path / "input.srt"
    original_bytes = _write_srt(source)
    output = tmp_path / "result.srt"
    task = PostprocessTask(
        str(source),
        postprocessed_subtitle_path=str(output),
        config_snapshot=PostprocessConfig(trim_trailing_punct=True),
    )

    result = run_postprocess_task(task)

    assert result.succeeded
    assert not result.used_fallback
    assert source.read_bytes() == original_bytes
    assert output.exists()
    assert "翻译。" not in output.read_text(encoding="utf-8")
    assert task.initial_subtitle_path == str(source)
    assert task.postprocessed_subtitle_path == str(output)
    assert task.active_subtitle_path == str(output)
    assert result.layout is SubtitleLayoutEnum.ONLY_ORIGINAL
    assert result.layout_confidence == 0.5
    assert result.warnings


def test_precise_timing_reports_degraded_when_all_windows_are_subtitle_fallbacks(tmp_path):
    source = tmp_path / "input.srt"
    _write_srt(source)
    task = PostprocessTask(
        str(source),
        media_path=str(tmp_path / "media.mp4"),
        config_snapshot=PostprocessConfig(precise_timing=True, speed_optimize=False),
    )
    fallback = _timing_window("cue-1", 0, 2000, fallback=True)

    result = run_postprocess_task(task, timing_resolver=lambda *_args: (fallback,))

    assert result.precise_timing_outcome == "degraded_failed"
    assert result.precise_timing_grades is None


def test_precise_timing_reports_applied_when_any_window_was_aligned(tmp_path):
    source = tmp_path / "input.srt"
    _write_srt(source)
    task = PostprocessTask(
        str(source),
        media_path=str(tmp_path / "media.mp4"),
        config_snapshot=PostprocessConfig(precise_timing=True, speed_optimize=False),
    )
    fallback = _timing_window("cue-1", 0, 1000, fallback=True)
    aligned = _timing_window("cue-2", 1000, 2000, fallback=False)

    result = run_postprocess_task(
        task,
        timing_resolver=lambda *_args: (fallback, aligned),
    )

    assert result.precise_timing_outcome == "applied"
    assert result.precise_timing_grades == (("HIGH", 1), ("LOW", 1))


def test_caller_supplied_subtitle_fallback_is_not_reported_as_applied(tmp_path):
    source = tmp_path / "input.srt"
    _write_srt(source)
    task = PostprocessTask(
        str(source),
        config_snapshot=PostprocessConfig(precise_timing=True, speed_optimize=False),
    )
    fallback = _timing_window("cue-1", 0, 2000, fallback=True)

    result = run_postprocess_task(task, timing_windows=(fallback,))

    assert result.precise_timing_outcome == "degraded_failed"
    assert result.precise_timing_grades is None


def test_default_output_replaces_known_artifact_prefixes(tmp_path):
    initial = tmp_path / "【初版字幕】foo.ass"
    repeated = tmp_path / "【后处理字幕】【初版字幕】foo.vtt"

    assert PostprocessTask(str(initial)).default_output_path() == str(
        tmp_path / "【后处理字幕】foo.srt"
    )
    assert PostprocessTask(str(repeated)).default_output_path() == str(
        tmp_path / "【后处理字幕】foo.srt"
    )


def test_auto_ass_preserves_translate_on_top_marker_and_writes_canonical_srt(tmp_path):
    source = tmp_path / "styled.ass"
    ASRData(
        [ASRDataSeg("Original.", 0, 1000, translated_text="译文。")]
    ).save(
        str(source),
        ass_style="[V4+ Styles]\nStyle: Default,DestroyedInputStyle",
        layout=SubtitleLayoutEnum.TRANSLATE_ON_TOP,
        video_width=1920,
        video_height=1080,
    )
    task = PostprocessTask(
        str(source),
        config_snapshot=PostprocessConfig(trim_trailing_punct=True),
    )

    result = run_postprocess_task(task)

    assert result.succeeded
    assert result.layout is SubtitleLayoutEnum.TRANSLATE_ON_TOP
    assert task.postprocessed_subtitle_path == str(
        tmp_path / "【后处理字幕】styled.srt"
    )
    output_text = Path(task.postprocessed_subtitle_path).read_text(encoding="utf-8")
    assert "DestroyedInputStyle" not in output_text
    assert output_text.index("译文") < output_text.index("Original.")


def test_explicit_non_srt_output_is_normalized_to_canonical_srt(tmp_path):
    source = tmp_path / "input.srt"
    _write_srt(source)
    requested_ass = tmp_path / "requested.ass"
    task = PostprocessTask(
        str(source),
        postprocessed_subtitle_path=str(requested_ass),
        config_snapshot=PostprocessConfig(),
    )

    result = run_postprocess_task(task)

    assert result.succeeded
    assert task.postprocessed_subtitle_path == str(tmp_path / "requested.srt")
    assert not requested_ass.exists()


def test_runner_accepts_in_memory_stage_handoff(tmp_path):
    source_path_for_naming = tmp_path / "【初版字幕】memory.srt"
    task = PostprocessTask(
        str(source_path_for_naming),
        input_data=ASRData(
            [ASRDataSeg("Original.", 0, 1000, translated_text="译文。")]
        ),
        layout_mode=PostprocessLayoutMode.TRANSLATE_ON_TOP,
        config_snapshot=PostprocessConfig(trim_trailing_punct=True),
    )

    result = run_postprocess_task(task)

    assert result.succeeded
    assert result.output_data.segments[0].translated_text == "译文"
    assert task.postprocessed_subtitle_path == str(
        tmp_path / "【后处理字幕】memory.srt"
    )
    assert task.result_data is not None
    assert task.result_data is not result.output_data
    assert task.result_data.segments[0] is not result.output_data.segments[0]


@pytest.mark.parametrize(
    ("enabled", "speed_mode", "expected_status"),
    [(False, "apply", "skipped"), (True, "analyze", "completed")],
)
def test_non_writing_paths_publish_detached_result_snapshot(
    tmp_path, enabled, speed_mode, expected_status
):
    source = tmp_path / "input.srt"
    _write_srt(source)
    task = PostprocessTask(
        str(source),
        enabled=enabled,
        config_snapshot=PostprocessConfig(speed_mode=speed_mode),
    )

    result = run_postprocess_task(task)

    assert task.status == expected_status
    assert task.result_data is not None
    assert task.result_data is not result.output_data
    assert task.result_data.segments[0] is not result.output_data.segments[0]


def test_explicit_bilingual_layout_changes_translation_side_only_by_default(tmp_path):
    source = tmp_path / "bilingual.srt"
    _write_srt(source, "Original.\n译文。")
    output = tmp_path / "done.srt"
    result = run_postprocess_task(
        PostprocessTask(
            str(source),
            layout_mode=PostprocessLayoutMode.ORIGINAL_ON_TOP,
            postprocessed_subtitle_path=str(output),
            config_snapshot=PostprocessConfig(trim_trailing_punct=True),
        )
    )

    segment = result.output_data.segments[0]
    assert segment.text == "Original."
    assert segment.translated_text == "译文"
    assert result.layout is SubtitleLayoutEnum.ORIGINAL_ON_TOP


def test_output_collision_falls_back_without_touching_input(tmp_path):
    source = tmp_path / "input.srt"
    original_bytes = _write_srt(source)
    task = PostprocessTask(
        str(source),
        postprocessed_subtitle_path=str(source),
        config_snapshot=PostprocessConfig(),
    )

    result = run_postprocess_task(task)

    assert not result.succeeded
    assert result.used_fallback
    assert task.status == "fallback"
    assert task.active_subtitle_path == str(source)
    assert task.result_data is not None
    assert task.result_data is not result.output_data
    assert source.read_bytes() == original_bytes


def test_invalid_initial_subtitle_is_not_reported_as_a_valid_fallback(tmp_path):
    source = tmp_path / "empty.srt"
    source.write_text("", encoding="utf-8")

    try:
        run_postprocess_task(
            PostprocessTask(str(source), config_snapshot=PostprocessConfig())
        )
    except ValueError as exc:
        assert "empty subtitle" in str(exc)
    else:
        raise AssertionError("invalid initial subtitle must terminate the task")


def test_analyze_is_stage_wide_read_only_and_keeps_initial_as_active(tmp_path):
    source = tmp_path / "input.srt"
    original_bytes = _write_srt(source, "[Music]\n一段非常非常长而且阅读时间很短的字幕。")
    requested_output = tmp_path / "must-not-exist.srt"
    task = PostprocessTask(
        str(source),
        postprocessed_subtitle_path=str(requested_output),
        config_snapshot=PostprocessConfig(
            remove_placeholders=True,
            trim_trailing_punct=True,
            fix_gaps=True,
            speed_optimize=True,
            speed_mode="analyze",
            audit_reading_speed=True,
        ),
    )

    result = run_postprocess_task(task)

    assert result.succeeded
    assert not result.used_fallback
    assert any(
        segment.text.endswith("。") or segment.translated_text.endswith("。")
        for segment in result.output_data.segments
    )
    assert source.read_bytes() == original_bytes
    assert not requested_output.exists()
    assert task.postprocessed_subtitle_path is None
    assert task.active_subtitle_path == str(source)
    assert result.report.speed is not None
    assert result.report.audit is not None


def test_speed_does_not_short_circuit_gap_audit_or_default_trim(monkeypatch):
    data = ASRData(
        [
            ASRDataSeg("原文。", 0, 1000, "译文。"),
            ASRDataSeg("第二句。", 1200, 2200, "第二句译文。"),
        ]
    )

    def identity_speed(value, **kwargs):
        class Result:
            pass

        return value, Result()

    monkeypatch.setattr("videocaptioner.core.speed.pipeline.optimize_speed", identity_speed)
    config = PostprocessConfig(
        trim_trailing_punct=True,
        fix_gaps=True,
        audit_reading_speed=True,
        speed_optimize=True,
    )

    output, report = run_post_stage(data, config)

    assert output.segments[0].translated_text == "译文"
    assert output.segments[0].text == "原文。"
    assert output.segments[0].end_time == 1200
    assert report.audit is not None
    assert report.speed is not None


def test_final_normalize_removes_punctuation_reintroduced_by_speed(monkeypatch):
    data = ASRData([ASRDataSeg("Original", 0, 2000, "译文，")])

    def reintroduce_punctuation(value, **kwargs):
        value.segments[0].translated_text += "，"

        class Result:
            pass

        return value, Result()

    monkeypatch.setattr(
        "videocaptioner.core.speed.pipeline.optimize_speed", reintroduce_punctuation
    )

    output, _ = run_post_stage(
        data,
        PostprocessConfig(trim_trailing_punct=True, speed_optimize=True),
    )

    assert output.segments[0].translated_text == "译文"


def test_memory_handoff_preserves_translate_only_layout(tmp_path):
    source = tmp_path / "initial.srt"
    source.write_text("placeholder", encoding="utf-8")
    task = PostprocessTask(
        str(source),
        input_data=ASRData([ASRDataSeg("Source", 0, 2000, "译文")]),
        layout_mode=PostprocessLayoutMode.TRANSLATE_ONLY,
        config_snapshot=PostprocessConfig(speed_optimize=False),
    )

    result = run_postprocess_task(task)

    assert result.succeeded
    assert result.layout is SubtitleLayoutEnum.ONLY_TRANSLATE
    saved = Path(task.postprocessed_subtitle_path).read_text(encoding="utf-8")
    assert "译文" in saved
    assert "Source" not in saved


def test_memory_handoff_preserves_original_only_layout(tmp_path):
    source = tmp_path / "initial.srt"
    source.write_text("placeholder", encoding="utf-8")
    task = PostprocessTask(
        str(source),
        input_data=ASRData([ASRDataSeg("Source", 0, 2000, "译文")]),
        layout_mode=PostprocessLayoutMode.ORIGINAL_ONLY,
        config_snapshot=PostprocessConfig(speed_optimize=False),
    )

    result = run_postprocess_task(task)

    assert result.succeeded
    assert result.layout is SubtitleLayoutEnum.ONLY_ORIGINAL
    saved = Path(task.postprocessed_subtitle_path).read_text(encoding="utf-8")
    assert "Source" in saved
    assert "译文" not in saved
