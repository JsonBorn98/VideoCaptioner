"""SubtitleOptimizer 可接收规则型后处理注入的额外 prompt 约束。"""

import json
from types import SimpleNamespace

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.optimize.optimize import SubtitleOptimizer


def _resp(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


def test_optimizer_injects_extra_rules_into_system_prompt(monkeypatch):
    captured = {}

    def fake_call_llm(messages, model, temperature=1, **kwargs):
        captured["system"] = messages[0]["content"]
        return _resp(json.dumps({"1": "你好"}, ensure_ascii=False))

    monkeypatch.setattr("videocaptioner.core.optimize.optimize.call_llm", fake_call_llm)
    optimizer = SubtitleOptimizer(
        thread_num=1,
        batch_num=5,
        model="gpt-4o-mini",
        custom_prompt="",
        extra_rules="中文引号使用「」/『』。",
    )

    data = ASRData([ASRDataSeg("你好", 0, 1000)])
    result = optimizer.optimize_subtitle(data)

    assert result.segments[0].text == "你好"
    assert "中文引号使用「」/『』。" in captured["system"]
