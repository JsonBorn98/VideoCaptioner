"""翻译执行快照与修复方式选择（票 06，D07/D15/D17/D18）。

设计记录（见 docs/dev/subtitle-postprocessing-design-record.md）：
- 完整 workflow 在任务开始时冻结翻译执行快照；后处理只读复用其中的
  翻译方式、角色、提示词和可用上下文资产（D15）。
- 修复方式从冻结快照中选择：增强型翻译跟随主翻译加高级校对；普通
  LLM 翻译只复用普通翻译方式，不自动增加高级校对或其他模型角色；
  非 LLM 翻译不得静默升级为 LLM（D07/D15）。
- 独立任务优先发现并验证过程目录资产；缺少可验证翻译资产时不猜测
  配置、不强制使用工具模型，只执行确定性处理、仅报告或明确报告
  无法自动重译（D15/D17）。
- ``boundary_context_radius`` 直接取快照中的上游设置，不为后处理
  新增第二套上下文范围设置（D18）。

运行期对象（模型角色连接、提示词）只存在于内存快照；持久化形式
（过程目录 ``translation-snapshot.json``）只记录方式、角色身份、
语言与半径，绝不包含 API key、完整 prompt、模型响应或推理内容。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

from ..utils.logger import setup_logger
from .planning import DEFAULT_BOUNDARY_CONTEXT_RADIUS

if TYPE_CHECKING:
    from ..llm.models import LLMModelProfile

logger = setup_logger("postprocess.translation")

TRANSLATION_SNAPSHOT_SCHEMA = "videocaptioner.translation_snapshot"
TRANSLATION_SNAPSHOT_VERSION = 1

# 翻译方式取值（与 entities.SubtitleConfig.effective_translation_mode 一致）。
METHOD_SINGLE_LLM = "single_llm"
METHOD_ENHANCED_LLM = "enhanced_llm"
METHOD_NON_LLM = "non_llm"


@dataclass(frozen=True)
class TranslationRoleIdentity:
    """一个翻译模型角色的持久化身份（不含任何连接机密）。"""

    role: str
    profile_id: str = ""
    name: str = ""
    model: str = ""

    def label(self) -> str:
        """角色身份的可读标签（报告 / 任务状态消费，无连接机密）。"""
        return " / ".join(part for part in (self.profile_id, self.model) if part)


def role_label(
    profile: Optional["LLMModelProfile"],
    identity: Optional[TranslationRoleIdentity],
) -> str:
    """角色标签的唯一实现：运行期对象优先，其次持久化身份，无则空。"""
    if profile is not None:
        return f"{profile.profile_id} / {profile.model}"
    if identity is not None:
        return identity.label()
    return ""


def _identity_from_profile(
    role: str, profile: Optional["LLMModelProfile"]
) -> Optional[TranslationRoleIdentity]:
    if profile is None:
        return None
    return TranslationRoleIdentity(
        role=role,
        profile_id=getattr(profile, "profile_id", "") or "",
        name=getattr(profile, "name", "") or "",
        model=getattr(profile, "model", "") or "",
    )


@dataclass(frozen=True)
class TranslationExecutionSnapshot:
    """任务开始时冻结的翻译执行快照（D15）。

    ``main_profile`` / ``review_profile`` / 提示词是运行期注入对象，
    只随完整 workflow 在内存中传递，绝不进入持久化载荷；
    ``*_identity`` 是可持久化的角色身份（无连接机密）。
    """

    method: str = ""
    """翻译方式：single_llm / enhanced_llm / non_llm；空表示未知。"""
    boundary_context_radius: int = DEFAULT_BOUNDARY_CONTEXT_RADIUS
    """上游边界上下文半径（D18）：修复主体上下扩展的相邻段数。"""
    main_profile: Optional["LLMModelProfile"] = None
    review_profile: Optional["LLMModelProfile"] = None
    main_prompt: str = ""
    review_prompt: str = ""
    main_identity: Optional[TranslationRoleIdentity] = None
    review_identity: Optional[TranslationRoleIdentity] = None
    source_language: str = ""
    target_language: str = ""

    def to_persisted(self) -> Dict[str, Any]:
        """可持久化载荷：方式、半径、语言与角色身份，无连接机密与提示词。"""
        main_identity = self.main_identity or _identity_from_profile("main", self.main_profile)
        review_identity = self.review_identity or _identity_from_profile(
            "review", self.review_profile
        )
        return {
            "schema": TRANSLATION_SNAPSHOT_SCHEMA,
            "version": TRANSLATION_SNAPSHOT_VERSION,
            "method": self.method,
            "boundary_context_radius": self.boundary_context_radius,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "main_role": (
                {
                    "role": "main",
                    "profile_id": main_identity.profile_id,
                    "name": main_identity.name,
                    "model": main_identity.model,
                }
                if main_identity is not None
                else None
            ),
            "review_role": (
                {
                    "role": "review",
                    "profile_id": review_identity.profile_id,
                    "name": review_identity.name,
                    "model": review_identity.model,
                }
                if review_identity is not None
                else None
            ),
        }

    @classmethod
    def from_persisted(cls, payload: Any) -> Optional["TranslationExecutionSnapshot"]:
        """从持久化载荷重建身份快照；格式无效时返回 None（不猜测）。

        重建结果只含身份与方式，不含运行期角色连接——独立任务据此
        核对上游翻译方式，但不会凭身份发起请求（D15 不猜测配置）。
        """
        if not isinstance(payload, dict):
            return None
        if payload.get("schema") != TRANSLATION_SNAPSHOT_SCHEMA:
            return None
        method = payload.get("method")
        radius = payload.get("boundary_context_radius")
        if not isinstance(method, str) or method not in (
            METHOD_SINGLE_LLM,
            METHOD_ENHANCED_LLM,
            METHOD_NON_LLM,
        ):
            return None
        if type(radius) is not int or radius < 0:
            return None

        def _identity(role: str, raw: Any) -> Optional[TranslationRoleIdentity]:
            if not isinstance(raw, dict):
                return None
            return TranslationRoleIdentity(
                role=role,
                profile_id=str(raw.get("profile_id", "") or ""),
                name=str(raw.get("name", "") or ""),
                model=str(raw.get("model", "") or ""),
            )

        source_language = payload.get("source_language")
        target_language = payload.get("target_language")
        return cls(
            method=method,
            boundary_context_radius=radius,
            main_identity=_identity("main", payload.get("main_role")),
            review_identity=_identity("review", payload.get("review_role")),
            source_language=source_language if isinstance(source_language, str) else "",
            target_language=target_language if isinstance(target_language, str) else "",
        )


def load_translation_snapshot_file(path: Path) -> Optional[TranslationExecutionSnapshot]:
    """读取并验证一个翻译执行快照文件；不可读或格式无效时返回 None。"""
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return None
        text = path.read_text(encoding="utf-8")
        payload = json.loads(text)
    except InterruptedError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        logger.warning("翻译执行快照不可读，已忽略 %s: %s", path.name, exc)
        return None
    snapshot = TranslationExecutionSnapshot.from_persisted(payload)
    if snapshot is None:
        logger.warning("翻译执行快照格式无效，已忽略: %s", path.name)
    return snapshot


def snapshot_from_subtitle_config(config) -> TranslationExecutionSnapshot:
    """从上游 SubtitleConfig 冻结翻译执行快照（完整 workflow 调用方使用）。"""
    method = config.effective_translation_mode()
    main_profile = getattr(config, "main_llm_profile", None)
    review_profile = getattr(config, "review_llm_profile", None)
    target_language = getattr(config, "target_language", None)
    return TranslationExecutionSnapshot(
        method=method,
        boundary_context_radius=int(
            getattr(config, "boundary_context_radius", DEFAULT_BOUNDARY_CONTEXT_RADIUS)
        ),
        main_profile=main_profile,
        review_profile=review_profile,
        main_prompt=getattr(config, "main_translation_prompt", "") or "",
        review_prompt=getattr(config, "review_translation_prompt", "") or "",
        main_identity=_identity_from_profile("main", main_profile),
        review_identity=_identity_from_profile("review", review_profile),
        source_language=str(getattr(config, "source_language", "") or ""),
        target_language=str(getattr(target_language, "value", target_language) or ""),
    )


@dataclass(frozen=True)
class _CliSnapshotConfig:
    """CLI 字幕命令解析结果的快照源（喂给 snapshot_from_subtitle_config）。

    与 ``SubtitleConfig`` 同一属性契约：``effective_translation_mode``
    是方法（不是 property），``target_language`` 是裸值（工厂里再取
    ``.value``，str 无 value 属性会原样返回）。
    """

    main_llm_profile: Optional["LLMModelProfile"] = None
    review_llm_profile: Optional["LLMModelProfile"] = None
    main_translation_prompt: str = ""
    review_translation_prompt: str = ""
    source_language: str = "auto"
    target_language: str = ""
    _method: str = METHOD_NON_LLM
    _radius: int = DEFAULT_BOUNDARY_CONTEXT_RADIUS

    def effective_translation_mode(self) -> str:
        return self._method

    @property
    def boundary_context_radius(self) -> int:
        return self._radius


def cli_translation_snapshot(
    *,
    translation_mode: str,
    need_translate: bool,
    main_profile: Optional["LLMModelProfile"],
    review_profile: Optional["LLMModelProfile"],
    main_prompt: str,
    review_prompt: str,
    source_language: str,
    target_language: str,
    boundary_context_radius: int,
) -> TranslationExecutionSnapshot:
    """CLI 路径的快照冻结：复用 SubtitleConfig 的同一冻结形状。"""
    return snapshot_from_subtitle_config(
        _CliSnapshotConfig(
            main_llm_profile=main_profile if need_translate else None,
            review_llm_profile=review_profile if need_translate else None,
            main_translation_prompt=main_prompt,
            review_translation_prompt=review_prompt,
            source_language=source_language if need_translate else "auto",
            target_language=target_language,
            _method=translation_mode if need_translate and translation_mode else METHOD_NON_LLM,
            _radius=boundary_context_radius,
        )
    )


@dataclass(frozen=True)
class RepairFlow:
    """从冻结快照选出的修复方式（repair.py 消费）。

    mode: "main_review"（主翻译 + 高级校对）| "main"（仅主翻译）
        | "report_only"（不发起模型修复，仅确定性处理与报告）。
    """

    mode: str
    reason: str = ""
    main_profile: Optional["LLMModelProfile"] = None
    review_profile: Optional["LLMModelProfile"] = None
    boundary_context_radius: int = DEFAULT_BOUNDARY_CONTEXT_RADIUS


# 修复方式的共享中文标签（报告 / 状态摘要共用；单一来源，见 repair.report/summary）。
FLOW_MODE_LABELS = {
    "main_review": "主翻译+高级校对",
    "main": "仅主翻译",
    "report_only": "仅报告",
}


def flow_mode_label(mode: str) -> str:
    """修复方式标签；未知方式回退到方式串本身（不吞错，也不阻断报告）。"""
    return FLOW_MODE_LABELS.get(mode, mode or "仅报告")


def resolve_repair_flow(
    snapshot: Optional[TranslationExecutionSnapshot],
    *,
    profile_resolver=None,
) -> RepairFlow:
    """按冻结快照选择修复方式（D07/D15）：不静默升级、不猜测配置。

    角色连接解析顺序：快照内运行期对象（完整 workflow 注入）→
    ``profile_resolver``（调用方按角色身份显式解析，可验证的资产
    身份才发起）；两者都无则仅报告，不猜测配置、不静默升级。
    """

    def _resolve(role: str) -> Optional["LLMModelProfile"]:
        if role == "main" and snapshot is not None and snapshot.main_profile is not None:
            return snapshot.main_profile
        if role == "review" and snapshot is not None and snapshot.review_profile is not None:
            return snapshot.review_profile
        if profile_resolver is None:
            return None
        return profile_resolver(role, snapshot)

    if snapshot is None:
        return RepairFlow("report_only", "缺少翻译执行快照，无法自动重译")
    if snapshot.method == METHOD_ENHANCED_LLM:
        main = _resolve("main")
        review = _resolve("review")
        if main is not None and review is not None:
            return RepairFlow(
                "main_review",
                main_profile=main,
                review_profile=review,
                boundary_context_radius=snapshot.boundary_context_radius,
            )
        missing = []
        if main is None:
            missing.append("主翻译")
        if review is None:
            missing.append("高级校对")
        return RepairFlow(
            "report_only",
            "增强型翻译缺少可用的" + "/".join(missing) + "角色连接，无法复现原任务流程",
        )
    if snapshot.method == METHOD_SINGLE_LLM:
        main = _resolve("main")
        if main is not None:
            # 普通 LLM 翻译只复用普通翻译方式：不自动增加高级校对或其他
            # 模型角色（D07「不无条件增加高级校对」）。
            return RepairFlow(
                "main",
                main_profile=main,
                boundary_context_radius=snapshot.boundary_context_radius,
            )
        return RepairFlow("report_only", "普通 LLM 翻译缺少可用的主翻译角色连接")
    # 非 LLM 翻译或未知方式：不静默升级为 LLM（D15）。
    if snapshot.method == METHOD_NON_LLM:
        return RepairFlow("report_only", "翻译方式为非 LLM，不静默升级为 LLM")
    return RepairFlow("report_only", f"翻译方式未知（{snapshot.method!r}），不猜测配置")


def store_profile_resolver():
    """按快照记录的角色身份解析角色连接（独立任务复用已验证过程资产）。

    只在快照缺少运行期角色对象时被调用：按持久化身份中的
    ``profile_id`` 查方案库，且 ``name`` / ``model`` 与身份一致才复用；
    身份漂移（方案已被改动）或缺失一律返回 None——不静默替换角色
    （D15 不猜测配置 / D17 复用必须可验证）。方案库惰性加载。
    """

    from ..llm.profiles import LLMProfileNotFoundError

    store = None

    def _store():
        nonlocal store
        if store is None:
            from ..llm.profiles import LLMModelProfileStore

            store = LLMModelProfileStore()
        return store

    def resolve(role: str, snapshot: TranslationExecutionSnapshot):
        identity = snapshot.main_identity if role == "main" else snapshot.review_identity
        if identity is None or not identity.profile_id:
            return None
        try:
            profile = _store().get(identity.profile_id)
        except LLMProfileNotFoundError:
            logger.info("翻译角色方案已不存在，不复用: %s", identity.profile_id)
            return None
        if profile.model != identity.model or profile.name != identity.name:
            logger.warning(
                "翻译角色身份漂移（记录 %s / %s，现存 %s / %s），不复用",
                identity.profile_id,
                identity.model,
                profile.profile_id,
                profile.model,
            )
            return None
        return profile

    return resolve


__all__ = [
    "METHOD_ENHANCED_LLM",
    "METHOD_NON_LLM",
    "METHOD_SINGLE_LLM",
    "RepairFlow",
    "TRANSLATION_SNAPSHOT_SCHEMA",
    "TRANSLATION_SNAPSHOT_VERSION",
    "TranslationExecutionSnapshot",
    "TranslationRoleIdentity",
    "cli_translation_snapshot",
    "flow_mode_label",
    "load_translation_snapshot_file",
    "resolve_repair_flow",
    "role_label",
    "snapshot_from_subtitle_config",
    "store_profile_resolver",
]
