from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from PyQt5.QtCore import QByteArray, QSize, Qt
from PyQt5.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt5.QtSvg import QSvgRenderer
from PyQt5.QtWidgets import QAbstractButton, QApplication

from videocaptioner.config import ASSETS_PATH

CUSTOM_ICON_DIR = ASSETS_PATH / "icons"


class AppIcon(StrEnum):
    """App-owned SVG icons in resource/assets/icons."""

    ADD = "add"
    ARROW_LEFT = "arrow-left"
    CANCEL = "cancel"
    CHEVRON_DOWN = "chevron-down"
    CLOSE = "close"
    COPY = "copy"
    DELETE = "delete"
    DOCUMENT = "document"
    DOWNLOAD = "download"
    FILE = "file"
    FOLDER = "folder"
    FOLDER_ADD = "folder_add"
    GITHUB = "github"
    HEART = "heart"
    LAYOUT = "layout"
    LINK = "link"
    MICROPHONE = "microphone"
    MUSIC = "music"
    PLAY = "play"
    RIGHT_ARROW = "right_arrow"
    SAVE = "save"
    SETTING = "setting"
    SUBTITLE = "subtitle"
    SYNC = "sync"
    TERMINAL = "terminal"
    VIDEO = "video"
    VOLUME = "volume"


def custom_icon_path(name: str | Path) -> Path:
    """Return the canonical path for an app-owned SVG icon."""
    path = Path(str(name))
    if not path.is_absolute():
        path = CUSTOM_ICON_DIR / path
    if not path.suffix:
        path = path.with_suffix(".svg")
    return path


def _color_name(color: str | QColor) -> str:
    qcolor = color if isinstance(color, QColor) else QColor(str(color))
    return qcolor.name() if qcolor.isValid() else str(color)


def _device_pixel_ratio() -> float:
    """Highest screen DPR; SVG icons must rasterize at physical resolution
    or they come out blurry on Retina displays."""
    app = QApplication.instance()
    if app is None:
        return 1.0
    ratios = [screen.devicePixelRatio() for screen in app.screens()]
    return max(ratios, default=1.0)


@lru_cache(maxsize=256)
def _render_svg_pixmap_cached(name: str, color: str, size: int, dpr: float) -> QPixmap:
    path = custom_icon_path(name)
    svg = path.read_text(encoding="utf-8").replace("currentColor", color)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))

    logical_size = max(1, int(size))
    physical_size = max(1, round(logical_size * dpr))
    pixmap = QPixmap(physical_size, physical_size)
    pixmap.fill(Qt.transparent)
    if renderer.isValid():
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        renderer.render(painter)
        painter.end()
    pixmap.setDevicePixelRatio(dpr)
    return pixmap


def render_svg_pixmap(
    icon: AppIcon | str | Path, color: str | QColor, size: int = 24
) -> QPixmap:
    """Render an app-owned SVG icon to a DPR-aware pixmap (crisp on Retina)."""
    return _render_svg_pixmap_cached(
        str(icon), _color_name(color), int(size), _device_pixel_ratio()
    )


def render_svg_icon(icon: AppIcon | str | Path, color: str | QColor, size: int = 24) -> QIcon:
    """Render an app-owned SVG icon using the requested theme color."""
    path = custom_icon_path(icon)
    if not path.exists():
        return QIcon(str(path))
    return QIcon(render_svg_pixmap(icon, color, size))


def to_qicon(icon: Any, color: str | QColor | None = None, size: int = 24) -> QIcon:
    """Convert a FluentIcon, QIcon, or app-owned SVG name/path to QIcon."""
    if isinstance(icon, QIcon):
        return icon

    icon_factory = getattr(icon, "icon", None)
    if callable(icon_factory):
        return icon_factory()

    if color is not None:
        return render_svg_icon(icon, color, size)

    return QIcon(str(custom_icon_path(icon)))


def apply_button_icon(
    button: QAbstractButton,
    icon: Any,
    size: int = 18,
    color: str | QColor | None = None,
) -> None:
    button.setIcon(to_qicon(icon, color=color, size=size))
    button.setIconSize(QSize(size, size))
    if isinstance(icon, AppIcon):
        button.setProperty("appIcon", icon.value)
