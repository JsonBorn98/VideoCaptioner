"""视频合成界面【视频编码】区 + task_factory 编码设置映射测试。

沿用项目 GUI 测试惯例：在 offscreen 子进程里构造界面并断言（见 test_postprocess_interface）。
"""

import os
import subprocess
import sys


def _run_qt_script(script: str) -> None:
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_encode_section_wiring():
    _run_qt_script(
        """
from PyQt5.QtWidgets import QApplication
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.view.video_synthesis_interface import VideoSynthesisInterface
from videocaptioner.core.synthesis import get_encoder_spec

app = QApplication([])
keys = [cfg.video_encoder, cfg.encode_mode, cfg.encode_cq,
        cfg.encode_bitrate_kbps, cfg.soft_subtitle, cfg.need_video]
saved = [k.value for k in keys]
try:
    w = VideoSynthesisInterface()

    # section built
    assert len(w.encoder_menu.actions()) >= 10, 'encoder menu too small'
    assert w.encode_mode_combo.count() == 2

    # encoder selection -> cfg + native quality range
    w._on_encoder_selected('svt_av1', 'AV1 (SVT)')
    assert cfg.video_encoder.value == 'svt_av1'
    assert w.quality_slider.maximum() == get_encoder_spec('svt_av1').quality_max == 63
    w._on_encoder_selected('x264', 'H.264 (x264)')
    assert w.quality_slider.maximum() == 51

    # mode toggle swaps visibility
    w._on_encode_mode_changed(1)
    assert cfg.encode_mode.value == 'abr'
    assert w.bitrate_container.isHidden() is False and w.quality_container.isHidden() is True
    w._on_encode_mode_changed(0)
    assert cfg.encode_mode.value == 'cq'
    assert w.quality_container.isHidden() is False and w.bitrate_container.isHidden() is True

    # cq slider -> cfg
    w._on_cq_changed(30)
    assert cfg.encode_cq.value == 30 and w.quality_value_label.text() == '30'

    # encode section disabled for soft subtitle (video is stream-copied)
    cfg.set(cfg.need_video, True)
    w.need_video_action.setChecked(True)
    w.soft_subtitle_action.setChecked(True)
    w._update_synthesis_controls_state()
    assert w.encoder_button.isEnabled() is False
    w.soft_subtitle_action.setChecked(False)
    w._update_synthesis_controls_state()
    assert w.encoder_button.isEnabled() is True

    w.close()
    print('OK')
finally:
    for k, v in zip(keys, saved):
        cfg.set(k, v)
"""
    )


def test_task_factory_reads_encode_settings_from_cfg():
    _run_qt_script(
        """
from PyQt5.QtWidgets import QApplication
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.task_factory import TaskFactory

app = QApplication([])
keys = [cfg.video_encoder, cfg.encode_mode, cfg.encode_cq,
        cfg.encode_bitrate_kbps, cfg.soft_subtitle]
saved = [k.value for k in keys]
try:
    cfg.set(cfg.soft_subtitle, False)
    cfg.set(cfg.video_encoder, 'hevc_nvenc')
    cfg.set(cfg.encode_mode, 'abr')
    cfg.set(cfg.encode_bitrate_kbps, 6000)
    cfg.set(cfg.encode_cq, 30)
    task = TaskFactory.create_synthesis_task('C:/x/video.mp4', 'C:/x/sub.srt')
    es = task.synthesis_config.encode_settings
    assert es.video_encoder == 'hevc_nvenc', es.video_encoder
    assert es.encode_mode == 'abr'
    assert es.bitrate_kbps == 6000
    assert es.quality == 30

    # hard-burn must never be copy (derived rule)
    cfg.set(cfg.video_encoder, 'copy')
    task2 = TaskFactory.create_synthesis_task('C:/x/v.mp4', 'C:/x/s.srt')
    assert task2.synthesis_config.encode_settings.video_encoder == 'x264'
    print('OK')
finally:
    for k, v in zip(keys, saved):
        cfg.set(k, v)
"""
    )


def test_ffmpeg_source_toggle_rebuilds_encoder_menu():
    _run_qt_script(
        """
from PyQt5.QtWidgets import QApplication
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.view.video_synthesis_interface import VideoSynthesisInterface

app = QApplication([])
saved = cfg.ffmpeg_source.value
try:
    w = VideoSynthesisInterface()
    assert hasattr(w, 'ffmpeg_button')
    assert len(w.encoder_menu.actions()) >= 10
    w._on_ffmpeg_source_changed('custom')
    assert cfg.ffmpeg_source.value == 'custom'
    assert len(w.encoder_menu.actions()) >= 10  # menu rebuilt for the new source
    w._on_ffmpeg_source_changed('default')
    assert cfg.ffmpeg_source.value == 'default'
    print('OK')
finally:
    cfg.set(cfg.ffmpeg_source, saved)
"""
    )
