from argparse import Namespace
from pathlib import Path

from videocaptioner.cli.main import _build_cli_overrides
from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.entities import (
    SubtitleConfig,
    SubtitleLayoutEnum,
    SubtitleTask,
)
from videocaptioner.core.postprocess import PostprocessConfig, run_post_stage
from videocaptioner.core.postprocess.profiles import PostprocessProfileStore
from videocaptioner.core.speed.profiles import SpeedProfileStore
from videocaptioner.ui.task_factory import TaskFactory
from videocaptioner.ui.thread.subtitle_thread import SubtitleThread


def test_unified_speed_path_replaces_old_speed_steps():
    data = ASRData(
        [
            ASRDataSeg("source one", 0, 2000, "短句"),
            ASRDataSeg(
                "source two",
                2400,
                3000,
                "这是一句明显过快并且需要更多显示时间的中文字幕",
            ),
        ]
    )
    config = PostprocessConfig(
        speed_optimize=True,
        fix_gaps=True,
        audit_reading_speed=True,
        compress_fast_subtitles=True,
    )
    optimized, report = run_post_stage(
        data,
        config,
        layout=SubtitleLayoutEnum.ONLY_TRANSLATE,
    )
    assert report.speed is not None
    assert report.audit is not None
    assert optimized.segments[0].end_time == optimized.segments[1].start_time


def test_speed_cli_flags_map_to_unified_config():
    overrides = _build_cli_overrides(
        Namespace(
            speed_optimize=True,
            no_speed_optimize=False,
            speed_mode="analyze",
            speed_profile="smooth",
            speed_profile_file="cinema.json",
            speed_primary="translate",
            speed_media="movie.mp4",
            speed_precise_timing=True,
            speed_save_timing_sidecar=True,
            speed_reference_audit=True,
            speed_semantic_repair=True,
            no_speed_semantic_repair=False,
            speed_semantic_window=7,
            no_speed_llm_review=True,
        )
    )
    postprocess = overrides["postprocess"]
    assert postprocess["speed_optimize"] is True
    assert postprocess["mode"] == "analyze"
    assert postprocess["profile"] == "smooth"
    assert postprocess["speed_profile_file"] == "cinema.json"
    assert postprocess["media"] == "movie.mp4"
    assert postprocess["precise_timing"] is True
    assert postprocess["reference_audit"] is True
    assert postprocess["semantic_repair"] is True
    assert postprocess["semantic_window"] == 7
    assert postprocess["llm_uncertain_review"] is False


def test_editor_snapshot_does_not_overwrite_original_input(tmp_path):
    input_path = tmp_path / "input.srt"
    output_path = tmp_path / "output.srt"
    original = ASRData([ASRDataSeg("original input", 0, 1000)])
    original.save(str(input_path), layout=SubtitleLayoutEnum.ONLY_ORIGINAL)
    edited = ASRData([ASRDataSeg("edited in memory", 0, 1000)])
    task = SubtitleTask(
        subtitle_path=str(input_path),
        editor_data_json=edited.to_json(),
        output_path=str(output_path),
        need_next_task=False,
        subtitle_config=SubtitleConfig(
            need_split=False,
            need_optimize=False,
            need_translate=False,
            subtitle_layout=SubtitleLayoutEnum.ONLY_ORIGINAL,
        ),
    )
    SubtitleThread(task).run()
    assert "original input" in input_path.read_text(encoding="utf-8")
    assert "edited in memory" in output_path.read_text(encoding="utf-8")


def test_custom_profile_and_task_overrides_reach_runtime(tmp_path, monkeypatch):
    import videocaptioner.core.speed.profiles as profiles

    store_path = tmp_path / "speed_profiles.json"
    store = SpeedProfileStore(store_path)
    store.create(
        "Runtime custom",
        profile_id="runtime-custom",
        overrides={"hard_cps_cjk": 30.0, "comfort_cps_cjk": 20.0},
    )
    monkeypatch.setattr(profiles, "DEFAULT_SPEED_PROFILES_PATH", store_path)
    data = ASRData([ASRDataSeg("source", 0, 1000, "这是运行时配置测试字幕")])

    _, report = run_post_stage(
        data,
        PostprocessConfig(
            speed_optimize=True,
            speed_mode="analyze",
            speed_profile="runtime-custom",
            speed_overrides={"hard_cps_cjk": 25.0},
        ),
        layout=SubtitleLayoutEnum.ONLY_TRANSLATE,
    )

    assert report.speed is not None
    assert report.speed.profile_id == "runtime-custom"
    assert report.speed.policy.comfort_cps_cjk == 20.0
    assert report.speed.policy.hard_cps_cjk == 25.0


def test_missing_custom_profile_falls_back_to_balanced(tmp_path, monkeypatch):
    import videocaptioner.core.speed.profiles as profiles

    monkeypatch.setattr(profiles, "DEFAULT_SPEED_PROFILES_PATH", tmp_path / "missing-profiles.json")
    data = ASRData([ASRDataSeg("source", 0, 1000, "字幕")])

    _, report = run_post_stage(
        data,
        PostprocessConfig(
            speed_optimize=True,
            speed_mode="analyze",
            speed_profile="deleted-custom",
        ),
    )

    assert report.speed is not None
    assert report.speed.profile_id == "balanced"
    assert report.speed.policy.hard_cps_cjk == 11.0


def test_exported_profile_file_runs_without_importing_to_app_store(tmp_path):
    store = SpeedProfileStore(tmp_path / "source-profiles.json")
    store.create(
        "Portable",
        profile_id="portable",
        overrides={"comfort_cps_cjk": 18.0, "hard_cps_cjk": 24.0},
    )
    profile_file = tmp_path / "portable.json"
    store.export_profile("portable", profile_file)
    data = ASRData([ASRDataSeg("source", 0, 1000, "便携方案测试字幕")])

    _, report = run_post_stage(
        data,
        PostprocessConfig(
            speed_optimize=True,
            speed_mode="analyze",
            speed_profile_file=str(profile_file),
        ),
    )

    assert report.speed is not None
    assert report.speed.profile_id == "portable"
    assert report.speed.policy.hard_cps_cjk == 24.0


def test_speed_settings_lists_custom_profiles_offscreen(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtWidgets import QApplication
    from qfluentwidgets import PushSettingCard, ToolButton

    from videocaptioner.ui.common.config import cfg
    from videocaptioner.ui.view.speed_setting_interface import SpeedSettingInterface

    store = PostprocessProfileStore(tmp_path / "profiles.json")
    store.copy_template("balanced", "Cinema", profile_id="cinema")
    app = QApplication.instance() or QApplication([])
    widget = SpeedSettingInterface(profile_store=store)

    assert "cinema" in widget._profileIds()
    assert widget.bidirectionalCard.isEnabled()
    assert widget.semanticRepairCard.isEnabled()
    assert not any(
        card.titleLabel.text().startswith("恢复") for card in widget.findChildren(PushSettingCard)
    )
    reset_buttons = widget.findChildren(ToolButton, "postprocessResetButton")
    assert reset_buttons

    original_mode = cfg.get(cfg.speed_mode)
    changed_mode = "analyze" if cfg.speed_mode.defaultValue == "apply" else "apply"
    try:
        cfg.set(cfg.speed_mode, changed_mode)
        app.processEvents()
        mode_reset = widget.modeCard.findChild(ToolButton, "postprocessResetButton")
        assert mode_reset is not None and mode_reset.isEnabled()
        mode_reset.click()
        app.processEvents()
        assert cfg.get(cfg.speed_mode) == cfg.speed_mode.defaultValue
        assert not mode_reset.isEnabled()
    finally:
        cfg.set(cfg.speed_mode, original_mode)
    for route_key, tab in widget._tabs.items():
        widget.pivot.items[route_key].click()
        app.processEvents()
        assert widget.stackedWidget.currentWidget() is tab
    widget.close()
    app.processEvents()


def test_direct_subtitle_task_preserves_legacy_split_settings(tmp_path):
    from videocaptioner.ui.common.config import cfg

    original_split = cfg.get(cfg.need_split)
    original_cjk = cfg.get(cfg.max_word_count_cjk)
    original_english = cfg.get(cfg.max_word_count_english)
    try:
        cfg.set(cfg.need_split, True)
        cfg.set(cfg.max_word_count_cjk, 8)
        cfg.set(cfg.max_word_count_english, 8)
        task = TaskFactory.create_subtitle_task(
            file_path=str(tmp_path / "input.srt"),
            need_next_task=False,
        )
        assert task.subtitle_config.need_split
        assert task.subtitle_config.max_word_count_cjk == 8
        assert task.subtitle_config.max_word_count_english == 8
        assert Path(task.output_path).name.startswith("【初版字幕】")
    finally:
        cfg.set(cfg.need_split, original_split)
        cfg.set(cfg.max_word_count_cjk, original_cjk)
        cfg.set(cfg.max_word_count_english, original_english)
