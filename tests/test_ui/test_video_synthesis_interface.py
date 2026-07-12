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
    # output naming: provisional 【视频合成】 name, height 'src' until worker probes
    assert task.output_path.endswith('【视频合成】video_src_nvenc_h265_6000k.mp4'), task.output_path

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


def test_encoder_selection_repopulates_encoder_options():
    _run_qt_script(
        """
from PyQt5.QtWidgets import QApplication
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.view.video_synthesis_interface import VideoSynthesisInterface

app = QApplication([])
keys = [cfg.video_encoder, cfg.enc_preset, cfg.enc_tune, cfg.fast_decode]
saved = [k.value for k in keys]
try:
    w = VideoSynthesisInterface()

    # x264: has presets ('medium' among them), tunes, and fastdecode support
    w._on_encoder_selected('x264', 'H.264 (x264)')
    preset_items = [w.enc_preset_combo.itemText(i) for i in range(w.enc_preset_combo.count())]
    assert 'medium' in preset_items, preset_items
    assert w.enc_preset_container.isHidden() is False
    assert w.enc_tune_container.isHidden() is False
    assert w.fast_decode_container.isHidden() is False

    # svt_av1: numeric presets, no tunes tuple in catalog -> tune row hidden
    w._on_encoder_selected('svt_av1', 'AV1 (SVT)')
    preset_items = [w.enc_preset_combo.itemText(i) for i in range(w.enc_preset_combo.count())]
    assert '8' in preset_items, preset_items
    assert w.enc_tune_container.isHidden() is True

    # selecting a preset value persists to cfg (skip the leading 自动/默认 item)
    w.enc_preset_combo.setCurrentIndex(1)
    assert cfg.enc_preset.value == w.enc_preset_combo.itemData(1)

    # '自动/默认' maps back to empty string (None at the EncodeSettings layer)
    w.enc_preset_combo.setCurrentIndex(0)
    assert cfg.enc_preset.value == ''

    print('OK')
finally:
    for k, v in zip(keys, saved):
        cfg.set(k, v)
"""
    )


def test_resolution_combo_updates_target_height():
    _run_qt_script(
        """
from PyQt5.QtWidgets import QApplication
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.view.video_synthesis_interface import VideoSynthesisInterface

app = QApplication([])
saved = cfg.target_height.value
try:
    w = VideoSynthesisInterface()

    w.resolution_combo.setCurrentIndex(2)  # 1080p
    assert cfg.target_height.value == 1080, cfg.target_height.value

    w.resolution_combo.setCurrentIndex(4)  # 4K -> height 2160
    assert cfg.target_height.value == 2160, cfg.target_height.value

    w.resolution_combo.setCurrentIndex(5)  # 自定义
    w.custom_height_input.setText('900')
    assert cfg.target_height.value == 900, cfg.target_height.value

    w.resolution_combo.setCurrentIndex(0)  # 与源相同
    assert cfg.target_height.value == 0, cfg.target_height.value

    print('OK')
finally:
    cfg.set(cfg.target_height, saved)
"""
    )


def test_audio_encoder_copy_disables_bitrate():
    _run_qt_script(
        """
from PyQt5.QtWidgets import QApplication
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.view.video_synthesis_interface import VideoSynthesisInterface

app = QApplication([])
saved = cfg.audio_encoder.value
try:
    w = VideoSynthesisInterface()

    # index 0 = 直通 (copy) -> bitrate disabled
    w.audio_encoder_combo.setCurrentIndex(0)
    assert cfg.audio_encoder.value == 'copy'
    assert w.audio_bitrate_combo.isEnabled() is False

    # index 1 = AAC -> bitrate enabled
    w.audio_encoder_combo.setCurrentIndex(1)
    assert cfg.audio_encoder.value == 'aac'
    assert w.audio_bitrate_combo.isEnabled() is True

    # FLAC (lossless) also disables bitrate
    w.audio_encoder_combo.setCurrentIndex(5)
    assert cfg.audio_encoder.value == 'flac'
    assert w.audio_bitrate_combo.isEnabled() is False

    print('OK')
finally:
    cfg.set(cfg.audio_encoder, saved)
"""
    )


def test_task_factory_maps_resolution_audio_container_fields():
    _run_qt_script(
        """
from PyQt5.QtWidgets import QApplication
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.task_factory import TaskFactory

app = QApplication([])
keys = [
    cfg.soft_subtitle, cfg.video_encoder, cfg.target_height, cfg.out_fps, cfg.vfr,
    cfg.audio_encoder, cfg.audio_bitrate_kbps, cfg.container, cfg.faststart,
    cfg.keep_metadata, cfg.start_zero, cfg.enc_preset, cfg.enc_tune, cfg.enc_profile,
    cfg.enc_level, cfg.fast_decode,
]
saved = [k.value for k in keys]
try:
    cfg.set(cfg.soft_subtitle, False)
    cfg.set(cfg.video_encoder, 'x264')
    cfg.set(cfg.target_height, 1080)
    cfg.set(cfg.out_fps, '')
    cfg.set(cfg.vfr, False)
    cfg.set(cfg.audio_encoder, 'aac')
    cfg.set(cfg.audio_bitrate_kbps, 128)
    cfg.set(cfg.container, 'mkv')
    cfg.set(cfg.faststart, False)
    cfg.set(cfg.keep_metadata, False)
    cfg.set(cfg.start_zero, False)
    cfg.set(cfg.enc_preset, 'slow')
    cfg.set(cfg.enc_tune, 'film')
    cfg.set(cfg.enc_profile, 'high')
    cfg.set(cfg.enc_level, '4.1')
    cfg.set(cfg.fast_decode, True)

    task = TaskFactory.create_synthesis_task('C:/x/video.mp4', 'C:/x/sub.srt')
    es = task.synthesis_config.encode_settings

    assert es.target_height == 1080, es.target_height
    assert es.fps is None, es.fps
    assert es.vfr is False
    assert es.audio_encoder == 'aac'
    assert es.audio_bitrate_kbps == 128
    assert es.container == 'mkv'
    assert es.faststart is False
    assert es.keep_metadata is False
    assert es.start_zero is False
    assert es.enc_preset == 'slow'
    assert es.enc_tune == 'film'
    assert es.enc_profile == 'high'
    assert es.enc_level == '4.1'
    assert es.fast_decode is True

    # '' target_height / out_fps map back to None
    cfg.set(cfg.target_height, 0)
    cfg.set(cfg.out_fps, '29.97')
    cfg.set(cfg.enc_preset, '')
    task2 = TaskFactory.create_synthesis_task('C:/x/video2.mp4', 'C:/x/sub2.srt')
    es2 = task2.synthesis_config.encode_settings
    assert es2.target_height is None, es2.target_height
    assert es2.fps == 29.97, es2.fps
    assert es2.enc_preset is None, es2.enc_preset

    print('OK')
finally:
    for k, v in zip(keys, saved):
        cfg.set(k, v)
"""
    )

