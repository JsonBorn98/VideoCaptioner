"""Font discovery and loading utilities"""

import filecmp
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, Optional, Union

from fontTools.ttLib import TTCollection, TTFont
from PIL import ImageFont

from videocaptioner.config import FONTS_PATH
from videocaptioner.core.utils.logger import setup_logger

FontType = Union[ImageFont.FreeTypeFont, ImageFont.ImageFont]
SUPPORTED_FONT_EXTENSIONS = {".ttf", ".otf", ".ttc", ".otc"}

logger = setup_logger("subtitle.font")


def is_supported_font_file(font_path: Path) -> bool:
    """Return whether the file extension is a supported font format."""
    return font_path.suffix.lower() in SUPPORTED_FONT_EXTENSIONS


def iter_font_files(font_dir: Optional[Path] = None) -> tuple[Path, ...]:
    """List supported font files under a directory."""
    font_dir = FONTS_PATH if font_dir is None else font_dir
    if not font_dir.exists():
        return ()

    return tuple(
        sorted(
            (
                font_file
                for font_file in font_dir.rglob("*")
                if font_file.is_file() and is_supported_font_file(font_file)
            ),
            key=lambda p: p.name.lower(),
        )
    )


def _extract_font_family_name(font: TTFont) -> Optional[str]:
    """Extract family name from a loaded font face."""
    name_table = font.get("name")
    if not name_table:
        return None

    # nameID 16: Typographic Family (preferred)
    # nameID 1: Font Family (fallback)
    for name_id in [16, 1]:
        for record in name_table.names:
            if record.nameID == name_id and record.platformID == 3:
                try:
                    family_name = record.toUnicode()
                    return family_name.split(",")[0].strip()
                except Exception:
                    continue

    for name_id in [16, 1]:
        for record in name_table.names:
            if record.nameID == name_id:
                try:
                    family_name = record.toUnicode()
                    return family_name.split(",")[0].strip()
                except Exception:
                    continue

    return None


def _get_font_family_name(font_path: Path, font_index: int = 0) -> Optional[str]:
    """Extract font family name from font file (cross-platform)."""
    try:
        font = TTFont(str(font_path), fontNumber=font_index)
        try:
            return _extract_font_family_name(font)
        finally:
            font.close()
    except Exception as e:
        logger.debug(f"Failed to parse font {font_path.name} (index={font_index}): {e}")
        return None


def _get_font_faces(font_path: Path) -> list[Dict[str, str]]:
    """Return all readable font faces from a font file."""
    faces: list[Dict[str, str]] = []

    if font_path.suffix.lower() in {".ttc", ".otc"}:
        collection = None
        try:
            collection = TTCollection(str(font_path))
            for index, font in enumerate(collection.fonts):
                family_name = _extract_font_family_name(font)
                if family_name:
                    faces.append(
                        {
                            "name": family_name,
                            "path": str(font_path),
                            "index": str(index),
                        }
                    )
        except Exception as e:
            logger.debug(f"Failed to parse font collection {font_path.name}: {e}")
        finally:
            if collection is not None:
                for font in collection.fonts:
                    font.close()
        return faces

    family_name = _get_font_family_name(font_path)
    if family_name:
        faces.append({"name": family_name, "path": str(font_path), "index": "0"})
    return faces


@lru_cache(maxsize=1)
def get_builtin_fonts() -> tuple[Dict[str, str], ...]:
    """Get built-in fonts list with actual family names"""
    builtin_fonts = []
    seen_names = set()

    for font_file in iter_font_files():
        faces = _get_font_faces(font_file)
        if not faces:
            faces = [{"name": font_file.stem, "path": str(font_file), "index": "0"}]
            logger.debug(
                f"Cannot get family name for {font_file.name}, using filename"
            )

        for face in faces:
            if face["name"] in seen_names:
                continue
            seen_names.add(face["name"])
            builtin_fonts.append(face)
            logger.debug(f"Built-in font: {font_file.name} -> {face['name']}")

    return tuple(builtin_fonts)


def _same_file_content(source: Path, destination: Path) -> bool:
    try:
        return source.resolve() == destination.resolve() or filecmp.cmp(
            source, destination, shallow=False
        )
    except OSError:
        return False


def _resolve_import_destination(source: Path, font_dir: Path) -> Path:
    destination = font_dir / source.name
    if not destination.exists() or _same_file_content(source, destination):
        return destination

    counter = 1
    while True:
        candidate = font_dir / f"{source.stem}-{counter}{source.suffix}"
        if not candidate.exists() or _same_file_content(source, candidate):
            return candidate
        counter += 1


def import_font_files(
    font_files: Iterable[Union[str, Path]],
    font_dir: Optional[Path] = None,
) -> list[Dict[str, str]]:
    """Copy one or more font files into the app font directory.

    Existing identical files are reused. Different files with the same filename
    are imported with a numeric suffix.
    """
    font_dir = FONTS_PATH if font_dir is None else font_dir
    font_dir.mkdir(parents=True, exist_ok=True)

    imported_fonts: list[Dict[str, str]] = []
    imported_any_file = False
    for raw_path in font_files:
        source = Path(raw_path)
        if not source.is_file() or not is_supported_font_file(source):
            logger.warning(f"Skip unsupported font file: {source}")
            continue

        destination = _resolve_import_destination(source, font_dir)
        if not _same_file_content(source, destination):
            shutil.copy2(source, destination)
            imported_any_file = True

        faces = _get_font_faces(destination)
        if not faces:
            faces = [
                {"name": destination.stem, "path": str(destination), "index": "0"}
            ]

        imported_fonts.extend(faces)

    if imported_fonts or imported_any_file:
        clear_font_cache()

    return imported_fonts


@lru_cache(maxsize=64)
def get_font(size: int, font_name: str = "") -> FontType:
    """Get font object (built-in fonts first, then system fonts)"""
    if font_name:
        builtin_fonts = get_builtin_fonts()
        for builtin in builtin_fonts:
            if builtin["name"] == font_name:
                try:
                    font = ImageFont.truetype(
                        builtin["path"],
                        size,
                        index=int(builtin.get("index", "0")),
                    )
                    logger.debug(f"Loaded built-in font: '{font_name}'")
                    return font
                except Exception as e:
                    logger.warning(f"Failed to load built-in font: {e}")
                    break

        try:
            font = ImageFont.truetype(font_name, size)
            logger.debug(f"Loaded system font: '{font_name}'")
            return font
        except (OSError, IOError):
            logger.warning(f"Cannot load font '{font_name}', using fallback")

    fallback_fonts = [f["name"] for f in get_builtin_fonts()]
    fallback_fonts.extend(
        [
            "PingFang SC",
            "Hiragino Sans GB",
            "Microsoft YaHei",
            "SimHei",
            "Arial Unicode MS",
            "Arial",
            "Helvetica",
        ]
    )

    for fallback in fallback_fonts:
        try:
            font = ImageFont.truetype(fallback, size)
            logger.debug(f"Using fallback font: '{fallback}'")
            return font
        except Exception:
            continue

    logger.warning("All fallback fonts failed, using default")
    return ImageFont.load_default()


@lru_cache(maxsize=128)
def get_ass_to_pil_ratio(font_name: str) -> float:
    """
    Get ASS to PIL font size conversion ratio

    ASS uses Windows line height (usWinAscent + usWinDescent),
    PIL uses em square (unitsPerEm).

    For Noto Sans SC: ratio = 1.448
    This means: PIL_size = ASS_size / 1.448

    Returns:
        Conversion ratio (typically 1.4-1.5 for CJK fonts)
    """
    font_path = None
    font_index = 0
    for builtin in get_builtin_fonts():
        if builtin["name"] == font_name:
            font_path = Path(builtin["path"])
            font_index = int(builtin.get("index", "0"))
            break

    if not font_path:
        candidates = [
            font_file for font_file in iter_font_files() if font_name in font_file.stem
        ]
        if candidates:
            font_path = candidates[0]

    # Default ratio for most CJK fonts
    if not font_path:
        logger.debug(f"Font file not found: {font_name}, using default ratio 1.448")
        return 1.448

    try:
        font = TTFont(str(font_path), fontNumber=font_index)
        try:
            units_per_em = font["head"].unitsPerEm  # type: ignore
            win_ascent = font["OS/2"].usWinAscent  # type: ignore
            win_descent = font["OS/2"].usWinDescent  # type: ignore
            ratio = (win_ascent + win_descent) / units_per_em
            logger.debug(f"Font metrics for {font_name}: ratio={ratio:.3f}")
            return ratio
        finally:
            font.close()
    except Exception as e:
        logger.warning(f"Failed to read font metrics for {font_name}: {e}")
        return 1.448


def clear_font_cache():
    """Clear font cache"""
    get_builtin_fonts.cache_clear()
    get_font.cache_clear()
    get_ass_to_pil_ratio.cache_clear()
    logger.debug("Font cache cleared")
