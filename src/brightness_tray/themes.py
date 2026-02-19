from __future__ import annotations

THEME_LIGHT = "light"
THEME_DARK = "dark"
THEME_GRAY = "gray"
THEME_SAND = "sand"

THEME_OPTIONS = (THEME_LIGHT, THEME_DARK, THEME_GRAY, THEME_SAND)


def normalize_theme_name(value: str | None) -> str:
    theme = str(value or "").strip().lower()
    if theme in THEME_OPTIONS:
        return theme
    return THEME_DARK


def build_stylesheet(theme_name: str) -> str:
    theme = normalize_theme_name(theme_name)
    palette = _theme_palette(theme)
    return f"""
    QWidget {{
        background-color: {palette["bg"]};
        color: {palette["fg"]};
    }}
    QDialog, QMenu {{
        background-color: {palette["bg"]};
    }}
    QGroupBox {{
        background-color: {palette["panel"]};
    }}
    QGroupBox QWidget {{
        background-color: {palette["panel"]};
    }}
    QGroupBox {{
        border: 1px solid {palette["border"]};
        border-radius: 8px;
        margin-top: 10px;
        padding-top: 8px;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 8px;
        padding: 0 4px;
        color: {palette["muted_fg"]};
    }}
    QLabel {{
        background: transparent;
    }}
    QLineEdit, QComboBox, QSpinBox, QTableWidget, QHeaderView::section {{
        background-color: {palette["panel"]};
        color: {palette["fg"]};
        border: 1px solid {palette["border"]};
        border-radius: 6px;
    }}
    QTableWidget {{
        gridline-color: {palette["border"]};
    }}
    QWidget#monitorScrollContent,
    QScrollArea#monitorScroll,
    QScrollArea#monitorScroll QAbstractScrollArea::viewport {{
        background-color: {palette["panel"]};
        border: none;
    }}
    QWidget#scheduleTimeCard {{
        background-color: {palette["panel"]};
        border: 1px solid {palette["border"]};
        border-radius: 8px;
    }}
    QWidget#scheduleTimeCard QLabel {{
        background: transparent;
        border: none;
        padding: 0;
        margin: 0;
    }}
    QWidget#settingsContentWidget,
    QScrollArea#settingsContentScroll,
    QScrollArea#settingsContentScroll QAbstractScrollArea::viewport {{
        background-color: {palette["bg"]};
        border: none;
    }}
    QPushButton, QToolButton {{
        background-color: {palette["button_bg"]};
        color: {palette["button_fg"]};
        border: 1px solid {palette["border"]};
        border-radius: 6px;
        padding: 4px 10px;
    }}
    QPushButton:hover, QToolButton:hover {{
        background-color: {palette["button_hover"]};
    }}
    QPushButton:checked,
    QPushButton:pressed,
    QToolButton:checked,
    QToolButton:pressed {{
        background-color: {palette["accent"]};
        color: {palette["accent_fg"]};
        border: 1px solid {palette["accent"]};
    }}
    QScrollArea {{
        border: none;
        background-color: {palette["bg"]};
    }}
    QSlider::groove:horizontal {{
        height: 6px;
        border-radius: 3px;
        background: {palette["panel"]};
        border: 1px solid {palette["border"]};
    }}
    QSlider::handle:horizontal {{
        width: 14px;
        margin: -5px 0;
        border-radius: 7px;
        background: {palette["accent"]};
    }}
    QCheckBox::indicator {{
        width: 14px;
        height: 14px;
        border: 1px solid {palette["border"]};
        border-radius: 3px;
        background: {palette["input_bg"]};
    }}
    QCheckBox::indicator:checked {{
        background: {palette["accent"]};
        border: 1px solid {palette["accent"]};
    }}
    """


def _theme_palette(theme_name: str) -> dict[str, str]:
    if theme_name == THEME_LIGHT:
        return {
            "bg": "#f5f6f8",
            "panel": "#ffffff",
            "input_bg": "#ffffff",
            "fg": "#1f2937",
            "muted_fg": "#4b5563",
            "border": "#cfd5dd",
            "button_bg": "#eef2f6",
            "button_hover": "#e2e8f0",
            "button_fg": "#1f2937",
            "accent": "#2563eb",
            "accent_fg": "#ffffff",
            "slider_track": "#d7dde6",
        }
    if theme_name == THEME_GRAY:
        return {
            "bg": "#dfe3e8",
            "panel": "#eceff3",
            "input_bg": "#f4f6f8",
            "fg": "#1f242b",
            "muted_fg": "#3f4752",
            "border": "#aeb7c2",
            "button_bg": "#d2d9e1",
            "button_hover": "#c2cad5",
            "button_fg": "#1f242b",
            "accent": "#475569",
            "accent_fg": "#ffffff",
            "slider_track": "#bbc5d1",
        }
    if theme_name == THEME_SAND:
        return {
            "bg": "#f4ede1",
            "panel": "#fbf7ef",
            "input_bg": "#fffaf1",
            "fg": "#3d3122",
            "muted_fg": "#5f4f3a",
            "border": "#d7c6a7",
            "button_bg": "#efe2ca",
            "button_hover": "#e5d4b7",
            "button_fg": "#3d3122",
            "accent": "#b7791f",
            "accent_fg": "#ffffff",
            "slider_track": "#dbc7a7",
        }
    return {
        "bg": "#181b20",
        "panel": "#232831",
        "input_bg": "#2a303a",
        "fg": "#e6ebf2",
        "muted_fg": "#b6c0ce",
        "border": "#3b4453",
        "button_bg": "#2f3642",
        "button_hover": "#3a4351",
        "button_fg": "#e6ebf2",
        "accent": "#3b82f6",
        "accent_fg": "#ffffff",
        "slider_track": "#4a5568",
    }
