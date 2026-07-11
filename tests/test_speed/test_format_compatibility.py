import pytest

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.entities import SubtitleLayoutEnum
from videocaptioner.core.speed.pipeline import optimize_speed


@pytest.mark.parametrize("suffix", [".srt", ".vtt", ".ass"])
def test_supported_subtitle_formats_use_the_same_bilingual_speed_pipeline(tmp_path, suffix):
    source = ASRData(
        [
            ASRDataSeg("First source line", 0, 1800, "第一句译文"),
            ASRDataSeg(
                "Second source line",
                2200,
                2800,
                "第二句译文明显更长并且需要更多显示时间",
            ),
        ]
    )
    input_path = tmp_path / f"input{suffix}"
    source.save(str(input_path), layout=SubtitleLayoutEnum.TRANSLATE_ON_TOP)

    loaded = ASRData.from_subtitle_file(str(input_path), layout=SubtitleLayoutEnum.TRANSLATE_ON_TOP)
    optimized, result = optimize_speed(
        loaded,
        mode="apply",
        layout=SubtitleLayoutEnum.TRANSLATE_ON_TOP,
        primary_side="translate",
    )

    assert len(optimized.segments) == 2
    assert all(segment.translated_text for segment in optimized.segments)
    assert result.before.unresolved_hard_count >= result.after.unresolved_hard_count
    output_path = tmp_path / f"output{suffix}"
    optimized.save(str(output_path), layout=SubtitleLayoutEnum.TRANSLATE_ON_TOP)
    assert output_path.exists()
