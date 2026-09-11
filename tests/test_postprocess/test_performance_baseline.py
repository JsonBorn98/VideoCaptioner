"""Offline baseline exercises the complete postprocess task, not repair internals."""

import pytest

from scripts.postprocess_benchmark import run_case


def test_mixed_subjects_split_and_partial_failure_is_not_a_false_success(tmp_path):
    mixed = run_case("mixed", tmp_path / "mixed", main_delay=0, review_delay=0)
    failed = run_case("partial-failure", tmp_path / "partial", main_delay=0, review_delay=0)

    assert mixed["outcome"]["output_segments"] == 130
    assert mixed["coverage"]["main_input_segments"] == 50
    assert mixed["coverage"]["review_input_segments"] == 50
    assert mixed["outcome"]["unresolved"] == 0
    assert failed["outcome"]["unresolved"] > 0
    assert failed["outcome"]["continue_downstream"] is True
    assert failed["outcome"]["output_segments"] == 129
    assert failed["outcome"]["repair"]["rollbacks"]
    assert failed["outcome"]["source_unchanged"]


def test_long_local_task_runs_processing_without_model_requests(tmp_path):
    report = run_case("local-long", tmp_path / "long", main_delay=0, review_delay=0)

    assert report["input"]["segments"] == 7257
    assert report["outcome"]["output_segments"] == 7257
    assert report["requests"]["main"]["logical"] == 0
    assert report["requests"]["review"]["logical"] == 0
    assert report["measurements"]["profiler_enabled"] is False
    assert report["diagnostic_profile"]["profiler_enabled"] is True
    assert report["diagnostic_profile"]["local_stages"]["run_post_stage"]["calls"] == 1


def test_warm_cache_is_reported_separately_from_real_attempts(tmp_path):
    report = run_case(
        "independent", tmp_path / "warm", main_delay=0, review_delay=0, cache_state="warm"
    )

    assert report["cache"] == "warm-isolated-disk"
    assert report["requests"]["main"]["logical"] == 4
    assert report["requests"]["main"]["attempts"] == 0
    assert report["requests"]["main"]["cache_hits"] == 4
    assert report["requests"]["review"]["cache_hits"] == 40
    assert report["outcome"]["unresolved"] == 0
    assert report["measurements"]["max_inflight"] == 0


def test_baseline_refuses_to_overwrite_existing_assets(tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    marker = existing / "input.srt"
    marker.write_bytes(b"original asset")
    with pytest.raises(FileExistsError):
        run_case("independent", existing)
    assert marker.read_bytes() == b"original asset"


def test_complete_baseline_preserves_source_and_covers_every_review_subject(tmp_path):
    report = run_case("independent", tmp_path / "case", main_delay=0, review_delay=0)

    assert report["outcome"]["status"] == "completed"
    assert report["outcome"]["continue_downstream"] is True
    assert report["outcome"]["source_unchanged"] is True
    assert report["outcome"]["unresolved"] == 0
    assert report["requests"]["main"]["logical"] == 4
    assert report["requests"]["review"]["logical"] == 40
    assert report["requests"]["main"]["attempts"] == 4
    assert report["coverage"]["main_input_segments"] == 40
    assert report["coverage"]["review_input_segments"] == 40
    assert report["input"]["segments"] == 120
    assert report["measurements"]["task_wall_seconds"] > 0
    assert report["measurements"]["max_inflight"] == 1
