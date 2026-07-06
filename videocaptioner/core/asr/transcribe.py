import time

from videocaptioner.core.asr.asr_data import ASRData
from videocaptioner.core.asr.bcut import BcutASR
from videocaptioner.core.asr.chunked_asr import ChunkedASR
from videocaptioner.core.asr.faster_whisper import FasterWhisperASR
from videocaptioner.core.asr.jianying import JianYingASR
from videocaptioner.core.asr.mimo_asr import MAX_RAW_AUDIO_BYTES_FOR_BASE64, MiMoASR
from videocaptioner.core.asr.qwen_local_asr import QwenLocalASR
from videocaptioner.core.asr.whisper_api import WhisperAPI
from videocaptioner.core.asr.whisper_cpp import WhisperCppASR
from videocaptioner.core.entities import TranscribeConfig, TranscribeModelEnum
from videocaptioner.core.utils.logger import setup_logger

logger = setup_logger("transcribe")

MIMO_TEXT_ONLY_CHUNK_SECONDS = 60 * 5
QWEN_FORCE_ALIGN_CHUNK_SECONDS = 60 * 3


def transcribe(audio_path: str, config: TranscribeConfig, callback=None) -> ASRData:
    """Transcribe audio file using specified configuration.

    Args:
        audio_path: Path to audio file
        config: Transcription configuration
        callback: Progress callback function(progress: int, message: str)

    Returns:
        ASRData: Transcription result data
    """

    def _default_callback(x, y):
        pass

    if callback is None:
        callback = _default_callback

    if config.transcribe_model is None:
        raise ValueError("Transcription model not set")

    total_started = time.perf_counter()
    model_type = config.transcribe_model
    logger.info(
        "ASR 转录开始: model=%s, audio=%s, word_timestamp=%s",
        model_type,
        audio_path,
        config.need_word_time_stamp,
    )

    # Create ASR instance based on model type
    step_started = time.perf_counter()
    asr = _create_asr_instance(audio_path, config)
    logger.info(
        "ASR 实例创建完成: model=%s, elapsed=%.2fs",
        model_type,
        time.perf_counter() - step_started,
    )

    # Run transcription
    step_started = time.perf_counter()
    asr_data = asr.run(callback=callback)
    logger.info(
        "ASR 模型运行完成: model=%s, elapsed=%.2fs, segments=%s",
        model_type,
        time.perf_counter() - step_started,
        len(asr_data.segments),
    )

    # Optimize subtitle timing if not using word timestamps
    if not config.need_word_time_stamp:
        step_started = time.perf_counter()
        asr_data.optimize_timing()
        logger.info(
            "ASR 字幕时间优化完成: model=%s, elapsed=%.2fs, segments=%s",
            model_type,
            time.perf_counter() - step_started,
            len(asr_data.segments),
        )

    logger.info(
        "ASR 转录完成: model=%s, total=%.2fs, segments=%s",
        model_type,
        time.perf_counter() - total_started,
        len(asr_data.segments),
    )
    return asr_data


def _create_asr_instance(
    audio_path: str, config: TranscribeConfig
) -> ChunkedASR:
    """Create appropriate ASR instance based on configuration.

    Args:
        audio_path: Path to audio file
        config: Transcription configuration

    Returns:
        ChunkedASR: Chunked ASR instance ready to run
    """
    model_type = config.transcribe_model

    if model_type == TranscribeModelEnum.JIANYING:
        return _create_jianying_asr(audio_path, config)

    elif model_type == TranscribeModelEnum.BIJIAN:
        return _create_bijian_asr(audio_path, config)

    elif model_type == TranscribeModelEnum.WHISPER_CPP:
        return _create_whisper_cpp_asr(audio_path, config)

    elif model_type == TranscribeModelEnum.WHISPER_API:
        return _create_whisper_api_asr(audio_path, config)

    elif model_type == TranscribeModelEnum.MIMO_ASR_API:
        return _create_mimo_asr(audio_path, config)

    elif model_type == TranscribeModelEnum.QWEN_LOCAL_ASR:
        return _create_qwen_local_asr(audio_path, config)

    elif model_type == TranscribeModelEnum.FASTER_WHISPER:
        return _create_faster_whisper_asr(audio_path, config)

    else:
        raise ValueError(f"Invalid transcription model: {model_type}")


def _create_jianying_asr(audio_path: str, config: TranscribeConfig) -> ChunkedASR:
    """Create JianYing ASR instance with chunking support."""
    asr_kwargs = {
        "use_cache": True,
        "need_word_time_stamp": config.need_word_time_stamp,
    }
    return ChunkedASR(
        asr_class=JianYingASR, audio_path=audio_path, asr_kwargs=asr_kwargs
    )


def _create_bijian_asr(audio_path: str, config: TranscribeConfig) -> ChunkedASR:
    """Create Bijian ASR instance with chunking support."""
    asr_kwargs = {
        "use_cache": True,
        "need_word_time_stamp": config.need_word_time_stamp,
    }
    return ChunkedASR(asr_class=BcutASR, audio_path=audio_path, asr_kwargs=asr_kwargs)


def _create_whisper_cpp_asr(audio_path: str, config: TranscribeConfig) -> ChunkedASR:
    """Create WhisperCpp ASR instance with chunking support."""
    asr_kwargs = {
        "use_cache": True,
        "need_word_time_stamp": config.need_word_time_stamp,
        "language": config.transcribe_language,
        "whisper_model": config.whisper_model.value if config.whisper_model else None,
    }
    return ChunkedASR(
        asr_class=WhisperCppASR,
        audio_path=audio_path,
        asr_kwargs=asr_kwargs,
        chunk_concurrency=1,  # 本地转录使用单线程
        chunk_length=60 * 20,  # 每块20分钟
    )


def _create_whisper_api_asr(audio_path: str, config: TranscribeConfig) -> ChunkedASR:
    """Create Whisper API ASR instance with chunking support."""
    asr_kwargs = {
        "use_cache": True,
        "need_word_time_stamp": config.need_word_time_stamp,
        "language": config.transcribe_language,
        "whisper_model": config.whisper_api_model or "whisper-1",
        "api_key": config.whisper_api_key or "",
        "base_url": config.whisper_api_base or "",
        "prompt": config.whisper_api_prompt or "",
    }
    return ChunkedASR(
        asr_class=WhisperAPI, audio_path=audio_path, asr_kwargs=asr_kwargs
    )


def _create_mimo_asr(audio_path: str, config: TranscribeConfig) -> ChunkedASR:
    """Create MiMo ASR API instance with chunking support.

    MiMo API accepts base64 mp3/wav payloads up to 10 MB and does not return
    native timestamps. When word timestamps are requested, the local
    Qwen3-ForcedAligner limits effective inputs to about three minutes, so the
    outer MiMo chunks must also stay within that window.
    """
    chunk_overlap = _qwen_chunk_overlap(config)
    chunk_length = (
        QWEN_FORCE_ALIGN_CHUNK_SECONDS
        if config.need_word_time_stamp
        else MIMO_TEXT_ONLY_CHUNK_SECONDS
    )
    asr_kwargs = {
        "use_cache": True,
        "need_word_time_stamp": config.need_word_time_stamp,
        "language": config.transcribe_language,
        "api_key": config.mimo_asr_api_key or "",
        "base_url": config.mimo_asr_api_base or "https://api.xiaomimimo.com/v1",
        "model": config.mimo_asr_model or "mimo-v2.5-asr",
        "timeout": config.mimo_asr_timeout,
        "aligner_model": config.qwen_aligner_model or "Qwen/Qwen3-ForcedAligner-0.6B",
        "aligner_model_dir": config.qwen_model_dir or "",
        "aligner_device": config.qwen_device or "auto",
        "aligner_dtype": config.qwen_dtype or "auto",
        "aligner_compile": config.qwen_compile_aligner,
        "aligner_temp_dir": config.runtime_temp_dir or "",
        "request_memo": {},
    }
    return ChunkedASR(
        asr_class=MiMoASR,
        audio_path=audio_path,
        asr_kwargs=asr_kwargs,
        chunk_length=chunk_length,
        chunk_overlap=chunk_overlap,
        chunk_concurrency=max(1, config.mimo_asr_concurrency),
        chunk_boundary_mode="vad",
        max_chunk_payload_bytes=MAX_RAW_AUDIO_BYTES_FOR_BASE64,
    )


def _create_qwen_local_asr(audio_path: str, config: TranscribeConfig) -> ChunkedASR:
    """Create local Qwen3 ASR instance with Qwen3-ForcedAligner support."""
    chunk_overlap = _qwen_chunk_overlap(config)
    asr_kwargs = {
        "use_cache": True,
        "need_word_time_stamp": config.need_word_time_stamp,
        "language": config.transcribe_language,
        "asr_model": config.qwen_asr_model or "Qwen/Qwen3-ASR-1.7B",
        "aligner_model": config.qwen_aligner_model or "Qwen/Qwen3-ForcedAligner-0.6B",
        "model_dir": config.qwen_model_dir or "",
        "device": config.qwen_device or "auto",
        "dtype": config.qwen_dtype or "auto",
        "max_new_tokens": config.qwen_max_new_tokens,
        "compile_aligner": config.qwen_compile_aligner,
        "temp_dir": config.runtime_temp_dir or "",
    }
    return ChunkedASR(
        asr_class=QwenLocalASR,
        audio_path=audio_path,
        asr_kwargs=asr_kwargs,
        chunk_length=60 * 5,
        chunk_overlap=chunk_overlap,
        chunk_concurrency=1,
        chunk_audio_format="wav",
        retry_same_chunk=False,
        chunk_boundary_mode="vad",
        pass_source_range=True,
    )


def _qwen_chunk_overlap(config: TranscribeConfig) -> int:
    return max(0, min(int(config.qwen_chunk_overlap_seconds), 60))


def _create_faster_whisper_asr(audio_path: str, config: TranscribeConfig) -> ChunkedASR:
    """Create FasterWhisper ASR instance with chunking support."""
    asr_kwargs = {
        "use_cache": True,
        "need_word_time_stamp": config.need_word_time_stamp,
        "faster_whisper_program": config.faster_whisper_program or "",
        "language": config.transcribe_language,
        "whisper_model": (
            config.faster_whisper_model.value if config.faster_whisper_model else "base"
        ),
        "model_dir": config.faster_whisper_model_dir or "",
        "device": config.faster_whisper_device,
        "vad_filter": config.faster_whisper_vad_filter,
        "vad_threshold": config.faster_whisper_vad_threshold,
        "vad_method": (
            config.faster_whisper_vad_method.value
            if config.faster_whisper_vad_method
            else ""
        ),
        "ff_mdx_kim2": config.faster_whisper_ff_mdx_kim2,
        "one_word": config.faster_whisper_one_word,
        "prompt": config.faster_whisper_prompt,
    }
    return ChunkedASR(
        asr_class=FasterWhisperASR,
        audio_path=audio_path,
        asr_kwargs=asr_kwargs,
        chunk_concurrency=1,  # 本地转录使用单线程
        chunk_length=60 * 20,  # 每块20分钟
    )


if __name__ == "__main__":
    # 示例用法
    from videocaptioner.core.entities import WhisperModelEnum

    # 创建配置
    config = TranscribeConfig(
        transcribe_model=TranscribeModelEnum.WHISPER_CPP,
        transcribe_language="zh",
        whisper_model=WhisperModelEnum.MEDIUM,
    )

    # 转录音频
    audio_file = "test.wav"

    def progress_callback(progress: int, message: str):
        print(f"Progress: {progress}%, Message: {message}")

    result = transcribe(audio_file, config, callback=progress_callback)
    print(result)
