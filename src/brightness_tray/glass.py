from __future__ import annotations

import sys
from typing import Callable

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QParallelAnimationGroup,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QVariantAnimation,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QToolButton,
    QWidget,
)

from .icons import app_glyph_pixmap, glyph_icon
from .themes import Theme, active_theme, get_theme, qcolor

# --------------------------------------------------------------------------------------
# Glass surface painting + Windows 11 backdrop
# --------------------------------------------------------------------------------------

_BLUR_ENABLED = True
GLASS_RADIUS = 18
BLUR_RADIUS = 8  # matches the DWM rounded-corner radius so the blur never pokes out


def set_blur_enabled(enabled: bool) -> None:
    global _BLUR_ENABLED
    _BLUR_ENABLED = bool(enabled)


def blur_enabled() -> bool:
    return _BLUR_ENABLED


def apply_backdrop(widget: QWidget) -> bool:
    """Ask Windows 11 for an acrylic backdrop behind ``widget``.

    Returns True when the OS blur is active. On other platforms (or older Windows builds) the
    window simply stays translucent and our own painted glass takes over.
    """

    active = False
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            build = sys.getwindowsversion().build  # type: ignore[attr-defined]
            hwnd = wintypes.HWND(int(widget.winId()))
            dwm = ctypes.windll.dwmapi  # type: ignore[attr-defined]

            def set_attribute(attribute: int, value: int) -> None:
                data = ctypes.c_int(value)
                dwm.DwmSetWindowAttribute(
                    hwnd, ctypes.c_uint(attribute), ctypes.byref(data), ctypes.sizeof(data)
                )

            class _Margins(ctypes.Structure):
                _fields_ = [
                    ("left", ctypes.c_int),
                    ("right", ctypes.c_int),
                    ("top", ctypes.c_int),
                    ("bottom", ctypes.c_int),
                ]

            set_attribute(20, 1 if active_theme().dark else 0)  # DWMWA_USE_IMMERSIVE_DARK_MODE
            set_attribute(33, 2)  # DWMWA_WINDOW_CORNER_PREFERENCE = round
            if build >= 22621:
                active = _BLUR_ENABLED
                set_attribute(38, 3 if active else 1)  # DWMWA_SYSTEMBACKDROP_TYPE
                margin = -1 if active else 0
                dwm.DwmExtendFrameIntoClientArea(
                    hwnd, ctypes.byref(_Margins(margin, margin, margin, margin))
                )
        except Exception:
            active = False
    widget.setProperty("glassBlur", active)
    widget.update()
    return active


def paint_glass(widget: QWidget, painter: QPainter, theme: Theme | None = None) -> None:
    """Paint the translucent frosted window surface (gradient, glows, edge highlight)."""

    theme = theme or active_theme()
    blurred = bool(widget.property("glassBlur"))
    radius = BLUR_RADIUS if blurred else GLASS_RADIUS
    scale = theme.blur_alpha_scale if blurred else 1.0

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    rect = QRectF(widget.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)

    base = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    top = qcolor(theme.top)
    bottom = qcolor(theme.bottom)
    top.setAlpha(int(top.alpha() * scale))
    bottom.setAlpha(int(bottom.alpha() * scale))
    base.setColorAt(0.0, top)
    base.setColorAt(1.0, bottom)
    painter.fillPath(path, QBrush(base))

    painter.setClipPath(path)
    for center, glow in (
        (QPointF(rect.left() + rect.width() * 0.12, rect.top() - 10), theme.glow_a),
        (QPointF(rect.right() - rect.width() * 0.05, rect.bottom() + 10), theme.glow_b),
    ):
        radial = QRadialGradient(center, max(rect.width(), rect.height()) * 0.85)
        glow_color = qcolor(glow)
        glow_color.setAlpha(int(glow_color.alpha() * (1.0 if not blurred else 1.15)))
        radial.setColorAt(0.0, glow_color)
        radial.setColorAt(1.0, QColor(glow_color.red(), glow_color.green(), glow_color.blue(), 0))
        painter.fillRect(rect, QBrush(radial))

    # Specular line along the top edge.
    shine = QLinearGradient(rect.left(), 0, rect.right(), 0)
    highlight = QColor(255, 255, 255, 120 if theme.dark else 200)
    shine.setColorAt(0.0, QColor(255, 255, 255, 0))
    shine.setColorAt(0.5, highlight)
    shine.setColorAt(1.0, QColor(255, 255, 255, 0))
    painter.fillRect(QRectF(rect.left(), rect.top(), rect.width(), 1.2), QBrush(shine))
    painter.setClipping(False)

    border = QLinearGradient(rect.topLeft(), rect.bottomRight())
    edge = qcolor(theme.border)
    border.setColorAt(0.0, QColor(255, 255, 255, 110 if theme.dark else 220))
    border.setColorAt(0.5, edge)
    border.setColorAt(1.0, QColor(edge.red(), edge.green(), edge.blue(), max(10, edge.alpha() // 2)))
    painter.setPen(QPen(QBrush(border), 1.0))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(path)
    painter.restore()


def animate_in(widget: QWidget, offset: QPoint | None = None) -> None:
    """Fade + slide the window into its current position."""

    offset = offset if offset is not None else QPoint(0, 14)
    end_pos = widget.pos()
    group = QParallelAnimationGroup(widget)

    fade = QPropertyAnimation(widget, b"windowOpacity", group)
    fade.setStartValue(0.0)
    fade.setEndValue(1.0)
    fade.setDuration(150)
    fade.setEasingCurve(QEasingCurve.Type.OutCubic)

    slide = QPropertyAnimation(widget, b"pos", group)
    slide.setStartValue(end_pos + offset)
    slide.setEndValue(end_pos)
    slide.setDuration(210)
    slide.setEasingCurve(QEasingCurve.Type.OutCubic)

    group.addAnimation(fade)
    group.addAnimation(slide)
    widget.setWindowOpacity(0.0)
    widget.move(end_pos + offset)
    previous = getattr(widget, "_intro_animation", None)
    if previous is not None:
        previous.stop()
    widget._intro_animation = group  # type: ignore[attr-defined]
    group.finished.connect(lambda: widget.setWindowOpacity(1.0))
    group.start()


# --------------------------------------------------------------------------------------
# Brightness colour ramp + custom slider
# --------------------------------------------------------------------------------------

_BRIGHTNESS_STOPS: tuple[tuple[float, str], ...] = (
    (0.00, "#5b4bd6"),  # deep indigo
    (0.34, "#38a9ff"),  # sky
    (0.56, "#4fe0c4"),  # mint (keeps blue -> gold from turning grey)
    (0.78, "#ffd36e"),  # warm gold
    (1.00, "#fff1b8"),  # sunlight
)


def brightness_color(ratio: float) -> QColor:
    ratio = max(0.0, min(1.0, float(ratio)))
    for (start_pos, start_hex), (end_pos, end_hex) in zip(_BRIGHTNESS_STOPS, _BRIGHTNESS_STOPS[1:]):
        if ratio <= end_pos:
            span = max(1e-6, end_pos - start_pos)
            mix = (ratio - start_pos) / span
            start = QColor(start_hex)
            end = QColor(end_hex)
            return QColor(
                round(start.red() + (end.red() - start.red()) * mix),
                round(start.green() + (end.green() - start.green()) * mix),
                round(start.blue() + (end.blue() - start.blue()) * mix),
            )
    return QColor(_BRIGHTNESS_STOPS[-1][1])


class GlowSlider(QSlider):
    """Horizontal slider with a gradient fill, a glowing thumb and click-to-jump."""

    _PAD = 16.0

    def __init__(
        self,
        orientation: Qt.Orientation = Qt.Orientation.Horizontal,
        brightness_scale: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(orientation, parent)
        self._brightness_scale = brightness_scale
        self._hover = 0.0
        self.setMinimumHeight(34)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSingleStep(1)
        self.setPageStep(5)
        self._hover_anim = QVariantAnimation(self)
        self._hover_anim.setDuration(150)
        self._hover_anim.valueChanged.connect(self._set_hover)

    def sizeHint(self) -> QSize:  # type: ignore[override]
        return QSize(180, 34)

    def _set_hover(self, value: object) -> None:
        self._hover = float(value)  # type: ignore[arg-type]
        self.update()

    def _animate_hover(self, target: float) -> None:
        self._hover_anim.stop()
        self._hover_anim.setStartValue(self._hover)
        self._hover_anim.setEndValue(target)
        self._hover_anim.start()

    def enterEvent(self, event) -> None:  # type: ignore[override]
        self._animate_hover(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self._animate_hover(0.0)
        super().leaveEvent(event)

    # -- geometry -------------------------------------------------------------------
    def _groove(self) -> QRectF:
        return QRectF(self._PAD, self.height() / 2 - 4, max(1.0, self.width() - 2 * self._PAD), 8)

    def _ratio(self) -> float:
        span = max(1, self.maximum() - self.minimum())
        return (self.value() - self.minimum()) / span

    def _value_from_x(self, x_pos: float) -> int:
        groove = self._groove()
        ratio = max(0.0, min(1.0, (x_pos - groove.left()) / groove.width()))
        return int(round(self.minimum() + ratio * (self.maximum() - self.minimum())))

    # -- input ----------------------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() != Qt.MouseButton.LeftButton or not self.isEnabled():
            super().mousePressEvent(event)
            return
        self.setSliderDown(True)
        self.setValue(self._value_from_x(event.position().x()))
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if self.isSliderDown():
            self.setValue(self._value_from_x(event.position().x()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if self.isSliderDown() and event.button() == Qt.MouseButton.LeftButton:
            self.setSliderDown(False)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # -- painting -------------------------------------------------------------------
    def _fill_color_at(self, ratio: float) -> QColor:
        if self._brightness_scale:
            return brightness_color(ratio)
        theme = active_theme()
        start = QColor(theme.accent)
        end = QColor(theme.accent2)
        return QColor(
            round(start.red() + (end.red() - start.red()) * ratio),
            round(start.green() + (end.green() - start.green()) * ratio),
            round(start.blue() + (end.blue() - start.blue()) * ratio),
        )

    def paintEvent(self, event) -> None:  # type: ignore[override]
        del event
        theme = active_theme()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if not self.isEnabled():
            painter.setOpacity(0.45)

        groove = self._groove()
        ratio = self._ratio()
        handle_x = groove.left() + groove.width() * ratio
        center = QPointF(handle_x, groove.center().y())

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(qcolor(theme.track)))
        painter.drawRoundedRect(groove, 4, 4)

        fill_rect = QRectF(groove.left(), groove.top(), max(0.0, handle_x - groove.left()), groove.height())
        if fill_rect.width() > 0.5:
            gradient = QLinearGradient(groove.left(), 0, groove.right(), 0)
            for stop in (0.0, 0.25, 0.5, 0.75, 1.0):
                gradient.setColorAt(stop, self._fill_color_at(stop))
            painter.save()
            clip = QPainterPath()
            clip.addRoundedRect(groove, 4, 4)
            painter.setClipPath(clip)
            painter.fillRect(fill_rect.adjusted(0, 0, 4, 0), QBrush(gradient))
            painter.restore()

        thumb_color = self._fill_color_at(ratio)
        radius = 8.5 + 1.5 * self._hover + (1.0 if self.isSliderDown() else 0.0)

        halo_radius = min(radius * 2.6, self._PAD - 0.5, self.height() / 2.0 - 0.5)
        halo = QRadialGradient(center, halo_radius)
        halo_color = QColor(thumb_color)
        halo_color.setAlpha(int(70 + 80 * self._hover))
        halo.setColorAt(0.0, halo_color)
        halo.setColorAt(1.0, QColor(thumb_color.red(), thumb_color.green(), thumb_color.blue(), 0))
        painter.setBrush(QBrush(halo))
        painter.drawEllipse(center, halo_radius, halo_radius)

        painter.setPen(QPen(QColor(0, 0, 0, 55), 1))
        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.drawEllipse(center, radius, radius)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(thumb_color))
        painter.drawEllipse(center, radius - 4.0, radius - 4.0)
        painter.end()


# --------------------------------------------------------------------------------------
# Title bar for frameless dialogs
# --------------------------------------------------------------------------------------


class GlassTitleBar(QWidget):
    def __init__(
        self,
        title: str,
        on_close: Callable[[], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("glassTitleBar")
        self.setFixedHeight(46)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 8, 10, 4)
        layout.setSpacing(10)

        self.glyph = QLabel()
        self.glyph.setPixmap(app_glyph_pixmap(44))
        self.glyph.setScaledContents(True)
        self.glyph.setFixedSize(22, 22)
        layout.addWidget(self.glyph)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("windowTitle")
        layout.addWidget(self.title_label)
        layout.addStretch(1)

        self.close_button = QToolButton()
        self.close_button.setObjectName("closeButton")
        self.close_button.setToolTip("Close")
        self.close_button.setFixedSize(30, 30)
        self.close_button.clicked.connect(lambda _checked=False: on_close())
        layout.addWidget(self.close_button)
        self._refresh_icons()

    def set_title(self, title: str) -> None:
        self.title_label.setText(title)

    def _refresh_icons(self) -> None:
        self.close_button.setIcon(glyph_icon("close", active_theme().fg))
        self.close_button.setIconSize(QSize(14, 14))

    def event(self, event) -> bool:  # type: ignore[override]
        if event.type() == QEvent.Type.StyleChange:
            self._refresh_icons()
        return super().event(event)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None and handle.startSystemMove():
                event.accept()
                return
        super().mousePressEvent(event)


# --------------------------------------------------------------------------------------
# Theme swatch button
# --------------------------------------------------------------------------------------


class ThemeSwatchButton(QPushButton):
    def __init__(self, theme_key: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme_key = theme_key
        self.setCheckable(True)
        self.setFixedSize(104, 62)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(get_theme(theme_key).label)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        del event
        theme = get_theme(self._theme_key)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(3, 3, -3, -3)

        path = QPainterPath()
        path.addRoundedRect(rect, 13, 13)
        base = QLinearGradient(rect.topLeft(), rect.bottomRight())
        top = qcolor(theme.top, 255)
        bottom = qcolor(theme.bottom, 255)
        base.setColorAt(0.0, top)
        base.setColorAt(1.0, bottom)
        painter.fillPath(path, QBrush(base))

        painter.save()
        painter.setClipPath(path)
        for center, glow in (
            (QPointF(rect.left() + 12, rect.top()), theme.glow_a),
            (QPointF(rect.right(), rect.bottom()), theme.glow_b),
        ):
            radial = QRadialGradient(center, rect.width() * 0.8)
            glow_color = qcolor(glow, min(255, glow[3] * 3))
            radial.setColorAt(0.0, glow_color)
            radial.setColorAt(1.0, QColor(glow_color.red(), glow_color.green(), glow_color.blue(), 0))
            painter.fillRect(rect, QBrush(radial))
        painter.restore()

        # Accent dots.
        painter.setPen(Qt.PenStyle.NoPen)
        for index, color in enumerate((theme.accent, theme.accent2)):
            painter.setBrush(QBrush(QColor(color)))
            painter.drawEllipse(QPointF(rect.left() + 15 + index * 13, rect.top() + 14), 4.5, 4.5)

        painter.setPen(QPen(QColor(theme.fg)))
        label_font = painter.font()
        label_font.setPointSizeF(8.5)
        label_font.setWeight(label_font.Weight.DemiBold)
        painter.setFont(label_font)
        painter.drawText(
            rect.adjusted(12, 0, -8, -7),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
            theme.label,
        )

        if self.isChecked():
            ring = QLinearGradient(rect.topLeft(), rect.bottomRight())
            ring.setColorAt(0.0, QColor(active_theme().accent))
            ring.setColorAt(1.0, QColor(active_theme().accent2))
            painter.setPen(QPen(QBrush(ring), 2.4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(-1, -1, 1, 1), 14, 14)
        else:
            hover_alpha = 120 if self.underMouse() else 50
            painter.setPen(QPen(QColor(128, 128, 128, hover_alpha), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect, 13, 13)
        painter.end()
