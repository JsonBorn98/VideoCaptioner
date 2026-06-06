import tempfile
from pathlib import Path

from PyQt5.QtCore import QThread, pyqtSignal

from videocaptioner.config import ASSETS_PATH, CACHE_PATH, RESOURCE_PATH
from videocaptioner.core.dubbing import build_dubbing_config, get_dubbing_preset
from videocaptioner.core.speech import (
    SpeechProviderConfig,
    SynthesisRequest,
    create_speech_synthesizer,
)
from videocaptioner.core.utils.logger import setup_logger
from videocaptioner.ui.common.config import cfg

logger = setup_logger("voice_preview_thread")

SAMPLE_TEXT = "你好，这是卡卡字幕助手的配音试听。"


class VoicePreviewThread(QThread):
    finished = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(
        self,
        preset_name: str,
        text: str = "",
        clone_audio_path: str = "",
        clone_audio_text: str = "",
    ):
        super().__init__()
        self.preset_name = preset_name
        self.text = text.strip()
        self.clone_audio_path = clone_audio_path.strip()
        self.clone_audio_text = clone_audio_text.strip()

    def run(self):
        try:
            use_bundled = not self.text and not self.clone_audio_path and not self.clone_audio_text
            bundled = bundled_voice_preview(self.preset_name) if use_bundled else None
            if bundled:
                self.finished.emit(str(bundled))
                return

            preset = get_dubbing_preset(self.preset_name)
            api_key = cfg.dubbing_api_key.value
            api_base = cfg.dubbing_api_base.value
            model = cfg.dubbing_model.value or preset.model
            if preset.provider == "edge":
                api_key = ""
                api_base = ""
            elif not api_key:
                raise ValueError(f"{preset.provider} 试听需要先在设置里填写配音 API Key")

            core_config = build_dubbing_config(
                provider=preset.provider,
                preset=self.preset_name,
                api_key=api_key,
                api_base=api_base,
                model=model,
                voice=preset.voice,
                timing="balanced",
                audio_mode="replace",
                tts_workers=1,
                use_cache=cfg.cache_enabled.value,
            )
            work = Path(tempfile.mkdtemp(prefix="videocaptioner-voice-"))
            response_format = core_config.response_format
            if core_config.provider == "gemini":
                response_format = "wav"
            elif core_config.provider == "edge":
                response_format = "mp3"
            synthesizer = create_speech_synthesizer(
                SpeechProviderConfig(
                    provider=core_config.provider,
                    api_key=core_config.api_key,
                    base_url=core_config.base_url,
                    model=core_config.model,
                    default_voice=core_config.voice,
                    response_format=response_format,
                    sample_rate=core_config.sample_rate,
                    speed=core_config.speed,
                    gain=core_config.gain,
                    timeout=core_config.timeout,
                    style_prompt=core_config.style_prompt,
                )
            )
            output = work / f"{self.preset_name}.wav"
            result = synthesizer.synthesize(
                SynthesisRequest(
                    text=self.text or SAMPLE_TEXT,
                    output_path=str(output),
                    voice=core_config.voice,
                    style_prompt=core_config.style_prompt or None,
                    clone_audio_path=self.clone_audio_path or None,
                    clone_audio_text=self.clone_audio_text or None,
                )
            )
            self.finished.emit(str(result.output_path))
        except Exception as exc:
            if isinstance(exc, ValueError):
                logger.warning("音色试听失败: %s", exc)
            else:
                logger.exception("音色试听失败: %s", exc)
            self.error.emit(str(exc))


def bundled_voice_preview(preset_name: str) -> Path | None:
    preview_dirs = (
        ASSETS_PATH / "voice-previews",
        RESOURCE_PATH / "assets" / "voice-previews",
        Path(__file__).resolve().parents[2] / "resources" / "assets" / "voice-previews",
        CACHE_PATH / "voice-previews",
    )
    for preview_dir in preview_dirs:
        for suffix in (".mp3", ".wav", ".flac"):
            path = preview_dir / f"{preset_name}{suffix}"
            if path.exists() and path.stat().st_size > 0:
                return path
    return None
