"""Shared Qwen3 ASR and forced-alignment runtime helpers."""

from __future__ import annotations

import gc
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional, Union

from videocaptioner.config import MODEL_PATH
from videocaptioner.core.asr.qwen_runtime_manager import ensure_qwen_runtime_on_path
from videocaptioner.core.utils.text_utils import is_mainly_cjk

from .asr_data import ASRDataSeg

QWEN_ASR_MODEL_OPTIONS = [
    "Qwen/Qwen3-ASR-1.7B",
    "Qwen/Qwen3-ASR-0.6B",
]
QWEN_ALIGNER_MODEL_OPTIONS = [
    "Qwen/Qwen3-ForcedAligner-0.6B",
]

QWEN_SUPPORTED_ASR_LANGUAGES = {
    "zh",
    "en",
    "yue",
    "ar",
    "de",
    "fr",
    "es",
    "pt",
    "id",
    "it",
    "ko",
    "ru",
    "th",
    "vi",
    "ja",
    "tr",
    "hi",
    "ms",
    "nl",
    "sv",
    "da",
    "fi",
    "pl",
    "cs",
    "fil",
    "fa",
    "el",
    "hu",
    "mk",
    "ro",
}

_QWEN_LANGUAGE_NAMES = {
    "zh": "Chinese",
    "en": "English",
    "yue": "Cantonese",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "pt": "Portuguese",
    "ru": "Russian",
    "es": "Spanish",
    "ar": "Arabic",
    "id": "Indonesian",
    "th": "Thai",
    "vi": "Vietnamese",
    "tr": "Turkish",
    "hi": "Hindi",
    "ms": "Malay",
    "nl": "Dutch",
    "sv": "Swedish",
    "da": "Danish",
    "fi": "Finnish",
    "pl": "Polish",
    "cs": "Czech",
    "fil": "Filipino",
    "tl": "Filipino",
    "fa": "Persian",
    "el": "Greek",
    "hu": "Hungarian",
    "mk": "Macedonian",
    "ro": "Romanian",
}

_ALIGNER_SUPPORTED_LANGUAGE_NAMES = {
    "Chinese",
    "English",
    "Cantonese",
    "French",
    "German",
    "Italian",
    "Japanese",
    "Korean",
    "Portuguese",
    "Russian",
    "Spanish",
}

_model_lock = threading.Lock()
_align_lock = threading.Lock()
_aligner_cache: dict[tuple[str, str, str], Any] = {}
_asr_cache: dict[tuple[str, str, str, str, str, int], Any] = {}


def clear_qwen_model_cache() -> None:
    """Release cached Qwen models and return CUDA memory to PyTorch's allocator."""
    with _model_lock:
        _aligner_cache.clear()
        _asr_cache.clear()

    release_qwen_cuda_cache(ipc_collect=True)


def release_qwen_cuda_cache(ipc_collect: bool = False) -> None:
    """Return unused CUDA cache without unloading cached Qwen models."""
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            if ipc_collect:
                torch.cuda.ipc_collect()
    except Exception:
        pass


def resolve_qwen_model_ref(model: str, model_dir: str = "") -> str:
    """Return a local model path when it exists, otherwise the repo id."""
    model = (model or "").strip()
    if not model:
        raise ValueError("Qwen model cannot be empty")

    path = Path(model).expanduser()
    if path.exists():
        return str(path)

    root = Path(model_dir).expanduser() if model_dir else MODEL_PATH
    candidates = [
        root / model,
        root / model.split("/")[-1],
        root / model.replace("/", "--"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    return model


def normalize_qwen_language(language: str, transcript: str = "") -> Optional[str]:
    """Map VideoCaptioner language codes to Qwen language names."""
    language = (language or "").strip().lower()
    if language in _QWEN_LANGUAGE_NAMES:
        return _QWEN_LANGUAGE_NAMES[language]
    if language and language.title() in _QWEN_LANGUAGE_NAMES.values():
        return language.title()

    if transcript:
        return "Chinese" if is_mainly_cjk(transcript) else "English"
    return None


def normalize_aligner_language(language: str, transcript: str = "") -> str:
    """Map language to a Qwen3-ForcedAligner supported language."""
    normalized = normalize_qwen_language(language, transcript)
    if normalized in _ALIGNER_SUPPORTED_LANGUAGE_NAMES:
        return normalized
    return "Chinese" if transcript and is_mainly_cjk(transcript) else "English"


def _torch_dtype(dtype: str) -> Any:
    dtype = (dtype or "auto").strip().lower()
    if dtype == "auto":
        return None
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError(
            "Qwen ASR requires PyTorch. Install qwen-asr and its runtime dependencies first."
        ) from exc

    mapping = {
        "bfloat16": torch.bfloat16,
        "bf16": torch.bfloat16,
        "float16": torch.float16,
        "fp16": torch.float16,
        "float32": torch.float32,
        "fp32": torch.float32,
    }
    if dtype not in mapping:
        raise ValueError(f"Unsupported Qwen dtype: {dtype}")
    return mapping[dtype]


def _auto_cuda_dtype(device: str) -> Any:
    device = (device or "auto").strip().lower()
    if device != "auto" and not device.startswith("cuda"):
        return None

    try:
        import torch
    except ImportError:
        return None

    if not torch.cuda.is_available():
        return None
    if torch.cuda.is_bf16_supported():
        return torch.bfloat16
    return torch.float16


def _load_kwargs(device: str, dtype: str) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    device = (device or "auto").strip()
    if device:
        kwargs["device_map"] = "auto" if device == "auto" else device
    torch_dtype = _torch_dtype(dtype)
    if torch_dtype is None:
        torch_dtype = _auto_cuda_dtype(device)
    if torch_dtype is not None:
        kwargs["dtype"] = torch_dtype
    return kwargs


@contextmanager
def audio_input_as_path(
    audio_input: Union[str, bytes],
    suffix: str = ".mp3",
    temp_dir: str = "",
) -> Iterator[str]:
    """Yield a filesystem path for Qwen runtimes that prefer local paths."""
    if isinstance(audio_input, str):
        yield audio_input
        return

    temp_root = Path(temp_dir).expanduser() if temp_dir else None
    if temp_root is not None:
        temp_root.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        suffix=suffix,
        dir=str(temp_root) if temp_root is not None else None,
        delete=False,
    ) as temp_file:
        temp_file.write(audio_input)
        temp_path = Path(temp_file.name)

    try:
        yield str(temp_path)
    finally:
        temp_path.unlink(missing_ok=True)


def normalize_timestamp_items(items: Any) -> list[dict[str, float | str]]:
    """Normalize qwen-asr timestamp objects/dicts to serializable dicts."""
    if not items:
        return []

    if hasattr(items, "items") and not isinstance(items, dict):
        items = getattr(items, "items")

    if isinstance(items, list) and items and isinstance(items[0], list):
        items = items[0]

    normalized: list[dict[str, float | str]] = []
    for item in items:
        if hasattr(item, "items") and not isinstance(item, dict):
            normalized.extend(normalize_timestamp_items(getattr(item, "items")))
            continue

        if isinstance(item, dict):
            text = str(item.get("text") or item.get("word") or "").strip()
            start_time = item.get("start_time", item.get("start", 0))
            end_time = item.get("end_time", item.get("end", 0))
        else:
            text = str(getattr(item, "text", getattr(item, "word", ""))).strip()
            start_time = getattr(item, "start_time", getattr(item, "start", 0))
            end_time = getattr(item, "end_time", getattr(item, "end", 0))

        if not text:
            continue

        start = float(start_time or 0)
        end = float(end_time or 0)
        if end <= start:
            continue

        normalized.append({"text": text, "start_time": start, "end_time": end})

    return normalized


def timestamp_items_to_segments(items: Any) -> list[ASRDataSeg]:
    """Convert normalized timestamp items in seconds to ASRDataSeg in ms."""
    segments: list[ASRDataSeg] = []
    for item in normalize_timestamp_items(items):
        segments.append(
            ASRDataSeg(
                text=str(item["text"]),
                start_time=int(round(float(item["start_time"]) * 1000)),
                end_time=int(round(float(item["end_time"]) * 1000)),
            )
        )
    return segments


def _require_qwen_asr() -> Any:
    ensure_qwen_runtime_on_path()
    try:
        import qwen_asr
    except ImportError as exc:
        raise RuntimeError(
            "Qwen ASR 后端需要安装 qwen-asr。桌面版请在“Qwen 组件管理”中安装运行时；"
            "源码开发环境请执行: uv sync --extra qwen"
        ) from exc
    return qwen_asr


def align_with_qwen(
    audio_input: Union[str, bytes],
    transcript: str,
    language: str,
    aligner_model: str,
    model_dir: str = "",
    device: str = "auto",
    dtype: str = "auto",
    temp_dir: str = "",
) -> list[dict[str, float | str]]:
    """Run Qwen3-ForcedAligner and return normalized timestamp items."""
    transcript = (transcript or "").strip()
    if not transcript:
        return []

    qwen_asr = _require_qwen_asr()
    model_ref = resolve_qwen_model_ref(aligner_model, model_dir)
    cache_key = (model_ref, device or "auto", dtype or "auto")

    with _model_lock:
        model = _aligner_cache.get(cache_key)
        if model is None:
            kwargs = _load_kwargs(device, dtype)
            model = qwen_asr.Qwen3ForcedAligner.from_pretrained(model_ref, **kwargs)
            _aligner_cache[cache_key] = model

    align_language = normalize_aligner_language(language, transcript)
    try:
        with audio_input_as_path(audio_input, temp_dir=temp_dir) as audio_path:
            with _align_lock:
                results = model.align(
                    audio=audio_path,
                    text=transcript,
                    language=align_language,
                )
    finally:
        release_qwen_cuda_cache()

    return normalize_timestamp_items(results)


def transcribe_with_qwen(
    audio_input: Union[str, bytes],
    language: str,
    asr_model: str,
    aligner_model: str,
    model_dir: str = "",
    device: str = "auto",
    dtype: str = "auto",
    max_new_tokens: int = 2048,
    return_time_stamps: bool = True,
    temp_dir: str = "",
) -> dict[str, Any]:
    """Run local Qwen3 ASR, optionally with forced-alignment timestamps."""
    qwen_asr = _require_qwen_asr()
    asr_ref = resolve_qwen_model_ref(asr_model, model_dir)
    aligner_ref = resolve_qwen_model_ref(aligner_model, model_dir)
    cache_key = (
        asr_ref,
        aligner_ref,
        device or "auto",
        dtype or "auto",
        str(return_time_stamps),
        int(max_new_tokens),
    )

    with _model_lock:
        model = _asr_cache.get(cache_key)
        if model is None:
            kwargs = _load_kwargs(device, dtype)
            if return_time_stamps:
                kwargs["forced_aligner"] = aligner_ref
                kwargs["forced_aligner_kwargs"] = _load_kwargs(device, dtype)
            kwargs["max_inference_batch_size"] = 1
            kwargs["max_new_tokens"] = int(max_new_tokens)
            model = qwen_asr.Qwen3ASRModel.from_pretrained(asr_ref, **kwargs)
            _asr_cache[cache_key] = model

    qwen_language = normalize_qwen_language(language)
    try:
        with audio_input_as_path(audio_input, temp_dir=temp_dir) as audio_path:
            with _align_lock:
                results = model.transcribe(
                    audio=audio_path,
                    language=qwen_language,
                    return_time_stamps=return_time_stamps,
                )
    finally:
        release_qwen_cuda_cache()

    result = results[0] if isinstance(results, list) and results else results
    text = str(getattr(result, "text", "") or "").strip()
    result_language = str(getattr(result, "language", "") or qwen_language or "").strip()
    time_stamps = normalize_timestamp_items(getattr(result, "time_stamps", []))

    return {
        "text": text,
        "language": result_language,
        "time_stamps": time_stamps,
    }
