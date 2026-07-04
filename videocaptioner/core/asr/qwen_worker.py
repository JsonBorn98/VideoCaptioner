"""Subprocess entry point for local Qwen ASR inference."""

from __future__ import annotations

import argparse
import json
import traceback
from pathlib import Path

from .qwen_runtime import align_with_qwen, transcribe_with_qwen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("transcribe", "align"), default="transcribe")
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    output_path = Path(args.output)
    try:
        request = json.loads(Path(args.request).read_text(encoding="utf-8"))
        if args.mode == "align":
            result = {
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
                )
            }
        else:
            result = transcribe_with_qwen(
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
            )
        output_path.write_text(json.dumps({"result": result}, ensure_ascii=False), encoding="utf-8")
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
