from dataclasses import dataclass

from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QApplication


@dataclass(frozen=True)
class AppPalette:
    accent: str
    accent_fg: str
    bg: str
    bg_alt: str
    panel: str
    field: str
    control: str
    control_hover: str
    control_border: str
    control_border_strong: str
    header: str
    line: str
    line_soft: str
    accent_soft: str
    accent_border: str
    text: str
    muted: str
    subtle: str
    disabled: str
    danger: str
    danger_fg: str
    selected: str


def app_palette() -> AppPalette:
    accent = theme_color_hex()
    dark = is_dark_theme()
    return AppPalette(
        accent=accent,
        accent_fg="#061810",
        bg="#1f2020" if dark else "#f5f5f5",
        bg_alt="#181919" if dark else "#f7f8f8",
        panel="#292b2b" if dark else "#ffffff",
        field="#303332" if dark else "#f8faf9",
        control="#313433" if dark else "#f0f1f1",
        control_hover=rgba("#ffffff", 0.075) if dark else rgba("#000000", 0.06),
        control_border=rgba("#ffffff", 0.24) if dark else rgba("#000000", 0.10),
        control_border_strong=rgba("#ffffff", 0.26) if dark else rgba("#000000", 0.16),
        header="#222424" if dark else "#eef1f0",
        line="#444948" if dark else "#d8dddd",
        line_soft="#383d3b" if dark else "#e5e8e8",
        accent_soft=rgba(accent, 0.20),
        accent_border=rgba(accent, 0.70),
        text="#f4f6f5" if dark else "#1f2523",
        muted="#b8bfbc" if dark else "#64706b",
        subtle="#8f9793" if dark else "#7a8580",
        disabled="#373b39" if dark else "#eef1f0",
        danger="#ff6b63",
        danger_fg="#ffe8e6" if dark else "#7a211d",
        selected=rgba(accent, 0.10 if dark else 0.13),
    )


def theme_color_hex() -> str:
    from videocaptioner.ui.common.config import cfg

    color = cfg.themeColor.value
    if isinstance(color, QColor):
        return color.name(QColor.HexRgb)
    parsed = QColor(str(color))
    return parsed.name(QColor.HexRgb) if parsed.isValid() else "#28f08b"


def is_dark_theme() -> bool:
    from qfluentwidgets import Theme

    from videocaptioner.ui.common.config import cfg

    theme = cfg.themeMode.value
    if theme == Theme.DARK:
        return True
    if theme == Theme.LIGHT:
        return False

    app = QApplication.instance()
    if app is None:
        return True
    return app.palette().window().color().lightness() < 128


def rgba(hex_color: str, alpha: float) -> str:
    color = QColor(hex_color)
    if not color.isValid():
        color = QColor("#28f08b")
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {alpha:.2f})"
