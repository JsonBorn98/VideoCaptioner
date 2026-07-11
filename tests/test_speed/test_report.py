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
