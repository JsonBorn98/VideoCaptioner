"""完整 workflow 后处理下游门控（票 09）：CLI process 按接缝字段决定是否继续。"""

from argparse import Namespace
from pathlib import Path

import pytest

from videocaptioner.cli import exit_codes as EXIT
from videocaptioner.cli.config import build_config
from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg


def _config():
    return build_config(
        {
            "subtitle": {"optimize": False, "split": False, "translate": True},
            "translate": {"mode": "non_llm", "service": "bing"},
        }
    )


def _args(tmp_path: Path, media: Path) -> Namespace:
    return Namespace(
        input=str(media),
        output=str(tmp_path),
        verbose=False,
        quiet=True,
        no_synthesize=True,
        dub=True,
        dub_only=False,
        no_postprocess=False,
        translator=None,
        translation_mode=None,
        target_language=None,
        config=None,
    )


def _patch_upstream(monkeypatch, tmp_path: Path, *, continue_downstream: bool, status: int):
    from videocaptioner.cli.commands import dub, postprocess, process, transcribe
    from videocaptioner.cli.commands import subtitle as subtitle_command

    captured: dict = {"dub": 0, "subtitle": None}

    def fake_transcribe(args, config):
        Path(args.output).write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8"
        )
        return EXIT.SUCCESS

    def fake_subtitle(args, config):
        Path(args.output).write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHello\n你好\n", encoding="utf-8"
        )
        args.result_data = ASRData([ASRDataSeg("Hello", 0, 1000, "你好")])
        return EXIT.SUCCESS

    def fake_postprocess(args, config):
        active = tmp_path / "【后处理字幕】talk.srt"
        active.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHello\n你好\n", encoding="utf-8"
        )
        args.continue_downstream = continue_downstream
        args.active_subtitle_path = str(active)
        args.result_data = ASRData([ASRDataSeg("Hello", 0, 1000, "你好")])
        return status

    def fake_dub(args, config):
        captured["dub"] += 1
        captured["subtitle"] = args.subtitle
        return EXIT.SUCCESS

    monkeypatch.setattr(transcribe, "run", fake_transcribe)
    monkeypatch.setattr(subtitle_command, "run", fake_subtitle)
    monkeypatch.setattr(postprocess, "run", fake_postprocess)
    monkeypatch.setattr(dub, "run", fake_dub)
    monkeypatch.setattr("videocaptioner.cli.validators.validate_dubbing", lambda *a, **k: True)
    monkeypatch.setattr("videocaptioner.core.llm.LLMGateway", lambda *a, **k: _Closer())
    return captured, process


class _Closer:
    def close(self) -> None:
        return None


@pytest.mark.parametrize(
    ("continue_downstream", "status", "expected", "dub_calls"),
    [
        (True, EXIT.SUCCESS, EXIT.SUCCESS, 1),  # 成功或局部未解决
        (False, EXIT.SUCCESS, EXIT.RUNTIME_ERROR, 0),  # 主动停止 / 接缝禁止下游
        (False, EXIT.RUNTIME_ERROR, EXIT.RUNTIME_ERROR, 0),  # 模块级失败
    ],
)
def test_process_gates_downstream_on_postprocess_continue_flag(
    tmp_path, monkeypatch, continue_downstream, status, expected, dub_calls
):
    media = tmp_path / "talk.mp3"
    media.write_bytes(b"fake")
    captured, process = _patch_upstream(
        monkeypatch, tmp_path, continue_downstream=continue_downstream, status=status
    )

    result = process.run(_args(tmp_path, media), _config())

    assert result == expected
    assert captured["dub"] == dub_calls
    if dub_calls:
        assert captured["subtitle"] == str(tmp_path / "【后处理字幕】talk.srt")
