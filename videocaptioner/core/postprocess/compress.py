"""F5 快速字幕 LLM 压缩重译。

对超过硬 CPS 限的中文侧文本做局部压缩（技能 S5 的自动化版本），复用
``optimize.py`` 的 ``agent_loop + 校验 + 回退`` 骨架。批量小、单批处理即可。
校验失败保留原文并记入 QA 报告"未能自动压缩"队列。
"""

from __future__ import annotations

import difflib
import json
import math
import re
from typing import TYPE_CHECKING, Optional, Tuple

import json_repair

from ..llm import call_llm
from ..prompts import get_prompt
from ..utils.logger import setup_logger
from ..utils.text_utils import is_mainly_cjk
from .config import PostprocessConfig
from .report import QualityReport

if TYPE_CHECKING:
    from ..asr.asr_data import ASRData, ASRDataSeg

logger = setup_logger("postprocess.compress")

MAX_STEPS = 3
_MIN_SIMILARITY = 0.3
_WS_RE = re.compile(r"\s+")


def _cjk_len(text: str) -> int:
    return len(_WS_RE.sub("", text))


def _cjk_field(seg: "ASRDataSeg") -> Optional[str]:
    """返回当前中文显示侧的字段名（text / translated_text），无则 None。"""
    if seg.text and is_mainly_cjk(seg.text):
        return "text"
    if seg.translated_text and is_mainly_cjk(seg.translated_text):
        return "translated_text"
    return None


def _build_candidates(asr_data: "ASRData", cfg: PostprocessConfig) -> list:
    """收集超过硬 CPS 限的中文侧条目（含 ±1 上下文）。"""
    segments = asr_data.segments
    candidates = []
    for i, seg in enumerate(segments):
        duration_s = (seg.end_time - seg.start_time) / 1000
        if duration_s <= 0:
            continue
        field_name = _cjk_field(seg)
        if field_name is None:
            continue
        value = getattr(seg, field_name)
        chars = _cjk_len(value)
        if chars / duration_s <= cfg.max_cps_cjk:
            continue
        context = []
        for j in (i - 1, i + 1):
            if 0 <= j < len(segments):
                nb = segments[j]
                context.append(nb.translated_text if _cjk_field(nb) == "translated_text" else nb.text)
        candidates.append(
            {
                "seg_index": i,
                "field": field_name,
                "text": value,
                "duration_s": round(duration_s, 2),
                "target_max_chars": max(1, math.floor(duration_s * cfg.max_cps_cjk)),
                "context": context,
            }
        )
    return candidates


def _validate(
    candidates: list, result: dict, cfg: PostprocessConfig
) -> Tuple[bool, str]:
    expected = {str(i + 1) for i in range(len(candidates))}
    actual = set(result.keys())
    if expected != actual:
        missing = expected - actual
        extra = actual - expected
        return False, f"Missing keys: {sorted(missing)}; Extra keys: {sorted(extra)}. Required: {sorted(expected)}"

    problems = []
    for idx, cand in enumerate(candidates, 1):
        compressed = result.get(str(idx), "")
        if not isinstance(compressed, str) or not compressed.strip():
            problems.append(f"Key '{idx}': empty output")
            continue
        limit = cand["target_max_chars"]
        if _cjk_len(compressed) > limit:
            problems.append(
                f"Key '{idx}': {_cjk_len(compressed)} chars > target {limit}"
            )
            continue
        ratio = difflib.SequenceMatcher(None, cand["text"], compressed).ratio()
        if ratio < _MIN_SIMILARITY:
            problems.append(
                f"Key '{idx}': similarity {ratio:.0%} < {_MIN_SIMILARITY:.0%} (stay on topic)"
            )
    if problems:
        return False, ";\n".join(problems)
    return True, ""


def _agent_loop(candidates: list, cfg: PostprocessConfig) -> dict:
    """LLM → 校验 → 反馈 → 重试（≤MAX_STEPS）。返回 {index: compressed}。"""
    payload = {
        str(i + 1): {
            "text": cand["text"],
            "duration_s": cand["duration_s"],
            "target_max_chars": cand["target_max_chars"],
            "context": cand["context"],
        }
        for i, cand in enumerate(candidates)
    }
    system_prompt = get_prompt("optimize/compress", max_cps_cjk=cfg.max_cps_cjk)
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": f"Compress the following subtitles:\n<input>{json.dumps(payload, ensure_ascii=False)}</input>",
        },
    ]

    last_result: dict = {}
    for step in range(MAX_STEPS):
        response = call_llm(messages=messages, model=cfg.llm_model or "", temperature=0.2)
        text = response.choices[0].message.content
        parsed = json_repair.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError(f"LLM 返回类型错误，期望 dict，实际 {type(parsed)}")
        last_result = {str(k): v for k, v in parsed.items()}

        ok, error = _validate(candidates, last_result, cfg)
        if ok:
            return last_result
        logger.warning("压缩校验失败(第%d次): %s", step + 1, error)
        messages.append({"role": "assistant", "content": text})
        messages.append(
            {
                "role": "user",
                "content": f"Validation failed: {error}\nFix and output ONLY a valid JSON object with all keys.",
            }
        )
    return last_result


def compress_fast_subtitles(
    asr_data: "ASRData",
    cfg: PostprocessConfig,
    report: QualityReport,
    llm_ctx: Optional[dict] = None,
) -> Tuple["ASRData", QualityReport]:
    """压缩超速中文行。校验失败的条目保留原文并记入 QA 报告。"""
    if not cfg.llm_model:
        logger.warning("未配置 LLM 模型，跳过快速字幕压缩")
        return asr_data, report

    candidates = _build_candidates(asr_data, cfg)
    if not candidates:
        return asr_data, report

    stage = report.stage("compress")
    try:
        result = _agent_loop(candidates, cfg)
    except Exception as exc:  # noqa: BLE001 —— 单点失败不阻断
        logger.warning("压缩重译调用失败，全部保留原文: %s", exc)
        for cand in candidates:
            report.compress_failures.append(cand["text"][:40])
        return asr_data, report

    # 逐条校验后写回：合格则替换，否则保留原文并记入失败队列
    for idx, cand in enumerate(candidates, 1):
        compressed = result.get(str(idx))
        seg = asr_data.segments[cand["seg_index"]]
        if not isinstance(compressed, str) or not compressed.strip():
            report.compress_failures.append(cand["text"][:40])
            continue
        if _cjk_len(compressed) > cand["target_max_chars"]:
            report.compress_failures.append(cand["text"][:40])
            continue
        ratio = difflib.SequenceMatcher(None, cand["text"], compressed).ratio()
        if ratio < _MIN_SIMILARITY:
            report.compress_failures.append(cand["text"][:40])
            continue
        setattr(seg, cand["field"], compressed.strip())
        stage.add(sample=f"{cand['text'][:20]} => {compressed.strip()[:20]}")

    if stage.changed:
        logger.info("快速字幕压缩：成功 %d 条", stage.changed)
    return asr_data, report


__all__ = ["compress_fast_subtitles"]
