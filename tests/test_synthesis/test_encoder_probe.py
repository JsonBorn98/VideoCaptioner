"""编码器能力探测单测（mock）+ 真实 ffmpeg 集成。"""

import shutil

import pytest

from videocaptioner.core.synthesis import encoder_probe as ep

_SAMPLE_ENCODERS = """Encoders:
 V..... = Video
 A..... = Audio
 S..... = Subtitle
 .F.... = Frame-level multithreading
 .....D = Supports direct rendering method 1
 ------
 V....D libx264              libx264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10
 V....D h264_nvenc           NVIDIA NVENC H.264 encoder (codec h264)
 A....D aac                  AAC (Advanced Audio Coding)
 V....D libaom-av1           libaom AV1 (codec av1)
"""


def test_parse_encoders_extracts_names():
    names = ep._parse_encoders(_SAMPLE_ENCODERS)
    assert {"libx264", "h264_nvenc", "aac", "libaom-av1"} <= names
    # legend lines before the separator must not leak in
    assert "=" not in " ".join(names)


def test_available_encoders_marks_missing(monkeypatch):
    monkeypatch.setattr(ep, "get_ffmpeg_path", lambda *a, **k: "ffmpeg")
    monkeypatch.setattr(ep, "compiled_encoders", lambda f: frozenset({"libx264", "aac", "libaom-av1"}))

    avail = ep.available_encoders()
    assert avail["x264"].compiled and avail["x264"].available
    assert avail["aom_av1"].available
    assert not avail["svt_av1"].compiled and not avail["svt_av1"].available
    assert "未编译" in avail["svt_av1"].reason
    assert not avail["h264_nvenc"].available  # not compiled in this fake set


def test_hardware_functional_probe_failure(monkeypatch):
    monkeypatch.setattr(ep, "get_ffmpeg_path", lambda *a, **k: "ffmpeg")
    monkeypatch.setattr(ep, "compiled_encoders", lambda f: frozenset({"h264_nvenc"}))
    monkeypatch.setattr(ep, "functional_probe", lambda f, n: False)

    a = ep.available_encoders(probe_hardware=True)["h264_nvenc"]
    assert a.compiled and a.functional is False and not a.available
    assert "硬件" in a.reason


def test_hardware_functional_probe_success(monkeypatch):
    monkeypatch.setattr(ep, "get_ffmpeg_path", lambda *a, **k: "ffmpeg")
    monkeypatch.setattr(ep, "compiled_encoders", lambda f: frozenset({"h264_nvenc"}))
    monkeypatch.setattr(ep, "functional_probe", lambda f, n: True)

    a = ep.available_encoders(probe_hardware=True)["h264_nvenc"]
    assert a.compiled and a.functional is True and a.available


# ---- integration (real ffmpeg) ----

_HAS_FFMPEG = shutil.which("ffmpeg") is not None
_SKIP = pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not available")


@pytest.mark.integration
@_SKIP
def test_real_compiled_and_functional_probe():
    ep.clear_probe_cache()
    ff = shutil.which("ffmpeg")
    compiled = ep.compiled_encoders(ff)
    assert "libx264" in compiled and "aac" in compiled
    assert ep.functional_probe(ff, "libx264") is True
    assert ep.functional_probe(ff, "definitely_not_a_real_encoder") is False

    avail = ep.available_encoders()
    assert avail["x264"].available


@pytest.mark.integration
@_SKIP
def test_real_availability_report():
    ep.clear_probe_cache()
    report = ep.run_availability_test()
    assert "ffmpeg version" in report.version
    assert "x264" in report.encoders and report.encoders["x264"].available
