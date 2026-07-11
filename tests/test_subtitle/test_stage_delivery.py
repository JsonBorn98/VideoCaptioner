from pathlib import Path

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.entities import SubtitleExportPolicy, SubtitleLayoutEnum
from videocaptioner.ui.task_factory import TaskFactory


def _subtitle() -> ASRData:
    return ASRData([ASRDataSeg("Original", 0, 2000, "译文")])


def test_stage_save_always_writes_srt_and_optional_ass_with_same_prefix(tmp_path):
    srt_path = tmp_path / "【初版字幕】episode.srt"
    policy = SubtitleExportPolicy(
        enabled=True,
        format="ass",
        layout=SubtitleLayoutEnum.TRANSLATE_ON_TOP,
        ass_style=(
            "[V4+ Styles]\n"
            "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
            "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,"
            "BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
            "Style: Default,Arial,60,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
            "-1,0,0,0,100,100,0,0,1,3,0,2,10,10,30,1\n"
            "Style: Secondary,Arial,40,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
            "-1,0,0,0,100,100,0,0,1,2,0,2,10,10,30,1"
        ),
        reference_width=1920,
        reference_height=1080,
    )

    canonical, exported, warning = TaskFactory.save_stage_subtitle(
        _subtitle(),
        str(srt_path),
        layout=SubtitleLayoutEnum.TRANSLATE_ON_TOP,
        export_policy=policy,
    )

    assert canonical == str(srt_path)
    assert exported == str(srt_path.with_suffix(".ass"))
    assert warning is None
    assert srt_path.is_file()
    ass = srt_path.with_suffix(".ass").read_text(encoding="utf-8")
    assert "PlayResX: 1920" in ass
    assert "PlayResY: 1080" in ass
    assert "; SubtitleLayout: TRANSLATE_ON_TOP" in ass


def test_stage_export_failure_keeps_canonical_srt(tmp_path, monkeypatch):
    from videocaptioner.ui import task_factory

    target = tmp_path / "【后处理字幕】episode.srt"
    policy = SubtitleExportPolicy(enabled=True, format="ass")

    def fail_export(*args, **kwargs):
        raise PermissionError("delivery file locked")

    monkeypatch.setattr(task_factory, "export_subtitle_atomic", fail_export)
    canonical, exported, warning = TaskFactory.save_stage_subtitle(
        _subtitle(),
        str(target),
        layout=SubtitleLayoutEnum.TRANSLATE_ON_TOP,
        export_policy=policy,
    )

    assert canonical == str(target)
    assert exported is None
    assert "locked" in (warning or "")
    assert target.is_file()


def test_subtitle_task_uses_initial_srt_and_no_legacy_layout_fanout(tmp_path):
    source = tmp_path / "【转录字幕】episode.srt"
    source.write_text("1\n00:00:00,000 --> 00:00:02,000\nOriginal\n", encoding="utf-8")
    task = TaskFactory.create_subtitle_task(
        str(source),
        need_next_task=True,
        workflow_base_name="episode",
        input_data=_subtitle(),
        export_policy=SubtitleExportPolicy(enabled=False),
    )

    assert Path(task.output_path or "").suffix == ".srt"
    assert Path(task.output_path or "").name.startswith("【初版字幕】")
