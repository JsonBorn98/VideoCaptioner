import pytest

from videocaptioner.core.speed import (
    GraphemeKind,
    calculate_reading_load,
    classify_grapheme,
    count_reading_units,
    get_speed_policy,
    split_graphemes,
)


def test_extended_graphemes_keep_combining_character_and_emoji_together():
    graphemes = split_graphemes("e\u0301👨‍👩‍👧‍👦")

    assert graphemes == ("e\u0301", "👨‍👩‍👧‍👦")


@pytest.mark.parametrize("text", ["汉", "あ", "ア", "한"])
def test_first_version_cjk_scripts(text):
    assert classify_grapheme(text) is GraphemeKind.CJK


@pytest.mark.parametrize("text", ["A", "7", "ع", "ก", "👩🏽‍💻"])
def test_other_scripts_digits_and_emoji_are_non_cjk(text):
    assert classify_grapheme(text) is GraphemeKind.NON_CJK


def test_punctuation_and_whitespace_have_configured_weight_without_double_counting():
    policy = get_speed_policy()
    units = count_reading_units("你好， world!", policy)

    assert units.cjk_units == 2
    assert units.non_cjk_units == pytest.approx(5 + 0.25 + 0.5)
    assert units.grapheme_count == 10


def test_punctuation_variation_selector_does_not_turn_punctuation_into_symbol():
    assert classify_grapheme("‼️") is GraphemeKind.STRONG_PUNCTUATION


def test_mixed_text_uses_component_reading_rates():
    policy = get_speed_policy()
    load = calculate_reading_load("中文AB", 1.0, policy)

    assert load.required_comfort_seconds == pytest.approx(2 / 9 + 2 / 16)
    assert load.required_hard_seconds == pytest.approx(2 / 11 + 2 / 20)
    assert load.comfort_load == pytest.approx(load.required_comfort_seconds)
    assert load.hard_load == pytest.approx(load.required_hard_seconds)


def test_empty_text_has_zero_load_but_valid_duration():
    load = calculate_reading_load(" \n", 2.0, get_speed_policy())

    assert load.valid
    assert load.units.total_units == 0
    assert load.comfort_load == 0
    assert load.hard_load == 0


@pytest.mark.parametrize("duration", [0.0, -1.0, float("inf"), float("nan")])
def test_invalid_duration_has_required_time_but_no_ratio(duration):
    load = calculate_reading_load("字幕", duration, get_speed_policy())

    assert not load.valid
    assert load.required_hard_seconds > 0
    assert load.comfort_load is None
    assert load.hard_load is None
