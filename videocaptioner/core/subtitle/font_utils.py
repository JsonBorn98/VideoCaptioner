"""Font discovery and loading utilities"""

import filecmp
import os
import shutil
import sys
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
_warned_unloadable_fonts: set[str] = set()
_warned_default_fallback = False


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


def _system_font_dirs() -> tuple[Path, ...]:
    font_dirs: list[Path] = []

    windir = os.environ.get("WINDIR")
    if windir:
        font_dirs.append(Path(windir) / "Fonts")

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        font_dirs.append(Path(local_app_data) / "Microsoft" / "Windows" / "Fonts")

    home = Path.home()
    font_dirs.extend(
        [
            home / "AppData" / "Local" / "Microsoft" / "Windows" / "Fonts",
            home / "Library" / "Fonts",
            Path("/Library/Fonts"),
            Path("/System/Library/Fonts"),
            home / ".fonts",
            home / ".local" / "share" / "fonts",
            Path("/usr/share/fonts"),
            Path("/usr/local/share/fonts"),
        ]
    )

    seen_dirs: set[str] = set()
    unique_dirs: list[Path] = []
    for font_dir in font_dirs:
        try:
            dir_key = str(font_dir.resolve()).lower()
        except OSError:
            dir_key = str(font_dir).lower()
        if dir_key in seen_dirs:
            continue
        seen_dirs.add(dir_key)
        unique_dirs.append(font_dir)

    return tuple(unique_dirs)


def _iter_existing_font_files(font_dirs: Iterable[Path]) -> tuple[Path, ...]:
    """List supported font files from existing directories."""
    font_files: list[Path] = []
    seen_paths: set[str] = set()
    for font_dir in font_dirs:
        if not font_dir.exists():
            continue

        try:
            candidates = font_dir.rglob("*")
            for font_file in candidates:
                if not font_file.is_file() or not is_supported_font_file(font_file):
                    continue

                path_key = str(font_file.resolve()).lower()
                if path_key in seen_paths:
                    continue

                seen_paths.add(path_key)
                font_files.append(font_file)
        except OSError as e:
            logger.debug(f"Failed to scan system font directory {font_dir}: {e}")

    return tuple(sorted(font_files, key=lambda p: str(p).lower()))


def _dedupe_font_file_paths(font_files: Iterable[Path]) -> tuple[Path, ...]:
    unique_files: list[Path] = []
    seen_paths: set[str] = set()
    for font_file in font_files:
        try:
            path_key = str(font_file.resolve()).casefold()
        except OSError:
            path_key = str(font_file).casefold()
        if path_key in seen_paths:
            continue

        seen_paths.add(path_key)
        unique_files.append(font_file)

    return tuple(unique_files)


def iter_system_font_files() -> tuple[Path, ...]:
    """List supported font files from common system font directories."""
    return _iter_existing_font_files(_system_font_dirs())


def _normalized_font_name(font_name: str) -> str:
    return " ".join(font_name.casefold().split())


def _compact_font_name(font_name: str) -> str:
    return "".join(ch for ch in font_name.casefold() if ch.isalnum())


def _dedupe_font_names(font_names: Iterable[str]) -> list[str]:
    names: list[str] = []
    seen_names: set[str] = set()
    for font_name in font_names:
        name = font_name.split(",")[0].strip()
        if not name:
            continue

        name_key = _normalized_font_name(name)
        if name_key in seen_names:
            continue

        seen_names.add(name_key)
        names.append(name)
    return names


def _extract_font_names(font: TTFont, name_ids: Iterable[int]) -> list[str]:
    """Extract unique names from a font name table, preferring Windows records."""
    name_table = font.get("name")
    if not name_table:
        return []

    names: list[str] = []
    requested_ids = list(name_ids)
    for name_id in requested_ids:
        for prefer_windows in [True, False]:
            for record in name_table.names:
                if record.nameID != name_id:
                    continue
                if prefer_windows and record.platformID != 3:
                    continue
                if not prefer_windows and record.platformID == 3:
                    continue
                try:
                    names.append(record.toUnicode())
                except Exception:
                    continue

    return _dedupe_font_names(names)


def _is_regular_font_style(style_names: Iterable[str]) -> bool:
    names = [_normalized_font_name(name) for name in style_names if name.strip()]
    if not names:
        return True

    non_regular_tokens = (
        "bold",
        "italic",
        "oblique",
        "light",
        "medium",
        "semilight",
        "semi light",
        "semibold",
        "semi bold",
        "black",
        "heavy",
        "thin",
        "condensed",
        "expanded",
        "narrow",
        "demi",
        "粗",
        "斜",
        "细",
        "中",
        "窄",
    )
    if any(token in name for name in names for token in non_regular_tokens):
        return False

    regular_names = {"regular", "normal", "book", "roman", "常规", "标准"}
    return any(name in regular_names for name in names)


def _extract_font_face_names(font: TTFont) -> list[str]:
    family_names = _extract_font_names(font, [16, 1])
    full_names = _extract_font_names(font, [4])
    postscript_names = _extract_font_names(font, [6])
    style_names = _extract_font_names(font, [17]) or _extract_font_names(font, [2])

    if not family_names and not full_names and not postscript_names:
        return []

    primary_name = (family_names or full_names or postscript_names)[0]
    if not _is_regular_font_style(style_names) and full_names:
        primary_name = full_names[0]

    return _dedupe_font_names(
        [primary_name, *family_names, *full_names, *postscript_names]
    )


def _extract_font_family_name(font: TTFont) -> Optional[str]:
    """Extract family name from a loaded font face."""
    names = _extract_font_face_names(font)
    return names[0] if names else None


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
                font_names = _extract_font_face_names(font)
                if font_names:
                    faces.append(
                        {
                            "name": font_names[0],
                            "path": str(font_path),
                            "index": str(index),
                            "aliases": "\n".join(font_names),
                        }
                    )
        except Exception as e:
            logger.debug(f"Failed to parse font collection {font_path.name}: {e}")
        finally:
            if collection is not None:
                for font in collection.fonts:
                    font.close()
        return faces

    try:
        font = TTFont(str(font_path))
        try:
            font_names = _extract_font_face_names(font)
        finally:
            font.close()
    except Exception as e:
        logger.debug(f"Failed to parse font {font_path.name}: {e}")
        font_names = []

    if font_names:
        faces.append(
            {
                "name": font_names[0],
                "path": str(font_path),
                "index": "0",
                "aliases": "\n".join(font_names),
            }
        )
    return faces


def _load_font_face(face: Dict[str, str], size: int) -> FontType:
    return ImageFont.truetype(
        face["path"],
        size,
        index=int(face.get("index", "0")),
    )


def _font_face_names(face: Dict[str, str]) -> tuple[str, ...]:
    return tuple(_dedupe_font_names([face["name"], *face.get("aliases", "").splitlines()]))


def _font_face_matches_name(face: Dict[str, str], font_name: str) -> bool:
    expected_name = _normalized_font_name(font_name)
    return any(_normalized_font_name(name) == expected_name for name in _font_face_names(face))


def _find_font_face(
    font_name: str,
    font_faces: Iterable[Dict[str, str]],
) -> Optional[Dict[str, str]]:
    for font_face in font_faces:
        if _font_face_matches_name(font_face, font_name):
            return font_face
    return None


def _clean_windows_registry_font_name(font_name: str) -> str:
    for suffix in [
        "(truetype)",
        "(opentype)",
        "(true type)",
        "(open type)",
        "truetype",
        "opentype",
    ]:
        if font_name.casefold().endswith(suffix):
            font_name = font_name[: -len(suffix)]
            break
    return font_name.strip()


def _resolve_windows_font_path(font_file_name: str) -> Optional[Path]:
    font_path = Path(font_file_name)
    if font_path.is_absolute() and font_path.is_file():
        return font_path

    for font_dir in _system_font_dirs():
        candidate = font_dir / font_file_name
        if candidate.is_file():
            return candidate

    return None


def _font_registry_name_matches(registry_name: str, font_name: str) -> bool:
    registry_name = _clean_windows_registry_font_name(registry_name)
    return (
        _normalized_font_name(registry_name) == _normalized_font_name(font_name)
        or _compact_font_name(registry_name) == _compact_font_name(font_name)
    )


@lru_cache(maxsize=1)
def _get_windows_registry_font_entries() -> tuple[tuple[str, Path], ...]:
    if sys.platform != "win32":
        return ()

    try:
        import winreg
    except ImportError:
        return ()

    registry_roots = [winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE]
    registry_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
    font_entries: list[tuple[str, Path]] = []
    seen_entries: set[tuple[str, str]] = set()

    for root in registry_roots:
        try:
            key = winreg.OpenKey(root, registry_path)
        except OSError:
            continue

        with key:
            value_count = winreg.QueryInfoKey(key)[1]
            for index in range(value_count):
                try:
                    registry_name, font_file_name, _ = winreg.EnumValue(key, index)
                except OSError:
                    continue

                font_path = _resolve_windows_font_path(str(font_file_name))
                if font_path is None or not is_supported_font_file(font_path):
                    continue

                clean_name = _clean_windows_registry_font_name(registry_name)
                entry_key = (clean_name.casefold(), str(font_path).casefold())
                if entry_key in seen_entries:
                    continue

                seen_entries.add(entry_key)
                font_entries.append((clean_name, font_path))

    return tuple(font_entries)


@lru_cache(maxsize=1)
def _get_windows_registry_font_file_paths() -> tuple[Path, ...]:
    font_files: list[Path] = []
    seen_paths: set[str] = set()
    for _, font_path in _get_windows_registry_font_entries():
        path_key = str(font_path).casefold()
        if path_key in seen_paths:
            continue

        seen_paths.add(path_key)
        font_files.append(font_path)

    return tuple(font_files)


@lru_cache(maxsize=128)
def _get_windows_registry_font_files(font_name: str) -> tuple[Path, ...]:
    font_files: list[Path] = []
    seen_paths: set[str] = set()
    for registry_name, font_path in _get_windows_registry_font_entries():
        if not _font_registry_name_matches(registry_name, font_name):
            continue

        path_key = str(font_path).casefold()
        if path_key in seen_paths:
            continue

        seen_paths.add(path_key)
        font_files.append(font_path)

    return tuple(font_files)


def _font_file_name_matches(font_path: Path, font_name: str) -> bool:
    target_name = _compact_font_name(font_name)
    file_name = _compact_font_name(font_path.stem)
    return len(target_name) >= 4 and (
        target_name in file_name or file_name in target_name
    )


def _may_need_localized_name_scan(font_name: str) -> bool:
    return any(ord(char) > 127 for char in font_name)


def _localized_name_scan_font_dirs() -> tuple[Path, ...]:
    local_dirs: list[Path] = [FONTS_PATH]

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        local_dirs.append(Path(local_app_data) / "Microsoft" / "Windows" / "Fonts")

    home = Path.home()
    local_dirs.extend(
        [
            home / "AppData" / "Local" / "Microsoft" / "Windows" / "Fonts",
            home / "Library" / "Fonts",
            home / ".fonts",
            home / ".local" / "share" / "fonts",
        ]
    )

    return _dedupe_font_file_paths(local_dirs)


def _iter_localized_name_scan_font_files() -> Iterable[Path]:
    seen_paths: set[str] = set()
    font_file_groups = [
        lambda: _iter_existing_font_files(_localized_name_scan_font_dirs()),
        _get_windows_registry_font_file_paths,
        iter_system_font_files,
    ]

    for font_file_group in font_file_groups:
        for font_file in font_file_group():
            try:
                path_key = str(font_file.resolve()).casefold()
            except OSError:
                path_key = str(font_file).casefold()
            if path_key in seen_paths:
                continue

            seen_paths.add(path_key)
            yield font_file


@lru_cache(maxsize=128)
def _get_system_font_files_for_name(font_name: str) -> tuple[Path, ...]:
    """Return likely system font files for one font name without parsing all fonts."""
    registry_matches = _get_windows_registry_font_files(font_name)
    if registry_matches:
        return registry_matches

    matches = []
    seen_paths: set[str] = set()
    for font_dir in _system_font_dirs():
        if not font_dir.exists():
            continue

        try:
            candidates = font_dir.rglob("*")
            for font_file in candidates:
                if not font_file.is_file() or not is_supported_font_file(font_file):
                    continue
                if not _font_file_name_matches(font_file, font_name):
                    continue

                path_key = str(font_file.resolve()).casefold()
                if path_key in seen_paths:
                    continue

                seen_paths.add(path_key)
                matches.append(font_file)
        except OSError as e:
            logger.debug(f"Failed to search system font directory {font_dir}: {e}")

    return tuple(matches)


@lru_cache(maxsize=128)
def _resolve_system_font_face(font_name: str) -> Optional[Dict[str, str]]:
    for font_file in _get_system_font_files_for_name(font_name):
        for face in _get_font_faces(font_file):
            if _font_face_matches_name(face, font_name):
                return face

    if _may_need_localized_name_scan(font_name):
        for font_file in _iter_localized_name_scan_font_files():
            for face in _get_font_faces(font_file):
                if _font_face_matches_name(face, font_name):
                    return face

    return None


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


@lru_cache(maxsize=1)
def get_system_fonts() -> tuple[Dict[str, str], ...]:
    """Get system fonts by parsing installed font files."""
    system_fonts = []
    seen_names = set()

    for font_file in iter_system_font_files():
        for face in _get_font_faces(font_file):
            if face["name"].startswith("."):
                continue

            name_key = _normalized_font_name(face["name"])
            if name_key in seen_names:
                continue

            seen_names.add(name_key)
            system_fonts.append(face)
            logger.debug(f"System font: {font_file.name} -> {face['name']}")

    return tuple(sorted(system_fonts, key=lambda face: face["name"].casefold()))


@lru_cache(maxsize=128)
def is_font_loadable(font_name: str) -> bool:
    """Return whether a font name can be loaded without falling back."""
    font_name = font_name.strip()
    if not font_name:
        return False

    for font_face in [
        _find_font_face(font_name, get_builtin_fonts()),
        _resolve_system_font_face(font_name),
    ]:
        if font_face is not None:
            try:
                _load_font_face(font_face, 12)
                return True
            except Exception:
                return False

    try:
        ImageFont.truetype(font_name, 12)
        return True
    except (OSError, IOError):
        return False


def _warn_unloadable_font_once(font_name: str) -> None:
    if font_name not in _warned_unloadable_fonts:
        logger.warning(f"Cannot load font '{font_name}', using fallback")
        _warned_unloadable_fonts.add(font_name)


def _warn_default_fallback_once() -> None:
    global _warned_default_fallback
    if not _warned_default_fallback:
        logger.warning("All fallback fonts failed, using default")
        _warned_default_fallback = True


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
        for source_name, font_face in [
            ("built-in", _find_font_face(font_name, get_builtin_fonts())),
            ("system", _resolve_system_font_face(font_name)),
        ]:
            if font_face is None:
                continue
            try:
                font = _load_font_face(font_face, size)
                logger.debug(
                    f"Loaded {source_name} font: '{font_name}' from {font_face['path']}"
                )
                return font
            except Exception as e:
                logger.warning(f"Failed to load {source_name} font: {e}")

        try:
            font = ImageFont.truetype(font_name, size)
            logger.debug(f"Loaded system font: '{font_name}'")
            return font
        except (OSError, IOError):
            _warn_unloadable_font_once(font_name)

    for builtin in get_builtin_fonts():
        try:
            font = _load_font_face(builtin, size)
            logger.debug(f"Using built-in fallback font: '{builtin['name']}'")
            return font
        except Exception:
            continue

    fallback_fonts = [
        "PingFang SC",
        "Hiragino Sans GB",
        "Microsoft YaHei",
        "SimHei",
        "Arial Unicode MS",
        "Arial",
        "Helvetica",
    ]

    for fallback in fallback_fonts:
        try:
            font = ImageFont.truetype(fallback, size)
            logger.debug(f"Using fallback font: '{fallback}'")
            return font
        except Exception:
            continue

    _warn_default_fallback_once()
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
    font_face = _find_font_face(font_name, get_builtin_fonts()) or _resolve_system_font_face(
        font_name
    )
    font_path = None
    font_index = 0
    if font_face is not None:
        font_path = Path(font_face["path"])
        font_index = int(font_face.get("index", "0"))

    if not font_path:
        candidates = [
            font_file
            for font_file in (*iter_font_files(), *iter_system_font_files())
            if _normalized_font_name(font_name)
            in _normalized_font_name(font_file.stem)
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
    global _warned_default_fallback
    for cached_func in [
        get_builtin_fonts,
        get_system_fonts,
        _get_windows_registry_font_entries,
        _get_windows_registry_font_file_paths,
        _get_windows_registry_font_files,
        _get_system_font_files_for_name,
        _resolve_system_font_face,
        is_font_loadable,
        get_font,
        get_ass_to_pil_ratio,
    ]:
        cache_clear = getattr(cached_func, "cache_clear", None)
        if cache_clear is not None:
            cache_clear()
    _warned_unloadable_fonts.clear()
    _warned_default_fallback = False
    logger.debug("Font cache cleared")
