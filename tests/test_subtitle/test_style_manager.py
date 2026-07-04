from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.entities import SubtitleLayoutEnum
from videocaptioner.core.subtitle.style_manager import SecondaryStyle, SubtitleStyle


def _style_field_counts(ass_text: str) -> tuple[int, list[int]]:
    format_count = 0
    style_counts: list[int] = []
    for line in ass_text.splitlines():
        if line.startswith("Format:") and "Fontname" in line:
            format_count = len(line.split(":", 1)[1].split(","))
        elif line.startswith("Style:"):
            style_counts.append(len(line.split(":", 1)[1].split(",")))
    return format_count, style_counts


def test_subtitle_style_outputs_valid_ass_style_field_counts():
    style = SubtitleStyle(
        font_name="LXGW WenKai",
        font_size=48,
        primary_color="#ffffff",
        outline_color="#002459",
        outline_width=5.0,
        spacing=3.2,
        margin_bottom=30,
        secondary=SecondaryStyle(
            font_name="Noto Sans SC",
            font_size=30,
            color="#ffffff",
            outline_color="#540000",
            outline_width=4.0,
            spacing=0.8,
        ),
    )

    style_text = style.to_ass_string()
    format_count, style_counts = _style_field_counts(style_text)

    assert format_count == 23
    assert style_counts == [format_count, format_count]
    assert "\\q" not in style_text


def test_asr_data_to_ass_places_wrap_style_in_script_info():
    style = SubtitleStyle().to_ass_string()
    ass_text = ASRData(
        [ASRDataSeg("hello", 0, 1000, translated_text="你好")]
    ).to_ass(
        style_str=style,
        layout=SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        video_width=1920,
        video_height=1080,
    )

    format_count, style_counts = _style_field_counts(ass_text)

    assert "WrapStyle: 1" in ass_text.split("[V4+ Styles]", 1)[0]
    assert format_count == 23
    assert style_counts == [format_count, format_count]
