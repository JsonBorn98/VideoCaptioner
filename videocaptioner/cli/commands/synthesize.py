"""synthesize command -- burn subtitles into video."""

import json
from argparse import Namespace
from pathlib import Path
from typing import Literal, Optional

from videocaptioner.cli import exit_codes as EXIT
from videocaptioner.cli import output
from videocaptioner.cli.config import get
from videocaptioner.cli.validators import validate_synthesize
from videocaptioner.core.subtitle.style_manager import (
    StyleMode,
    SubtitleStyle,
    available_style_names,
    load_style,
)

EncodePreset = Literal[
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
]

# Quality presets: name -> (crf, ffmpeg preset)
_QUALITY_MAP: dict[str, tuple[int, EncodePreset]] = {
    "ultra": (18, "slow"),
    "high": (23, "medium"),
    "medium": (28, "medium"),
    "low": (32, "fast"),
}

def _resolve_style(config: dict, verbose: bool) -> tuple:
    """Resolve style settings from config.

    Returns (render_mode, ass_style_str, rounded_style_dict, font_file, style_obj).
    """
    render_mode = get(config, "synthesize.render_mode", "ass")
    style_name = get(config, "synthesize.style", "default")
    style_override_str = get(config, "synthesize.style_override", None)
    font_file = get(config, "synthesize.font_file", None)

    # Validate font_file
    if font_file and not Path(font_file).exists():
        output.error(f"Font file not found: {font_file}")
        return None, None, None, None, None

    # Parse --style-override JSON
    override_dict: dict = {}
    if style_override_str:
        try:
            override_dict = json.loads(style_override_str)
            if not isinstance(override_dict, dict):
                output.error("--style-override must be a JSON object, e.g. '{\"font_size\": 48}'")
                return None, None, None, None, None
        except json.JSONDecodeError as e:
            output.error(f"Invalid --style-override JSON: {e}")
            output.hint('Example: --style-override \'{"outline_color": "#ff0000", "font_size": 48}\'')
            return None, None, None, None, None

    # Load base style from preset
    style = load_style(style_name, mode=render_mode)
    if style is None:
        names = available_style_names()
        output.error(f"Style preset not found: '{style_name}'")
        if names:
            output.hint(f"Available presets: {', '.join(names)}")
        output.hint("Run 'videocaptioner style' to see all options")
        return None, None, None, None, None

    # Mode mismatch
    if render_mode == "rounded" and style.mode == StyleMode.ASS:
        output.warn(f"'{style.name}' is an ASS preset. Switching to default rounded style.")
        style = load_style("default", mode="rounded") or style
    elif render_mode == "ass" and style.mode == StyleMode.ROUNDED:
        output.warn(f"'{style.name}' is a rounded preset. Switching to default ASS style.")
        style = load_style("default", mode="ass") or style

    # Apply --style-override on top of base style
    if override_dict:
        # Auto-detect mode from override fields
        if any(k in override_dict for k in ("bg_color", "text_color", "corner_radius")):
            if render_mode == "ass":
                render_mode = "rounded"
                # Reload with rounded base if currently ASS
                if style.mode == StyleMode.ASS:
                    style = load_style("default", mode="rounded") or style
        base = style.to_json_dict()
        base.update(override_dict)
        style = SubtitleStyle.from_json(base)

    # Print final style config
    if verbose:
        final = style.to_json_dict()
        output.info(f"Render mode: {render_mode}")
        output.info(f"Style config: {json.dumps(final, ensure_ascii=False)}")

    # Build output
    if render_mode == "rounded":
        rounded_dict = style.to_rounded_dict()
        if font_file:
            rounded_dict["font_name"] = _get_font_family_name(font_file)
        return render_mode, "", rounded_dict, font_file, style

    # ASS mode
    ass_style = style.to_ass_string()
    if font_file:
        ass_style = _override_ass_font(ass_style, font_file, None)
    return render_mode, ass_style, None, font_file, style


def _get_font_family_name(font_path: str) -> str:
    """Read the font family name from a TTF/OTF file's name table."""
    try:
        from fontTools.ttLib import TTFont
        font = TTFont(font_path)
        name_table = font["name"]
        # nameID 1 = Font Family Name
        for record in name_table.names:
            if record.nameID == 1 and record.platformID in (0, 3):
                return str(record)
        # Fallback to any nameID 1
        for record in name_table.names:
            if record.nameID == 1:
                return str(record)
    except Exception:
        pass
    # Last resort: use filename without extension
    return Path(font_path).stem


def _override_ass_font(ass_style: str, font_file: Optional[str], font_size: Optional[int]) -> str:
    """Override font name/size in ASS style string."""
    font_name = _get_font_family_name(font_file) if font_file else None
    lines = ass_style.splitlines()
    result = []
    for line in lines:
        if line.startswith("Style:"):
            parts = line.split(",")
            if font_name and len(parts) > 1:
                parts[1] = font_name
            if font_size and len(parts) > 2:
                parts[2] = str(font_size)
            line = ",".join(parts)
        result.append(line)
    return "\n".join(result)


def run(args: Namespace, config: dict) -> int:
    # --raw-ffmpeg: execute a full ffmpeg command verbatim (argv[0] forced to managed ffmpeg)
    if getattr(args, "raw_ffmpeg", None):
        return _run_raw_ffmpeg(args)

    video_path = Path(args.video)
    subtitle_path = Path(args.subtitle)

    if not video_path.exists():
        output.error(f"Video file not found: {video_path}")
        return EXIT.FILE_NOT_FOUND
    if not subtitle_path.exists():
        output.error(f"Subtitle file not found: {subtitle_path}")
        return EXIT.FILE_NOT_FOUND

    from videocaptioner.cli.validators import validate_video_input
    err = validate_video_input(video_path)
    if err is not None:
        return err

    if not validate_synthesize(config):
        return EXIT.DEPENDENCY_MISSING

    subtitle_mode = get(config, "synthesize.subtitle_mode", "soft")
    quality = get(config, "synthesize.quality", "medium")
    quiet = getattr(args, "quiet", False)
    verbose = getattr(args, "verbose", False)

    crf, preset = _QUALITY_MAP.get(quality, (28, "medium"))
    soft = subtitle_mode == "soft"

    # New engine settings from flags (native CQ falls back to the --quality tier value)
    encode_settings = _build_encode_settings(args, quality)
    if not soft and encode_settings.video_encoder == "copy":
        from dataclasses import replace
        encode_settings = replace(encode_settings, video_encoder="x264")

    # Warn if user explicitly passed style options with soft mode
    style_arg = getattr(args, "style", None)
    override_arg = getattr(args, "style_override", None)
    render_arg = getattr(args, "render_mode", None)
    if soft and any([style_arg, override_arg, render_arg]):
        output.warn("Style options are ignored in soft subtitle mode (player controls rendering)")

    # Output path: -o wins; otherwise 【视频合成】…_{h}p_{enc}_{codec}_{q} (probe for height)
    if args.output:
        output_path = args.output
    else:
        from dataclasses import replace

        from videocaptioner.core.synthesis import build_output_name, media_probe
        _probe = media_probe.probe(str(video_path), source=encode_settings.ffmpeg_source)
        _eff_h = _probe.effective_height(encode_settings.target_height)
        _name_es = replace(encode_settings, video_encoder="copy") if soft else encode_settings
        output_path = str(
            video_path.parent
            / build_output_name(video_path.stem, _name_es, _eff_h, encode_settings.container)
        )

    # Check input != output
    if Path(output_path).resolve() == video_path.resolve():
        output.error("Output path is the same as input video. Use -o to specify a different output.")
        return EXIT.USAGE_ERROR

    if verbose:
        output.info(f"Mode: {'soft (embedded track)' if soft else 'hard (burned in)'}")
        output.info(f"Quality: {quality} (CRF={crf}, preset={preset})")

    if getattr(args, "print_command", False):
        return _print_command(encode_settings, video_path, subtitle_path, output_path)

    progress = None if quiet else output.ProgressLine(f"Synthesizing video [{subtitle_mode}]").start()

    def progress_callback(*cb_args) -> None:
        if progress and cb_args:
            try:
                progress.update(int(float(cb_args[0])), f"Encoding [{subtitle_mode}]")
            except (ValueError, TypeError):
                pass

    try:
        if soft:
            # Soft subtitle: embed as track (no style control)
            from videocaptioner.core.utils.video_utils import add_subtitles
            add_subtitles(
                input_file=str(video_path),
                subtitle_file=str(subtitle_path),
                output=output_path,
                crf=crf,
                preset=preset,
                soft_subtitle=True,
                progress_callback=progress_callback,
            )
        else:
            # Hard subtitle: resolve style and render
            resolved = _resolve_style(config, verbose)
            mode, ass_style, rounded_style, font_file, style = resolved
            if mode is None:
                if progress:
                    progress.fail("Style configuration error")
                return EXIT.USAGE_ERROR

            from videocaptioner.cli.validators import resolve_layout
            layout_str = get(config, "synthesize.layout", "target-above")
            layout = resolve_layout(layout_str)

            if mode == "rounded":
                from videocaptioner.core.asr.asr_data import ASRData
                from videocaptioner.core.subtitle.rounded_renderer import render_rounded_video
                asr_data = ASRData.from_subtitle_file(str(subtitle_path), layout=layout)
                render_rounded_video(
                    video_path=str(video_path),
                    asr_data=asr_data,
                    output_path=output_path,
                    rounded_style=rounded_style,
                    layout=layout,
                    crf=crf,
                    preset=preset,
                    progress_callback=progress_callback,
                    reference_height=style.reference_height,
                    encode_settings=encode_settings,
                )
            else:
                from videocaptioner.core.asr.asr_data import ASRData
                from videocaptioner.core.subtitle.ass_renderer import render_ass_video
                asr_data = ASRData.from_subtitle_file(str(subtitle_path), layout=layout)

                # Register custom font if provided
                if font_file:
                    _register_font(font_file)

                render_ass_video(
                    video_path=str(video_path),
                    asr_data=asr_data,
                    output_path=output_path,
                    style_str=ass_style,
                    layout=layout,
                    crf=crf,
                    preset=preset,
                    progress_callback=progress_callback,
                    reference_height=style.reference_height,
                    encode_settings=encode_settings,
                )

        if progress:
            progress.finish(f"Done -> {output_path}")
        if quiet:
            print(output_path)
        return EXIT.SUCCESS

    except Exception as e:
        msg = output.clean_error(str(e))
        if progress:
            progress.fail(msg)
        else:
            output.error(msg)
        if verbose:
            import traceback
            traceback.print_exc()
        return EXIT.RUNTIME_ERROR


def _register_font(font_file: str) -> None:
    """Copy custom font file to FONTS_PATH so FFmpeg can find it."""
    import shutil

    from videocaptioner.config import FONTS_PATH
    FONTS_PATH.mkdir(parents=True, exist_ok=True)
    dest = FONTS_PATH / Path(font_file).name
    if not dest.exists():
        shutil.copy(font_file, dest)


def _build_encode_settings(args: Namespace, quality_tier: str):
    """Build EncodeSettings; native CQ falls back to the legacy quality tier value."""
    from videocaptioner.core.synthesis.models import EncodeSettings

    tier_crf = {"ultra": 18, "high": 23, "medium": 28, "low": 32}.get(quality_tier, 28)

    def _v(name: str):
        return getattr(args, name, None)

    def _opt_str(name: str):
        v = _v(name)
        return str(v) if v is not None else None

    cq = _v("cq")
    bitrate = _v("bitrate")
    audio_br = _v("audio_bitrate")
    height = _v("height")
    fps_raw = _v("out_fps")
    try:
        fps = float(fps_raw) if fps_raw else None
    except (TypeError, ValueError):
        fps = None

    encode_mode = "abr" if _v("encode_mode") == "abr" else "cq"
    container = "mkv" if _v("container") == "mkv" else "mp4"
    vfr_v, faststart_v, keep_meta_v = _v("vfr"), _v("faststart"), _v("keep_metadata")

    return EncodeSettings(
        video_encoder=str(_v("video_encoder") or "x264"),
        encode_mode=encode_mode,
        quality=int(cq) if cq is not None else tier_crf,
        bitrate_kbps=int(bitrate) if bitrate is not None else 4000,
        two_pass=bool(_v("two_pass")),
        enc_preset=_opt_str("enc_preset"),
        enc_tune=_opt_str("enc_tune"),
        enc_profile=_opt_str("enc_profile"),
        enc_level=_opt_str("enc_level"),
        fast_decode=bool(_v("fast_decode")),
        target_height=int(height) if height is not None else None,
        fps=fps,
        vfr=True if vfr_v is None else bool(vfr_v),
        audio_encoder=str(_v("audio_encoder") or "copy"),
        audio_bitrate_kbps=int(audio_br) if audio_br is not None else 192,
        container=container,
        faststart=True if faststart_v is None else bool(faststart_v),
        keep_metadata=True if keep_meta_v is None else bool(keep_meta_v),
        extra_args=str(_v("extra_args") or ""),
    )


def _print_command(encode_settings, video_path: Path, subtitle_path: Path, output_path: str) -> int:
    """Print the hard-burn ffmpeg command the engine would run (for inspection / --raw-ffmpeg)."""
    import subprocess as _sp

    from videocaptioner.core.synthesis import get_ffmpeg_path, media_probe
    from videocaptioner.core.synthesis.command_builder import build_ffmpeg_command

    probe = media_probe.probe(str(video_path), source=encode_settings.ffmpeg_source)
    ffmpeg = get_ffmpeg_path(encode_settings.ffmpeg_source)
    # Representative subtitle filter; the real temp .ass path is resolved at run time.
    vf = f"ass='{subtitle_path.name}'"
    cmd = build_ffmpeg_command(
        ffmpeg=ffmpeg,
        input_path=str(video_path),
        output_path=output_path,
        video_filter=vf,
        settings=encode_settings,
        probe=probe,
    )
    print(_sp.list2cmdline(cmd))
    return EXIT.SUCCESS


def _run_raw_ffmpeg(args: Namespace) -> int:
    """Execute a full ffmpeg command verbatim; argv[0] is forced to the managed ffmpeg."""
    import shlex

    from videocaptioner.core.synthesis import get_ffmpeg_path, runner

    tokens = shlex.split(args.raw_ffmpeg)
    if not tokens:
        output.error("--raw-ffmpeg is empty")
        return EXIT.USAGE_ERROR
    if Path(tokens[0]).stem.lower() not in ("ffmpeg", "ffprobe"):
        output.error("--raw-ffmpeg must be an ffmpeg invocation (first token must be 'ffmpeg')")
        return EXIT.USAGE_ERROR
    tokens[0] = get_ffmpeg_path()

    quiet = getattr(args, "quiet", False)
    progress = None if quiet else output.ProgressLine("Running ffmpeg [raw]").start()

    def _cb(*cb_args) -> None:
        if progress and cb_args:
            try:
                progress.update(int(float(cb_args[0])), "Encoding [raw]")
            except (ValueError, TypeError):
                pass

    try:
        runner.run_encode(tokens, progress_callback=_cb)
    except Exception as e:
        msg = output.clean_error(str(e))
        if progress:
            progress.fail(msg)
        else:
            output.error(msg)
        return EXIT.RUNTIME_ERROR
    if progress:
        progress.finish("Done (raw)")
    return EXIT.SUCCESS
