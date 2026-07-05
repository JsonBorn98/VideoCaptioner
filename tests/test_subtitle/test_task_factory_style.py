import json

from videocaptioner.core.entities import SubtitleRenderModeEnum
from videocaptioner.ui.task_factory import TaskFactory


def test_task_factory_does_not_use_rounded_style_for_ass(monkeypatch, tmp_path):
    """需要 ASS 时，不能把同名/仅有的圆角样式当 ASS 样式使用。"""
    styles_dir = tmp_path / "styles"
    styles_dir.mkdir()
    (styles_dir / "ass-default.json").write_text(
        json.dumps(
            {
                "name": "default",
                "mode": "ass",
                "reference_width": 1920,
                "reference_height": 1080,
                "font_name": "Ass Font",
                "font_size": 64,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (styles_dir / "rounded-poster.json").write_text(
        json.dumps(
            {
                "name": "poster",
                "mode": "rounded",
                "reference_width": 3840,
                "reference_height": 2160,
                "font_name": "Rounded Font",
                "font_size": 72,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("videocaptioner.config.SUBTITLE_STYLE_PATH", styles_dir)

    ass_style = TaskFactory.get_ass_style("poster")
    reference = TaskFactory.get_style_reference(
        "poster",
        SubtitleRenderModeEnum.ASS_STYLE,
    )

    assert "Ass Font" in ass_style
    assert "Rounded Font" not in ass_style
    assert reference == (1920, 1080)
