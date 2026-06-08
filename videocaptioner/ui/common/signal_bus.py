from PyQt5.QtCore import QObject, QUrl, pyqtSignal


class SignalBus(QObject):
    video_play = pyqtSignal()  # 播放信号
    video_pause = pyqtSignal()  # 暂停信号
    video_stop = pyqtSignal()  # 停止信号
    video_source_changed = pyqtSignal(QUrl)  # 视频源改变信号
    video_segment_play = pyqtSignal(int, int)  # 播放片段信号，参数为开始和结束时间(ms)
    video_subtitle_added = pyqtSignal(str)  # 添加字幕文件信号

    def play_video(self):
        """触发视频播放"""
        self.video_play.emit()

    def pause_video(self):
        """触发视频暂停"""
        self.video_pause.emit()

    def stop_video(self):
        """触发视频停止"""
        self.video_stop.emit()

    def set_video_source(self, url: QUrl):
        """设置视频源

        Args:
            url: 视频文件的URL
        """
        self.video_source_changed.emit(url)

    def play_video_segment(self, start_time: int, end_time: int):
        """播放指定时间段的视频

        Args:
            start_time: 开始时间(毫秒)
            end_time: 结束时间(毫秒)
        """
        self.video_segment_play.emit(start_time, end_time)

    def add_subtitle(self, subtitle_file: str):
        """添加字幕文件

        Args:
            subtitle_file: 字幕文件路径
        """
        self.video_subtitle_added.emit(subtitle_file)


signalBus = SignalBus()
