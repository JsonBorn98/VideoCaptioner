import json

import pytest

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.entities import SubtitleLayoutEnum
from videocaptioner.core.subtitle.io import (
    canonical_stage_path,
    export_subtitle_atomic,
    import_subtitle,
    save_canonical_srt,
)
from videocaptioner.core.subtitle.style_manager import SecondaryStyle, SubtitleStyle


def _bilingual_data() -> ASRData:
    return ASRData(
        [ASRDataSeg("Original text.", 0, 1500, translated_text="译文。")]
    )


def test_import_videocaptioner_ass_honors_layout_marker_and_discards_style(tmp_path):
    source = tmp_path / "styled.ass"
    source.write_text(
        _bilingual_data().to_ass(
            style_str=SubtitleStyle(
                font_name="InputOnlyFont",
                secondary=SecondaryStyle(font_name="InputSecondaryFont"),
            ).to_ass_string(),
            layout=SubtitleLayoutEnum.TRANSLATE_ON_TOP,
            video_width=1920,
            video_height=1080,
        ),
        encoding="utf-8",
    )

    imported = import_subtitle(source)

    assert imported.layout is SubtitleLayoutEnum.TRANSLATE_ON_TOP
    assert imported.confidence == 1.0
    assert imported.metadata_layout is SubtitleLayoutEnum.TRANSLATE_ON_TOP
    assert imported.data.segments[0].text == "Original text."
    assert imported.data.segments[0].translated_text == "译文。"

    canonical = save_canonical_srt(
        imported.data,
        tmp_path / "canonical.ass",
        layout=imported.layout,
    )
    canonical_text = canonical.read_text(encoding="utf-8")
    assert canonical.suffix == ".srt"
    assert "InputOnlyFont" not in canonical_text
    assert "1920" not in canonical_text
    assert canonical_text.index("译文。") < canonical_text.index("Original text.")


def test_unmarked_bilingual_srt_requires_explicit_layout_confirmation(tmp_path):
    source = tmp_path / "unmarked.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:01,500\n译文。\nOriginal text.\n",
        encoding="utf-8",
    )

    imported = import_subtitle(source)

    assert imported.confidence < 0.7
    assert imported.metadata_layout is None
    assert any("确认" in warning for warning in imported.warnings)


@pytest.mark.parametrize("export_format", ["srt", "vtt", "ass", "json", "txt"])
def test_export_subtitle_atomic_supports_delivery_formats(tmp_path, export_format):
    target = export_subtitle_atomic(
        _bilingual_data(),
        tmp_path / "delivery.tmp",
        export_format=export_format,
        layout=SubtitleLayoutEnum.TRANSLATE_ON_TOP,
        ass_style=SubtitleStyle(font_name="ConfiguredFont"),
        reference_resolution=(1920, 1080),
    )

    assert target == tmp_path / f"delivery.{export_format}"
    assert target.exists()
    assert not list(tmp_path.glob(".delivery.*"))
    if export_format == "ass":
        content = target.read_text(encoding="utf-8")
        assert "Style: Default,ConfiguredFont" in content
        assert "PlayResX: 1920" in content
        assert "PlayResY: 1080" in content
        assert "; SubtitleLayout: TRANSLATE_ON_TOP" in content
    elif export_format == "json":
        assert json.loads(target.read_text(encoding="utf-8"))["1"][
            "translated_subtitle"
        ] == "译文。"


def test_canonical_stage_path_removes_prior_prefix_and_source_extension(tmp_path):
    source = tmp_path / "【初版字幕】【转录字幕】example.ass"

    assert canonical_stage_path(source, "后处理字幕") == (
        tmp_path / "【后处理字幕】example.srt"
    )


def test_atomic_export_preserves_existing_target_when_serialization_fails(
    monkeypatch, tmp_path
):
    target = tmp_path / "result.srt"
    target.write_text("existing", encoding="utf-8")
    data = _bilingual_data()

    def fail_save(*_args, **_kwargs):
        raise OSError("simulated serialization failure")

    monkeypatch.setattr(data, "save", fail_save)

    with pytest.raises(OSError, match="simulated"):
        export_subtitle_atomic(data, target)

    assert target.read_text(encoding="utf-8") == "existing"
    assert not list(tmp_path.glob(".result.*"))
