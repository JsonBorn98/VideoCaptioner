import json

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.speed.pipeline import optimize_speed
from videocaptioner.core.speed.report import build_speed_qa, write_changes


def test_report_contains_m3_and_writes_versioned_json(tmp_path):
    data = ASRData([ASRDataSeg("source", 0, 300, "很长很长很长很长很长的译文")])
    _, result = optimize_speed(data, mode="analyze")
    markdown = build_speed_qa(result)
    assert "HardDeficit" in markdown
    path = write_changes(tmp_path / "changes.json", result)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["mode"] == "analyze"


def test_result_round_trip_preserves_reportable_facts(tmp_path):
    """result_from_dict 重建（票 06 阶段末检查点）：可报告事实往返不变。"""
    from videocaptioner.core.speed.report import result_from_dict, result_to_dict

    data = ASRData([ASRDataSeg("source", 0, 300, "很长很长很长很长很长的译文")])
    _, result = optimize_speed(data, mode="analyze")
    restored = result_from_dict(json.loads(json.dumps(result_to_dict(result))))
    assert restored.profile_id == result.profile_id
    assert restored.mode == result.mode
    assert restored.policy == result.policy
    assert restored.before == result.before
    assert restored.after == result.after
    assert restored.changes == result.changes
    assert restored.unresolved_cue_ids == result.unresolved_cue_ids
    assert restored.invalid_cue_ids == result.invalid_cue_ids
    assert restored.protected == result.protected
    assert restored.reference_before == result.reference_before
    assert restored.reference_after == result.reference_after
    # QA 渲染等价：重建对象渲染出同样的速度报告。
    assert build_speed_qa(restored) == build_speed_qa(result)
