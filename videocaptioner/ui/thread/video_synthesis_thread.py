import datetime
import tempfile
from pathlib import Path
from threading import Event

import psutil
from PyQt5.QtCore import QThread, pyqtSignal

from videocaptioner.core.asr.asr_data import ASRData
from videocaptioner.core.entities import SynthesisTask
from videocaptioner.core.subtitle import clone_subtitle_data
from videocaptioner.core.synthesis import SynthesisCancelled, SynthesisControl
from videocaptioner.core.utils.logger import setup_logger
from videocaptioner.core.utils.video_utils import add_subtitles, add_subtitles_with_style

logger = setup_logger("video_synthesis_thread")


class VideoSynthesisThread(QThread):
    finished = pyqtSignal(SynthesisTask)
    progress = pyqtSignal(int, str)
    error = pyqtSignal(str)
    cancelled = pyqtSignal()
    log = pyqtSignal(str)  # ffmpeg 逐行输出（供控制台）

    def __init__(self, task: SynthesisTask):
        super().__init__()
        self.task = task
        self._cancel_event = Event()
        self._current_process = None
        self._paused = False
        self._control = SynthesisControl(
            cancel_event=self._cancel_event,
            log_callback=self.log.emit,
            on_process=self._register_process,
        )
        logger.debug(f"初始化 VideoSynthesisThread，任务: {self.task}")

    # ---- 取消 / 暂停控制（由 GUI 调用） ----

    def _register_process(self, process) -> None:
        self._current_process = process

    def stop(self) -> None:
        """请求停止：置取消位并 kill 当前 ffmpeg（若暂停则先恢复再 kill）。"""
        self._cancel_event.set()
        p = self._current_process
        if p is not None and p.poll() is None:
            try:
                if self._paused:
                    psutil.Process(p.pid).resume()
            except Exception:
                pass
            try:
                p.kill()
            except Exception:
                pass
        self._paused = False

    def pause(self) -> None:
        p = self._current_process
        if p is not None and p.poll() is None and not self._paused:
            try:
                psutil.Process(p.pid).suspend()
                self._paused = True
            except Exception as e:
                logger.warning(f"暂停失败: {e}")

    def resume(self) -> None:
        p = self._current_process
        if p is not None and self._paused:
            try:
                psutil.Process(p.pid).resume()
            finally:
                self._paused = False

    def is_paused(self) -> bool:
        return self._paused

    def run(self):
        output_path = self.task.output_path
        try:
            self.task.started_at = datetime.datetime.now()
            config = self.task.synthesis_config
            logger.info(f"\n{config.print_config()}")

            video_file = self.task.video_path
            subtitle_file = self.task.subtitle_path

            if not config.need_video:
                logger.info("不需要合成视频，跳过")
                self.progress.emit(100, self.tr("合成完成"))
                self.finished.emit(self.task)
                return

            logger.info(f"开始合成视频: {video_file}")
            self.progress.emit(5, self.tr("正在合成"))

            if not video_file:
                raise ValueError(self.tr("视频路径为空"))
            if not subtitle_file:
                raise ValueError(self.tr("字幕路径为空"))
            if not output_path:
                raise ValueError(self.tr("输出路径为空"))

            # 输出命名：worker 内探测源，按编码设置 + 有效高度细化 【视频合成】… 名（见 §9/§16.1）
            es = config.encode_settings
            if es is not None:
                from dataclasses import replace

                from videocaptioner.core.synthesis import build_output_name, media_probe

                try:
                    probe = media_probe.probe(video_file, source=es.ffmpeg_source)
                    eff_h = probe.effective_height(es.target_height)
                except Exception:
                    eff_h = None
                # 软字幕走流复制，命名体现 copy
                name_es = replace(es, video_encoder="copy") if config.soft_subtitle else es
                out_dir = Path(output_path).parent
                output_path = str(
                    out_dir
                    / build_output_name(Path(video_file).stem, name_es, eff_h, es.container)
                )
                self.task.output_path = output_path

            video_quality = config.video_quality
            crf = video_quality.get_crf()
            preset = video_quality.get_preset()

            # 完整 workflow 直接消费上游内存快照；独立调用才读取 SRT。
            asr_data = (
                clone_subtitle_data(self.task.input_data)
                if self.task.input_data is not None
                else ASRData.from_subtitle_file(
                    subtitle_file,
                    layout=config.subtitle_layout,
                )
            )

            if config.soft_subtitle:
                # 软字幕：转为 SRT 后内嵌
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    suffix=".srt",
                    delete=False,
                    encoding="utf-8",
                    prefix="VideoCaptioner_soft_",
                ) as f:
                    srt_content = asr_data.to_srt(layout=config.subtitle_layout)
                    f.write(srt_content)
                    temp_srt_path = f.name

                try:
                    add_subtitles(
                        video_file,
                        temp_srt_path,
                        output_path,
                        crf=crf,
                        preset=preset,
                        soft_subtitle=True,
                        progress_callback=self.progress_callback,
                        control=self._control,
                    )
                finally:
                    Path(temp_srt_path).unlink(missing_ok=True)

            else:
                # 硬字幕：使用样式配置渲染
                add_subtitles_with_style(
                    video_path=video_file,
                    asr_data=asr_data,
                    output_path=output_path,
                    render_mode=config.render_mode,
                    subtitle_layout=config.subtitle_layout,
                    ass_style=config.ass_style,
                    rounded_style=config.rounded_style,
                    reference_width=config.reference_width,
                    reference_height=config.reference_height,
                    crf=crf,
                    preset=preset,
                    progress_callback=self.progress_callback,
                    encode_settings=config.encode_settings,
                    control=self._control,
                )

            self.progress.emit(100, self.tr("合成完成"))
            logger.info(f"视频合成完成，保存路径: {output_path}")
            self.finished.emit(self.task)

        except SynthesisCancelled:
            logger.info("视频合成已取消")
            if output_path:
                Path(output_path).unlink(missing_ok=True)  # 清理半成品
            self.progress.emit(0, self.tr("已取消"))
            self.cancelled.emit()
        except Exception as e:
            logger.exception(f"视频合成失败: {e}")
            self.error.emit(str(e))
            self.progress.emit(100, self.tr("视频合成失败"))

    def progress_callback(self, value, message):
        progress = int(5 + int(value) / 100 * 95)
        logger.debug(f"合成进度: {progress}% - {message}")
        self.progress.emit(progress, str(progress) + "% " + message)
