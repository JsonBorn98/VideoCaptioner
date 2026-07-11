from videocaptioner.cli import exit_codes as EXIT
from videocaptioner.cli.main import main
from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.entities import SubtitleLayoutEnum


def test_subtitle_command_normalizes_ass_output_to_canonical_srt(tmp_path, capsys):
    source = tmp_path / "styled.ass"
    ASRData([ASRDataSeg("Hello", 0, 1000)]).save(
        str(source),
        ass_style="[V4+ Styles]\nStyle: Default,InputStyle",
        layout=SubtitleLayoutEnum.ONLY_ORIGINAL,
    )
    original = source.read_bytes()
    requested = tmp_path / "initial.ass"

    result = main(
        [
            "subtitle",
            str(source),
            "--no-optimize",
            "--no-translate",
            "--no-split",
            "-o",
            str(requested),
        ]
    )

    canonical = tmp_path / "initial.srt"
    assert result == EXIT.SUCCESS
    assert source.read_bytes() == original
    assert canonical.exists()
    assert not requested.exists()
    assert "InputStyle" not in canonical.read_text(encoding="utf-8")
    assert str(canonical) in capsys.readouterr().err


def test_subtitle_help_describes_canonical_srt_output(capsys):
    try:
        main(["subtitle", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
    help_text = capsys.readouterr().out

    assert "Canonical SRT output" in help_text
    assert "--format" not in help_text
