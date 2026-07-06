from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.entities import SubtitleLayoutEnum
from videocaptioner.ui.view import subtitle_interface
from videocaptioner.ui.view.subtitle_interface import load_editor_asr_data, save_editor_asr_data


def test_save_editor_asr_data_preserves_ass_style_reference(monkeypatch, tmp_path):
    """编辑页写回 ASS 时不能退化成 SRT 或丢失样式基准分辨率。"""
    style = (
        "[V4+ Styles]\n"
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,"
        "OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,"
        "ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
        "Alignment,MarginL,MarginR,MarginV,Encoding\n"
        "Style: Default,Arial,68,&H00FFFFFF,&H000000FF,&H00000000,"
        "&H00000000,-1,0,0,0,100,100,0,0,1,5,0,2,10,10,30,1\n"
        "Style: Secondary,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,"
        "&H00000000,-1,0,0,0,100,100,0,0,1,4,0,2,10,10,30,1"
    )
    monkeypatch.setattr(subtitle_interface, "get_subtitle_style", lambda _: style)
    monkeypatch.setattr(
        subtitle_interface.TaskFactory,
        "get_style_reference",
        lambda *_args: (1920, 1080),
    )

    ass_path = tmp_path / "edited.ass"
    asr_data = ASRData([ASRDataSeg("Hello", 0, 1000, translated_text="你好")])

    save_editor_asr_data(
        asr_data,
        str(ass_path),
        SubtitleLayoutEnum.TRANSLATE_ON_TOP,
        "1080p",
    )

    ass_text = ass_path.read_text(encoding="utf-8")
    assert ass_text.startswith("[Script Info]")
    assert "PlayResX: 1920" in ass_text
    assert "PlayResY: 1080" in ass_text
    assert "Style: Default,Arial,68" in ass_text
    assert "00:00:00,000 --> 00:00:01,000" not in ass_text


def test_save_editor_asr_data_keeps_non_ass_outputs_plain(tmp_path):
    srt_path = tmp_path / "edited.srt"
    asr_data = ASRData([ASRDataSeg("Hello", 0, 1000, translated_text="你好")])

    save_editor_asr_data(
        asr_data,
        str(srt_path),
        SubtitleLayoutEnum.TRANSLATE_ON_TOP,
        "1080p",
    )

    srt_text = srt_path.read_text(encoding="utf-8")
    assert srt_text.startswith("1\n00:00:00,000 --> 00:00:01,000")
    assert "[Script Info]" not in srt_text


def test_load_editor_asr_data_uses_translate_on_top_layout(tmp_path):
    """编辑页重新加载 raw SRT 时，应按当前布局还原主副字幕语义。"""
    asr_data = ASRData(
        [
            ASRDataSeg(
                "This is original text.",
                0,
                1000,
                translated_text="这是译文",
            )
        ]
    )
    srt_path = tmp_path / "raw.srt"
    srt_path.write_text(
        asr_data.to_srt(layout=SubtitleLayoutEnum.TRANSLATE_ON_TOP),
        encoding="utf-8",
    )

    parsed = load_editor_asr_data(
        str(srt_path),
        SubtitleLayoutEnum.TRANSLATE_ON_TOP,
    )

    assert parsed.segments[0].text == "This is original text."
    assert parsed.segments[0].translated_text == "这是译文"
