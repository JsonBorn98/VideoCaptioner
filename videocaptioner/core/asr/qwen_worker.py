"""Subprocess entry point for local Qwen ASR inference."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from .qwen_runtime import align_with_qwen, transcribe_batch_with_qwen, transcribe_with_qwen


def _handle_request(mode: str, request: dict) -> dict:
    if mode == "align":
        return {
            "time_stamps": align_with_qwen(
                audio_input=request["audio_path"],
                transcript=request.get("transcript", ""),
                language=request.get("language", ""),
                aligner_model=request.get(
                    "aligner_model",
                    "Qwen/Qwen3-ForcedAligner-0.6B",
                ),
                model_dir=request.get("model_dir", ""),
                device=request.get("device", "auto"),
                dtype=request.get("dtype", "auto"),
                temp_dir=request.get("temp_dir", ""),
                clip_start_ms=request.get("clip_start_ms"),
                clip_duration_ms=request.get("clip_duration_ms"),
                compile_aligner=bool(request.get("compile_aligner", False)),
            )
        }

    if mode == "transcribe_batch":
        items = [
            {
                "audio_input": item["audio_path"],
                "clip_start_ms": item.get("clip_start_ms"),
                "clip_duration_ms": item.get("clip_duration_ms"),
            }
            for item in request.get("items", [])
        ]
        return {
            "results": transcribe_batch_with_qwen(
                requests=items,
                language=request.get("language", ""),
                asr_model=request.get("asr_model", "Qwen/Qwen3-ASR-1.7B"),
                aligner_model=request.get(
                    "aligner_model",
                    "Qwen/Qwen3-ForcedAligner-0.6B",
                ),
                model_dir=request.get("model_dir", ""),
                device=request.get("device", "auto"),
                dtype=request.get("dtype", "auto"),
                max_new_tokens=int(request.get("max_new_tokens", 2048)),
                return_time_stamps=bool(request.get("return_time_stamps", True)),
                temp_dir=request.get("temp_dir", ""),
                max_inference_batch_size=int(
                    request.get("max_inference_batch_size", 0) or 0
                ),
                compile_aligner=bool(request.get("compile_aligner", False)),
            )
        }

    return transcribe_with_qwen(
        audio_input=request["audio_path"],
        language=request.get("language", ""),
        asr_model=request.get("asr_model", "Qwen/Qwen3-ASR-1.7B"),
        aligner_model=request.get("aligner_model", "Qwen/Qwen3-ForcedAligner-0.6B"),
        model_dir=request.get("model_dir", ""),
        device=request.get("device", "auto"),
        dtype=request.get("dtype", "auto"),
        max_new_tokens=int(request.get("max_new_tokens", 2048)),
        return_time_stamps=bool(request.get("return_time_stamps", True)),
        temp_dir=request.get("temp_dir", ""),
        max_inference_batch_size=int(request.get("max_inference_batch_size", 0) or 0),
        clip_start_ms=request.get("clip_start_ms"),
        clip_duration_ms=request.get("clip_duration_ms"),
        compile_aligner=bool(request.get("compile_aligner", False)),
    )


def _serve_json_lines() -> int:
    """Serve multiple Qwen requests over stdin/stdout JSON Lines.

    Keeping this process alive lets qwen_runtime's in-process model caches do
    real work across chunks while preserving CUDA/Torch isolation from PyQt.
    """
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            envelope = json.loads(line)
            request_id = envelope.get("id")
            if envelope.get("op") == "shutdown":
                response = {"id": request_id, "result": {"ok": True}}
                print(json.dumps(response, ensure_ascii=False), flush=True)
                return 0

            mode = envelope.get("mode", "transcribe")
            result = _handle_request(mode, envelope.get("request", {}))
            response = {"id": request_id, "result": result}
        except Exception as exc:
            response = {
                "id": locals().get("request_id"),
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }

        print(json.dumps(response, ensure_ascii=False), flush=True)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("transcribe", "transcribe_batch", "align"),
        default="transcribe",
    )
    parser.add_argument("--request")
    parser.add_argument("--output")
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args(argv)

    if args.serve:
        return _serve_json_lines()

    if not args.request or not args.output:
        parser.error("--request and --output are required outside --serve")

    output_path = Path(args.output)
    try:
        request = json.loads(Path(args.request).read_text(encoding="utf-8"))
        result = _handle_request(args.mode, request)
        output_path.write_text(
            json.dumps({"result": result}, ensure_ascii=False),
            encoding="utf-8",
        )
        return 0
    except Exception as exc:
        output_path.write_text(
            json.dumps(
                {
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
