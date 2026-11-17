from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)

_GRID = 64


def _canvas(size: int) -> tuple[QPixmap, QPainter]:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    painter.scale(size / _GRID, size / _GRID)
    return pixmap, painter


def _pen(color: QColor, width: float = 5.0) -> QPen:
    pen = QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _draw_glyph(painter: QPainter, kind: str, color: QColor) -> None:
    painter.setPen(_pen(color))
    painter.setBrush(Qt.BrushStyle.NoBrush)

    if kind == "refresh":
        rect = QRectF(14, 14, 36, 36)
        painter.drawArc(rect, 40 * 16, 280 * 16)
        # Arrow head at the arc start (upper right).
        tip = QPointF(32 + 18 * math.cos(math.radians(40)), 32 - 18 * math.sin(math.radians(40)))
        painter.drawLine(tip, QPointF(tip.x() + 1, tip.y() - 11))
        painter.drawLine(tip, QPointF(tip.x() - 11, tip.y() - 1))
    elif kind == "settings":
        for y, knob in ((17, 24), (32, 42), (47, 28)):
            painter.drawLine(QPointF(10, y), QPointF(54, y))
            painter.setBrush(QBrush(color))
            painter.drawEllipse(QPointF(knob, y), 6, 6)
            painter.setBrush(Qt.BrushStyle.NoBrush)
    elif kind == "close":
        painter.drawLine(QPointF(18, 18), QPointF(46, 46))
        painter.drawLine(QPointF(46, 18), QPointF(18, 46))
    elif kind == "monitor":
        painter.drawRoundedRect(QRectF(9, 11, 46, 31), 6, 6)
        painter.drawLine(QPointF(32, 42), QPointF(32, 52))
        painter.drawLine(QPointF(21, 53), QPointF(43, 53))
    elif kind == "laptop":
        painter.drawRoundedRect(QRectF(14, 13, 36, 27), 5, 5)
        painter.drawLine(QPointF(7, 51), QPointF(57, 51))
    elif kind == "sun":
        painter.drawEllipse(QPointF(32, 32), 10, 10)
        for index in range(8):
            angle = math.tau * index / 8.0
            painter.drawLine(
                QPointF(32 + math.cos(angle) * 17, 32 + math.sin(angle) * 17),
                QPointF(32 + math.cos(angle) * 24, 32 + math.sin(angle) * 24),
            )
    elif kind == "link":
        painter.drawRoundedRect(QRectF(8, 22, 28, 20), 10, 10)
        painter.drawRoundedRect(QRectF(28, 22, 28, 20), 10, 10)


def glyph_icon(kind: str, color: QColor | str) -> QIcon:
    """Crisp vector-style glyph rendered at several sizes, tinted with ``color``."""
    tint = QColor(color)
    icon = QIcon()
    for size in (16, 24, 32, 48, 64):
        pixmap, painter = _canvas(size)
        _draw_glyph(painter, kind, tint)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def glyph_pixmap(kind: str, color: QColor | str, size: int) -> QPixmap:
    pixmap, painter = _canvas(size)
    _draw_glyph(painter, kind, QColor(color))
    painter.end()
    return pixmap


def _draw_app_glyph(painter: QPainter) -> None:
    # Gradient tile.
    tile = QPainterPath()
    tile.addRoundedRect(QRectF(2, 2, 60, 60), 17, 17)
    gradient = QLinearGradient(0, 0, 64, 64)
    gradient.setColorAt(0.0, QColor("#7c6bff"))
    gradient.setColorAt(0.55, QColor("#3b9cff"))
    gradient.setColorAt(1.0, QColor("#2ee6c5"))
    painter.fillPath(tile, QBrush(gradient))

    # Soft highlight.
    shine = QRadialGradient(QPointF(20, 10), 40)
    shine.setColorAt(0.0, QColor(255, 255, 255, 110))
    shine.setColorAt(1.0, QColor(255, 255, 255, 0))
    painter.fillPath(tile, QBrush(shine))

    # Monitor outline.
    painter.setPen(_pen(QColor(255, 255, 255, 245), 4.6))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(QRectF(11, 14, 42, 29), 6, 6)
    painter.drawLine(QPointF(32, 43), QPointF(32, 50))
    painter.drawLine(QPointF(23, 51), QPointF(41, 51))

    # Sun inside the monitor.
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor("#fff3b0")))
    painter.drawEllipse(QPointF(32, 28.5), 5.2, 5.2)
    painter.setPen(_pen(QColor("#fff3b0"), 2.4))
    for index in range(8):
        angle = math.tau * index / 8.0
        painter.drawLine(
            QPointF(32 + math.cos(angle) * 8.4, 28.5 + math.sin(angle) * 8.4),
            QPointF(32 + math.cos(angle) * 10.6, 28.5 + math.sin(angle) * 10.6),
        )


def app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64, 128):
        pixmap, painter = _canvas(size)
        _draw_app_glyph(painter)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def app_glyph_pixmap(size: int) -> QPixmap:
    pixmap, painter = _canvas(size)
    _draw_app_glyph(painter)
    painter.end()
    return pixmap
