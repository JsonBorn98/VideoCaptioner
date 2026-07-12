"""runner 取消：真实长编码中途取消应 kill ffmpeg 并抛 SynthesisCancelled。"""

import shutil
from threading import Event, Timer

import pytest

from videocaptioner.core.synthesis import (
    SynthesisCancelled,
    SynthesisControl,
    runner,
)

pytestmark = pytest.mark.integration
_SKIP = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")


@_SKIP
def test_run_encode_cancellation(tmp_path):
    out = str(tmp_path / "o.mp4")
    cmd = [
        shutil.which("ffmpeg"), "-y", "-f", "lavfi",
        "-i", "testsrc=size=640x480:rate=30:duration=60",
        "-c:v", "libx264", "-preset", "veryslow", out,
    ]
    ev = Event()
    procs = []
    ctrl = SynthesisControl(cancel_event=ev, on_process=procs.append)
    Timer(1.0, ev.set).start()  # cancel mid-encode

    with pytest.raises(SynthesisCancelled):
        runner.run_encode(cmd, control=ctrl)

    assert procs and procs[0].poll() is not None  # ffmpeg 已被 kill


@_SKIP
def test_run_encode_logs_lines(tmp_path):
    out = str(tmp_path / "o.mp4")
    cmd = [
        shutil.which("ffmpeg"), "-y", "-f", "lavfi",
        "-i", "testsrc=size=320x240:rate=10:duration=1",
        "-c:v", "libx264", "-preset", "ultrafast", out,
    ]
    lines = []
    ctrl = SynthesisControl(log_callback=lines.append)
    runner.run_encode(cmd, control=ctrl)
    assert any("frame=" in ln or "time=" in ln for ln in lines)  # 捕获到 ffmpeg 状态行
