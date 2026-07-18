from videocaptioner.core.utils.stage_summary import (
    build_optimize_stage_summary,
    build_split_stage_summary,
    build_translate_stage_summary,
    format_stage_summary,
)


def test_shared_subtitle_stage_summary_builders_report_degradation():
    split = build_split_stage_summary(12, use_llm=True, fallback_count=2)
    optimize = build_optimize_stage_summary(12, failed_batches=1, maxed_batches=3)
    translate = build_translate_stage_summary(12, failed_count=4)

    assert format_stage_summary(split) == "split · 12 段 · 2 规则回退 [degraded]"
    assert format_stage_summary(optimize) == (
        "optimize · 12 段 · 1 批失败 · 3 校验未过 [degraded]"
    )
    assert format_stage_summary(translate) == (
        "translate · 12 段 · 4 翻译失败 [degraded]"
    )


def test_merge_summary_stays_clean_without_fallbacks():
    summary = build_split_stage_summary(8, use_llm=False)

    assert format_stage_summary(summary) == "merge · 8 段"
