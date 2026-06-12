# -*- coding: utf-8 -*-
"""媒体信息线程：ffprobe 读取媒体信息并生成缩略图。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from PyQt5.QtCore import pyqtSignal

from videocaptioner.core.entities import VideoInfo
from videocaptioner.core.utils.logger import setup_logger
from videocaptioner.core.utils.video_utils import get_video_info
from videocaptioner.ui.thread.worker import WorkerThread

logger = setup_logger("video_info_thread")


class VideoInfoThread(WorkerThread):
    finished = pyqtSignal(VideoInfo)

    def __init__(self, file_path: str):
        super().__init__()
        self.file_path = file_path

    def _work(self):
        thumbnail_path = (
            Path(tempfile.gettempdir()) / f"{Path(self.file_path).stem}_thumbnail.jpg"
        )
        video_info = get_video_info(self.file_path, thumbnail_path=str(thumbnail_path))
        self.checkpoint()
        if video_info is None:
            raise ValueError("无法获取媒体文件信息，请确保文件格式正确")
        self.finished.emit(video_info)
