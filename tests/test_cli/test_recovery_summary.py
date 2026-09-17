"""CLI 恢复摘要与 ``--fresh``（票 10）：命令函数把旗标转为传给两个模块入口的恢复决定。

无旗标：模块入口收到打印恢复摘要并返回「继续」的回调；``--fresh``：收到
恒返「从头开始」的回调（无检查点时模块入口不调回调，两条路径都不打印）。
process 把旗标同时传给翻译与后处理两个阶段。
"""

from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from videocaptioner.cli import exit_codes as EXIT
from videocaptioner.cli.commands import postprocess as postprocess_command
from videocaptioner.cli.commands import process as process_command
from videocaptioner.cli.commands import subtitle as subtitle_command
from videocaptioner.cli.config import build_config
from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.recovery import RecoveryDecision, RecoverySummary

from .test_translation_modes import _args as _subtitle_args


@pytest.fixture(autouse=True)
def _seed_llm_profile_store(tmp_path, monkeypatch):
    """Seed main/review model profiles for both module entry points.

    Same shape as ``test_postprocess_command._seed_llm_profile_store``: the
    balanced postprocess profile derives its utility model from the main
    profile, and the enhanced translation config binds both roles.
    """

    from videocaptioner.core.llm.models import (
        LLMModelProfile,
        LLMTransport,
        ProviderDialect,
    )
    from videocaptioner.core.llm.profiles import LLMModelProfileStore

    def _profile(profile_id: str, model: str) -> LLMModelProfile:
        return LLMModelProfile(
            profile_id=profile_id,
            name=f"{profile_id} Profile",
            transport=LLMTransport.OPENAI_COMPATIBLE,
            dialect=ProviderDialect.GENERIC,
            base_url=f"https://{profile_id}.test/v1",
            api_key=f"{profile_id}-secret",
            model=model,
            work_context_tokens=16_384,
        )

    store_path = tmp_path / "llm_model_profiles.json"
    store = LLMModelProfileStore(store_path)
    store.save(_profile("main-profile", "main-model"))
    store.save(_profile("review-profile", "review-model"))
    monkeypatch.setattr(
        "videocaptioner.core.llm.profiles.DEFAULT_LLM_PROFILES_PATH", store_path
    )
    return store_path


def _summary(**overrides) -> RecoverySummary:
    """翻译侧恢复摘要替身：与 core 真实构造处同形（票 10 只测外部行为）。"""

    values = {
        "module": "enhanced_translation",
        "identity": {"source_fingerprint": "abc", "source_language": "en", "target_language": "zh"},
        "completed": {
            "analysis": 1,
            "glossary": 1,
            "translation_segments": 12,
            "audit_batches": 3,
        },
        "checkpoint_time": "2026-09-17T02:00:00Z",
        "configuration_drift": ("主翻译提示词：检查点 A，当前 B",),
    }
    values.update(overrides)
    return RecoverySummary(**values)


def _postprocess_summary(**overrides) -> RecoverySummary:
    """后处理侧恢复摘要替身：phase / rounds 两级（对齐 core 侧权威标注）。"""

    values = {
        "module": "subtitle_postprocess",
        "identity": {"subtitle_fingerprint": "abc", "source_language": "en", "target_language": "zh"},
        "completed": {"phase": 1, "rounds": 2},
        "checkpoint_time": "2026-09-17T03:00:00Z",
        "configuration_drift": (),
    }
    values.update(overrides)
    return RecoverySummary(**values)


def _install_fake_enhanced(monkeypatch, tmp_path: Path, summary: RecoverySummary | None):
    """替换翻译模块入口：把收到的恢复决定回调交回测试手动调用。

    ``summary=None`` 模拟无匹配检查点（模块入口不调回调）。
    """

    captured = {}

    def fake_run(data, config, **kwargs):
        captured["kwargs"] = kwargs
        translated = ASRData.from_json(data.to_json())
        translated.segments[0].translated_text = "你好"
        glossary = tmp_path / "【项目术语表】source.vcglossary.json"
        audit = tmp_path / "【翻译审计】source.md"
        glossary.write_text("{}", encoding="utf-8")
        audit.write_text("# audit", encoding="utf-8")
        decision_callback = kwargs.get("recovery_decision")
        # 模块入口只在发现可复用检查点时调回调（票 10 测试决策：fake 同形）。
        if summary is not None and decision_callback is not None:
            decision_callback(summary)
        return SimpleNamespace(
            subtitle_data=translated,
            artifacts=SimpleNamespace(glossary_path=glossary, audit_report_path=audit),
            result=SimpleNamespace(audit_report=SimpleNamespace(issues=(), usages=())),
            recovery_summary=summary,
        )

    import videocaptioner.core.translate.enhanced as enhanced_package

    monkeypatch.setattr(enhanced_package, "run_enhanced_translation", fake_run)
    return captured


def _enhanced_config():
    return build_config(
        {
            "llm": {"profile_id": "main-profile", "review_profile_id": "review-profile"},
            "subtitle": {"optimize": False, "split": False, "translate": True},
            "translate": {"mode": "enhanced_llm"},
        }
    )


class TestSubtitleFreshFlag:
    def test_no_flag_prints_resume_summary_and_continues(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        source = tmp_path / "source.srt"
        destination = tmp_path / "initial.srt"
        captured = _install_fake_enhanced(
            monkeypatch, tmp_path, _summary(checkpoint_time="2026-09-17T02:00:00Z")
        )

        result = subtitle_command.run(_subtitle_args(source, destination, quiet=False), _enhanced_config())

        assert result == EXIT.SUCCESS
        decision = captured["kwargs"]["recovery_decision"]
        assert callable(decision)
        # 无旗标：默认继续，且标准输出打印恢复摘要（已完成级别与计数、时间、漂移）。
        err = capsys.readouterr().err
        assert "从检查点继续" in err
        assert "全文分析" in err and "术语阶段" in err
        assert "字幕段 12" in err and "审计批 3" in err
        assert "2026-09-17T02:00:00Z" in err
        assert "主翻译提示词：检查点 A，当前 B" in err

    def test_fresh_flag_returns_start_fresh(self, tmp_path: Path, monkeypatch) -> None:
        source = tmp_path / "source.srt"
        destination = tmp_path / "initial.srt"
        captured = _install_fake_enhanced(monkeypatch, tmp_path, _summary())

        result = subtitle_command.run(
            _subtitle_args(source, destination, fresh=True), _enhanced_config()
        )

        assert result == EXIT.SUCCESS
        decision = captured["kwargs"]["recovery_decision"]
        assert decision(_summary()) is RecoveryDecision.START_FRESH

    def test_no_checkpoint_prints_no_resume_summary(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        source = tmp_path / "source.srt"
        destination = tmp_path / "initial.srt"
        _install_fake_enhanced(monkeypatch, tmp_path, summary=None)

        result = subtitle_command.run(_subtitle_args(source, destination, quiet=False), _enhanced_config())

        assert result == EXIT.SUCCESS
        err = capsys.readouterr().err
        assert "从检查点继续" not in err
        assert "恢复摘要" not in err

    def test_resume_summary_prints_even_in_quiet_mode(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        # 无人值守脚本（spec 用户故事 37）常用 -q：恢复摘要是日志可查的关键行，
        # 不随 quiet 抑制（quiet 仍只出最终结果路径 + ERROR）。
        source = tmp_path / "source.srt"
        destination = tmp_path / "initial.srt"
        captured = _install_fake_enhanced(monkeypatch, tmp_path, _summary())

        result = subtitle_command.run(_subtitle_args(source, destination, quiet=True), _enhanced_config())

        assert result == EXIT.SUCCESS
        assert captured["kwargs"]["recovery_decision"] is not None
        err = capsys.readouterr().err
        assert "从检查点继续" in err
        assert "检查点时间" in err


class TestPostprocessFreshFlag:
    def _args(self, source: Path, **overrides) -> Namespace:
        values = {
            "input": str(source),
            "output": None,
            "layout": "source-only",
            "profile": "balanced",
            "speed_profile": None,
            "media": None,
            "speed_media": None,
            "quiet": False,
            "verbose": False,
        }
        values.update(overrides)
        return Namespace(**values)

    def _install_fake_postprocess(self, monkeypatch, tmp_path: Path, summary: RecoverySummary | None):
        """后处理模块入口替身：同翻译侧，只在有检查点时调恢复决定回调。"""

        import videocaptioner.core.postprocess as postprocess_package

        captured = {}

        def fake_run(task, **kwargs):
            captured["kwargs"] = kwargs
            decision_callback = kwargs.get("recovery_decision")
            if summary is not None and decision_callback is not None:
                decision_callback(summary)

            class Result:
                succeeded = True
                used_fallback = False
                warnings = ()
                recovery_summary = summary
                output_data = ASRData([ASRDataSeg("你好", 0, 1000)])
                report = SimpleNamespace(
                    stages={},
                    compress_failures=[],
                    placeholder_review=[],
                    audit=None,
                    viewing_repair=None,
                    unresolved_viewing_problems=lambda: (),
                    speed=None,
                )
                precise_timing_outcome = None
                precise_timing_grades = None
                layout = None
                confidence = 1.0
                continue_downstream = True

                @property
                def task(self_inner):
                    return task

            task.status = "completed"
            task.persisted_outputs = {}
            task.active_subtitle_path = str(tmp_path / "【后处理字幕】sample.srt")
            return Result()

        monkeypatch.setattr(postprocess_package, "run_postprocess_task", fake_run)
        return captured

    def test_no_flag_prints_resume_summary_and_continues(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        import videocaptioner.core.postprocess as postprocess_package

        source = tmp_path / "sample.srt"
        source.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        store = postprocess_package.PostprocessProfileStore(tmp_path / "profiles.json")
        monkeypatch.setattr(postprocess_package, "PostprocessProfileStore", lambda: store)
        captured = self._install_fake_postprocess(
            monkeypatch, tmp_path, _postprocess_summary()
        )

        result = postprocess_command.run(self._args(source), build_config({"llm": {"profile_id": "main-profile"}}))

        assert result == EXIT.SUCCESS
        err = capsys.readouterr().err
        assert "从检查点继续" in err
        assert "阶段末检查点" in err and "修复轮次 2" in err
        assert "2026-09-17T03:00:00Z" in err
        assert callable(captured["kwargs"]["recovery_decision"])

    def test_fresh_flag_returns_start_fresh(self, tmp_path: Path, monkeypatch) -> None:
        import videocaptioner.core.postprocess as postprocess_package

        source = tmp_path / "sample.srt"
        source.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        store = postprocess_package.PostprocessProfileStore(tmp_path / "profiles.json")
        monkeypatch.setattr(postprocess_package, "PostprocessProfileStore", lambda: store)
        captured = self._install_fake_postprocess(monkeypatch, tmp_path, _postprocess_summary())

        result = postprocess_command.run(
            self._args(source, fresh=True), build_config({"llm": {"profile_id": "main-profile"}})
        )

        assert result == EXIT.SUCCESS
        decision = captured["kwargs"]["recovery_decision"]
        assert decision(_postprocess_summary()) is RecoveryDecision.START_FRESH

    def test_no_checkpoint_prints_no_resume_summary(self, tmp_path: Path, monkeypatch, capsys) -> None:
        import videocaptioner.core.postprocess as postprocess_package

        source = tmp_path / "sample.srt"
        source.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        store = postprocess_package.PostprocessProfileStore(tmp_path / "profiles.json")
        monkeypatch.setattr(postprocess_package, "PostprocessProfileStore", lambda: store)
        self._install_fake_postprocess(monkeypatch, tmp_path, summary=None)

        result = postprocess_command.run(self._args(source), build_config({"llm": {"profile_id": "main-profile"}}))

        assert result == EXIT.SUCCESS
        err = capsys.readouterr().err
        assert "从检查点继续" not in err
        assert "恢复摘要" not in err


class TestProcessFreshFlag:
    def _media(self, tmp_path: Path) -> Path:
        media = tmp_path / "talk.mp3"
        media.write_bytes(b"fake")
        return media

    def _config(self):
        return build_config(
            {
                "llm": {"profile_id": "main-profile"},
                "subtitle": {"optimize": False, "split": False, "translate": True},
                "translate": {"mode": "non_llm", "service": "bing"},
            }
        )

    @staticmethod
    def _install_fakes(monkeypatch, decisions: dict):
        from videocaptioner.cli.commands import postprocess, transcribe

        def fake_transcribe(args, config):
            Path(args.output).write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8"
            )
            return EXIT.SUCCESS

        def fake_subtitle(args, config):
            decisions["subtitle_fresh"] = getattr(args, "fresh", None)
            Path(args.output).write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8"
            )
            return EXIT.SUCCESS

        def fake_postprocess(args, config):
            decisions["postprocess_fresh"] = getattr(args, "fresh", None)
            args.continue_downstream = True
            args.active_subtitle_path = args.input
            args.result_data = None
            Path(args.output).write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8"
            )
            return EXIT.SUCCESS

        monkeypatch.setattr(transcribe, "run", fake_transcribe)
        monkeypatch.setattr(subtitle_command, "run", fake_subtitle)
        monkeypatch.setattr(postprocess, "run", fake_postprocess)

    def test_fresh_flag_reaches_both_stages(self, tmp_path: Path, monkeypatch) -> None:
        decisions = {}
        self._install_fakes(monkeypatch, decisions)

        result = process_command.run(self._fresh_args(self._media(tmp_path)), self._config())

        assert result == EXIT.SUCCESS
        # --fresh 同时传给翻译与后处理两个阶段（fresh=True）；不传时默认继续。
        assert decisions["subtitle_fresh"] is True
        assert decisions["postprocess_fresh"] is True

    def test_default_without_flag_continues_both_stages(self, tmp_path: Path, monkeypatch) -> None:
        decisions = {}
        self._install_fakes(monkeypatch, decisions)

        args = self._fresh_args(self._media(tmp_path))
        args.fresh = False
        result = process_command.run(args, self._config())

        assert result == EXIT.SUCCESS
        assert decisions["subtitle_fresh"] is False
        assert decisions["postprocess_fresh"] is False

    def _fresh_args(self, media: Path) -> Namespace:
        return Namespace(
            input=str(media),
            output=str(media.parent),
            verbose=False,
            quiet=True,
            fresh=True,
            no_synthesize=True,
            dub=False,
            dub_only=False,
            no_postprocess=False,
            translator=None,
            translation_mode=None,
            target_language=None,
            config=None,
        )


class TestFreshHelpText:
    @pytest.mark.parametrize(
        "command",
        ["subtitle", "postprocess", "process"],
    )
    def test_help_documents_fresh(self, command: str, capsys) -> None:
        from videocaptioner.cli.main import main

        # --fresh 必须出现在三个命令的帮助文本里（含义：忽略检查点、从头开始）。
        # argparse 会在终端宽度处折行，断言前把换行折叠成空格。
        with pytest.raises(SystemExit):
            main([command, "--help"])
        out = " ".join(capsys.readouterr().out.split())
        assert "--fresh" in out
        assert "start from scratch" in out
        assert "resume" in out
