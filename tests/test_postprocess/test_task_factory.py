from videocaptioner.core.entities import LLMServiceEnum, SubtitleExportPolicy, SubtitleLayoutEnum
from videocaptioner.core.postprocess import PostprocessConfig, PostprocessLayoutMode
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.task_factory import TaskFactory


def test_workflow_postprocess_task_trusts_upstream_layout(tmp_path):
    subtitle = tmp_path / "【初版字幕】sample.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n译文\nsource\n", encoding="utf-8")
    original = cfg.get(cfg.subtitle_layout)
    try:
        cfg.set(cfg.subtitle_layout, SubtitleLayoutEnum.TRANSLATE_ON_TOP)
        task = TaskFactory.create_postprocess_task(str(subtitle), need_next_task=True)
        assert task.layout_mode is PostprocessLayoutMode.TRANSLATE_ON_TOP
    finally:
        cfg.set(cfg.subtitle_layout, original)


def test_independent_postprocess_task_uses_auto_structure_detection(tmp_path):
    subtitle = tmp_path / "external.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")

    task = TaskFactory.create_postprocess_task(str(subtitle), need_next_task=False)

    assert task.layout_mode is PostprocessLayoutMode.AUTO


def test_workflow_postprocess_task_carries_frozen_export_contract(tmp_path):
    subtitle = tmp_path / "【初版字幕】sample.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n译文\nsource\n", encoding="utf-8"
    )
    policy = SubtitleExportPolicy(enabled=True, format="ass")

    task = TaskFactory.create_postprocess_task(
        str(subtitle),
        need_next_task=True,
        workflow_base_name="episode-01",
        export_policy=policy,
    )

    assert task.workflow_base_name == "episode-01"
    assert task.export_policy is policy


def test_workflow_postprocess_task_preserves_single_side_and_injects_llm(tmp_path):
    originals = (
        cfg.get(cfg.subtitle_layout),
        cfg.get(cfg.llm_service),
        cfg.get(cfg.openai_model),
    )
    try:
        cfg.set(cfg.subtitle_layout, SubtitleLayoutEnum.ONLY_TRANSLATE)
        cfg.set(cfg.llm_service, LLMServiceEnum.OPENAI)
        cfg.set(cfg.openai_model, "current-model")
        task = TaskFactory.create_postprocess_task(
            str(tmp_path / "initial.srt"),
            need_next_task=True,
            config_snapshot=PostprocessConfig(llm_model=None),
        )

        assert task.layout_mode is PostprocessLayoutMode.TRANSLATE_ONLY
        assert task.config_snapshot.llm_model == "current-model"
    finally:
        cfg.set(cfg.subtitle_layout, originals[0])
        cfg.set(cfg.llm_service, originals[1])
        cfg.set(cfg.openai_model, originals[2])
