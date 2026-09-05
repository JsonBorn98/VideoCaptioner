"""翻译执行快照与修复方式选择（票 06）：只验证外部行为。

按 spec「Testing Decisions」：测试穿过修复执行 seam 与核心任务入口，
覆盖普通翻译、增强型翻译、非 LLM 翻译和独立任务缺少资产时的行为，
确认后处理不静默改变翻译方式；fake gateway 只观察请求形状与结果状态。
"""

from __future__ import annotations

import json

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.entities import SubtitleLayoutEnum
from videocaptioner.core.llm import LLMModelProfile, LLMResult, LLMTransport, ProviderDialect
from videocaptioner.core.postprocess import PostprocessConfig, PostprocessTask
from videocaptioner.core.postprocess.models import PostprocessLayoutMode
from videocaptioner.core.postprocess.repair import execute_viewing_repair
from videocaptioner.core.postprocess.report import QualityReport, build_qa_report
from videocaptioner.core.postprocess.runner import run_postprocess_task
from videocaptioner.core.postprocess.summary import build_postprocess_stage_summary
from videocaptioner.core.postprocess.translation import (
    TranslationExecutionSnapshot,
    snapshot_from_subtitle_config,
)


def _profile(profile_id: str, model: str = "test-model") -> LLMModelProfile:
    return LLMModelProfile(
        profile_id=profile_id,
        name=f"Profile {profile_id}",
        transport=LLMTransport.OPENAI_COMPATIBLE,
        dialect=ProviderDialect.GENERIC,
        base_url="https://translation.test/v1",
        api_key="secret",
        model=model,
        work_context_tokens=16_384,
    )


class _ScriptedGateway:
    """Fake gateway：按脚本回放响应，记录每次请求载荷与所用角色方案。"""

    def __init__(self, scripts: list):
        self.scripts = list(scripts)
        self.requests: list[dict] = []
        self.used_profiles: list[str] = []

    def complete(self, profile, request, *, cancelled=None):
        self.used_profiles.append(profile.profile_id)
        user = next(m.content for m in request.messages if m.role == "user")
        self.requests.append(
            {
                "system": next(m.content for m in request.messages if m.role == "system"),
                "user": user,
            }
        )
        script = self.scripts.pop(0) if self.scripts else None
        if isinstance(script, Exception):
            raise script
        return LLMResult(text="" if script is None else script)


def _data(*pairs: tuple[str, str]) -> ASRData:
    return ASRData(
        [ASRDataSeg(text, i * 4000, i * 4000 + 4000, tr) for i, (text, tr) in enumerate(pairs)]
    )


def _config(**overrides) -> PostprocessConfig:
    return PostprocessConfig(trim_trailing_punct=False, **overrides)


def _response(repairs: list[dict]) -> str:
    return json.dumps({"repairs": repairs}, ensure_ascii=False)


def _repairs_for(payload_text: str, chunk: int = 20, translated: str = "短短") -> str:
    """从请求 JSON 文本构造合法响应：把每段原文切成 ≤chunk 字的片段。"""
    payload = json.loads(payload_text)
    repairs: list[dict] = []
    for subject in payload["review_subjects" if "review_subjects" in payload else "repair_subjects"]:
        for segment in subject["segments"]:
            if "problem_ids" in segment:
                pid = segment["problem_ids"][0]
            else:
                pid = "review"
            if "proposals" in segment:
                # 复校响应：按 proposals 原样确认。
                for proposal in segment["proposals"]:
                    repairs.append(
                        {
                            "problem_id": pid,
                            "output_index": proposal["output_index"],
                            "translated": proposal["translated"],
                        }
                    )
                continue
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
    return _response(repairs)


def _review_response(payload_text: str) -> str:
    """从复校请求 JSON 文本构造原样确认的 reviews 响应。"""
    payload = json.loads(payload_text)
    reviews: list[dict] = []
    for subject in payload["review_subjects"]:
        for segment in subject["segments"]:
            for proposal in segment["proposals"]:
                reviews.append(
                    {
                        "problem_id": segment["problem_ids"][0],
                        "output_index": proposal["output_index"],
                        "translated": proposal["translated"],
                    }
                )
    return json.dumps({"reviews": reviews}, ensure_ascii=False)


def _enhanced_snapshot(**overrides) -> TranslationExecutionSnapshot:
    payload = dict(
        method="enhanced_llm",
        boundary_context_radius=3,
        main_profile=_profile("main-profile"),
        review_profile=_profile("review-profile"),
        main_prompt="主翻译提示",
        review_prompt="高级校对提示",
        source_language="en",
        target_language="zh-Hans",
    )
    payload.update(overrides)
    return TranslationExecutionSnapshot(**payload)


# ---- 验收 1：完整 workflow 冻结快照（方式 / 角色 / 提示 / 资产身份）----


def test_snapshot_freezes_method_roles_prompts_and_identity():
    """快照冻结方式、角色、提示配置；持久化身份与运行期角色一致。"""
    snapshot = _enhanced_snapshot()
    persisted = snapshot.to_persisted()
    assert persisted["method"] == "enhanced_llm"
    assert persisted["main_role"]["profile_id"] == "main-profile"
    assert persisted["review_role"]["model"] == "test-model"
    assert persisted["boundary_context_radius"] == 3
    # 提示词与 API key 绝不进入持久化载荷。
    dumped = json.dumps(persisted)
    assert "主翻译提示" not in dumped
    assert "secret" not in dumped
    assert "api_key" not in dumped


def test_persisted_snapshot_round_trips_identity_not_connections():
    """持久化重建只含身份与方式；运行期角色连接不凭身份恢复。"""
    snapshot = _enhanced_snapshot()
    rebuilt = TranslationExecutionSnapshot.from_persisted(
        json.loads(json.dumps(snapshot.to_persisted()))
    )
    assert rebuilt is not None
    assert rebuilt.method == "enhanced_llm"
    assert rebuilt.main_identity is not None and rebuilt.main_identity.profile_id == "main-profile"
    # 重建结果不含运行期角色连接：不凭身份发起请求。
    assert rebuilt.main_profile is None and rebuilt.review_profile is None
    # 无效载荷不猜测：坏 schema / 未知方式一律 None。
    assert TranslationExecutionSnapshot.from_persisted({"schema": "other"}) is None
    assert (
        TranslationExecutionSnapshot.from_persisted(
            {**json.loads(json.dumps(snapshot.to_persisted())), "method": "ghost"}
        )
        is None
    )


def test_snapshot_from_subtitle_config_freezes_task_config():
    """从 SubtitleConfig 冻结：方式 / 半径 / 提示 / 语言取任务配置。"""

    class _Target:
        value = "英语"

    class _Config:
        translation_mode = None
        translator_service = None
        main_llm_profile = _profile("cfg-main")
        review_llm_profile = _profile("cfg-review")
        main_translation_prompt = "cfg 主提示"
        review_translation_prompt = "cfg 校对提示"
        boundary_context_radius = 5
        source_language = "auto"
        target_language = _Target()

        def effective_translation_mode(self):
            return "enhanced_llm"

    snapshot = snapshot_from_subtitle_config(_Config())
    assert snapshot.method == "enhanced_llm"
    assert snapshot.boundary_context_radius == 5
    assert snapshot.main_profile is not None and snapshot.main_profile.profile_id == "cfg-main"
    assert snapshot.main_prompt == "cfg 主提示"
    assert snapshot.target_language == "英语"


# ---- 验收 2：增强型翻译的修复跟随主翻译 + 高级校对 ----


def test_enhanced_repair_uses_main_and_review_roles_consistently():
    """增强修复按两段流程执行：主翻译拆分 + 高级校对复校，角色一致。"""
    data = _data(("超长" * 30, "短"))

    class _FlowGateway(_ScriptedGateway):
        def complete(self, profile, request, *, cancelled=None):
            self.used_profiles.append(profile.profile_id)
            user = next(m.content for m in request.messages if m.role == "user")
            self.requests.append({"system_prompt": next(
                m.content for m in request.messages if m.role == "system"
            ), "user": user})
            if "Review the following proposed repair fragments" in user:
                return LLMResult(text=_review_response(
                    user.split("<input>", 1)[1].split("</input>", 1)[0]
                ))
            return LLMResult(text=_repairs_for(
                user.split("<input>", 1)[1].split("</input>", 1)[0]
            ))

    gateway = _FlowGateway([])
    repaired, report = execute_viewing_repair(
        data,
        _config(),
        QualityReport(),
        SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway,
        snapshot=_enhanced_snapshot(),
    )
    assert report.viewing_repair is not None
    # 角色与原任务一致：先主翻译（拆分）、再高级校对（复校）。
    assert "main-profile" in gateway.used_profiles
    assert "review-profile" in gateway.used_profiles
    assert gateway.used_profiles.index("review-profile") > gateway.used_profiles.index(
        "main-profile"
    )
    # 请求携带任务冻结的原翻译提示配置。
    assert any("主翻译提示" in req["user"] for req in gateway.requests if "Repair" in req["user"])
    assert any(
        "高级校对提示" in req["user"] for req in gateway.requests if "Review" in req["user"]
    )
    assert report.viewing_repair.flow_mode == "main_review"
    assert not report.viewing_problems
    assert len(repaired.segments) == 3


def test_enhanced_repair_without_review_role_reports_not_silent_substitute():
    """增强快照缺高级校对连接：仅报告原因，不静默换成别的角色。"""
    data = _data(("超长" * 30, "短"))
    snapshot = _enhanced_snapshot(review_profile=None)
    gateway = _ScriptedGateway([])
    repaired, report = execute_viewing_repair(
        data, _config(), QualityReport(), SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway, snapshot=snapshot,
    )
    assert report.viewing_repair is not None
    assert report.viewing_repair.flow_mode == "report_only"
    assert gateway.requests == []  # 未发起任何请求
    assert repaired.segments[0].text == "超长" * 30  # 原样返回


# ---- 验收 3：普通 LLM 翻译只复用主翻译，不自动增加角色 ----


def test_single_llm_repair_uses_main_only_without_review():
    """普通 LLM 翻译：修复只用主翻译，不自动增加高级校对。"""
    data = _data(("超长" * 30, "短"))
    snapshot = TranslationExecutionSnapshot(
        method="single_llm",
        main_profile=_profile("only-main"),
        main_prompt="主翻译提示",
    )
    gateway = _ScriptedGateway([])
    # 自适应脚本：主翻译请求返回合法拆分。
    class _MainGateway(_ScriptedGateway):
        def complete(self, profile, request, *, cancelled=None):
            self.used_profiles.append(profile.profile_id)
            user = next(m.content for m in request.messages if m.role == "user")
            self.requests.append({"user": user})
            return LLMResult(text=_repairs_for(
                user.split("<input>", 1)[1].split("</input>", 1)[0]
            ))

    gateway = _MainGateway([])
    repaired, report = execute_viewing_repair(
        data, _config(), QualityReport(), SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway, snapshot=snapshot,
    )
    assert report.viewing_repair is not None
    assert report.viewing_repair.flow_mode == "main"
    assert set(gateway.used_profiles) == {"only-main"}  # 不引入第二个角色
    assert report.viewing_repair.review_role == ""
    assert not report.viewing_problems


# ---- 验收 4：非 LLM 翻译不静默升级为 LLM ----


def test_non_llm_repair_never_issues_llm_requests():
    """非 LLM 快照：不发起任何模型请求，原样返回并仅报告。"""
    data = _data(("超长" * 30, "短"))
    snapshot = TranslationExecutionSnapshot(method="non_llm")
    gateway = _ScriptedGateway([])
    repaired, report = execute_viewing_repair(
        data, _config(), QualityReport(), SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway, snapshot=snapshot,
    )
    assert report.viewing_repair is not None
    assert report.viewing_repair.flow_mode == "report_only"
    assert gateway.requests == []
    assert report.viewing_repair.translation_method == "non_llm"
    assert any("非 LLM" in warning for warning in report.viewing_repair.warnings)
    assert repaired.segments[0].text == "超长" * 30


def test_missing_snapshot_with_explicit_profile_uses_plain_mode():
    """缺快照但有显式备用角色：按普通方式修复，不自动增加校对。"""
    data = _data(("超长" * 30, "短"))

    class _MainGateway(_ScriptedGateway):
        def complete(self, profile, request, *, cancelled=None):
            self.used_profiles.append(profile.profile_id)
            user = next(m.content for m in request.messages if m.role == "user")
            self.requests.append({"user": user})
            return LLMResult(text=_repairs_for(
                user.split("<input>", 1)[1].split("</input>", 1)[0]
            ))

    gateway = _MainGateway([])
    repaired, report = execute_viewing_repair(
        data, _config(), QualityReport(), SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        gateway=gateway, snapshot=None, profile=_profile("backup"),
    )
    assert report.viewing_repair.flow_mode == "main"
    assert set(gateway.used_profiles) == {"backup"}


# ---- 验收 5：独立任务读取验证过程资产，缺失时明确提示 + 显式备用 ----


def _write_srt(path, text="Hello there.", translated="你好。"):
    body = f"1\n00:00:00,000 --> 00:00:02,000\n{text}\n{translated}\n"
    path.write_text(body, encoding="utf-8")


def test_standalone_task_rebuilds_snapshot_from_workspace_asset(tmp_path):
    """完整 workflow 落盘的快照资产：独立任务按 manifest 验证后重建复用。"""
    source = tmp_path / "input.srt"
    _write_srt(source)
    first = run_postprocess_task(
        PostprocessTask(
            str(source),
            postprocessed_subtitle_path=str(tmp_path / "out1.srt"),
            workflow_base_name="demo",
            source_language="en",
            target_language="zh",
            layout_mode=PostprocessLayoutMode.ORIGINAL_ON_TOP,
            config_snapshot=_config(),
            translation_snapshot=_enhanced_snapshot(),
        )
    )
    discovery = first.task.asset_discovery
    assert discovery is not None
    # 快照作为过程资产落盘并列入 manifest。
    assert discovery.asset_path("translation_snapshot") is not None
    manifest = json.loads(discovery.manifest_path.read_text(encoding="utf-8"))
    assert manifest["assets"]["translation_snapshot"] == "translation-snapshot.json"

    second = run_postprocess_task(
        PostprocessTask(
            str(source),
            postprocessed_subtitle_path=str(tmp_path / "out2.srt"),
            workflow_base_name="demo",
            source_language="en",
            target_language="zh",
            layout_mode=PostprocessLayoutMode.ORIGINAL_ON_TOP,
            config_snapshot=_config(utility_llm_profile=_profile("utility")),
        )
    )
    # 独立任务从验证过的资产重建方式身份（无快照注入时）。
    assert second.task.translation_snapshot is not None
    assert second.task.translation_snapshot.method == "enhanced_llm"
    assert second.task.translation_method == "enhanced_llm"


def test_standalone_task_missing_assets_reports_and_accepts_explicit_backup(tmp_path):
    """独立任务无可用快照资产：不猜测配置仅报告；显式降级与备用资产可补充。"""
    source = tmp_path / "fresh.srt"
    _write_srt(source, "超长" * 30, "短")
    gateway = _ScriptedGateway([])
    result = run_postprocess_task(
        PostprocessTask(
            str(source),
            postprocessed_subtitle_path=str(tmp_path / "fresh-out.srt"),
            workflow_base_name="fresh",
            source_language="en",
            target_language="zh",
            layout_mode=PostprocessLayoutMode.ORIGINAL_ON_TOP,
            config_snapshot=_config(),
        ),
        gateway=gateway,
    )
    repair = result.report.viewing_repair
    assert repair is not None
    # 缺少可验证翻译资产：不猜测配置，模型修复按仅报告处理并明确提示。
    assert repair.flow_mode == "report_only"
    assert gateway.requests == []
    assert any("模型修复未执行" in warning for warning in repair.warnings)

    # 显式降级：独立任务显式绑定工具角色方案时按普通方式修复（不静默发起）。

    class _MainGateway(_ScriptedGateway):
        def complete(self, profile, request, *, cancelled=None):
            self.used_profiles.append(profile.profile_id)
            user = next(m.content for m in request.messages if m.role == "user")
            self.requests.append({"user": user})
            return LLMResult(text=_repairs_for(
                user.split("<input>", 1)[1].split("</input>", 1)[0]
            ))

    explicit = run_postprocess_task(
        PostprocessTask(
            str(source),
            postprocessed_subtitle_path=str(tmp_path / "fresh-out-explicit.srt"),
            workflow_base_name="fresh",
            source_language="en",
            target_language="zh",
            layout_mode=PostprocessLayoutMode.ORIGINAL_ON_TOP,
            config_snapshot=_config(utility_llm_profile=_profile("utility")),
        ),
        gateway=_MainGateway([]),
    )
    assert explicit.report.viewing_repair.flow_mode == "main"

    # 显式备用资产：把完整 workflow 落盘的快照文件补进独立任务。
    seeded = tmp_path / "seed"
    seeded.mkdir()
    first = run_postprocess_task(
        PostprocessTask(
            str(source),
            postprocessed_subtitle_path=str(seeded / "seed-out.srt"),
            workflow_base_name="seed",
            source_language="en",
            target_language="zh",
            layout_mode=PostprocessLayoutMode.ORIGINAL_ON_TOP,
            config_snapshot=_config(),
            translation_snapshot=_enhanced_snapshot(),
        )
    )
    seed_discovery = first.task.asset_discovery
    assert seed_discovery is not None
    seed_path = seed_discovery.asset_path("translation_snapshot")
    assert seed_path is not None
    backup = run_postprocess_task(
        PostprocessTask(
            str(source),
            postprocessed_subtitle_path=str(tmp_path / "fresh-out2.srt"),
            workflow_base_name="fresh",
            source_language="en",
            target_language="zh",
            layout_mode=PostprocessLayoutMode.ORIGINAL_ON_TOP,
            config_snapshot=_config(),
            explicit_assets={"translation_snapshot": str(seed_path)},
        )
    )
    backup_discovery = backup.task.asset_discovery
    assert backup_discovery is not None
    assert backup_discovery.asset_path("translation_snapshot") is not None
    assert "translation_snapshot" not in backup_discovery.missing


# ---- 验收 6：翻译方式与资产身份进入报告与任务状态 ----


def test_method_and_roles_enter_report_and_task_status(tmp_path):
    """修复方式选择与角色身份进入 QA 报告与阶段状态摘要。"""
    source = tmp_path / "report.srt"
    _write_srt(source, "超长" * 30, "短")

    class _FlowGateway(_ScriptedGateway):
        def complete(self, profile, request, *, cancelled=None):
            self.used_profiles.append(profile.profile_id)
            user = next(m.content for m in request.messages if m.role == "user")
            self.requests.append({"user": user})
            if "Review the following" in user:
                return LLMResult(text=_review_response(
                    user.split("<input>", 1)[1].split("</input>", 1)[0]
                ))
            return LLMResult(text=_repairs_for(
                user.split("<input>", 1)[1].split("</input>", 1)[0]
            ))

    gateway = _FlowGateway([])
    result = run_postprocess_task(
        PostprocessTask(
            str(source),
            postprocessed_subtitle_path=str(tmp_path / "report-out.srt"),
            workflow_base_name="report",
            source_language="en",
            target_language="zh",
            layout_mode=PostprocessLayoutMode.ORIGINAL_ON_TOP,
            config_snapshot=_config(utility_llm_profile=_profile("utility")),
        ),
        gateway=gateway,
    )
    repair = result.report.viewing_repair
    assert repair is not None
    assert repair.translation_method == ""  # 快照缺失：方式未知如实记录
    # QA 报告列出方式选择与角色身份。
    qa = build_qa_report(result.report)
    assert "修复方式" in qa
    # 阶段状态摘要包含修复方式（供 CLI / GUI 展示核对）。
    summary = build_postprocess_stage_summary(result)
    assert summary.status is not None and "修复" in summary.status
    # 显式备用角色路径：方式身份进报告。
    result2 = run_postprocess_task(
        PostprocessTask(
            str(source),
            postprocessed_subtitle_path=str(tmp_path / "report-out2.srt"),
            workflow_base_name="report2",
            source_language="en",
            target_language="zh",
            layout_mode=PostprocessLayoutMode.ORIGINAL_ON_TOP,
            config_snapshot=_config(),
            translation_snapshot=_enhanced_snapshot(),
        ),
        gateway=_FlowGateway([]),
    )
    qa2 = build_qa_report(result2.report)
    assert "enhanced_llm" in qa2
    assert "main-profile" in qa2
    assert "review-profile" in qa2
    summary2 = build_postprocess_stage_summary(result2)
    assert "enhanced_llm" in (summary2.status or "")
