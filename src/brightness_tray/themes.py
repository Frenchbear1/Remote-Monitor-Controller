from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap

THEME_LIGHT = "light"
THEME_DARK = "dark"
THEME_GRAY = "gray"
THEME_SAND = "sand"
THEME_AURORA = "aurora"
THEME_CLEAR = "clear"

THEME_OPTIONS = (THEME_DARK, THEME_AURORA, THEME_CLEAR, THEME_GRAY, THEME_LIGHT, THEME_SAND)

RGBA = tuple[int, int, int, int]


@dataclass(frozen=True)
class Theme:
    key: str
    label: str
    dark: bool
    # Window glass gradient (top -> bottom). Alpha is scaled down when the OS blur is active.
    top: RGBA
    bottom: RGBA
    blur_alpha_scale: float
    glow_a: RGBA
    glow_b: RGBA
    fg: str
    muted: str
    solid: str  # fully opaque surface for menus / message boxes
    border: RGBA
    card: RGBA
    card_border: RGBA
    hover: RGBA
    field: RGBA
    accent: str
    accent2: str
    accent_fg: str
    track: RGBA
    shine: bool = False  # liquid-glass extras: rim light + diagonal specular sweep


_THEMES: dict[str, Theme] = {
    THEME_DARK: Theme(
        key=THEME_DARK,
        label="Midnight",
        dark=True,
        top=(30, 36, 54, 232),
        bottom=(13, 16, 27, 242),
        blur_alpha_scale=0.60,
        glow_a=(139, 123, 255, 62),
        glow_b=(90, 169, 255, 46),
        fg="#eef2fa",
        muted="#a3afc4",
        solid="#161a26",
        border=(255, 255, 255, 40),
        card=(255, 255, 255, 15),
        card_border=(255, 255, 255, 28),
        hover=(255, 255, 255, 28),
        field=(0, 0, 0, 74),
        accent="#5aa9ff",
        accent2="#8b7bff",
        accent_fg="#ffffff",
        track=(255, 255, 255, 30),
    ),
    THEME_AURORA: Theme(
        key=THEME_AURORA,
        label="Aurora",
        dark=True,
        top=(12, 32, 50, 230),
        bottom=(24, 12, 48, 244),
        blur_alpha_scale=0.58,
        glow_a=(46, 230, 197, 70),
        glow_b=(155, 107, 255, 70),
        fg="#e8fbff",
        muted="#9fc4cf",
        solid="#0f1a2c",
        border=(150, 255, 235, 46),
        card=(255, 255, 255, 14),
        card_border=(160, 255, 240, 30),
        hover=(160, 255, 240, 30),
        field=(0, 10, 20, 90),
        accent="#2ee6c5",
        accent2="#9b6bff",
        accent_fg="#04141a",
        track=(255, 255, 255, 30),
    ),
    THEME_CLEAR: Theme(
        key=THEME_CLEAR,
        label="Liquid Glass",
        dark=True,
        top=(18, 22, 34, 34),
        bottom=(10, 12, 22, 66),
        blur_alpha_scale=1.0,
        glow_a=(255, 255, 255, 34),
        glow_b=(160, 210, 255, 30),
        fg="#ffffff",
        muted="#e4e9f4",
        solid="#1a1f2e",
        border=(255, 255, 255, 120),
        card=(255, 255, 255, 22),
        card_border=(255, 255, 255, 62),
        hover=(255, 255, 255, 44),
        field=(0, 0, 0, 58),
        accent="#a9dcff",
        accent2="#d9c0ff",
        accent_fg="#0b1224",
        track=(255, 255, 255, 46),
        shine=True,
    ),
    THEME_GRAY: Theme(
        key=THEME_GRAY,
        label="Graphite",
        dark=True,
        top=(66, 71, 82, 232),
        bottom=(34, 37, 44, 244),
        blur_alpha_scale=0.64,
        glow_a=(200, 210, 230, 38),
        glow_b=(134, 167, 222, 38),
        fg="#f1f3f7",
        muted="#b3bac8",
        solid="#262a32",
        border=(255, 255, 255, 44),
        card=(255, 255, 255, 18),
        card_border=(255, 255, 255, 30),
        hover=(255, 255, 255, 30),
        field=(0, 0, 0, 70),
        accent="#86a7de",
        accent2="#b9c6e0",
        accent_fg="#10151f",
        track=(255, 255, 255, 34),
    ),
    THEME_LIGHT: Theme(
        key=THEME_LIGHT,
        label="Frost",
        dark=False,
        top=(255, 255, 255, 240),
        bottom=(228, 237, 252, 240),
        blur_alpha_scale=0.66,
        glow_a=(122, 92, 255, 38),
        glow_b=(47, 107, 255, 34),
        fg="#172033",
        muted="#5a6781",
        solid="#f3f6fc",
        border=(15, 23, 42, 46),
        card=(255, 255, 255, 150),
        card_border=(15, 23, 42, 28),
        hover=(15, 23, 42, 20),
        field=(255, 255, 255, 170),
        accent="#2f6bff",
        accent2="#7a5cff",
        accent_fg="#ffffff",
        track=(15, 23, 42, 28),
    ),
    THEME_SAND: Theme(
        key=THEME_SAND,
        label="Dune",
        dark=False,
        top=(255, 249, 236, 240),
        bottom=(242, 224, 192, 240),
        blur_alpha_scale=0.68,
        glow_a=(219, 84, 120, 36),
        glow_b=(222, 128, 36, 44),
        fg="#3b2b17",
        muted="#7b6546",
        solid="#f7ecd7",
        border=(92, 64, 20, 52),
        card=(255, 255, 255, 120),
        card_border=(92, 64, 20, 34),
        hover=(92, 64, 20, 22),
        field=(255, 252, 244, 180),
        accent="#d97a1f",
        accent2="#d9486f",
        accent_fg="#ffffff",
        track=(92, 64, 20, 30),
    ),
}

_active_theme_key = THEME_DARK


def normalize_theme_name(value: str | None) -> str:
    theme = str(value or "").strip().lower()
    if theme in _THEMES:
        return theme
    return THEME_DARK


def get_theme(name: str | None = None) -> Theme:
    return _THEMES[normalize_theme_name(name if name is not None else _active_theme_key)]


def active_theme() -> Theme:
    return _THEMES[_active_theme_key]


def set_active_theme(name: str | None) -> Theme:
    global _active_theme_key
    _active_theme_key = normalize_theme_name(name)
    return _THEMES[_active_theme_key]


def rgba(value: RGBA) -> str:
    return f"rgba({value[0]}, {value[1]}, {value[2]}, {value[3]})"


def qcolor(value: RGBA | str, alpha: int | None = None) -> QColor:
    if isinstance(value, str):
        color = QColor(value)
    else:
        color = QColor(value[0], value[1], value[2], value[3])
    if alpha is not None:
        color.setAlpha(max(0, min(255, alpha)))
    return color


def _icon_dir() -> Path:
    path = Path(tempfile.gettempdir()) / "remote_monitor_controller_icons"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _glyph_path(kind: str, color_hex: str) -> str:
    """Render a tiny chevron / check glyph to disk so the stylesheet can reference it."""
    target = _icon_dir() / f"{kind}_{color_hex.lstrip('#')}.png"
    if not target.exists():
        size = 36
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(color_hex), 4.2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        if kind == "down":
            painter.drawPolyline([_pt(10, 14), _pt(18, 22), _pt(26, 14)])
        elif kind == "up":
            painter.drawPolyline([_pt(10, 22), _pt(18, 14), _pt(26, 22)])
        else:  # check
            painter.drawPolyline([_pt(8, 19), _pt(15, 26), _pt(28, 10)])
        painter.end()
        pixmap.save(str(target), "PNG")
    return target.as_posix()


def _pt(x: float, y: float):
    from PySide6.QtCore import QPointF

    return QPointF(x, y)


def build_stylesheet(theme_name: str) -> str:
    theme = get_theme(theme_name)
    fg = theme.fg
    muted = theme.muted
    accent = theme.accent
    accent2 = theme.accent2
    arrow_color = muted
    down = _glyph_path("down", arrow_color)
    up = _glyph_path("up", arrow_color)
    check = _glyph_path("check", theme.accent_fg)

    return f"""
    QWidget {{
        color: {fg};
        font-family: "Segoe UI Variable Text", "Segoe UI", "SF Pro Text", "Noto Sans", sans-serif;
        font-size: 9pt;
    }}
    QLabel {{
        background: transparent;
    }}
    QLabel#muted, QLabel#hint {{
        color: {muted};
    }}
    QLabel#hint {{
        font-size: 8pt;
    }}
    QLabel#windowTitle {{
        font-size: 10pt;
        font-weight: 600;
        letter-spacing: 0.3px;
    }}
    QLabel#sectionTitle {{
        font-size: 8pt;
        font-weight: 600;
        color: {muted};
    }}
    QLabel#statusChip {{
        background: {rgba(theme.card)};
        border: 1px solid {rgba(theme.card_border)};
        border-radius: 10px;
        padding: 3px 10px;
        color: {muted};
        font-size: 8pt;
    }}
    QLabel#bigValue {{
        font-size: 24pt;
        font-weight: 300;
    }}
    QLabel#clockValue {{
        font-size: 15pt;
        font-weight: 300;
    }}
    QToolTip {{
        background-color: {theme.solid};
        color: {fg};
        border: 1px solid {rgba(theme.border)};
        border-radius: 8px;
        padding: 6px 8px;
    }}

    QDialog#settingsDialog, QDialog#pictureControlsDialog, QWidget#brightnessControlPopup {{
        background: transparent;
    }}
    QMessageBox {{
        background-color: {theme.solid};
    }}

    QFrame#card {{
        background: {rgba(theme.card)};
        border: 1px solid {rgba(theme.card_border)};
        border-radius: 14px;
    }}
    QGroupBox {{
        background: {rgba(theme.card)};
        border: 1px solid {rgba(theme.card_border)};
        border-radius: 14px;
        margin-top: 12px;
        padding: 10px 14px 10px 14px;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        subcontrol-position: top left;
        left: 14px;
        padding: 0 6px;
        color: {muted};
        font-weight: 600;
    }}
    QWidget#scheduleTimeCard {{
        background: {rgba(theme.field)};
        border: 1px solid {rgba(theme.card_border)};
        border-radius: 12px;
    }}
    QWidget#scheduleTimeCard QLabel {{
        background: transparent;
        border: none;
    }}

    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
        background: {rgba(theme.field)};
        border: 1px solid {rgba(theme.card_border)};
        border-radius: 9px;
        padding: 5px 9px;
        selection-background-color: {accent};
        selection-color: {theme.accent_fg};
    }}
    QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
        border: 1px solid {accent};
    }}
    QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{
        color: {muted};
    }}
    QComboBox::drop-down {{
        border: none;
        width: 22px;
    }}
    QComboBox::down-arrow {{
        image: url({down});
        width: 12px;
        height: 12px;
    }}
    QComboBox QAbstractItemView {{
        background: {theme.solid};
        color: {fg};
        border: 1px solid {rgba(theme.border)};
        border-radius: 8px;
        padding: 4px;
        outline: none;
        selection-background-color: {rgba(theme.hover)};
        selection-color: {fg};
    }}
    QSpinBox::up-button, QSpinBox::down-button,
    QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
        width: 16px;
        border: none;
        background: transparent;
    }}
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
        image: url({up});
        width: 10px;
        height: 10px;
    }}
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
        image: url({down});
        width: 10px;
        height: 10px;
    }}

    QPushButton {{
        background: {rgba(theme.card)};
        border: 1px solid {rgba(theme.card_border)};
        border-radius: 10px;
        padding: 6px 14px;
    }}
    QPushButton:hover {{
        background: {rgba(theme.hover)};
        border: 1px solid {rgba(theme.border)};
    }}
    QPushButton:pressed {{
        background: {rgba(theme.field)};
    }}
    QPushButton:disabled {{
        color: {muted};
        background: transparent;
    }}
    QPushButton#primary, QPushButton#pill:checked {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {accent}, stop:1 {accent2});
        color: {theme.accent_fg};
        border: 1px solid transparent;
        font-weight: 600;
    }}
    QPushButton#primary:hover, QPushButton#pill:checked:hover {{
        border: 1px solid {rgba(theme.border)};
    }}
    QPushButton#pill {{
        border-radius: 13px;
        padding: 5px 16px;
    }}
    QPushButton#chip {{
        padding: 3px 10px;
        border-radius: 10px;
        font-size: 8pt;
    }}
    QPushButton#danger:hover {{
        background: rgba(239, 68, 68, 70);
        border: 1px solid rgba(239, 68, 68, 140);
    }}
    QToolButton {{
        background: transparent;
        border: 1px solid transparent;
        border-radius: 10px;
        padding: 5px;
    }}
    QToolButton:hover {{
        background: {rgba(theme.hover)};
    }}
    QToolButton:pressed {{
        background: {rgba(theme.field)};
    }}
    QToolButton#closeButton:hover {{
        background: rgba(239, 68, 68, 190);
    }}

    QCheckBox {{
        spacing: 9px;
        background: transparent;
    }}
    QCheckBox::indicator {{
        width: 18px;
        height: 18px;
        border: 1px solid {rgba(theme.border)};
        border-radius: 6px;
        background: {rgba(theme.field)};
    }}
    QCheckBox::indicator:hover {{
        border: 1px solid {accent};
    }}
    QCheckBox::indicator:checked {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {accent}, stop:1 {accent2});
        border: 1px solid transparent;
        image: url({check});
    }}

    QScrollArea, QScrollArea > QWidget > QWidget {{
        background: transparent;
        border: none;
    }}
    QScrollBar:vertical {{
        background: transparent;
        width: 10px;
        margin: 2px;
    }}
    QScrollBar::handle:vertical {{
        background: {rgba(theme.border)};
        border-radius: 3px;
        min-height: 28px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {accent};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
        background: transparent;
        height: 0;
    }}
    QScrollBar:horizontal {{
        height: 0;
    }}

    QMenu {{
        background: {theme.solid};
        color: {fg};
        border: 1px solid {rgba(theme.border)};
        border-radius: 12px;
        padding: 6px;
    }}
    QMenu::item {{
        padding: 8px 26px 8px 14px;
        border-radius: 8px;
        background: transparent;
    }}
    QMenu::item:selected {{
        background: {rgba(theme.hover)};
    }}
    QMenu::separator {{
        height: 1px;
        background: {rgba(theme.card_border)};
        margin: 5px 8px;
    }}
    """
