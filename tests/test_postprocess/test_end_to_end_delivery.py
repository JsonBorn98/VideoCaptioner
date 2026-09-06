"""端到端回归（票 09）：穿过核心任务入口覆盖交付矩阵。

切面是 ``run_postprocess_task``：临时输入 + fake gateway / 资产，
验证完整 workflow 语义、独立调用、显示策略、重试回退、过程资产与旧配置残留。
不锁定内部函数顺序或私有数据结构。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.entities import SubtitleLayoutEnum
from videocaptioner.core.llm import (
    LLMModelProfile,
    LLMResult,
    LLMTransport,
    ProviderDialect,
)
from videocaptioner.core.postprocess.config import PostprocessConfig
from videocaptioner.core.postprocess.models import PostprocessLayoutMode, PostprocessTask
from videocaptioner.core.postprocess.profiles import (
    PROFILE_SCHEMA,
    PROFILE_SCHEMA_VERSION,
    PostprocessProfileStore,
)
from videocaptioner.core.postprocess.runner import run_postprocess_task
from videocaptioner.core.postprocess.workspace import (
    ProcessAssetExportError,
    export_process_assets,
)


def _profile() -> LLMModelProfile:
    return LLMModelProfile(
        profile_id="repair-profile",
        name="Repair Profile",
        transport=LLMTransport.OPENAI_COMPATIBLE,
        dialect=ProviderDialect.GENERIC,
        base_url="https://e2e.test/v1",
        api_key="secret",
        model="repair-model",
        work_context_tokens=16_384,
    )


class _ScriptedGateway:
    def __init__(self, scripts: list):
        self.scripts = list(scripts)
        self.requests: list[dict] = []

    def complete(self, profile, request, *, cancelled=None):
        self.requests.append(json.loads(_payload_text(request)))
        script = self.scripts.pop(0) if self.scripts else None
        if isinstance(script, Exception):
            raise script
        return LLMResult(text="" if script is None else script)


def _payload_text(request) -> str:
    user = next(message.content for message in request.messages if message.role == "user")
    return user.split("<input>", 1)[1].split("</input>", 1)[0]


def _config(**overrides) -> PostprocessConfig:
    values = {
        "trim_trailing_punct": False,
        "speed_optimize": False,
        "speed_semantic_repair": False,
        "compress_fast_subtitles": False,
    }
    values.update(overrides)
    return PostprocessConfig(**values)


def _write_srt(path: Path, text: str = "你好。", translated: str = "") -> None:
    body = (
        f"1\n00:00:00,000 --> 00:00:04,000\n{text}\n{translated}\n"
        if translated
        else f"1\n00:00:00,000 --> 00:00:04,000\n{text}\n"
    )
    path.write_text(body, encoding="utf-8")


def _overlong(*pairs: tuple[str, str]) -> ASRData:
    cues = pairs or (("超长" * 30, "短"),)
    return ASRData(
        [ASRDataSeg(text, index * 4000, index * 4000 + 4000, translated)
         for index, (text, translated) in enumerate(cues)]
    )


def _response(repairs: list[dict]) -> str:
    return json.dumps({"repairs": repairs}, ensure_ascii=False)


def _split_repairs(payload: dict, chunk: int = 20, translated: str = "短短") -> list[dict]:
    repairs: list[dict] = []
    for subject in payload["repair_subjects"]:
        for segment in subject["segments"]:
            pid = segment["problem_ids"][0]
            compact = "".join(segment["text"].split())
            pieces = [compact[i : i + chunk] for i in range(0, len(compact), chunk)]
            for output_index, piece in enumerate(pieces):
                repairs.append(
                    {
                        "problem_id": pid,
                        "output_index": output_index,
                        "original": piece,
                        "translated": translated,
                    }
                )
    return repairs


class _RepairGateway(_ScriptedGateway):
    """按请求载荷就地构造合法拆分响应的修复网关（自动修复场景共用）。"""

    def complete(self, profile, request, *, cancelled=None):
        payload = json.loads(_payload_text(request))
        self.requests.append(payload)
        return LLMResult(text=_response(_split_repairs(payload)))


def _task(
    tmp_path: Path,
    *,
    name: str = "demo",
    data: ASRData | None = None,
    layout_mode=PostprocessLayoutMode.ORIGINAL_ON_TOP,
    config: PostprocessConfig | None = None,
    **kwargs,
) -> PostprocessTask:
    source = tmp_path / f"{name}.srt"
    if not source.exists():
        _write_srt(source)
    return PostprocessTask(
        str(source),
        postprocessed_subtitle_path=str(tmp_path / f"{name}-out.srt"),
        input_data=data,
        layout_mode=layout_mode,
        workflow_base_name=name,
        source_language="zh",
        target_language="en",
        config_snapshot=config or _config(),
        **kwargs,
    )


def _assert_export(task: PostprocessTask, tmp_path: Path, *, allowed: bool) -> None:
    destination = tmp_path / f"export-{task.task_id[:8]}"
    if allowed:
        copied = export_process_assets(task, destination)
        assert copied
        assert (destination / "manifest.json").is_file()
        return
    with pytest.raises(ProcessAssetExportError, match="未成功完成"):
        export_process_assets(task, destination)


# ---- 完整 workflow 语义：成功 / 局部未解决继续 / 模块失败回退 / 主动停止 ----


def test_successful_task_continues_downstream_and_allows_export(tmp_path):
    result = run_postprocess_task(_task(tmp_path, data=_overlong(("你好", "Hello"))))

    assert result.succeeded
    assert result.task.status == "completed"
    assert result.continue_downstream is True
    assert result.task.active_subtitle_path == result.task.postprocessed_subtitle_path
    _assert_export(result.task, tmp_path, allowed=True)


def test_partial_unresolved_continues_downstream_and_allows_export(tmp_path):
    over = _response(
        [{"problem_id": "length:original:0", "output_index": 0,
          "original": "超长" * 30, "translated": "短"}]
    )
    gateway = _ScriptedGateway([over] * 6)
    result = run_postprocess_task(
        _task(
            tmp_path,
            data=_overlong(),
            config=_config(utility_llm_profile=_profile(), qa_report=True),
        ),
        gateway=gateway,
    )

    assert result.succeeded
    assert result.continue_downstream is True
    assert result.task.status == "completed"
    assert result.report.unresolved_viewing_problems()
    assert result.report.viewing_repair is not None
    assert result.report.viewing_repair.requests == 5
    assert result.report.viewing_repair.rollbacks
    assert any("回退" in warning for warning in result.warnings)
    _assert_export(result.task, tmp_path, allowed=True)


def test_module_failure_rolls_back_and_blocks_export(tmp_path):
    source = tmp_path / "demo.srt"
    _write_srt(source)
    original = source.read_bytes()
    result = run_postprocess_task(
        PostprocessTask(
            str(source),
            postprocessed_subtitle_path=str(source),
            workflow_base_name="demo",
            config_snapshot=_config(qa_report=True),
        )
    )

    assert not result.succeeded
    assert result.used_fallback
    assert result.task.status == "fallback"
    assert result.continue_downstream is False
    assert result.task.active_subtitle_path == str(source)
    assert source.read_bytes() == original
    _assert_export(result.task, tmp_path, allowed=False)


def test_cancelled_and_invalid_initial_block_downstream_and_export(tmp_path):
    source = tmp_path / "demo.srt"
    _write_srt(source)
    cancelled = run_postprocess_task(
        _task(tmp_path, name="demo"),
        cancelled=lambda: True,
    )
    assert cancelled.task.status == "cancelled"
    assert cancelled.continue_downstream is False
    _assert_export(cancelled.task, tmp_path, allowed=False)

    empty = tmp_path / "empty.srt"
    empty.write_text("", encoding="utf-8")
    invalid = run_postprocess_task(
        PostprocessTask(str(empty), config_snapshot=_config())
    )
    assert invalid.task.status == "invalid_initial"
    assert invalid.continue_downstream is False
    _assert_export(invalid.task, tmp_path, allowed=False)


# ---- 独立后处理：资产发现 / 备用输入 / 仅报告 / 自动修复 ----


def test_standalone_discovers_workspace_and_accepts_explicit_backup(tmp_path):
    glossary = tmp_path / "seed-glossary.json"
    glossary.write_text(
        '{"schema": "videocaptioner.project_glossary", "terms": []}\n', encoding="utf-8"
    )
    first = run_postprocess_task(
        _task(tmp_path, name="seed", explicit_assets={"glossary": str(glossary)})
    )
    discovery = first.task.asset_discovery
    assert discovery is not None
    assert discovery.workspace_root.name == "videocaptioner-workspace"
    assert discovery.asset_path("glossary") is not None

    reused = run_postprocess_task(_task(tmp_path, name="seed"))
    assert reused.task.asset_discovery.asset_path("glossary") is not None

    audit = tmp_path / "manual-audit.md"
    audit.write_text("# 翻译审计\n无问题。\n", encoding="utf-8")
    backup = run_postprocess_task(
        _task(tmp_path, name="fresh", explicit_assets={"audit": str(audit)})
    )
    assert backup.task.asset_discovery.asset_path("audit") is not None
    assert "glossary" in backup.task.asset_discovery.missing


def test_standalone_report_only_and_analyze_do_not_call_gateway(tmp_path):
    gateway = _ScriptedGateway([])
    report_only = run_postprocess_task(
        _task(tmp_path, data=_overlong()),
        gateway=gateway,
    )
    assert report_only.succeeded
    assert report_only.continue_downstream is True
    repair = report_only.report.viewing_repair
    assert repair is not None
    assert repair.flow_mode == "report_only"
    assert gateway.requests == []
    assert report_only.report.unresolved_viewing_problems()

    analyze = run_postprocess_task(
        _task(
            tmp_path,
            name="analyze",
            data=_overlong(),
            config=_config(speed_mode="analyze", qa_report=True, speed_optimize=True),
        )
    )
    assert analyze.succeeded
    assert analyze.task.postprocessed_subtitle_path is None
    assert analyze.task.active_subtitle_path == analyze.task.initial_subtitle_path
    assert not (tmp_path / "analyze-out.srt").exists()
    _assert_export(analyze.task, tmp_path, allowed=True)


def test_standalone_auto_repair_uses_explicit_profile(tmp_path):
    gateway = _RepairGateway([])
    result = run_postprocess_task(
        _task(
            tmp_path,
            data=_overlong(),
            config=_config(utility_llm_profile=_profile()),
        ),
        gateway=gateway,
    )
    assert result.succeeded
    assert result.continue_downstream is True
    assert result.report.viewing_repair is not None
    assert result.report.viewing_repair.flow_mode == "main"
    assert gateway.requests
    assert len(result.output_data.segments) == 3
    assert "".join(segment.text for segment in result.output_data.segments) == "超长" * 30
    assert not result.report.unresolved_viewing_problems()


# ---- 单语 / 双语 / 两侧独立显示 / 自动换行 / 一对多 ----


def test_monolingual_bilingual_independent_modes_and_one_to_many(tmp_path):
    mono = run_postprocess_task(
        _task(
            tmp_path,
            name="mono",
            data=ASRData([ASRDataSeg("超长" * 30, 0, 4000)]),
            layout_mode=PostprocessLayoutMode.ORIGINAL_ONLY,
        )
    )
    assert mono.layout is SubtitleLayoutEnum.ONLY_ORIGINAL
    assert all(problem.side == "original" for problem in mono.report.viewing_problems)

    bilingual = run_postprocess_task(
        _task(tmp_path, name="bilingual", data=_overlong(("超长" * 30, "超长" * 30)))
    )
    sides = {problem.side for problem in bilingual.report.viewing_problems}
    assert sides == {"original", "translated"}

    independent = run_postprocess_task(
        _task(
            tmp_path,
            name="independent",
            data=_overlong(("超长" * 30, "超长" * 30)),
            config=_config(
                original_display_mode="auto_wrap",
                translated_display_mode="single_line",
            ),
        )
    )
    assert independent.report.viewing_problems
    assert all(problem.side == "translated" for problem in independent.report.viewing_problems)

    wrapped = run_postprocess_task(
        _task(
            tmp_path,
            name="wrap",
            data=_overlong(("超长" * 30, "超长" * 30)),
            config=_config(
                original_display_mode="auto_wrap",
                translated_display_mode="auto_wrap",
            ),
        )
    )
    assert wrapped.report.viewing_problems == []

    split = run_postprocess_task(
        _task(
            tmp_path,
            name="split",
            data=_overlong(),
            config=_config(utility_llm_profile=_profile()),
        ),
        gateway=_RepairGateway([]),
    )
    assert len(split.output_data.segments) == 3
    assert "".join(segment.text for segment in split.output_data.segments) == "超长" * 30


# ---- 首次提交 / 4 次业务重试 / 网络重试 / 重复候选 / 区域回退 ----


def test_retry_budget_transport_duplicate_and_region_rollback(tmp_path):
    over = _response(
        [{"problem_id": "length:original:0", "output_index": 0,
          "original": "超长" * 30, "translated": "短"}]
    )
    exhausted = run_postprocess_task(
        _task(
            tmp_path,
            name="budget",
            data=_overlong(),
            config=_config(utility_llm_profile=_profile()),
        ),
        gateway=_ScriptedGateway([over] * 6),
    )
    summary = exhausted.report.viewing_repair
    assert summary is not None
    assert summary.requests == 5
    assert summary.rollbacks
    assert summary.rollbacks[0].reason == "业务修复重试耗尽"
    assert exhausted.continue_downstream is True

    transport = run_postprocess_task(
        _task(
            tmp_path,
            name="net",
            data=_overlong(),
            config=_config(utility_llm_profile=_profile()),
        ),
        gateway=_ScriptedGateway(
            [RuntimeError("flaky"), over, over, over, over, over]
        ),
    )
    assert transport.report.viewing_repair is not None
    assert transport.report.viewing_repair.rollbacks
    # 1 次传输 + 1 正常提交 + 4 次业务重试；传输不占用业务预算。
    assert transport.report.viewing_repair.requests == 6

    consecutive = run_postprocess_task(
        _task(
            tmp_path,
            name="net2",
            data=_overlong(),
            config=_config(utility_llm_profile=_profile()),
        ),
        gateway=_ScriptedGateway([RuntimeError("down")] * 2),
    )
    assert consecutive.report.viewing_repair is not None
    assert consecutive.report.viewing_repair.requests == 2
    assert consecutive.report.viewing_repair.rollbacks == []
    assert any("传输失败" in warning for warning in consecutive.report.viewing_repair.warnings)
    assert consecutive.continue_downstream is True

    mixed = run_postprocess_task(
        _task(
            tmp_path,
            name="region",
            data=_overlong(("超长" * 30, "短"), ("第二段超长" * 4, "第二短")),
            config=_config(utility_llm_profile=_profile()),
        ),
        gateway=_SelectiveGateway(),
    )
    texts = [segment.text for segment in mixed.output_data.segments]
    assert "第二段超长" * 4 in texts
    assert any("超长" in text and len(text) <= 20 for text in texts)
    assert mixed.continue_downstream is True
    assert mixed.report.unresolved_viewing_problems()


class _SelectiveGateway(_ScriptedGateway):
    def __init__(self):
        super().__init__([])

    def complete(self, profile, request, *, cancelled=None):
        payload = json.loads(_payload_text(request))
        self.requests.append(payload)
        repairs: list[dict] = []
        for subject in payload["repair_subjects"]:
            for segment in subject["segments"]:
                text = segment["text"]
                pid = segment["problem_ids"][0]
                if "第二段" in text:
                    repairs.append(
                        {
                            "problem_id": pid,
                            "output_index": 0,
                            "original": text,
                            "translated": segment["translated"],
                        }
                    )
                    continue
                repairs.extend(
                    [
                        {"problem_id": pid, "output_index": 0,
                         "original": text[:30], "translated": "短"},
                        {"problem_id": pid, "output_index": 1,
                         "original": text[30:], "translated": "短"},
                    ]
                )
        return LLMResult(text=_response(repairs))


# ---- 过程目录隔离 / manifest / 成功导出 / 失败不导出 / 旧配置残留 ----


def test_workspace_isolation_manifest_and_legacy_residue(tmp_path):
    glossary = tmp_path / "seed-glossary.json"
    glossary.write_text(
        '{"schema": "videocaptioner.project_glossary", "terms": []}\n', encoding="utf-8"
    )
    result = run_postprocess_task(
        _task(
            tmp_path,
            name="iso",
            explicit_assets={"glossary": str(glossary)},
            config=_config(qa_report=True),
        )
    )
    discovery = result.task.asset_discovery
    assert discovery is not None
    workspace = discovery.workspace_root
    stray = [
        path
        for path in tmp_path.rglob("*")
        if path.is_file()
        and not path.is_relative_to(workspace)
        and path.suffix in {".md", ".json"}
        and path != glossary
        and "export-" not in path.name
    ]
    assert stray == []
    manifest = json.loads(discovery.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema"] == "videocaptioner.workspace_manifest"
    assert "api_key" not in json.dumps(manifest)
    _assert_export(result.task, tmp_path, allowed=True)

    archive = tmp_path / "profiles.json"
    archive.write_text(
        json.dumps(
            {
                "schema": PROFILE_SCHEMA,
                "version": PROFILE_SCHEMA_VERSION,
                "profiles": [
                    {
                        "id": "cinema",
                        "name": "Cinema",
                        "base_template_id": "balanced",
                        "is_template": False,
                        "config": {
                            "optimize_both_sides": True,
                            "max_word_count_cjk": 8,
                            "max_word_count_english": 12,
                            "speed_optimize": False,
                            "speed_semantic_repair": False,
                            "trim_trailing_punct": False,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    leftover = PostprocessProfileStore(archive).get("cinema").config
    assert leftover.single_line_absolute_cjk == 20
    assert leftover.original_display_mode == "single_line"
    assert not hasattr(leftover, "optimize_both_sides")
    assert not hasattr(leftover, "max_word_count_cjk")
    seeded = run_postprocess_task(
        _task(tmp_path, name="legacy", data=_overlong(("你好", "Hello")), config=leftover)
    )
    assert seeded.succeeded
    assert seeded.task.config_snapshot.single_line_absolute_cjk == 20
