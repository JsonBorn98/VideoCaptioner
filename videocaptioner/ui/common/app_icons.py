from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from PyQt5.QtCore import QByteArray, QSize, Qt
from PyQt5.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt5.QtSvg import QSvgRenderer
from PyQt5.QtWidgets import QAbstractButton

from videocaptioner.config import ASSETS_PATH

CUSTOM_ICON_DIR = ASSETS_PATH / "icons"


class AppIcon(StrEnum):
    """App-owned SVG icons in resource/assets/icons."""

    ARROW_LEFT = "arrow-left"


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


@lru_cache(maxsize=256)
def _render_svg_icon_cached(name: str, color: str, size: int) -> QIcon:
    path = custom_icon_path(name)
    if not path.exists():
        return QIcon(str(path))

    svg = path.read_text(encoding="utf-8").replace("currentColor", color)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    if not renderer.isValid():
        return QIcon(str(path))

    icon_size = max(1, int(size))
    pixmap = QPixmap(icon_size, icon_size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return QIcon(pixmap)


def render_svg_icon(icon: AppIcon | str | Path, color: str | QColor, size: int = 24) -> QIcon:
    """Render an app-owned SVG icon using the requested theme color."""
    return _render_svg_icon_cached(str(icon), _color_name(color), int(size))


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
