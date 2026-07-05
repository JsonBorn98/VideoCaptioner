"""Shared Qwen3 ASR and forced-alignment runtime helpers."""

from __future__ import annotations

import gc
import importlib.util
import io
import re
import tempfile
import threading
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional, Union, cast

from pydub import AudioSegment

from videocaptioner.config import MODEL_PATH
from videocaptioner.core.asr.qwen_runtime_manager import ensure_qwen_runtime_on_path
from videocaptioner.core.utils.logger import setup_logger
from videocaptioner.core.utils.text_utils import is_mainly_cjk

from .asr_data import ASRDataSeg

logger = setup_logger("qwen_runtime")

QWEN_ASR_MODEL_OPTIONS = [
    "Qwen/Qwen3-ASR-1.7B",
    "Qwen/Qwen3-ASR-0.6B",
]
QWEN_ALIGNER_MODEL_OPTIONS = [
    "Qwen/Qwen3-ForcedAligner-0.6B",
]
QWEN_PCM_SAMPLE_RATE = 16_000

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
_aligner_cache: dict[tuple[str, str, str, bool], Any] = {}
_asr_cache: dict[tuple[str, str, str, str, str, int, int, bool], Any] = {}


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
    language_value = (language or "").strip().lower()
    language_transcript = "" if language_value in {"", "auto"} else transcript
    normalized = normalize_qwen_language(language, language_transcript)
    if normalized in _ALIGNER_SUPPORTED_LANGUAGE_NAMES:
        return normalized
    guessed = _guess_aligner_language_from_text(transcript)
    if guessed:
        return guessed
    return "Chinese" if transcript and is_mainly_cjk(transcript) else "English"


def _guess_aligner_language_from_text(transcript: str) -> str | None:
    """Best-effort language hint for aligner languages when ASR config is auto."""
    text = (transcript or "").strip()
    if not text:
        return None

    if re.search(r"[\u3040-\u30ff]", text):
        return "Japanese"
    if re.search(r"[\uac00-\ud7af]", text):
        return "Korean"
    if re.search(r"[\u4e00-\u9fff]", text):
        return "Chinese"
    if re.search(r"[\u0400-\u04ff]", text):
        return "Russian"

    lowered = f" {text.lower()} "
    if re.search(r"[ãõ]|ção|\bnão\b|\buma\b", lowered):
        return "Portuguese"
    if re.search(r"[ñ¿¡]|\bqué\b|\bestá\b|\busted\b", lowered):
        return "Spanish"
    if re.search(r"[çœàâêîôûùè]|\bbonjour\b|\bavec\b|\bles\b|\bdes\b", lowered):
        return "French"
    if re.search(r"[äöüß]|\bund\b|\bder\b|\bdie\b|\bdas\b|\bnicht\b", lowered):
        return "German"
    if re.search(r"\bciao\b|\bgli\b|\bdelle\b|\bperché\b|\bnon\b", lowered):
        return "Italian"
    return None


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


def _validate_requested_device(device: str) -> None:
    device = (device or "auto").strip().lower()
    if not device.startswith("cuda"):
        return

    ensure_qwen_runtime_on_path()
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError(
            "Qwen 配置选择了 CUDA 设备，但当前 Qwen runtime 未安装 PyTorch。"
            "请在“Qwen 组件管理”中安装 / 修复运行时，或将运行设备改为 cpu/auto。"
        ) from exc

    torch_version = getattr(torch, "__version__", "unknown")
    torch_cuda = getattr(torch.version, "cuda", None)
    torch_c = getattr(torch, "_C", None)
    cuda_compiled = bool(torch_cuda) or hasattr(torch_c, "_cuda_getDeviceCount")
    if not cuda_compiled:
        raise RuntimeError(
            "Qwen 配置选择了 CUDA 设备，但当前 Qwen runtime 中的 PyTorch 是 CPU 版 "
            f"({torch_version})。请将“运行设备”改为 cpu/auto，或在 Qwen runtime 中安装 "
            "CUDA 版 PyTorch 后再选择 cuda:0。"
        )

    if not torch.cuda.is_available():
        raise RuntimeError(
            "Qwen 配置选择了 CUDA 设备，但 PyTorch 当前无法访问 CUDA。"
            "请确认 NVIDIA 驱动和 GPU 可用，或将“运行设备”改为 cpu/auto。"
        )

    if ":" not in device:
        return
    try:
        device_index = int(device.split(":", 1)[1])
    except ValueError:
        raise RuntimeError(f"无效的 Qwen CUDA 设备配置: {device}")

    device_count = torch.cuda.device_count()
    if device_index < 0 or device_index >= device_count:
        raise RuntimeError(
            f"Qwen 配置选择了 {device}，但 PyTorch 只检测到 {device_count} 个 CUDA 设备。"
        )


def _single_cuda_device_map(device: str) -> str:
    device = (device or "auto").strip()
    if device != "auto":
        return device

    try:
        import torch
    except ImportError:
        return "auto"

    if torch.cuda.is_available() and torch.cuda.device_count() == 1:
        return "cuda:0"
    return "auto"


def _attention_implementation(device: str) -> str | None:
    device = (device or "auto").strip().lower()
    if device != "auto" and not device.startswith("cuda"):
        return None

    try:
        import torch
    except ImportError:
        return None

    if not torch.cuda.is_available():
        return None
    if importlib.util.find_spec("flash_attn") is not None:
        return "flash_attention_2"
    return "sdpa"


def _load_kwargs(device: str, dtype: str) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    device = (device or "auto").strip()
    if device:
        _validate_requested_device(device)
        kwargs["device_map"] = _single_cuda_device_map(device)
    torch_dtype = _torch_dtype(dtype)
    if torch_dtype is None:
        torch_dtype = _auto_cuda_dtype(device)
    if torch_dtype is not None:
        kwargs["dtype"] = torch_dtype
    attn_implementation = _attention_implementation(device)
    if attn_implementation:
        kwargs["attn_implementation"] = attn_implementation
    return kwargs


def _from_pretrained_with_fallback(model_cls: Any, model_ref: str, kwargs: dict[str, Any]) -> Any:
    try:
        return model_cls.from_pretrained(model_ref, **kwargs)
    except TypeError as exc:
        if "attn_implementation" not in kwargs:
            raise
        fallback_kwargs = dict(kwargs)
        fallback_kwargs.pop("attn_implementation", None)
        if isinstance(fallback_kwargs.get("forced_aligner_kwargs"), dict):
            forced_aligner_kwargs = dict(fallback_kwargs["forced_aligner_kwargs"])
            forced_aligner_kwargs.pop("attn_implementation", None)
            fallback_kwargs["forced_aligner_kwargs"] = forced_aligner_kwargs
        logger.warning(
            "Qwen loader rejected attn_implementation=%s; retrying without it",
            kwargs.get("attn_implementation"),
        )
        try:
            return model_cls.from_pretrained(model_ref, **fallback_kwargs)
        except TypeError:
            raise exc


def _auto_inference_batch_size(device: str, requested: int = 0) -> int:
    if requested > 0:
        return requested

    device = (device or "auto").strip().lower()
    if device != "auto" and not device.startswith("cuda"):
        return 1

    try:
        import torch
    except ImportError:
        return 1

    if not torch.cuda.is_available():
        return 1
    try:
        total_gb = torch.cuda.get_device_properties(0).total_memory / (1024**3)
    except Exception:
        return 1
    if total_gb >= 22:
        return 8
    if total_gb >= 14:
        return 4
    if total_gb >= 8:
        return 2
    return 1


@contextmanager
def _inference_mode() -> Iterator[None]:
    try:
        import torch
    except ImportError:
        yield
        return

    with torch.inference_mode():
        yield


def _compile_model_if_requested(model: Any, *, enabled: bool, label: str) -> Any:
    """Best-effort torch.compile wrapper; failures keep the original model."""
    if not enabled:
        return model
    try:
        torch_module = importlib.import_module("torch")
        compile_fn = getattr(torch_module, "compile", None)
        if compile_fn is None:
            logger.warning("torch.compile is not available; skip compiling %s", label)
            return model
        logger.info("Compiling %s with torch.compile", label)
        return compile_fn(model)
    except Exception as exc:
        logger.warning("torch.compile failed for %s; using original model: %s", label, exc)
        return model


def _compile_embedded_aligner_if_requested(model: Any, *, enabled: bool) -> None:
    """Compile common embedded ForcedAligner attributes when qwen-asr exposes them."""
    if not enabled:
        return
    for attr_name in (
        "forced_aligner",
        "aligner",
        "forced_aligner_model",
        "_forced_aligner",
    ):
        aligner = getattr(model, attr_name, None)
        if aligner is None:
            continue
        compiled = _compile_model_if_requested(
            aligner,
            enabled=True,
            label=f"embedded Qwen ForcedAligner ({attr_name})",
        )
        if compiled is not aligner:
            try:
                setattr(model, attr_name, compiled)
            except Exception as exc:
                logger.warning(
                    "Unable to attach compiled embedded Qwen ForcedAligner (%s): %s",
                    attr_name,
                    exc,
                )
        return
    logger.info(
        "Qwen ASR model did not expose an embedded ForcedAligner attribute to compile"
    )


@contextmanager
def audio_input_as_path(
    audio_input: Union[str, bytes],
    suffix: str = ".mp3",
    temp_dir: str = "",
    clip_start_ms: int | None = None,
    clip_duration_ms: int | None = None,
) -> Iterator[str]:
    """Yield a filesystem path for Qwen runtimes that prefer local paths."""
    if clip_start_ms is not None or clip_duration_ms is not None:
        temp_path = _export_audio_clip(
            audio_input,
            temp_dir=temp_dir,
            clip_start_ms=clip_start_ms,
            clip_duration_ms=clip_duration_ms,
        )
        try:
            yield str(temp_path)
        finally:
            temp_path.unlink(missing_ok=True)
        return

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


def _export_audio_clip(
    audio_input: Union[str, bytes],
    *,
    temp_dir: str,
    clip_start_ms: int | None,
    clip_duration_ms: int | None,
) -> Path:
    temp_root = Path(temp_dir).expanduser() if temp_dir else None
    if temp_root is not None:
        temp_root.mkdir(parents=True, exist_ok=True)

    if isinstance(audio_input, str):
        audio = AudioSegment.from_file(audio_input)
    else:
        audio = AudioSegment.from_file(io.BytesIO(audio_input))

    start_ms = min(max(0, int(clip_start_ms or 0)), max(0, len(audio) - 1))
    if clip_duration_ms is None:
        end_ms = len(audio)
    else:
        end_ms = start_ms + max(1, int(clip_duration_ms))
    end_ms = min(len(audio), max(start_ms + 1, end_ms))
    clip = cast(AudioSegment, audio[start_ms:end_ms])

    with tempfile.NamedTemporaryFile(
        suffix=".wav",
        dir=str(temp_root) if temp_root is not None else None,
        delete=False,
    ) as temp_file:
        temp_path = Path(temp_file.name)

    try:
        clip.export(str(temp_path), format="wav")
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return temp_path


def _audio_input_as_pcm_tuple(
    audio_input: Union[str, bytes],
    *,
    clip_start_ms: int | None = None,
    clip_duration_ms: int | None = None,
) -> tuple[Any, int]:
    """Decode an input/range into qwen-asr's in-memory ``(np.ndarray, sr)`` form."""
    numpy_module = importlib.import_module("numpy")
    if isinstance(audio_input, str):
        audio = AudioSegment.from_file(audio_input)
    else:
        audio = AudioSegment.from_file(io.BytesIO(audio_input))

    if clip_start_ms is not None or clip_duration_ms is not None:
        start_ms = min(max(0, int(clip_start_ms or 0)), max(0, len(audio) - 1))
        if clip_duration_ms is None:
            end_ms = len(audio)
        else:
            end_ms = start_ms + max(1, int(clip_duration_ms))
        end_ms = min(len(audio), max(start_ms + 1, end_ms))
        audio = cast(AudioSegment, audio[start_ms:end_ms])

    pcm = (
        audio.set_channels(1)
        .set_frame_rate(QWEN_PCM_SAMPLE_RATE)
        .set_sample_width(2)
    )
    samples = numpy_module.asarray(
        pcm.get_array_of_samples(),
        dtype=numpy_module.float32,
    )
    samples = samples / numpy_module.float32(32768.0)
    return samples, QWEN_PCM_SAMPLE_RATE


@contextmanager
def audio_input_as_qwen_audio(
    audio_input: Union[str, bytes],
    suffix: str = ".mp3",
    temp_dir: str = "",
    clip_start_ms: int | None = None,
    clip_duration_ms: int | None = None,
) -> Iterator[Any]:
    """Yield a qwen-asr audio input, preferring in-memory PCM when useful.

    Plain source paths without clipping stay as paths so qwen-asr can use its
    native loader. Byte inputs and source ranges are decoded once in the worker
    process and passed as ``(float32_ndarray, 16000)`` to avoid temp WAV files.
    """
    should_use_pcm = (
        isinstance(audio_input, bytes)
        or clip_start_ms is not None
        or clip_duration_ms is not None
    )
    if should_use_pcm:
        try:
            yield _audio_input_as_pcm_tuple(
                audio_input,
                clip_start_ms=clip_start_ms,
                clip_duration_ms=clip_duration_ms,
            )
            return
        except Exception as exc:
            logger.warning(
                "Failed to prepare in-memory Qwen audio; falling back to path input: %s",
                exc,
            )

    with audio_input_as_path(
        audio_input,
        suffix=suffix,
        temp_dir=temp_dir,
        clip_start_ms=clip_start_ms,
        clip_duration_ms=clip_duration_ms,
    ) as audio_path:
        yield audio_path


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
    clip_start_ms: int | None = None,
    clip_duration_ms: int | None = None,
    compile_aligner: bool = False,
) -> list[dict[str, float | str]]:
    """Run Qwen3-ForcedAligner and return normalized timestamp items."""
    transcript = (transcript or "").strip()
    if not transcript:
        return []

    _validate_requested_device(device)
    qwen_asr = _require_qwen_asr()
    model_ref = resolve_qwen_model_ref(aligner_model, model_dir)
    cache_key = (model_ref, device or "auto", dtype or "auto", bool(compile_aligner))

    with _model_lock:
        model = _aligner_cache.get(cache_key)
        if model is None:
            kwargs = _load_kwargs(device, dtype)
            model = _from_pretrained_with_fallback(
                qwen_asr.Qwen3ForcedAligner,
                model_ref,
                kwargs,
            )
            model = _compile_model_if_requested(
                model,
                enabled=compile_aligner,
                label="Qwen3-ForcedAligner",
            )
            _aligner_cache[cache_key] = model

    align_language = normalize_aligner_language(language, transcript)
    with audio_input_as_qwen_audio(
        audio_input,
        temp_dir=temp_dir,
        clip_start_ms=clip_start_ms,
        clip_duration_ms=clip_duration_ms,
    ) as audio:
        with _align_lock:
            with _inference_mode():
                results = model.align(
                    audio=audio,
                    text=transcript,
                    language=align_language,
                )

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
    max_inference_batch_size: int = 0,
    return_time_stamps: bool = True,
    temp_dir: str = "",
    clip_start_ms: int | None = None,
    clip_duration_ms: int | None = None,
    compile_aligner: bool = False,
) -> dict[str, Any]:
    """Run local Qwen3 ASR, optionally with forced-alignment timestamps."""
    _validate_requested_device(device)
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
        int(max_inference_batch_size or 0),
        bool(compile_aligner),
    )

    with _model_lock:
        model = _asr_cache.get(cache_key)
        if model is None:
            kwargs = _load_kwargs(device, dtype)
            if return_time_stamps:
                kwargs["forced_aligner"] = aligner_ref
                kwargs["forced_aligner_kwargs"] = _load_kwargs(device, dtype)
            kwargs["max_inference_batch_size"] = _auto_inference_batch_size(
                device,
                int(max_inference_batch_size or 0),
            )
            kwargs["max_new_tokens"] = int(max_new_tokens)
            model = _from_pretrained_with_fallback(
                qwen_asr.Qwen3ASRModel,
                asr_ref,
                kwargs,
            )
            _compile_embedded_aligner_if_requested(
                model,
                enabled=compile_aligner and return_time_stamps,
            )
            _asr_cache[cache_key] = model

    qwen_language = normalize_qwen_language(language)
    with audio_input_as_qwen_audio(
        audio_input,
        temp_dir=temp_dir,
        clip_start_ms=clip_start_ms,
        clip_duration_ms=clip_duration_ms,
    ) as audio:
        with _align_lock:
            with _inference_mode():
                results = model.transcribe(
                    audio=audio,
                    language=qwen_language,
                    return_time_stamps=return_time_stamps,
                )

    result = results[0] if isinstance(results, list) and results else results
    text = str(getattr(result, "text", "") or "").strip()
    result_language = str(getattr(result, "language", "") or qwen_language or "").strip()
    time_stamps = normalize_timestamp_items(getattr(result, "time_stamps", []))

    return {
        "text": text,
        "language": result_language,
        "time_stamps": time_stamps,
    }


def transcribe_batch_with_qwen(
    requests: list[dict[str, Any]],
    language: str,
    asr_model: str,
    aligner_model: str,
    model_dir: str = "",
    device: str = "auto",
    dtype: str = "auto",
    max_new_tokens: int = 2048,
    max_inference_batch_size: int = 0,
    return_time_stamps: bool = True,
    temp_dir: str = "",
    compile_aligner: bool = False,
) -> list[dict[str, Any]]:
    """Run local Qwen3 ASR for multiple audio inputs in one model call."""
    if not requests:
        return []

    _validate_requested_device(device)
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
        int(max_inference_batch_size or 0),
        bool(compile_aligner),
    )

    with _model_lock:
        model = _asr_cache.get(cache_key)
        if model is None:
            kwargs = _load_kwargs(device, dtype)
            if return_time_stamps:
                kwargs["forced_aligner"] = aligner_ref
                kwargs["forced_aligner_kwargs"] = _load_kwargs(device, dtype)
            kwargs["max_inference_batch_size"] = _auto_inference_batch_size(
                device,
                int(max_inference_batch_size or 0),
            )
            kwargs["max_new_tokens"] = int(max_new_tokens)
            model = _from_pretrained_with_fallback(
                qwen_asr.Qwen3ASRModel,
                asr_ref,
                kwargs,
            )
            _compile_embedded_aligner_if_requested(
                model,
                enabled=compile_aligner and return_time_stamps,
            )
            _asr_cache[cache_key] = model

    qwen_language = normalize_qwen_language(language)
    with ExitStack() as stack:
        audio_batch = [
            stack.enter_context(
                audio_input_as_qwen_audio(
                    request["audio_input"],
                    temp_dir=temp_dir,
                    clip_start_ms=request.get("clip_start_ms"),
                    clip_duration_ms=request.get("clip_duration_ms"),
                )
            )
            for request in requests
        ]
        with _align_lock:
            with _inference_mode():
                raw_results = model.transcribe(
                    audio=audio_batch,
                    language=qwen_language,
                    return_time_stamps=return_time_stamps,
                )

    if not isinstance(raw_results, list):
        raw_results = [raw_results]
    if len(raw_results) != len(requests):
        raise RuntimeError(
            "Qwen ASR batch returned "
            f"{len(raw_results)} result(s) for {len(requests)} input(s)."
        )

    results: list[dict[str, Any]] = []
    for result in raw_results:
        text = str(getattr(result, "text", "") or "").strip()
        result_language = str(
            getattr(result, "language", "") or qwen_language or ""
        ).strip()
        time_stamps = normalize_timestamp_items(getattr(result, "time_stamps", []))
        results.append(
            {
                "text": text,
                "language": result_language,
                "time_stamps": time_stamps,
            }
        )
    return results
