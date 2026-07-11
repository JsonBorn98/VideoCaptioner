from videocaptioner.core.asr.asr_data import ASRDataSeg
from videocaptioner.core.speed.policy import get_speed_policy
from videocaptioner.core.speed.protection import detect_protected_cues


def test_explicit_music_and_short_long_cues_are_protected():
    segments = [
        ASRDataSeg("normal", 0, 1000),
        ASRDataSeg("music", 2500, 5000),
        ASRDataSeg("title", 7000, 12000),
    ]
    matches = detect_protected_cues(
        segments,
        ["普通字幕", "♪ singing ♪", "标题"],
        get_speed_policy(),
        explicit_indices=(0,),
    )
    assert [(match.index, match.reason) for match in matches] == [
        (0, "explicit"),
        (1, "music_or_lyric_marker"),
        (2, "short_text_long_display"),
    ]
