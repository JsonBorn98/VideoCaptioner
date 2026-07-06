from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.entities import SubtitleLayoutEnum
from videocaptioner.core.subtitle.style_manager import (
    DEFAULT_REFERENCE_HEIGHT,
    DEFAULT_REFERENCE_WIDTH,
    SecondaryStyle,
    StyleMode,
    SubtitleStyle,
)


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


def test_ass_style_defaults_reference_resolution_for_legacy_json():
    style = SubtitleStyle.from_json(
        {
            "name": "legacy",
            "mode": "ass",
            "font_name": "Arial",
            "font_size": 40,
        }
    )

    assert style.reference_width == DEFAULT_REFERENCE_WIDTH
    assert style.reference_height == DEFAULT_REFERENCE_HEIGHT


def test_ass_style_serializes_reference_resolution():
    style = SubtitleStyle(
        name="hd",
        mode=StyleMode.ASS,
        reference_width=1920,
        reference_height=1080,
    )

    data = style.to_json_dict()

    assert data["reference_width"] == 1920
    assert data["reference_height"] == 1080


def test_rounded_style_serializes_and_loads_reference_resolution():
    style = SubtitleStyle.from_json(
        {
            "name": "rounded-hd",
            "mode": "rounded",
            "reference_width": 2560,
            "reference_height": 1440,
            "font_name": "Noto Sans SC",
            "font_size": 28,
        }
    )

    data = style.to_json_dict()
    rounded = style.to_rounded_dict()

    assert style.reference_width == 2560
    assert style.reference_height == 1440
    assert data["reference_width"] == 2560
    assert data["reference_height"] == 1440
    assert rounded["reference_width"] == 2560
    assert rounded["reference_height"] == 1440


def test_reference_resolution_is_clamped_when_loading_json():
    style = SubtitleStyle.from_json(
        {
            "mode": "ass",
            "reference_width": 1,
            "reference_height": 0,
        }
    )

    assert style.reference_width == 320
    assert style.reference_height == 180


def test_ass_style_emits_shadow_and_margins():
    """to_ass_string 应输出 Shadow / MarginL / MarginR 字段。"""
    style = SubtitleStyle(
        mode=StyleMode.ASS,
        shadow=1.5,
        margin_l=20,
        margin_r=25,
        margin_bottom=40,
    )
    default_line = [
        ln for ln in style.to_ass_string().splitlines() if ln.startswith("Style: Default,")
    ][0]
    parts = default_line.split(",")
    assert parts[17] == "1.5"  # Shadow
    assert parts[19] == "20"   # MarginL
    assert parts[20] == "25"   # MarginR
    assert parts[21] == "40"   # MarginV


def test_ass_style_default_is_backward_compatible():
    """默认样式（无新字段）应保持旧输出：Shadow=0、Margin=10,10。"""
    default_line = [
        ln for ln in SubtitleStyle().to_ass_string().splitlines()
        if ln.startswith("Style: Default,")
    ][0]
    assert default_line.endswith(",0,2,10,10,30,1")


def test_secondary_shadow_and_margin_bottom_fallback():
    """副样式 shadow 输出，margin_bottom=None 时沿用主样式。"""
    style = SubtitleStyle(
        mode=StyleMode.ASS,
        margin_bottom=30,
        secondary=SecondaryStyle(shadow=2.0, margin_bottom=None),
    )
    sec_line = [
        ln for ln in style.to_ass_string().splitlines() if ln.startswith("Style: Secondary,")
    ][0]
    parts = sec_line.split(",")
    assert parts[17] == "2"    # Shadow (compact)
    assert parts[21] == "30"   # MarginV falls back to main margin_bottom


def test_scaled_border_and_shadow_in_generated_ass():
    """生成的 ASS 头应包含 ScaledBorderAndShadow: yes。"""
    ass = ASRData([ASRDataSeg("hi", 0, 1000, "你好")]).to_ass()
    assert "ScaledBorderAndShadow: yes" in ass


def test_new_fields_round_trip_through_json():
    """新字段应能通过 JSON 往返。"""
    style = SubtitleStyle(
        mode=StyleMode.ASS, shadow=1.0, margin_l=15, margin_r=18, wrap_style=2
    )
    restored = SubtitleStyle.from_json(style.to_json_dict())
    assert restored.shadow == 1.0
    assert restored.margin_l == 15
    assert restored.margin_r == 18
    assert restored.wrap_style == 2
