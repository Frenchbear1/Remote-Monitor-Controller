from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import math
import time as wall_time
from zoneinfo import ZoneInfo

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QRegularExpression, QSize, QTimer, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
    QRegularExpressionValidator,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QGraphicsOpacityEffect,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from astral import LocationInfo
from astral.sun import sun
from tzlocal import get_localzone_name

from .ambient_light import AmbientLightService
from .brightness_service import (
    BrightnessService,
    BrightnessWriter,
    MonitorHandle,
    PictureControl,
    com_initialized,
)
from .config_store import ConfigStore
from .glass import (
    GlassTitleBar,
    GlowSlider,
    ThemeSwatchButton,
    animate_in,
    apply_backdrop,
    brightness_color,
    paint_glass,
    set_blur_enabled,
)
from .icons import glyph_icon
from .location import LocationContext, detect_location_context_from_ip
from .models import AppConfig, ScheduleRule, clamp_brightness, default_schedule_rules
from .themes import (
    THEME_OPTIONS,
    active_theme,
    build_stylesheet,
    normalize_theme_name,
    qcolor,
    set_active_theme,
)
from .workers import run_async

POPUP_EDGE_MARGIN_PX = 12
POPUP_TASKBAR_CLEARANCE_PX = 6


def apply_app_theme(theme_name: str) -> None:
    """Switch the active theme everywhere (stylesheet + custom-painted widgets)."""
    app = QApplication.instance()
    normalized = set_active_theme(theme_name)
    if app is not None:
        app.setStyleSheet(build_stylesheet(normalized.key))
        for widget in app.topLevelWidgets():
            widget.update()


def _format_time_from_minutes(minutes: int | float) -> str:
    bounded_minutes = int(round(float(minutes))) % 1440
    return f"{bounded_minutes // 60:02d}:{bounded_minutes % 60:02d}"


def _parse_time_to_minutes(value: str | None) -> int | None:
    text = str(value or "").strip()
    if ":" not in text:
        return None
    parts = text.split(":", 1)
    if len(parts) != 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except (TypeError, ValueError):
        return None
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return hour * 60 + minute


def _round_to_step(value: float, step: int) -> int:
    if step <= 0:
        return int(round(value))
    return int(round(value / step) * step)


def _brightness_handle_color(value: int | float) -> QColor:
    return brightness_color(float(value) / 100.0)


def _apply_brightness_slider_style(slider: QSlider, value: int | float) -> None:
    del value
    slider.update()


def _brightness_value_label_style(value: int | float) -> str:
    color = _brightness_handle_color(value)
    if not active_theme().dark:
        # The sunlit end of the ramp is too pale to read on light glass.
        color = color.darker(150 if value > 60 else 105)
    return f"font-weight: 600; color: {color.name()};"


def _big_value_style(value: int | float) -> str:
    color = _brightness_handle_color(value)
    if not active_theme().dark:
        color = color.darker(150 if value > 60 else 105)
    return f"color: {color.name()};"


def _bottom_right_popup_position(widget: QWidget, available) -> QPoint:
    """Anchor the popup to the corner next to the taskbar / tray."""
    frame = widget.frameGeometry()
    screen = widget.screen() or QGuiApplication.primaryScreen()
    full = screen.geometry() if screen is not None else available
    margin = POPUP_EDGE_MARGIN_PX
    clearance = POPUP_TASKBAR_CLEARANCE_PX

    at_left = available.left() > full.left()
    at_top = available.top() > full.top()

    if at_left:
        target_x = available.left() + clearance
    else:
        target_x = available.right() - frame.width() - (clearance if available.right() < full.right() else margin) + 1
    if at_top:
        target_y = available.top() + clearance
    else:
        target_y = available.bottom() - frame.height() - (clearance if available.bottom() < full.bottom() else margin) + 1

    target_x = max(available.left(), min(target_x, available.right() - frame.width() + 1))
    target_y = max(available.top(), min(target_y, available.bottom() - frame.height() + 1))
    return QPoint(target_x, target_y)


def _slide_offset_for(widget: QWidget) -> QPoint:
    screen = widget.screen() or QGuiApplication.primaryScreen()
    if screen is None:
        return QPoint(0, 14)
    available = screen.availableGeometry()
    full = screen.geometry()
    if available.left() > full.left():
        return QPoint(-14, 0)
    if available.right() < full.right():
        return QPoint(14, 0)
    if available.top() > full.top():
        return QPoint(0, -14)
    return QPoint(0, 14)


def _apply_native_rounded_corners(widget: QWidget) -> None:
    apply_backdrop(widget)


def _apply_rounded_popup_chrome(widget: QWidget, object_name: str) -> None:
    widget.setObjectName(object_name)
    widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    widget.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)


def _flat_tool_button(icon_kind: str, tooltip: str) -> QToolButton:
    button = QToolButton()
    button.setToolTip(tooltip)
    button.setIconSize(QSize(18, 18))
    button.setFixedSize(34, 34)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setProperty("iconKind", icon_kind)
    button.setIcon(glyph_icon(icon_kind, active_theme().fg))
    return button


def _refresh_flat_button_icon(button: QToolButton) -> None:
    kind = button.property("iconKind")
    if kind:
        button.setIcon(glyph_icon(str(kind), active_theme().fg))


def _elided(label: QLabel, text: str, width: int) -> None:
    label.setToolTip(text)
    label.setText(QFontMetrics(label.font()).elidedText(text, Qt.TextElideMode.ElideRight, width))


class MonitorSliderRow(QWidget):
    brightness_changed = Signal(str, int)

    def __init__(self, monitor: MonitorHandle, initial_value: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.monitor = monitor

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        header = QHBoxLayout()
        header.setContentsMargins(2, 0, 2, 0)
        header.setSpacing(8)
        is_builtin = "wmi" in (monitor.method_name or "").lower()
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(18, 18)
        self.icon_label.setScaledContents(True)
        self._icon_kind = "laptop" if is_builtin else "monitor"
        self._refresh_icon()
        header.addWidget(self.icon_label)

        self.name_label = QLabel()
        _elided(self.name_label, monitor.name, 270)
        header.addWidget(self.name_label, stretch=1)

        self.value_label = QLabel(f"{clamp_brightness(initial_value)}%")
        self.value_label.setMinimumWidth(44)
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self.value_label)
        layout.addLayout(header)

        self.slider = GlowSlider(Qt.Orientation.Horizontal, brightness_scale=True)
        self.slider.setRange(0, 100)
        self.slider.setValue(clamp_brightness(initial_value))
        layout.addWidget(self.slider)
        self.value_label.setStyleSheet(_brightness_value_label_style(self.slider.value()))

        self.slider.valueChanged.connect(self._handle_slider_change)

    def _refresh_icon(self) -> None:
        self.icon_label.setPixmap(glyph_icon(self._icon_kind, active_theme().muted).pixmap(36, 36))

    def event(self, event) -> bool:  # type: ignore[override]
        if event.type() == QEvent.Type.StyleChange:
            self._refresh_icon()
        return super().event(event)

    def set_value(self, value: int) -> None:
        bounded = clamp_brightness(value)
        self.slider.blockSignals(True)
        self.slider.setValue(bounded)
        self.slider.blockSignals(False)
        self.value_label.setText(f"{bounded}%")
        self.value_label.setStyleSheet(_brightness_value_label_style(bounded))

    def _handle_slider_change(self, value: int) -> None:
        bounded = clamp_brightness(value)
        self.value_label.setText(f"{bounded}%")
        self.value_label.setStyleSheet(_brightness_value_label_style(bounded))
        self.brightness_changed.emit(self.monitor.key, bounded)


class PictureControlSliderRow(QWidget):
    control_changed = Signal(int, int)
    _DRAG_EMIT_INTERVAL_MS = 180

    def __init__(self, control: PictureControl, parent: QWidget | None = None):
        super().__init__(parent)
        self.control = control
        self._last_emitted_value = int(control.value)
        self._pending_drag_emit = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.name_label = QLabel(control.name)
        self.name_label.setToolTip(f"VCP code 0x{control.code:02X}")
        self.name_label.setMinimumWidth(130)
        layout.addWidget(self.name_label)

        self.slider = GlowSlider(Qt.Orientation.Horizontal, brightness_scale=False)
        self.slider.setRange(control.minimum, control.maximum)
        self.slider.setValue(control.value)
        layout.addWidget(self.slider, stretch=1)

        self.value_label = QLabel(str(self.slider.value()))
        self.value_label.setMinimumWidth(42)
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.value_label)

        self._drag_apply_timer = QTimer(self)
        self._drag_apply_timer.setInterval(self._DRAG_EMIT_INTERVAL_MS)
        self._drag_apply_timer.timeout.connect(self._handle_drag_tick)

        self.slider.valueChanged.connect(self._handle_slider_change)
        self.slider.sliderReleased.connect(self._emit_committed_value)

    def set_value(self, value: int) -> None:
        bounded = max(self.slider.minimum(), min(self.slider.maximum(), int(value)))
        self.slider.blockSignals(True)
        self.slider.setValue(bounded)
        self.slider.blockSignals(False)
        self.value_label.setText(str(bounded))
        self._last_emitted_value = bounded

    def _handle_slider_change(self, value: int) -> None:
        bounded = max(self.slider.minimum(), min(self.slider.maximum(), int(value)))
        self.value_label.setText(str(bounded))
        if self.slider.isSliderDown():
            self._pending_drag_emit = True
            if not self._drag_apply_timer.isActive():
                self._drag_apply_timer.start()
            return

        self._pending_drag_emit = False
        if self._drag_apply_timer.isActive():
            self._drag_apply_timer.stop()
        self._emit_if_changed(bounded)

    def _handle_drag_tick(self) -> None:
        if not self.slider.isSliderDown():
            self._drag_apply_timer.stop()
            return
        if not self._pending_drag_emit:
            return
        self._pending_drag_emit = False
        current_value = max(self.slider.minimum(), min(self.slider.maximum(), self.slider.value()))
        self._emit_if_changed(current_value)

    def _emit_committed_value(self) -> None:
        self._pending_drag_emit = False
        if self._drag_apply_timer.isActive():
            self._drag_apply_timer.stop()
        value = max(self.slider.minimum(), min(self.slider.maximum(), self.slider.value()))
        self._emit_if_changed(value)

    def _emit_if_changed(self, value: int) -> None:
        bounded = max(self.slider.minimum(), min(self.slider.maximum(), int(value)))
        if bounded == self._last_emitted_value:
            return
        self._last_emitted_value = bounded
        self.control_changed.emit(self.control.code, bounded)


class PictureControlsDialog(QDialog):
    def __init__(
        self,
        service: BrightnessService,
        monitors: list[MonitorHandle],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.service = service
        self.monitors = monitors

        self.setWindowTitle("Monitor Picture Controls")
        self.setMinimumWidth(560)
        self.resize(620, 420)
        _apply_rounded_popup_chrome(self, "pictureControlsDialog")

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 12)
        root_layout.setSpacing(8)

        self.title_bar = GlassTitleBar("Picture Controls", self.reject)
        root_layout.addWidget(self.title_bar)

        body_margin = 16
        toolbar_layout = QHBoxLayout()
        toolbar_layout.setContentsMargins(body_margin, 0, body_margin, 0)
        toolbar_layout.setSpacing(8)
        self.refresh_button = QPushButton("Deep scan for controls")
        self.refresh_button.setToolTip(
            "Ask each monitor which picture settings it supports (slower, but finds more)."
        )
        toolbar_layout.addWidget(self.refresh_button)
        toolbar_layout.addStretch(1)
        root_layout.addLayout(toolbar_layout)

        self.content_scroll = QScrollArea()
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_row = QHBoxLayout()
        scroll_row.setContentsMargins(body_margin, 0, body_margin - 6, 0)
        scroll_row.addWidget(self.content_scroll)
        root_layout.addLayout(scroll_row, stretch=1)

        self.content_widget = QWidget()
        self.content_layout = QVBoxLayout(self.content_widget)
        self.content_layout.setContentsMargins(0, 0, 6, 0)
        self.content_layout.setSpacing(6)
        self.content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.content_scroll.setWidget(self.content_widget)

        self.status_label = QLabel("")
        self.status_label.setObjectName("muted")
        self.status_label.setWordWrap(True)
        footer_layout = QHBoxLayout()
        footer_layout.setContentsMargins(body_margin, 0, body_margin, 0)
        footer_layout.setSpacing(8)
        footer_layout.addWidget(self.status_label, stretch=1)
        self.close_button = QPushButton("Done")
        self.close_button.setObjectName("primary")
        self.close_button.clicked.connect(self.reject)
        footer_layout.addWidget(self.close_button)
        root_layout.addLayout(footer_layout)

        self.refresh_button.clicked.connect(
            lambda _checked=False: self._reload_controls(force_refresh=True)
        )
        self._reload_controls(force_refresh=False)

    def _content_table_height(self) -> int:
        self.content_layout.activate()
        self.content_widget.adjustSize()
        return max(1, self.content_widget.sizeHint().height())

    def _max_dialog_height(self) -> int:
        available = self._available_geometry()
        if available is None:
            return 920
        return max(320, available.height() - 36)

    def _refresh_dialog_size(self) -> None:
        self.content_widget.adjustSize()
        layout = self.layout()
        if layout is None:
            return
        margins = layout.contentsMargins()
        spacing = max(0, layout.spacing())
        static_height = (
            margins.top()
            + margins.bottom()
            + self.title_bar.height()
            + self.refresh_button.sizeHint().height()
            + max(self.status_label.sizeHint().height(), self.close_button.sizeHint().height())
            + (spacing * 3)
        )
        preferred_content = self._content_table_height()
        max_height = self._max_dialog_height()
        max_content_height = max(1, max_height - static_height)
        content_height = min(preferred_content, max_content_height)
        self.content_scroll.setFixedHeight(content_height)
        self.content_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            if preferred_content <= max_content_height
            else Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )

        target_width = max(self.minimumWidth(), self.sizeHint().width())
        self.resize(target_width, static_height + content_height)
        if self.isVisible():
            self._position_bottom_right()

    def _available_geometry(self):
        screen = self.screen()
        if screen is None and self.parentWidget() is not None:
            screen = self.parentWidget().screen()
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is None:
            return None
        return screen.availableGeometry()

    def _position_bottom_right(self) -> None:
        available = self._available_geometry()
        if available is None:
            return
        self.move(_bottom_right_popup_position(self, available))

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        _apply_native_rounded_corners(self)
        QTimer.singleShot(0, self._refresh_dialog_size)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        del event
        painter = QPainter(self)
        paint_glass(self, painter)
        painter.end()

    def _clear_content(self) -> None:
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _reload_controls(self, force_refresh: bool) -> None:
        self._clear_content()

        if not self.monitors:
            self.status_label.setText("No monitors are currently available.")
            return

        monitor_count_with_controls = 0
        for monitor in self.monitors:
            group = QGroupBox(monitor.name)
            group.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
            group_layout = QVBoxLayout(group)
            group_layout.setContentsMargins(6, 6, 6, 4)
            group_layout.setSpacing(8)

            try:
                controls = self.service.list_picture_controls(
                    monitor,
                    use_cache=not force_refresh,
                    include_capabilities=force_refresh,
                )
            except Exception as error:
                controls = []
                failure_label = QLabel(f"Could not read controls: {error}")
                failure_label.setObjectName("muted")
                failure_label.setWordWrap(True)
                group_layout.addWidget(failure_label)

            if not controls:
                unsupported_label = QLabel(
                    "No supported picture controls were reported by this display."
                )
                unsupported_label.setObjectName("muted")
                unsupported_label.setWordWrap(True)
                group_layout.addWidget(unsupported_label)
            else:
                monitor_count_with_controls += 1
                for control in controls:
                    row = PictureControlSliderRow(control, parent=group)
                    row.control_changed.connect(
                        lambda code, value, monitor_ref=monitor, row_ref=row: self._apply_control_value(
                            monitor_ref,
                            row_ref,
                            code,
                            value,
                        )
                    )
                    group_layout.addWidget(row)

            self.content_layout.addWidget(group)

        if monitor_count_with_controls == 0:
            self.status_label.setText(
                "No picture sliders are available. "
                "Verify DDC/CI is enabled in the monitor OSD."
            )
        else:
            self.status_label.setText(
                f"Loaded picture controls for {monitor_count_with_controls} of "
                f"{len(self.monitors)} monitor(s)."
            )
        self._refresh_dialog_size()

    def _apply_control_value(
        self,
        monitor: MonitorHandle,
        row: PictureControlSliderRow,
        code: int,
        value: int,
    ) -> None:
        success = self.service.set_picture_control(monitor, code, value)
        if success:
            return

        self.status_label.setText(
            f"{monitor.name}: could not update {row.control.name}."
        )


class ScheduleGraphWidget(QWidget):
    edit_started = Signal()
    rules_changed = Signal(object)
    selection_changed = Signal(int)

    _LEFT_MARGIN = 54
    _RIGHT_MARGIN = 18
    _TOP_MARGIN = 30
    _BOTTOM_MARGIN = 32
    _HIT_RADIUS = 12
    _DRAG_MINUTE_STEP = 5
    _STARS = tuple(
        (((index * 0.61803398875) % 1.0), ((index * 0.7548776662) % 1.0), 0.9 + (index % 3) * 0.35)
        for index in range(1, 46)
    )

    def __init__(
        self,
        target_choices: list[tuple[str, str]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setMinimumHeight(232)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setToolTip("Double-click the graph to add a point. Drag points to change time and brightness.")

        self._rules: list[ScheduleRule] = []
        self._target_labels = {value: label for value, label in target_choices}
        self._selected_index = -1
        self._drag_index: int | None = None
        self._drag_edit_started = False
        self._hover_index = -1
        self._sunrise_minutes: int | None = None
        self._sunset_minutes: int | None = None
        self._now_minutes: float | None = None
        self._gradual = True

    def sizeHint(self) -> QSize:  # type: ignore[override]
        return QSize(720, 240)

    def set_rules(self, rules: list[ScheduleRule], selected_index: int | None = None) -> None:
        self._rules = [deepcopy(rule) for rule in rules]
        if selected_index is None:
            selected_index = self._selected_index
        if selected_index < 0 or selected_index >= len(self._rules):
            selected_index = 0 if self._rules else -1
        selection_changed = selected_index != self._selected_index
        self._selected_index = selected_index
        self.update()
        if selection_changed:
            self.selection_changed.emit(self._selected_index)

    def rules(self) -> list[ScheduleRule]:
        return [deepcopy(rule) for rule in self._rules]

    def selected_index(self) -> int:
        return self._selected_index

    def set_selected_index(self, index: int) -> None:
        if index < 0 or index >= len(self._rules):
            index = -1
        if index == self._selected_index:
            return
        self._selected_index = index
        self.selection_changed.emit(index)
        self.update()

    def set_sun_times(self, sunrise_time: datetime | None, sunset_time: datetime | None) -> None:
        self._sunrise_minutes = self._datetime_to_minutes(sunrise_time)
        self._sunset_minutes = self._datetime_to_minutes(sunset_time)
        self.update()

    def set_current_time(self, current_time: datetime | None) -> None:
        if current_time is None:
            self._now_minutes = None
        else:
            self._now_minutes = (
                current_time.hour * 60
                + current_time.minute
                + current_time.second / 60.0
            )
        self.update()

    def set_gradual(self, gradual: bool) -> None:
        if self._gradual == gradual:
            return
        self._gradual = gradual
        self.update()

    @staticmethod
    def _datetime_to_minutes(value: datetime | None) -> int | None:
        if value is None:
            return None
        return value.hour * 60 + value.minute

    @staticmethod
    def _target_short_label(target: str) -> str:
        if target == "display1":
            return "D1"
        if target == "display2":
            return "D2"
        return "Both"

    @staticmethod
    def _target_dot_label(target: str) -> str:
        if target == "display1":
            return "D1"
        if target == "display2":
            return "D2"
        return "B"

    @staticmethod
    def _target_color(target: str) -> QColor:
        if target == "display1":
            return QColor("#4d9bff")
        if target == "display2":
            return QColor("#3ddc97")
        return QColor("#ffb347")

    def _plot_rect(self) -> QRectF:
        return QRectF(
            self._LEFT_MARGIN,
            self._TOP_MARGIN,
            max(120, self.width() - self._LEFT_MARGIN - self._RIGHT_MARGIN),
            max(120, self.height() - self._TOP_MARGIN - self._BOTTOM_MARGIN),
        )

    def _effective_sunrise_minutes(self) -> int:
        return self._sunrise_minutes if self._sunrise_minutes is not None else 6 * 60

    def _effective_sunset_minutes(self) -> int:
        return self._sunset_minutes if self._sunset_minutes is not None else 18 * 60

    def _base_anchor_minutes(self, anchor: str) -> int:
        if anchor == "sunset":
            return self._effective_sunset_minutes()
        return self._effective_sunrise_minutes()

    def _rule_minutes(self, rule: ScheduleRule) -> int:
        if rule.anchor == "time":
            parsed = _parse_time_to_minutes(rule.specific_time)
            return parsed if parsed is not None else 12 * 60
        return self._base_anchor_minutes(rule.anchor) + int(rule.offset_minutes)

    def clock_minutes_for_rule(self, rule: ScheduleRule) -> int:
        return self._rule_minutes(rule) % 1440

    def offset_for_anchor_time(
        self,
        anchor: str,
        clock_minutes: int,
        previous_offset: int = 0,
    ) -> int:
        anchor_minutes = self._base_anchor_minutes(anchor)
        return self._offset_for_drag(anchor_minutes, clock_minutes, previous_offset)

    def _x_from_minutes(self, minutes: int | float, plot: QRectF) -> float:
        bounded = float(minutes) % 1440.0
        return plot.left() + (bounded / 1440.0) * plot.width()

    def _minutes_from_x(self, x_pos: float, plot: QRectF) -> int:
        ratio = (x_pos - plot.left()) / max(1.0, plot.width())
        minutes = max(0.0, min(1439.0, ratio * 1440.0))
        return max(0, min(1439, _round_to_step(minutes, self._DRAG_MINUTE_STEP)))

    def _y_from_brightness(self, brightness: int | float, plot: QRectF) -> float:
        bounded = max(0.0, min(100.0, float(brightness)))
        return plot.top() + ((100.0 - bounded) / 100.0) * plot.height()

    def _brightness_from_y(self, y_pos: float, plot: QRectF) -> int:
        ratio = (y_pos - plot.top()) / max(1.0, plot.height())
        brightness = 100.0 - max(0.0, min(1.0, ratio)) * 100.0
        return clamp_brightness(brightness)

    def _point_positions(self, plot: QRectF) -> list[tuple[int, QPointF, ScheduleRule]]:
        points: list[tuple[int, QPointF, ScheduleRule]] = []
        for index, rule in enumerate(self._rules):
            points.append(
                (
                    index,
                    QPointF(
                        self._x_from_minutes(self._rule_minutes(rule), plot),
                        self._y_from_brightness(rule.brightness, plot),
                    ),
                    rule,
                )
            )
        return points

    def _hit_test(self, pos: QPointF) -> int:
        plot = self._plot_rect()
        best_index = -1
        best_distance = float(self._HIT_RADIUS)
        for index, point, _rule in self._point_positions(plot):
            distance = math.hypot(point.x() - pos.x(), point.y() - pos.y())
            if distance <= best_distance:
                best_distance = distance
                best_index = index
        return best_index

    def _event_position(self, event) -> QPointF:
        if hasattr(event, "position"):
            return QPointF(event.position())
        return QPointF(event.pos())

    def _offset_for_drag(self, anchor_minutes: int, plotted_minutes: int, previous_offset: int) -> int:
        base_offset = plotted_minutes - anchor_minutes
        candidates = [base_offset - 1440, base_offset, base_offset + 1440]
        closest = min(candidates, key=lambda value: abs(value - previous_offset))
        return max(-1440, min(1440, _round_to_step(float(closest), self._DRAG_MINUTE_STEP)))

    def _update_rule_from_position(self, index: int, pos: QPointF) -> None:
        if index < 0 or index >= len(self._rules):
            return
        plot = self._plot_rect()
        minutes = self._minutes_from_x(pos.x(), plot)
        brightness = self._brightness_from_y(pos.y(), plot)
        rule = deepcopy(self._rules[index])
        if (
            self._drag_index is not None
            and not self._drag_edit_started
            and (
                rule.brightness != brightness
                or self._rule_minutes(rule) % 1440 != minutes
            )
        ):
            self._drag_edit_started = True
            self.edit_started.emit()
        rule.brightness = brightness

        if rule.anchor == "time":
            rule.offset_minutes = 0
            rule.specific_time = _format_time_from_minutes(minutes)
        else:
            anchor_minutes = self._base_anchor_minutes(rule.anchor)
            rule.offset_minutes = self._offset_for_drag(
                anchor_minutes,
                minutes,
                int(rule.offset_minutes),
            )
            rule.specific_time = None

        self._rules[index] = rule
        self.rules_changed.emit(self.rules())
        self.update()

    def _add_rule_at_position(self, pos: QPointF) -> None:
        plot = self._plot_rect()
        if not plot.contains(pos):
            return
        self.edit_started.emit()
        target = "both"
        if 0 <= self._selected_index < len(self._rules):
            target = self._rules[self._selected_index].target
        minutes = self._minutes_from_x(pos.x(), plot)
        brightness = self._brightness_from_y(pos.y(), plot)
        self._rules.append(
            ScheduleRule(
                anchor="time",
                offset_minutes=0,
                brightness=brightness,
                target=target,
                specific_time=_format_time_from_minutes(minutes),
            )
        )
        self._selected_index = len(self._rules) - 1
        self.rules_changed.emit(self.rules())
        self.selection_changed.emit(self._selected_index)
        self.update()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        index = self._hit_test(self._event_position(event))
        if index < 0:
            super().mousePressEvent(event)
            return
        self._drag_index = index
        self._drag_edit_started = False
        self.set_selected_index(index)
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        pos = self._event_position(event)
        if (
            self._drag_index is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            self._update_rule_from_position(self._drag_index, pos)
            return

        hover_index = self._hit_test(pos)
        if hover_index != self._hover_index:
            self._hover_index = hover_index
            if hover_index >= 0:
                self.setToolTip(self._summary_for_rule(hover_index))
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            else:
                self.setToolTip(
                    "Double-click the graph to add a point. Drag points to change time and brightness."
                )
                self.unsetCursor()
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        self._drag_index = None
        self._drag_edit_started = False
        if self._hover_index >= 0:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.unsetCursor()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self._add_rule_at_position(self._event_position(event))
            return
        super().mouseDoubleClickEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self._hover_index = -1
        if self._drag_index is None:
            self.unsetCursor()
        super().leaveEvent(event)

    def _points_for_target_values(self, target_values: set[str]) -> list[tuple[int, int]]:
        points: list[tuple[int, int]] = []
        for rule in self._rules:
            if rule.target not in target_values:
                continue
            points.append((self._rule_minutes(rule) % 1440, clamp_brightness(rule.brightness)))
        points.sort(key=lambda point: point[0])
        return points

    def _points_for_display_curve(self, target: str) -> list[tuple[int, int]]:
        specific_points = self._points_for_target_values({target})
        if specific_points:
            return specific_points
        return self._points_for_target_values({"both"})

    def _value_for_minute(self, points: list[tuple[int, int]], minute: float) -> float | None:
        if not points:
            return None
        if len(points) == 1:
            return float(clamp_brightness(points[0][1]))

        target_minute = float(minute) % 1440.0
        previous = points[-1]
        following = points[0]
        for point in points:
            if point[0] <= target_minute:
                previous = point
                continue
            following = point
            break

        if not self._gradual:
            return float(clamp_brightness(previous[1]))

        start_minute = float(previous[0])
        end_minute = float(following[0])
        adjusted_target = target_minute
        if end_minute <= start_minute:
            end_minute += 1440.0
        if adjusted_target < start_minute:
            adjusted_target += 1440.0
        duration = end_minute - start_minute
        if duration <= 0:
            return float(clamp_brightness(following[1]))
        ratio = max(0.0, min(1.0, (adjusted_target - start_minute) / duration))
        return previous[1] + (following[1] - previous[1]) * ratio

    def _draw_sky(self, painter: QPainter, plot: QRectF) -> None:
        sunrise = self._effective_sunrise_minutes()
        sunset = self._effective_sunset_minutes()
        painter.save()
        clip = QPainterPath()
        clip.addRoundedRect(plot, 12, 12)
        painter.setClipPath(clip)

        sky_gradient = QLinearGradient(plot.left(), 0, plot.right(), 0)
        night = "#0b1226"
        stops: list[tuple[float, QColor]] = [(0.0, QColor(night)), (1.0, QColor(night))]

        def add_stop(minutes: int | float, color: str) -> None:
            if minutes < 0 or minutes > 1440:
                return
            stops.append((float(minutes) / 1440.0, QColor(color)))

        if sunset > sunrise:
            midday = sunrise + (sunset - sunrise) / 2.0
            add_stop(sunrise - 210, night)
            add_stop(sunrise - 135, "#1e2a78")
            add_stop(sunrise - 72, "#d9663a")
            add_stop(sunrise + 16, "#f6c667")
            add_stop(sunrise + 125, "#5fb4ee")
            add_stop(midday, "#9cc9f5")
            add_stop(sunset - 125, "#5fb4ee")
            add_stop(sunset - 18, "#f0a4b4")
            add_stop(sunset + 78, "#8e1a52")
            add_stop(sunset + 155, night)
        else:
            add_stop(360, "#d9663a")
            add_stop(420, "#5fb4ee")
            add_stop(720, "#9cc9f5")
            add_stop(1080, "#e0607a")

        for position, color in sorted(stops, key=lambda stop: stop[0]):
            sky_gradient.setColorAt(position, color)
        painter.fillRect(plot, QBrush(sky_gradient))

        # Stars fade in over the night stretches.
        night_start = (sunset + 70) % 1440
        night_end = sunrise - 70
        painter.setPen(Qt.PenStyle.NoPen)
        for u_pos, v_pos, size in self._STARS:
            minute = u_pos * 1440.0
            in_night = (
                minute >= night_start or minute <= night_end
                if night_start > night_end
                else night_start <= minute <= night_end
            )
            if not in_night:
                continue
            painter.setBrush(QBrush(QColor(255, 255, 255, 150 if size > 1.3 else 95)))
            painter.drawEllipse(
                QPointF(plot.left() + u_pos * plot.width(), plot.top() + 6 + v_pos * plot.height() * 0.55),
                size,
                size,
            )

        # Darken slightly toward the bottom so curves pop.
        shade = QLinearGradient(0, plot.top(), 0, plot.bottom())
        shade.setColorAt(0.0, QColor(0, 0, 0, 0))
        shade.setColorAt(1.0, QColor(0, 0, 0, 90))
        painter.fillRect(plot, QBrush(shade))
        painter.restore()

    def _draw_sun_icon(self, painter: QPainter, center: QPointF, color: QColor) -> None:
        painter.save()
        glow = QRadialGradient(center, 20)
        glow_color = QColor(color)
        glow_color.setAlpha(110)
        glow.setColorAt(0.0, glow_color)
        glow.setColorAt(1.0, QColor(color.red(), color.green(), color.blue(), 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(glow))
        painter.drawEllipse(center, 20, 20)
        painter.setPen(QPen(color, 1.4))
        painter.setBrush(QBrush(color))
        painter.drawEllipse(center, 5, 5)
        for index in range(8):
            angle = math.tau * index / 8.0
            inner = QPointF(center.x() + math.cos(angle) * 8, center.y() + math.sin(angle) * 8)
            outer = QPointF(center.x() + math.cos(angle) * 11, center.y() + math.sin(angle) * 11)
            painter.drawLine(inner, outer)
        painter.restore()

    def _draw_moon_icon(self, painter: QPainter, center: QPointF, mask_color: QColor) -> None:
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor("#e8edf9")))
        painter.drawEllipse(center, 6, 6)
        painter.setBrush(QBrush(mask_color))
        painter.drawEllipse(QPointF(center.x() + 4, center.y() - 2), 6, 6)
        painter.restore()

    def _draw_event_marker(
        self,
        painter: QPainter,
        plot: QRectF,
        minutes: int,
        color: QColor,
        label: str,
    ) -> None:
        x_pos = self._x_from_minutes(minutes, plot)
        line_color = QColor(color)
        line_color.setAlpha(110)
        pen = QPen(line_color, 1)
        pen.setStyle(Qt.PenStyle.DotLine)
        painter.setPen(pen)
        painter.drawLine(QPointF(x_pos, plot.top() + 28), QPointF(x_pos, plot.bottom()))
        self._draw_sun_icon(painter, QPointF(x_pos, plot.top() + 18), color)
        label_rect = QRectF(x_pos - 34, plot.top() + 31, 68, 16)
        painter.setPen(QPen(QColor(0, 0, 0, 150), 1))
        painter.drawText(label_rect.translated(0.8, 0.8), Qt.AlignmentFlag.AlignCenter, label)
        painter.setPen(QPen(QColor(255, 255, 255, 235), 1))
        painter.drawText(label_rect, Qt.AlignmentFlag.AlignCenter, label)

    def _draw_axes(self, painter: QPainter, plot: QRectF) -> None:
        text_color = QColor(active_theme().muted)
        small_font = QFont(painter.font())
        point_size = small_font.pointSizeF()
        small_font.setPointSizeF(8.0 if point_size <= 0 else max(8.0, point_size * 0.82))
        painter.setFont(small_font)

        for brightness in (100, 75, 50, 25, 0):
            y_pos = self._y_from_brightness(brightness, plot)
            grid_pen = QPen(QColor(255, 255, 255, 46 if brightness not in (0, 100) else 28), 1)
            grid_pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(grid_pen)
            painter.drawLine(QPointF(plot.left() + 6, y_pos), QPointF(plot.right() - 6, y_pos))
            painter.setPen(QPen(text_color, 1))
            painter.drawText(
                QRectF(4, y_pos - 9, self._LEFT_MARGIN - 10, 18),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{brightness}%",
            )

        for minutes, label in (
            (0, "00:00"),
            (360, "06:00"),
            (720, "12:00"),
            (1080, "18:00"),
            (1440, "24:00"),
        ):
            x_pos = plot.right() if minutes == 1440 else self._x_from_minutes(minutes, plot)
            tick_pen = QPen(QColor(255, 255, 255, 30), 1)
            painter.setPen(tick_pen)
            painter.drawLine(QPointF(x_pos, plot.top() + 6), QPointF(x_pos, plot.bottom() - 6))
            painter.setPen(QPen(text_color, 1))
            painter.drawText(
                QRectF(x_pos - 28, plot.bottom() + 8, 56, 18),
                Qt.AlignmentFlag.AlignCenter,
                label,
            )

    def _curve_path(self, plot: QRectF, points: list[tuple[int, int]]) -> QPainterPath | None:
        if not points:
            return None
        path = QPainterPath()
        sample_count = 240
        first = True
        for sample_index in range(sample_count + 1):
            minutes = (1440.0 / sample_count) * sample_index
            value = self._value_for_minute(points, minutes)
            if value is None:
                continue
            point = QPointF(
                self._x_from_minutes(min(minutes, 1439.99), plot),
                self._y_from_brightness(value, plot),
            )
            if first:
                path.moveTo(point)
                first = False
            else:
                path.lineTo(point)
        return path

    def _draw_interpolation_path(
        self,
        painter: QPainter,
        plot: QRectF,
        target: str,
        color: QColor,
    ) -> None:
        points = self._points_for_display_curve(target)
        path = self._curve_path(plot, points)
        if path is None:
            return

        painter.save()
        clip = QPainterPath()
        clip.addRoundedRect(plot, 12, 12)
        painter.setClipPath(clip)

        area = QPainterPath(path)
        area.lineTo(plot.right(), plot.bottom())
        area.lineTo(plot.left(), plot.bottom())
        area.closeSubpath()
        fill = QLinearGradient(0, plot.top(), 0, plot.bottom())
        top_color = QColor(color)
        top_color.setAlpha(70)
        bottom_color = QColor(color)
        bottom_color.setAlpha(0)
        fill.setColorAt(0.0, top_color)
        fill.setColorAt(1.0, bottom_color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(fill))
        painter.drawPath(area)

        glow_color = QColor(color)
        glow_color.setAlpha(55)
        glow_pen = QPen(glow_color, 8)
        glow_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        glow_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(glow_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

        line_pen = QPen(QColor(color).lighter(115), 2.6)
        line_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        line_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(line_pen)
        painter.drawPath(path)
        painter.restore()

    def _draw_now_marker(self, painter: QPainter, plot: QRectF) -> None:
        if self._now_minutes is None:
            return
        x_pos = self._x_from_minutes(self._now_minutes, plot)
        now_pen = QPen(QColor(255, 255, 255, 190), 1)
        now_pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(now_pen)
        painter.drawLine(QPointF(x_pos, plot.top() + 16), QPointF(x_pos, plot.bottom()))

        pill = QRectF(x_pos - 20, plot.top() - 1, 40, 17)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(255, 255, 255, 235)))
        painter.drawRoundedRect(pill, 8.5, 8.5)
        painter.setPen(QPen(QColor("#111827"), 1))
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, "Now")

        for target in ("display1", "display2"):
            target_points = self._points_for_display_curve(target)
            value = self._value_for_minute(target_points, self._now_minutes)
            if value is None:
                continue
            color = self._target_color(target)
            painter.setPen(QPen(QColor("#ffffff"), 2))
            painter.setBrush(QBrush(color))
            painter.drawEllipse(QPointF(x_pos, self._y_from_brightness(value, plot)), 5, 5)

    def _draw_points(self, painter: QPainter, plot: QRectF) -> None:
        points = self._point_positions(plot)
        points.sort(key=lambda item: item[0] == self._selected_index)
        for index, point, rule in points:
            selected = index == self._selected_index
            hovered = index == self._hover_index
            color = self._target_color(rule.target)
            radius = 9.5 if selected else (8.5 if hovered else 7.5)

            halo = QRadialGradient(point, radius + 14)
            halo_color = QColor(color)
            halo_color.setAlpha(150 if selected else (90 if hovered else 0))
            halo.setColorAt(0.0, halo_color)
            halo.setColorAt(1.0, QColor(color.red(), color.green(), color.blue(), 0))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(halo))
            painter.drawEllipse(point, radius + 14, radius + 14)

            painter.setPen(QPen(QColor(0, 0, 0, 70), 1))
            painter.setBrush(QBrush(QColor("#ffffff")))
            painter.drawEllipse(point, radius, radius)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(color))
            painter.drawEllipse(point, radius - 2.8, radius - 2.8)

            if selected or hovered:
                painter.setPen(QPen(QColor("#ffffff"), 1))
                dot_font = QFont(painter.font())
                dot_font.setPointSizeF(max(6.5, dot_font.pointSizeF() - 1.5))
                dot_font.setBold(True)
                painter.setFont(dot_font)
                painter.drawText(
                    QRectF(point.x() - 16, point.y() - 8, 32, 16),
                    Qt.AlignmentFlag.AlignCenter,
                    self._target_dot_label(rule.target),
                )
                dot_font.setBold(False)
                dot_font.setPointSizeF(dot_font.pointSizeF() + 1.5)
                painter.setFont(dot_font)

        if 0 <= self._selected_index < len(self._rules):
            self._draw_selected_callout(painter, plot, self._selected_index)

    def _draw_selected_callout(self, painter: QPainter, plot: QRectF, index: int) -> None:
        rule = self._rules[index]
        point = next(
            (point for point_index, point, _rule in self._point_positions(plot) if point_index == index),
            None,
        )
        if point is None:
            return
        text = (
            f"{self._target_short_label(rule.target)} · "
            f"{_format_time_from_minutes(self._rule_minutes(rule))} · "
            f"{clamp_brightness(rule.brightness)}%"
        )
        width = min(max(118, painter.fontMetrics().horizontalAdvance(text) + 26), int(plot.width()))
        height = 26
        x_pos = min(max(plot.left(), point.x() - width / 2), plot.right() - width)
        y_pos = point.y() - height - 18
        if y_pos < plot.top() + 4:
            y_pos = point.y() + 18
        rect = QRectF(x_pos, y_pos, width, height)
        painter.setPen(QPen(QColor(255, 255, 255, 120), 1))
        painter.setBrush(QBrush(QColor(12, 16, 28, 220)))
        painter.drawRoundedRect(rect, 13, 13)
        painter.setPen(QPen(QColor("#f8fafc"), 1))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    def _draw_legend(self, painter: QPainter, plot: QRectF) -> None:
        entries = (("display1", "Display 1"), ("display2", "Display 2"), ("both", "Both"))
        x_pos = max(plot.left(), plot.right() - 232)
        y_pos = 9
        text_color = QColor(active_theme().muted)
        for target, label in entries:
            color = self._target_color(target)
            painter.setBrush(QBrush(color))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPointF(x_pos, y_pos + 7), 4.5, 4.5)
            painter.setPen(QPen(text_color, 1))
            painter.drawText(
                QRectF(x_pos + 10, y_pos - 1, 74, 16),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                label,
            )
            x_pos += 78

    def paintEvent(self, event) -> None:  # type: ignore[override]
        del event
        theme = active_theme()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        base_font = QFont(self.font())
        base_font.setPointSizeF(8.0)
        painter.setFont(base_font)

        outer_rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        painter.setPen(QPen(qcolor(theme.card_border), 1))
        painter.setBrush(QBrush(qcolor(theme.field)))
        painter.drawRoundedRect(outer_rect, 14, 14)

        plot = self._plot_rect()
        self._draw_sky(painter, plot)
        painter.setPen(QPen(QColor(255, 255, 255, 70), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(plot, 12, 12)
        self._draw_axes(painter, plot)
        self._draw_moon_icon(painter, QPointF(plot.left() + 22, plot.top() + 22), QColor("#0f1630"))
        self._draw_event_marker(
            painter,
            plot,
            self._effective_sunrise_minutes(),
            QColor("#fde68a"),
            "Sunrise",
        )
        self._draw_event_marker(
            painter,
            plot,
            self._effective_sunset_minutes(),
            QColor("#fecdd3"),
            "Sunset",
        )
        self._draw_interpolation_path(painter, plot, "display1", self._target_color("display1"))
        self._draw_interpolation_path(painter, plot, "display2", self._target_color("display2"))
        self._draw_now_marker(painter, plot)
        self._draw_points(painter, plot)
        self._draw_legend(painter, plot)
        painter.end()

    def _summary_for_rule(self, index: int) -> str:
        if index < 0 or index >= len(self._rules):
            return ""
        rule = self._rules[index]
        display = self._target_labels.get(rule.target, self._target_short_label(rule.target))
        time_text = _format_time_from_minutes(self._rule_minutes(rule))
        if rule.anchor == "time":
            anchor_text = f"Fixed time {time_text}"
        else:
            offset = int(rule.offset_minutes)
            sign = "+" if offset >= 0 else ""
            anchor_text = f"{rule.anchor.title()} {sign}{offset} min ({time_text})"
        return f"{display}\n{anchor_text}\nBrightness {clamp_brightness(rule.brightness)}%"


class SettingsDialog(QDialog):
    def __init__(
        self,
        current_config: AppConfig,
        monitor_labels: list[str] | None = None,
        brightness_service: BrightnessService | None = None,
        monitor_handles: list[MonitorHandle] | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Remote Monitor Controller Settings")
        self.setMinimumWidth(740)
        _apply_rounded_popup_chrome(self, "settingsDialog")

        self._source_config = deepcopy(current_config)
        self._initial_theme = normalize_theme_name(current_config.theme)
        self._initial_blur = bool(current_config.blur_enabled)
        self._selected_theme = self._initial_theme
        self.updated_config: AppConfig | None = None
        self.monitor_labels = monitor_labels or []
        self.brightness_service = brightness_service
        self.monitor_handles = monitor_handles or []
        self._detected_location: LocationContext | None = None
        self._location_timezone_name: str | None = None
        self._sun_times_cache_key: tuple[str, float, float, str] | None = None
        self._schedule_rules: list[ScheduleRule] = []
        self._syncing_rule_editor = False
        self._schedule_undo_enabled = False
        self._restoring_schedule_undo = False
        self._schedule_undo_stack: list[tuple[list[ScheduleRule], int, bool, bool]] = []

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 14)
        root_layout.setSpacing(8)
        self.title_bar = GlassTitleBar("Settings", self.reject)
        root_layout.addWidget(self.title_bar)

        self.content_scroll = QScrollArea()
        self.content_scroll.setObjectName("settingsContentScroll")
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_row = QHBoxLayout()
        scroll_row.setContentsMargins(16, 0, 8, 0)
        scroll_row.addWidget(self.content_scroll)
        root_layout.addLayout(scroll_row, stretch=1)

        self.content_widget = QWidget()
        self.content_widget.setObjectName("settingsContentWidget")
        content_layout = QVBoxLayout(self.content_widget)
        content_layout.setContentsMargins(0, 0, 8, 0)
        content_layout.setSpacing(4)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        appearance_box = QGroupBox("Appearance")
        appearance_layout = QGridLayout(appearance_box)
        appearance_layout.setHorizontalSpacing(8)
        appearance_layout.setVerticalSpacing(8)
        self.theme_button_group = QButtonGroup(self)
        self.theme_button_group.setExclusive(True)
        self.theme_buttons: dict[str, QPushButton] = {}
        for theme_index, theme_name in enumerate(THEME_OPTIONS):
            button = ThemeSwatchButton(theme_name)
            self.theme_button_group.addButton(button)
            self.theme_buttons[theme_name] = button
            button.toggled.connect(
                lambda checked, selected_theme=theme_name: self._handle_theme_toggle(
                    selected_theme,
                    checked,
                )
            )
            appearance_layout.addWidget(button, theme_index // 3, theme_index % 3)
        top_row.addWidget(appearance_box, stretch=0)

        startup_box = QGroupBox("General")
        startup_layout = QVBoxLayout(startup_box)
        startup_layout.setSpacing(8)
        self.startup_checkbox = QCheckBox("Launch at Windows startup")
        startup_layout.addWidget(self.startup_checkbox)
        self.blur_checkbox = QCheckBox("Frosted blur (Windows 11)")
        self.blur_checkbox.setToolTip(
            "Uses the Windows acrylic backdrop. Turn off if the windows look wrong on your system."
        )
        startup_layout.addWidget(self.blur_checkbox)
        startup_layout.addStretch(1)
        self.picture_controls_button = QPushButton("Picture controls…")
        self.picture_controls_button.setToolTip(
            "Contrast, sharpness, black level and colour sliders detected through DDC/CI"
        )
        startup_layout.addWidget(self.picture_controls_button)
        top_row.addWidget(startup_box, stretch=1)
        content_layout.addLayout(top_row)

        schedule_box = QGroupBox("Sunrise/Sunset Scheduling")
        schedule_layout = QVBoxLayout(schedule_box)

        schedule_top_layout = QHBoxLayout()
        schedule_toggle_layout = QHBoxLayout()
        schedule_toggle_layout.setSpacing(18)
        self.schedule_enabled_checkbox = QCheckBox("Enable automatic schedule")
        self.gradual_checkbox = QCheckBox("Gradual transitions")
        schedule_toggle_layout.addWidget(self.schedule_enabled_checkbox)
        schedule_toggle_layout.addWidget(self.gradual_checkbox)
        schedule_toggle_layout.addStretch(1)
        schedule_top_layout.addLayout(schedule_toggle_layout, stretch=1)

        self.location_status_widget = QWidget()
        self.location_status_widget.setObjectName("scheduleTimeCard")
        location_status_layout = QHBoxLayout(self.location_status_widget)
        location_status_layout.setContentsMargins(12, 5, 12, 5)
        location_status_layout.setSpacing(14)
        self.location_time_label = QLabel("--:--")
        self.location_time_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.sunrise_time_label = QLabel("☀ --:--")
        self.sunrise_time_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.sunset_time_label = QLabel("☾ --:--")
        self.sunset_time_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        location_status_layout.addWidget(self.location_time_label)
        location_status_layout.addWidget(self.sunrise_time_label)
        location_status_layout.addWidget(self.sunset_time_label)
        self.location_status_widget.setSizePolicy(
            QSizePolicy.Policy.Maximum,
            QSizePolicy.Policy.Maximum,
        )
        schedule_top_layout.addWidget(
            self.location_status_widget,
            0,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
        )
        schedule_layout.addLayout(schedule_top_layout)

        location_row = QHBoxLayout()
        location_row.setSpacing(8)
        self.auto_location_checkbox = QCheckBox("Auto-detect location")
        location_row.addWidget(self.auto_location_checkbox)
        self.location_hint_label = QLabel("")
        self.location_hint_label.setObjectName("hint")
        self.location_hint_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        location_row.addWidget(self.location_hint_label, stretch=1)
        self.latitude_spin = QDoubleSpinBox()
        self.latitude_spin.setRange(-90.0, 90.0)
        self.latitude_spin.setDecimals(4)
        self.latitude_spin.setPrefix("Lat ")
        self.latitude_spin.setSuffix("°")
        self.latitude_spin.setMinimumWidth(124)
        self.longitude_spin = QDoubleSpinBox()
        self.longitude_spin.setRange(-180.0, 180.0)
        self.longitude_spin.setDecimals(4)
        self.longitude_spin.setPrefix("Lon ")
        self.longitude_spin.setSuffix("°")
        self.longitude_spin.setMinimumWidth(130)
        location_row.addWidget(self.latitude_spin)
        location_row.addWidget(self.longitude_spin)
        self.detect_location_button = QPushButton("Detect now")
        location_row.addWidget(self.detect_location_button)
        schedule_layout.addLayout(location_row)

        self.schedule_graph = ScheduleGraphWidget(self._build_target_choices())
        schedule_layout.addWidget(self.schedule_graph)

        self.rule_editor_widget = QWidget()
        rule_editor_layout = QHBoxLayout(self.rule_editor_widget)
        rule_editor_layout.setContentsMargins(0, 0, 0, 0)
        rule_editor_layout.setSpacing(5)

        self.selected_rule_label = QLabel("Point")
        self.selected_rule_label.setMinimumWidth(52)
        rule_editor_layout.addWidget(self.selected_rule_label)

        self.point_target_combo = QComboBox()
        for value, label in self._build_target_choices():
            self.point_target_combo.addItem(label, userData=value)
        self.point_target_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.point_target_combo.setMinimumContentsLength(12)
        self.point_target_combo.view().setMinimumWidth(260)
        rule_editor_layout.addWidget(QLabel("Display"))
        rule_editor_layout.addWidget(self.point_target_combo)

        self.point_anchor_combo = QComboBox()
        self.point_anchor_combo.addItems(["sunrise", "sunset", "specific time"])
        self.point_anchor_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.point_anchor_combo.setMinimumContentsLength(8)
        rule_editor_layout.addWidget(QLabel("Anchor"))
        rule_editor_layout.addWidget(self.point_anchor_combo)

        self.point_time_edit = QLineEdit()
        self.point_time_edit.setPlaceholderText("HH:MM")
        time_regex = QRegularExpression(r"^([01]?\d|2[0-3]):[0-5]\d$")
        self.point_time_edit.setValidator(QRegularExpressionValidator(time_regex, self))
        self.point_time_edit.setMaximumWidth(64)
        rule_editor_layout.addWidget(QLabel("Time"))
        rule_editor_layout.addWidget(self.point_time_edit)

        self.point_offset_spin = QSpinBox()
        self.point_offset_spin.setRange(-1440, 1440)
        self.point_offset_spin.setSingleStep(5)
        self.point_offset_spin.setSuffix(" min")
        self.point_offset_spin.setMaximumWidth(92)
        rule_editor_layout.addWidget(QLabel("Offset"))
        rule_editor_layout.addWidget(self.point_offset_spin)

        self.point_brightness_spin = QSpinBox()
        self.point_brightness_spin.setRange(0, 100)
        self.point_brightness_spin.setSuffix("%")
        self.point_brightness_spin.setMaximumWidth(70)
        rule_editor_layout.addWidget(QLabel("Level"))
        rule_editor_layout.addWidget(self.point_brightness_spin)
        rule_editor_layout.addStretch(1)
        schedule_layout.addWidget(self.rule_editor_widget)

        self.rule_buttons_widget = QWidget()
        rule_button_layout = QHBoxLayout(self.rule_buttons_widget)
        rule_button_layout.setContentsMargins(0, 0, 0, 0)
        self.add_rule_button = QPushButton("Add Point")
        self.remove_rule_button = QPushButton("Remove Point")
        self.undo_schedule_button = QPushButton("Undo")
        self.undo_schedule_button.setVisible(False)
        self.load_default_sun_rules_button = QPushButton("Apply Preset Curve")
        self.add_rule_button.clicked.connect(lambda _checked=False: self._add_empty_rule())
        self.remove_rule_button.clicked.connect(lambda _checked=False: self._remove_selected_rule())
        self.undo_schedule_button.clicked.connect(lambda _checked=False: self._undo_schedule_change())
        self.load_default_sun_rules_button.clicked.connect(
            lambda _checked=False: self._apply_default_sunrise_sunset_rules()
        )
        self.load_default_sun_rules_button.setToolTip(
            "Replace the selected display curve with the original default sunrise/sunset preset."
        )
        rule_button_layout.addWidget(self.add_rule_button)
        rule_button_layout.addWidget(self.remove_rule_button)
        rule_button_layout.addWidget(self.undo_schedule_button)
        rule_button_layout.addWidget(self.load_default_sun_rules_button)
        rule_button_layout.addStretch(1)
        schedule_layout.addWidget(self.rule_buttons_widget)

        content_layout.addWidget(schedule_box)
        self.content_scroll.setWidget(self.content_widget)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self._save_and_close)
        button_box.rejected.connect(self.reject)
        save_button = button_box.button(QDialogButtonBox.StandardButton.Save)
        if save_button is not None:
            save_button.setObjectName("primary")
            save_button.setText("Save changes")
            save_button.setDefault(True)
        footer_row = QHBoxLayout()
        footer_row.setContentsMargins(16, 0, 16, 0)
        footer_row.addWidget(button_box)
        root_layout.addLayout(footer_row)
        self.button_box = button_box

        self.picture_controls_button.clicked.connect(
            lambda _checked=False: self._open_picture_controls_dialog()
        )
        self.blur_checkbox.toggled.connect(self._handle_blur_toggled)
        self.auto_location_checkbox.toggled.connect(self._handle_auto_location_toggled)
        self.latitude_spin.valueChanged.connect(lambda _v: self._handle_manual_location_changed())
        self.longitude_spin.valueChanged.connect(lambda _v: self._handle_manual_location_changed())
        self.detect_location_button.clicked.connect(lambda _checked=False: self._detect_location_now())
        self.schedule_enabled_checkbox.toggled.connect(self._handle_schedule_enabled_toggled)
        self.gradual_checkbox.toggled.connect(self._handle_gradual_toggled)
        self.schedule_graph.edit_started.connect(self._remember_schedule_state)
        self.schedule_graph.rules_changed.connect(self._handle_schedule_graph_rules_changed)
        self.schedule_graph.selection_changed.connect(self._handle_schedule_graph_selection_changed)
        self.point_target_combo.currentIndexChanged.connect(
            lambda _index: self._apply_rule_editor_to_selected()
        )
        self.point_anchor_combo.currentTextChanged.connect(
            lambda _text: self._handle_point_anchor_changed()
        )
        self.point_time_edit.textEdited.connect(
            lambda _text: self._apply_rule_editor_to_selected(time_is_source=True)
        )
        self.point_offset_spin.valueChanged.connect(
            lambda _value: self._handle_point_offset_changed()
        )
        self.point_brightness_spin.valueChanged.connect(
            lambda _value: self._apply_rule_editor_to_selected()
        )
        self._load_from_config()
        self._schedule_undo_enabled = True
        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(1_000)
        self._clock_timer.timeout.connect(self._update_location_time_label)
        self._clock_timer.start()
        QTimer.singleShot(0, self._refresh_dialog_size)

    def _build_target_choices(self) -> list[tuple[str, str]]:
        device_name = BrightnessService.system_device_name()
        label_one = self.monitor_labels[0] if len(self.monitor_labels) >= 1 else (device_name or "Display 1")
        label_two = self.monitor_labels[1] if len(self.monitor_labels) >= 2 else "Display 2"
        return [
            ("display1", f"Display 1 ({label_one})"),
            ("display2", f"Display 2 ({label_two})"),
            ("both", "Both displays"),
        ]

    def _open_picture_controls_dialog(self) -> None:
        if self.brightness_service is None:
            QMessageBox.information(
                self,
                "Picture Controls",
                "Picture controls are unavailable because the monitor service is not ready.",
            )
            return
        if not self.monitor_handles:
            QMessageBox.information(
                self,
                "Picture Controls",
                "No monitor handles are currently available. Refresh monitors first.",
            )
            return

        dialog = PictureControlsDialog(
            service=self.brightness_service,
            monitors=list(self.monitor_handles),
            parent=self,
        )
        dialog.exec()

    def _load_from_config(self) -> None:
        self._set_selected_theme(normalize_theme_name(self._source_config.theme), preview=False)
        self.startup_checkbox.setChecked(self._source_config.startup_enabled)
        self.blur_checkbox.setChecked(self._source_config.blur_enabled)
        schedule_settings = self._source_config.schedule
        self.auto_location_checkbox.setChecked(schedule_settings.auto_location)
        for spin, value in (
            (self.latitude_spin, schedule_settings.latitude),
            (self.longitude_spin, schedule_settings.longitude),
        ):
            spin.blockSignals(True)
            spin.setValue(float(value) if value is not None else 0.0)
            spin.blockSignals(False)
        self._update_location_controls()
        self.schedule_enabled_checkbox.setChecked(self._source_config.schedule.enabled)
        self.gradual_checkbox.setChecked(self._source_config.schedule.gradual)
        controls_available = self.brightness_service is not None and bool(self.monitor_handles)
        self.picture_controls_button.setEnabled(controls_available)
        if not controls_available:
            self.picture_controls_button.setToolTip("Refresh monitors to enable picture controls")
        else:
            self.picture_controls_button.setToolTip("")
        self._refresh_auto_location(refresh_detection=False)

        rules = self._source_config.schedule.rules or default_schedule_rules()
        self.schedule_graph.set_gradual(self._source_config.schedule.gradual)
        self._set_schedule_rules(rules, selected_index=0 if rules else -1)
        self._refresh_default_rules_button_state()
        self._update_schedule_controls_visibility(self.schedule_enabled_checkbox.isChecked())
        self._refresh_dialog_size()

    def _handle_blur_toggled(self, checked: bool) -> None:
        set_blur_enabled(checked)
        apply_backdrop(self)
        parent = self.parentWidget()
        if parent is not None:
            apply_backdrop(parent)

    def _update_location_controls(self) -> None:
        auto = self.auto_location_checkbox.isChecked()
        self.latitude_spin.setVisible(not auto)
        self.longitude_spin.setVisible(not auto)
        self.detect_location_button.setVisible(auto)
        self.location_hint_label.setVisible(auto)
        if auto:
            if self._detected_location is not None:
                self.location_hint_label.setText(f"Detected: {self._detected_location.region}")
            elif self._source_config.schedule.latitude is not None:
                self.location_hint_label.setText("Using last detected location")
            else:
                self.location_hint_label.setText("Detected from your IP address")
        else:
            self.location_hint_label.setText("")

    def _handle_auto_location_toggled(self, _checked: bool) -> None:
        self._update_location_controls()
        if not self.auto_location_checkbox.isChecked():
            self._handle_manual_location_changed()
        self._refresh_dialog_size()

    def _handle_manual_location_changed(self) -> None:
        if self.auto_location_checkbox.isChecked():
            return
        self._source_config.schedule.latitude = self.latitude_spin.value()
        self._source_config.schedule.longitude = self.longitude_spin.value()
        self._sun_times_cache_key = None
        self._update_location_time_label()

    def _detect_location_now(self) -> None:
        self.detect_location_button.setEnabled(False)
        self.location_hint_label.setText("Detecting location…")

        def finished(result, error) -> None:
            self.detect_location_button.setEnabled(True)
            if error is not None or result is None:
                self.location_hint_label.setText("Couldn't detect. Turn off auto-detect to type it.")
                return
            self._detected_location = result
            self._location_timezone_name = result.timezone_name or self._location_timezone_name
            self._source_config.schedule.latitude = result.latitude
            self._source_config.schedule.longitude = result.longitude
            for spin, value in ((self.latitude_spin, result.latitude), (self.longitude_spin, result.longitude)):
                spin.blockSignals(True)
                spin.setValue(value)
                spin.blockSignals(False)
            self._sun_times_cache_key = None
            self._update_location_controls()
            self._update_location_time_label()

        run_async(self, detect_location_context_from_ip, finished)

    def _handle_schedule_enabled_toggled(self, checked: bool) -> None:
        snapshot = self._schedule_snapshot(schedule_enabled=not checked)
        self._remember_schedule_state(snapshot)
        self._update_schedule_controls_visibility(checked)
        self._refresh_dialog_size()
        QTimer.singleShot(0, self._refresh_dialog_size)

    def _handle_gradual_toggled(self, checked: bool) -> None:
        snapshot = self._schedule_snapshot(gradual=not checked)
        self._remember_schedule_state(snapshot)
        self.schedule_graph.set_gradual(checked)

    def _update_schedule_controls_visibility(self, schedule_enabled: bool) -> None:
        dim_opacity = 1.0 if schedule_enabled else 0.5
        self.gradual_checkbox.setVisible(True)
        self.location_status_widget.setVisible(True)
        self.schedule_graph.setVisible(True)
        self.rule_editor_widget.setVisible(True)
        self.rule_buttons_widget.setVisible(True)
        for widget in (
            self.gradual_checkbox,
            self.location_status_widget,
            self.schedule_graph,
            self.rule_editor_widget,
            self.rule_buttons_widget,
        ):
            self._set_widget_opacity(widget, dim_opacity)

    @staticmethod
    def _set_widget_opacity(widget: QWidget, opacity: float) -> None:
        effect = widget.graphicsEffect()
        if not isinstance(effect, QGraphicsOpacityEffect):
            effect = QGraphicsOpacityEffect(widget)
            widget.setGraphicsEffect(effect)
        effect.setOpacity(max(0.0, min(1.0, opacity)))

    def _refresh_auto_location(self, refresh_detection: bool) -> None:
        if refresh_detection:
            context = detect_location_context_from_ip()
            if context is not None:
                self._detected_location = context
                self._location_timezone_name = context.timezone_name
                self._source_config.schedule.latitude = context.latitude
                self._source_config.schedule.longitude = context.longitude
                self._sun_times_cache_key = None

        if not self._location_timezone_name:
            try:
                self._location_timezone_name = get_localzone_name()
            except Exception:
                self._location_timezone_name = None
        self._update_location_time_label()

    def _update_location_time_label(self) -> None:
        timezone_name = self._location_timezone_name
        timezone_label = ""
        if timezone_name:
            try:
                current_time = datetime.now(ZoneInfo(timezone_name))
                timezone_label = current_time.strftime("%Z").strip()
            except Exception:
                current_time = datetime.now().astimezone()
        else:
            current_time = datetime.now().astimezone()
        if not timezone_label:
            timezone_label = (current_time.astimezone().tzname() or "").strip()
        time_text = current_time.strftime("%H:%M")
        if timezone_label:
            time_text = f"{time_text} {timezone_label}"
        self.location_time_label.setText(time_text)
        self.schedule_graph.set_current_time(current_time)
        self._update_sun_event_labels(current_time, timezone_name)

    def _update_sun_event_labels(self, current_time: datetime, timezone_name: str | None) -> None:
        latitude = self._source_config.schedule.latitude
        longitude = self._source_config.schedule.longitude
        if latitude is None or longitude is None:
            self.sunrise_time_label.setText("☀ --:--")
            self.sunset_time_label.setText("☾ --:--")
            self.schedule_graph.set_sun_times(None, None)
            self._sun_times_cache_key = None
            return

        if timezone_name:
            try:
                timezone = ZoneInfo(timezone_name)
            except Exception:
                timezone = current_time.astimezone().tzinfo
        else:
            timezone = current_time.astimezone().tzinfo
        if timezone is None:
            self.sunrise_time_label.setText("☀ --:--")
            self.sunset_time_label.setText("☾ --:--")
            self.schedule_graph.set_sun_times(None, None)
            self._sun_times_cache_key = None
            return

        timezone_key = timezone_name or str(timezone)
        cache_key = (
            current_time.date().isoformat(),
            round(float(latitude), 5),
            round(float(longitude), 5),
            timezone_key,
        )
        if self._sun_times_cache_key == cache_key:
            return

        try:
            location = LocationInfo(
                name="Local",
                region="Local",
                timezone=timezone_key,
                latitude=float(latitude),
                longitude=float(longitude),
            )
            sun_times = sun(location.observer, date=current_time.date(), tzinfo=timezone)
            sunrise_time = sun_times.get("sunrise")
            sunset_time = sun_times.get("sunset")
        except Exception:
            sunrise_time = None
            sunset_time = None

        sunrise_text = sunrise_time.strftime("%H:%M") if sunrise_time is not None else "--:--"
        sunset_text = sunset_time.strftime("%H:%M") if sunset_time is not None else "--:--"
        self.sunrise_time_label.setText(f"☀ {sunrise_text}")
        self.sunset_time_label.setText(f"☾ {sunset_text}")
        self.schedule_graph.set_sun_times(sunrise_time, sunset_time)
        self._sun_times_cache_key = cache_key

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        _apply_native_rounded_corners(self)
        self._refresh_auto_location(refresh_detection=False)
        self._position_bottom_right()
        QTimer.singleShot(0, self._refresh_dialog_size)

    def _max_dialog_height(self) -> int:
        available = self._available_geometry()
        if available is None:
            return 920
        return max(360, available.height() - 12)

    def _available_geometry(self):
        screen = self.screen()
        if screen is None and self.parentWidget() is not None:
            screen = self.parentWidget().screen()
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is None:
            return None
        return screen.availableGeometry()

    def _clamp_to_available_geometry(self) -> None:
        available = self._available_geometry()
        if available is None:
            return

        frame = self.frameGeometry()
        max_x = available.right() - frame.width() + 1
        max_y = max(
            available.top(),
            available.bottom() - frame.height() - POPUP_TASKBAR_CLEARANCE_PX + 1,
        )
        target_x = min(max(frame.x(), available.left()), max_x)
        target_y = min(max(frame.y(), available.top()), max_y)
        self.move(target_x, target_y)

    def _position_bottom_right(self) -> None:
        available = self._available_geometry()
        if available is None:
            return

        self.move(_bottom_right_popup_position(self, available))

    def _refresh_dialog_size(self) -> None:
        self.content_widget.adjustSize()
        content_layout = self.content_widget.layout()
        if content_layout is not None:
            content_layout.invalidate()
            content_layout.activate()
            content_size = content_layout.sizeHint()
        else:
            content_size = self.content_widget.sizeHint()
        layout = self.layout()
        margins = layout.contentsMargins()
        layout_spacing = max(0, layout.spacing())
        button_height = self.button_box.sizeHint().height()

        static_height = (
            margins.top()
            + margins.bottom()
            + self.title_bar.height()
            + (layout_spacing * 2)
            + button_height
        )
        preferred_height = static_height + content_size.height()
        max_height = self._max_dialog_height()
        max_content_height = max(160, max_height - static_height)
        needs_scroll = content_size.height() > max_content_height
        content_height = min(content_size.height(), max_content_height)
        target_height = static_height + content_height

        self.content_scroll.setFixedHeight(content_height)
        target_width = max(self.minimumWidth(), content_size.width() + 40)
        if needs_scroll:
            scrollbar_width = self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
            target_width += max(0, scrollbar_width)

        self.content_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            if preferred_height <= max_height
            else Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.resize(target_width, target_height)
        if self.isVisible():
            self._position_bottom_right()

    def _schedule_snapshot(
        self,
        rules: list[ScheduleRule] | None = None,
        selected_index: int | None = None,
        schedule_enabled: bool | None = None,
        gradual: bool | None = None,
    ) -> tuple[list[ScheduleRule], int, bool, bool]:
        if rules is None:
            rules = self._schedule_rules
        if selected_index is None:
            selected_index = self.schedule_graph.selected_index()
        if schedule_enabled is None:
            schedule_enabled = self.schedule_enabled_checkbox.isChecked()
        if gradual is None:
            gradual = self.gradual_checkbox.isChecked()
        return (
            [deepcopy(rule) for rule in rules],
            int(selected_index),
            bool(schedule_enabled),
            bool(gradual),
        )

    @staticmethod
    def _schedule_snapshots_match(
        left: tuple[list[ScheduleRule], int, bool, bool],
        right: tuple[list[ScheduleRule], int, bool, bool],
    ) -> bool:
        return (
            left[0] == right[0]
            and left[1] == right[1]
            and left[2] == right[2]
            and left[3] == right[3]
        )

    def _remember_schedule_state(
        self,
        snapshot: tuple[list[ScheduleRule], int, bool, bool] | None = None,
    ) -> None:
        if not self._schedule_undo_enabled or self._restoring_schedule_undo:
            return
        current_snapshot = snapshot or self._schedule_snapshot()
        if self._schedule_undo_stack and self._schedule_snapshots_match(
            self._schedule_undo_stack[-1],
            current_snapshot,
        ):
            return
        self._schedule_undo_stack.append(current_snapshot)
        if len(self._schedule_undo_stack) > 30:
            self._schedule_undo_stack.pop(0)
        self.undo_schedule_button.setVisible(True)

    def _undo_schedule_change(self) -> None:
        if not self._schedule_undo_stack:
            self.undo_schedule_button.setVisible(False)
            return

        rules, selected_index, schedule_enabled, gradual = self._schedule_undo_stack.pop()
        self._restoring_schedule_undo = True
        try:
            self.schedule_enabled_checkbox.blockSignals(True)
            self.gradual_checkbox.blockSignals(True)
            self.schedule_enabled_checkbox.setChecked(schedule_enabled)
            self.gradual_checkbox.setChecked(gradual)
            self.schedule_enabled_checkbox.blockSignals(False)
            self.gradual_checkbox.blockSignals(False)
            self.schedule_graph.set_gradual(gradual)
            self._set_schedule_rules(rules, selected_index=selected_index)
            self._update_schedule_controls_visibility(schedule_enabled)
        finally:
            self.schedule_enabled_checkbox.blockSignals(False)
            self.gradual_checkbox.blockSignals(False)
            self._restoring_schedule_undo = False

        self.undo_schedule_button.setVisible(bool(self._schedule_undo_stack))
        self._refresh_dialog_size()

    def _add_empty_rule(self) -> None:
        self._remember_schedule_state()
        selected_index = self.schedule_graph.selected_index()
        if 0 <= selected_index < len(self._schedule_rules):
            selected_rule = self._schedule_rules[selected_index]
            target = selected_rule.target
            brightness = selected_rule.brightness
        else:
            target = "both"
            brightness = 70

        timezone_name = self._location_timezone_name
        try:
            current_time = datetime.now(ZoneInfo(timezone_name)) if timezone_name else datetime.now().astimezone()
        except Exception:
            current_time = datetime.now().astimezone()
        minutes = _round_to_step(current_time.hour * 60 + current_time.minute, 5)
        new_rule = ScheduleRule(
            anchor="time",
            offset_minutes=0,
            brightness=clamp_brightness(brightness),
            target=target,
            specific_time=_format_time_from_minutes(minutes),
        )
        updated_rules = [deepcopy(rule) for rule in self._schedule_rules]
        updated_rules.append(new_rule)
        self.schedule_enabled_checkbox.setChecked(True)
        self._set_schedule_rules(updated_rules, selected_index=len(updated_rules) - 1)
        self._refresh_dialog_size()

    @staticmethod
    def _default_sunrise_sunset_ramp_rules(target: str = "both") -> list[ScheduleRule]:
        if target not in ("display1", "display2", "both"):
            target = "both"
        return [
            ScheduleRule(anchor="sunrise", offset_minutes=-60, brightness=50, target=target),
            ScheduleRule(anchor="sunrise", offset_minutes=-30, brightness=75, target=target),
            ScheduleRule(anchor="sunrise", offset_minutes=0, brightness=100, target=target),
            ScheduleRule(anchor="sunset", offset_minutes=0, brightness=100, target=target),
            ScheduleRule(anchor="sunset", offset_minutes=30, brightness=75, target=target),
            ScheduleRule(anchor="sunset", offset_minutes=60, brightness=50, target=target),
        ]

    def _apply_default_sunrise_sunset_rules(self) -> None:
        self._remember_schedule_state()
        selected_index = self.schedule_graph.selected_index()
        if 0 <= selected_index < len(self._schedule_rules):
            target = self._schedule_rules[selected_index].target
        else:
            target = str(self.point_target_combo.currentData() or "both")
        if target not in ("display1", "display2", "both"):
            target = "both"

        preset_rules = self._default_sunrise_sunset_ramp_rules(target)
        if target == "both":
            updated_rules = preset_rules
        else:
            updated_rules = [
                deepcopy(rule)
                for rule in self._schedule_rules
                if rule.target not in (target, "both")
            ]
            updated_rules.extend(preset_rules)
        first_new_index = max(0, len(updated_rules) - len(preset_rules))
        self.schedule_enabled_checkbox.setChecked(True)
        self.gradual_checkbox.setChecked(True)
        self.schedule_graph.set_gradual(True)
        self._set_schedule_rules(updated_rules, selected_index=first_new_index)
        self._refresh_default_rules_button_state()
        self._refresh_dialog_size()

    def _set_schedule_rules(
        self,
        rules: list[ScheduleRule],
        selected_index: int | None = None,
    ) -> None:
        self._schedule_rules = [deepcopy(rule) for rule in rules]
        if selected_index is None:
            selected_index = self.schedule_graph.selected_index()
        self.schedule_graph.set_rules(self._schedule_rules, selected_index=selected_index)
        self._refresh_default_rules_button_state()
        self._sync_selected_rule_editor()

    def _handle_schedule_graph_rules_changed(self, rules: object) -> None:
        if not isinstance(rules, list):
            return
        self._schedule_rules = [
            deepcopy(rule) for rule in rules if isinstance(rule, ScheduleRule)
        ]
        self._refresh_default_rules_button_state()
        self._sync_selected_rule_editor()

    def _handle_schedule_graph_selection_changed(self, _index: int) -> None:
        self._sync_selected_rule_editor()

    def _set_combo_current_data(self, combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _sync_selected_rule_editor(self) -> None:
        selected_index = self.schedule_graph.selected_index()
        selected_rule = (
            self._schedule_rules[selected_index]
            if 0 <= selected_index < len(self._schedule_rules)
            else None
        )
        enabled = selected_rule is not None
        self._syncing_rule_editor = True
        try:
            self.selected_rule_label.setText(
                f"Point {selected_index + 1}/{len(self._schedule_rules)}"
                if enabled
                else "Point"
            )
            for widget in (
                self.point_target_combo,
                self.point_anchor_combo,
                self.point_time_edit,
                self.point_offset_spin,
                self.point_brightness_spin,
            ):
                widget.setEnabled(enabled)
            self.remove_rule_button.setEnabled(enabled)

            if selected_rule is None:
                self.point_time_edit.clear()
                self.point_offset_spin.setValue(0)
                self.point_brightness_spin.setValue(0)
                return

            self._set_combo_current_data(self.point_target_combo, selected_rule.target)
            if selected_rule.anchor == "time":
                self.point_anchor_combo.setCurrentText("specific time")
            else:
                self.point_anchor_combo.setCurrentText(selected_rule.anchor)
            self.point_time_edit.setText(
                _format_time_from_minutes(
                    self.schedule_graph.clock_minutes_for_rule(selected_rule)
                )
            )
            self.point_offset_spin.setValue(int(selected_rule.offset_minutes))
            self.point_brightness_spin.setValue(clamp_brightness(selected_rule.brightness))
            self._sync_rule_anchor_mode(
                self.point_anchor_combo,
                self.point_time_edit,
                self.point_offset_spin,
            )
        finally:
            self._syncing_rule_editor = False

    def _handle_point_anchor_changed(self) -> None:
        if self._syncing_rule_editor:
            return
        selected_index = self.schedule_graph.selected_index()
        if (
            _parse_time_to_minutes(self.point_time_edit.text()) is None
            and 0 <= selected_index < len(self._schedule_rules)
        ):
            self.point_time_edit.setText(
                _format_time_from_minutes(
                    self.schedule_graph.clock_minutes_for_rule(
                        self._schedule_rules[selected_index]
                    )
                )
            )
        self._syncing_rule_editor = True
        try:
            self._sync_rule_anchor_mode(
                self.point_anchor_combo,
                self.point_time_edit,
                self.point_offset_spin,
            )
        finally:
            self._syncing_rule_editor = False
        self._apply_rule_editor_to_selected(time_is_source=True)

    def _handle_point_offset_changed(self) -> None:
        if self._syncing_rule_editor:
            return
        self._apply_rule_editor_to_selected(time_is_source=False)

    def _rule_from_editor(
        self,
        time_is_source: bool = False,
    ) -> tuple[ScheduleRule | None, str | None]:
        selected_index = self.schedule_graph.selected_index()
        if selected_index < 0 or selected_index >= len(self._schedule_rules):
            return (None, None)

        current_rule = self._schedule_rules[selected_index]
        target = str(self.point_target_combo.currentData() or "").strip().lower()
        if target not in ("display1", "display2", "both"):
            target = "both"

        anchor_text = self.point_anchor_combo.currentText().strip().lower()
        brightness = clamp_brightness(self.point_brightness_spin.value())
        if anchor_text == "specific time":
            specific_time = self._normalize_time_text(self.point_time_edit.text())
            if specific_time is None:
                return (None, "specific time must be HH:MM (24-hour).")
            return (
                ScheduleRule(
                    anchor="time",
                    offset_minutes=0,
                    brightness=brightness,
                    target=target,
                    specific_time=specific_time,
                ),
                None,
            )

        if anchor_text not in ("sunrise", "sunset"):
            return (None, "anchor must be sunrise, sunset, or specific time.")

        if time_is_source:
            clock_minutes = _parse_time_to_minutes(self.point_time_edit.text())
            if clock_minutes is None:
                return (None, "time must be HH:MM (24-hour).")
            previous_offset = (
                int(current_rule.offset_minutes)
                if current_rule.anchor in ("sunrise", "sunset")
                else 0
            )
            offset_minutes = self.schedule_graph.offset_for_anchor_time(
                anchor_text,
                clock_minutes,
                previous_offset,
            )
        else:
            offset_minutes = int(self.point_offset_spin.value())

        return (
            ScheduleRule(
                anchor=anchor_text,
                offset_minutes=offset_minutes,
                brightness=brightness,
                target=target,
                specific_time=None,
            ),
            None,
        )

    def _apply_rule_editor_to_selected(self, time_is_source: bool = False) -> None:
        if self._syncing_rule_editor:
            return
        selected_index = self.schedule_graph.selected_index()
        if selected_index < 0 or selected_index >= len(self._schedule_rules):
            return
        rule, error = self._rule_from_editor(time_is_source=time_is_source)
        if error is not None or rule is None:
            return
        if self._schedule_rules[selected_index] == rule:
            return
        self._remember_schedule_state()
        self._schedule_rules[selected_index] = rule
        self.schedule_graph.set_rules(self._schedule_rules, selected_index=selected_index)
        self._refresh_default_rules_button_state()
        if time_is_source:
            self._syncing_rule_editor = True
            try:
                self.point_offset_spin.setValue(int(rule.offset_minutes))
            finally:
                self._syncing_rule_editor = False
        else:
            self._sync_selected_rule_editor()

    def _rules_match_exact_slice(self, start_index: int, expected_rules: list[ScheduleRule]) -> bool:
        for offset, expected_rule in enumerate(expected_rules):
            actual_rule = self._schedule_rules[start_index + offset]
            if actual_rule != expected_rule:
                return False
        return True

    def _has_untouched_default_sunrise_sunset_block(self) -> bool:
        for target in ("display1", "display2", "both"):
            expected_rules = self._default_sunrise_sunset_ramp_rules(target)
            expected_count = len(expected_rules)
            row_count = len(self._schedule_rules)
            if row_count < expected_count:
                continue
            for start_index in range(0, row_count - expected_count + 1):
                if self._rules_match_exact_slice(start_index, expected_rules):
                    return True
        return False

    def _refresh_default_rules_button_state(self) -> None:
        self.load_default_sun_rules_button.setVisible(True)
        self.load_default_sun_rules_button.setText("Apply Preset Curve")
        self.load_default_sun_rules_button.setToolTip(
            "Replace the selected display curve with the original default sunrise/sunset preset."
        )

    def _sync_rule_anchor_mode(
        self, anchor_combo: QComboBox, time_edit: QLineEdit, offset_spin: QSpinBox
    ) -> None:
        anchor_text = anchor_combo.currentText().strip().lower()
        is_specific_time = anchor_text == "specific time"

        time_edit.setEnabled(True)
        offset_spin.setEnabled(not is_specific_time)
        if is_specific_time:
            offset_spin.setValue(0)
            if not time_edit.text().strip():
                time_edit.setText("12:00")

    def _remove_selected_rule(self) -> None:
        selected_index = self.schedule_graph.selected_index()
        if selected_index < 0 or selected_index >= len(self._schedule_rules):
            return
        self._remember_schedule_state()
        updated_rules = [deepcopy(rule) for rule in self._schedule_rules]
        updated_rules.pop(selected_index)
        next_index = min(selected_index, len(updated_rules) - 1)
        self._set_schedule_rules(updated_rules, selected_index=next_index)
        self._refresh_dialog_size()

    @staticmethod
    def _normalize_time_text(raw_text: str) -> str | None:
        minutes = _parse_time_to_minutes(raw_text)
        if minutes is None:
            return None
        return _format_time_from_minutes(minutes)

    def _collect_rules(self) -> tuple[list[ScheduleRule], str | None]:
        rules = [deepcopy(rule) for rule in self._schedule_rules]
        selected_index = self.schedule_graph.selected_index()
        if 0 <= selected_index < len(rules):
            editor_rule, error = self._rule_from_editor(time_is_source=True)
            if error is not None:
                return ([], f"Point {selected_index + 1}: {error}")
            if editor_rule is not None:
                rules[selected_index] = editor_rule

        normalized_rules: list[ScheduleRule] = []
        for rule_index, rule in enumerate(rules):
            target = str(rule.target).strip().lower()
            if target not in ("display1", "display2", "both"):
                target = "both"

            if rule.anchor == "time":
                specific_time = self._normalize_time_text(rule.specific_time or "")
                if specific_time is None:
                    return ([], f"Point {rule_index + 1}: specific time must be HH:MM (24-hour).")
                normalized_rules.append(
                    ScheduleRule(
                        anchor="time",
                        offset_minutes=0,
                        brightness=clamp_brightness(rule.brightness),
                        target=target,
                        specific_time=specific_time,
                    )
                )
                continue

            if rule.anchor not in ("sunrise", "sunset"):
                continue

            normalized_rules.append(
                ScheduleRule(
                    anchor=rule.anchor,
                    offset_minutes=max(-1440, min(1440, int(rule.offset_minutes))),
                    brightness=clamp_brightness(rule.brightness),
                    target=target,
                    specific_time=None,
                )
            )
        return (normalized_rules, None)

    def _save_and_close(self) -> None:
        self._refresh_auto_location(refresh_detection=False)

        rules, error = self._collect_rules()
        if error:
            QMessageBox.warning(self, "Invalid Rule", error)
            return
        if (
            not self.auto_location_checkbox.isChecked()
            and self.latitude_spin.value() == 0.0
            and self.longitude_spin.value() == 0.0
            and any(rule.anchor in ("sunrise", "sunset") for rule in rules)
        ):
            QMessageBox.warning(
                self,
                "Location Needed",
                "Enter your latitude and longitude, or turn on automatic location detection.",
            )
            return
        if self.schedule_enabled_checkbox.isChecked() and not rules:
            QMessageBox.warning(
                self,
                "Missing Schedule Rules",
                "Add at least one rule when schedule is enabled.",
            )
            return

        updated = deepcopy(self._source_config)
        updated.theme = self._selected_theme
        updated.startup_enabled = self.startup_checkbox.isChecked()
        updated.blur_enabled = self.blur_checkbox.isChecked()
        updated.schedule.enabled = self.schedule_enabled_checkbox.isChecked()
        updated.schedule.gradual = self.gradual_checkbox.isChecked()
        updated.schedule.auto_location = self.auto_location_checkbox.isChecked()
        if updated.schedule.auto_location:
            if self._detected_location is not None:
                updated.schedule.latitude = self._detected_location.latitude
                updated.schedule.longitude = self._detected_location.longitude
        else:
            updated.schedule.latitude = self.latitude_spin.value()
            updated.schedule.longitude = self.longitude_spin.value()
        updated.schedule.rules = rules or default_schedule_rules()

        self.updated_config = updated
        self.accept()

    def _handle_theme_toggle(self, theme_name: str, checked: bool) -> None:
        if not checked:
            return
        self._set_selected_theme(theme_name, preview=True)

    def _set_selected_theme(self, theme_name: str, preview: bool) -> None:
        normalized_theme = normalize_theme_name(theme_name)
        self._selected_theme = normalized_theme
        for option_theme, button in self.theme_buttons.items():
            should_check = option_theme == normalized_theme
            if button.isChecked() == should_check:
                continue
            button.blockSignals(True)
            button.setChecked(should_check)
            button.blockSignals(False)
        if preview:
            apply_app_theme(normalized_theme)

    def reject(self) -> None:  # type: ignore[override]
        apply_app_theme(self._initial_theme)
        if self.blur_checkbox.isChecked() != self._initial_blur:
            set_blur_enabled(self._initial_blur)
            parent = self.parentWidget()
            if parent is not None:
                apply_backdrop(parent)
        super().reject()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        del event
        painter = QPainter(self)
        paint_glass(self, painter)
        painter.end()


class BrightnessControlWindow(QWidget):
    settings_changed = Signal(object)
    write_failed = Signal(str)
    write_recovered = Signal(str)
    _AMBIENT_TIMER_INTERVAL_MS = 1_100
    _AMBIENT_APPLY_DEADBAND = 2
    _AMBIENT_MAX_STEP = 4
    _AMBIENT_SMOOTHING_ALPHA = 0.24
    _AMBIENT_PERSIST_INTERVAL_SEC = 5.0

    def __init__(
        self,
        service: BrightnessService,
        config_store: ConfigStore,
        config: AppConfig,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.service = service
        self.config_store = config_store
        self.config = config

        self.setWindowTitle("Remote Monitor Controller")
        self.setWindowFlag(Qt.WindowType.Popup, True)
        self.setMinimumWidth(400)
        self.resize(420, 260)
        _apply_rounded_popup_chrome(self, "brightnessControlPopup")

        self._internal_ui_update = False
        self.monitor_rows: list[MonitorSliderRow] = []
        self._schedule_status_text = "Schedule: off"
        self._popup_anchor_point: QPoint | None = None
        self._ambient_service = AmbientLightService()
        self._ambient_smoothed_target: float | None = None
        self._last_ambient_applied: int | None = None
        self._last_ambient_persist_ts = 0.0
        self._last_popup_hide_monotonic = 0.0
        self._refresh_in_flight = False
        self._ambient_probe_in_flight = False

        self._writer = BrightnessWriter(
            service,
            on_failure=self.write_failed.emit,
            on_success=self.write_recovered.emit,
        )
        self.write_failed.connect(self._handle_write_failed)
        self.write_recovered.connect(self._handle_write_recovered)

        self._persist_timer = QTimer(self)
        self._persist_timer.setSingleShot(True)
        self._persist_timer.setInterval(350)
        self._persist_timer.timeout.connect(self._persist_config)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(18, 14, 18, 16)
        root_layout.setSpacing(12)

        header_row = QHBoxLayout()
        header_row.setSpacing(10)
        self.info_label = QLabel("")
        self.info_label.setObjectName("muted")
        self.info_label.setWordWrap(True)
        header_row.addWidget(self.info_label, stretch=1)
        self.refresh_button = _flat_tool_button("refresh", "Refresh monitors")
        self.settings_button = _flat_tool_button("settings", "Settings")
        header_row.addWidget(self.refresh_button)
        header_row.addWidget(self.settings_button)
        root_layout.addLayout(header_row)

        control_row = QHBoxLayout()
        control_row.setSpacing(8)
        self.link_button = QPushButton("Unlinked")
        self.link_button.setObjectName("pill")
        self.link_button.setCheckable(True)
        self.link_button.setMinimumWidth(96)
        self.link_button.setCursor(Qt.CursorShape.PointingHandCursor)
        control_row.addWidget(self.link_button)
        self.ambient_button = QPushButton("Auto Light")
        self.ambient_button.setObjectName("pill")
        self.ambient_button.setCheckable(True)
        self.ambient_button.setMinimumWidth(100)
        self.ambient_button.setCursor(Qt.CursorShape.PointingHandCursor)
        control_row.addWidget(self.ambient_button)
        control_row.addStretch(1)
        self.status_chip = QLabel("")
        self.status_chip.setObjectName("statusChip")
        self.status_chip.setVisible(False)
        control_row.addWidget(self.status_chip)
        root_layout.addLayout(control_row)

        self.combined_group = QFrame()
        self.combined_group.setObjectName("card")
        combined_layout = QVBoxLayout(self.combined_group)
        combined_layout.setContentsMargins(16, 12, 16, 12)
        combined_layout.setSpacing(0)
        combined_header = QHBoxLayout()
        self.combined_label = QLabel("All displays")
        self.combined_label.setObjectName("muted")
        combined_header.addWidget(self.combined_label)
        combined_header.addStretch(1)
        self.global_value_label = QLabel("0%")
        self.global_value_label.setObjectName("bigValue")
        self.global_value_label.setMinimumWidth(86)
        self.global_value_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.global_value_label.setStyleSheet(
            _big_value_style(self.config.last_global_brightness)
        )
        combined_header.addWidget(self.global_value_label)
        combined_layout.addLayout(combined_header)
        self.global_slider = GlowSlider(Qt.Orientation.Horizontal, brightness_scale=True)
        self.global_slider.setRange(0, 100)
        combined_layout.addWidget(self.global_slider)
        root_layout.addWidget(self.combined_group)

        self.monitors_group = QFrame()
        self.monitors_group.setObjectName("card")
        monitors_layout = QVBoxLayout(self.monitors_group)
        monitors_layout.setContentsMargins(14, 12, 10, 12)
        self.monitor_scroll = QScrollArea()
        self.monitor_scroll.setObjectName("monitorScroll")
        self.monitor_scroll.setWidgetResizable(True)
        self.monitor_scroll.setMinimumHeight(1)
        self.monitor_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.monitor_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.monitor_scroll_content = QWidget()
        self.monitor_scroll_content.setObjectName("monitorScrollContent")
        self.monitor_rows_layout = QVBoxLayout(self.monitor_scroll_content)
        self.monitor_rows_layout.setContentsMargins(0, 0, 4, 0)
        self.monitor_rows_layout.setSpacing(12)
        self.monitor_scroll.setWidget(self.monitor_scroll_content)
        monitors_layout.addWidget(self.monitor_scroll)
        root_layout.addWidget(self.monitors_group)

        self.refresh_button.clicked.connect(lambda _checked=False: self._handle_refresh_button())
        self.settings_button.clicked.connect(lambda _checked=False: self._open_settings_dialog())
        self.global_slider.valueChanged.connect(self._handle_global_slider_change)
        self.link_button.toggled.connect(self._handle_link_toggle)
        self.ambient_button.toggled.connect(self._handle_ambient_toggle)

        self._ambient_timer = QTimer(self)
        self._ambient_timer.setInterval(self._AMBIENT_TIMER_INTERVAL_MS)
        self._ambient_timer.timeout.connect(self._handle_ambient_timer_tick)

        self._set_global_slider_value(self.config.last_global_brightness)
        self._internal_ui_update = True
        self.link_button.setChecked(self.config.link_mode)
        self.ambient_button.setChecked(self.config.ambient_auto_enabled)
        self._internal_ui_update = False
        self._update_link_mode_ui()
        self._refresh_toolbar_icons()
        self.refresh_monitors(apply_saved=True)
        self._set_ambient_enabled(self.config.ambient_auto_enabled, persist=False)

    def show_as_popup(self, anchor_point: QPoint) -> None:
        self._popup_anchor_point = QPoint(anchor_point)
        self._apply_popup_geometry(anchor_point)
        self.show()
        _apply_native_rounded_corners(self)
        self.raise_()
        self.activateWindow()
        animate_in(self, _slide_offset_for(self))
        self._last_popup_hide_monotonic = 0.0

    def has_open_popups(self) -> bool:
        if self.isVisible():
            return True
        for dialog in self.findChildren(QDialog):
            if dialog.isVisible():
                return True
        return False

    def close_all_popups(self) -> None:
        for dialog in self.findChildren(QDialog):
            if not dialog.isVisible():
                continue
            try:
                dialog.reject()
            except Exception:
                dialog.close()
        self.hide()

    def was_recently_hidden(self, within_seconds: float) -> bool:
        if self._last_popup_hide_monotonic <= 0.0:
            return False
        return (wall_time.monotonic() - self._last_popup_hide_monotonic) <= max(
            0.0,
            float(within_seconds),
        )

    def is_ambient_auto_enabled(self) -> bool:
        return bool(self.config.ambient_auto_enabled)

    def _apply_popup_geometry(self, anchor_point: QPoint | None = None) -> None:
        if anchor_point is None:
            anchor = self._popup_anchor_point or QCursor.pos()
        else:
            anchor = QPoint(anchor_point)
            self._popup_anchor_point = QPoint(anchor_point)

        self._sync_monitor_scroll_height()
        self.layout().activate()
        self.adjustSize()
        preferred_size = self.sizeHint()
        width = max(400, min(520, preferred_size.width()))
        compact_layout = self._is_link_mode_effective() or len(self.monitor_rows) <= 1
        min_height = 118 if compact_layout else 160
        height = max(min_height, min(640, preferred_size.height()))
        self.resize(width, height)

        screen = (
            QGuiApplication.screenAt(anchor)
            or self.screen()
            or QGuiApplication.primaryScreen()
        )
        target_x = anchor.x()
        target_y = anchor.y()

        if screen is not None:
            available = screen.availableGeometry()
            target = _bottom_right_popup_position(self, available)
            target_x = target.x()
            target_y = target.y()

        self.move(target_x, target_y)

    def refresh_monitors(self, apply_saved: bool) -> None:
        """Blocking refresh (used once at startup). Prefer ``refresh_monitors_async``."""
        try:
            monitors = self.service.refresh_monitors()
            readings = [(monitor, self.service.get_brightness(monitor)) for monitor in monitors]
        except Exception as error:
            self.info_label.setText(f"Could not access monitors: {error}")
            readings = []
        self._rebuild_rows(readings, apply_saved)

    def refresh_monitors_async(self) -> None:
        """Re-detect displays off the GUI thread so opening the popup never stutters."""
        if self._refresh_in_flight:
            return
        self._refresh_in_flight = True
        self.refresh_button.setEnabled(False)
        self.refresh_button.setToolTip("Refreshing…")

        def work():
            with com_initialized():
                monitors = self.service.refresh_monitors()
                return [(monitor, self.service.get_brightness(monitor)) for monitor in monitors]

        run_async(self, work, self._finish_refresh)

    def _finish_refresh(self, readings, error) -> None:
        self._refresh_in_flight = False
        self.refresh_button.setEnabled(True)
        self.refresh_button.setToolTip("Refresh monitors")
        if error is not None:
            self.info_label.setText(f"Could not access monitors: {error}")
            return

        known_keys = [row.monitor.key for row in self.monitor_rows]
        fresh_keys = [monitor.key for monitor, _level in readings]
        if known_keys and known_keys == fresh_keys:
            for row, (_monitor, level) in zip(self.monitor_rows, readings):
                if level is None or row.slider.isSliderDown():
                    continue
                row.set_value(level)
            self.info_label.setText(f"Detected {len(readings)} display(s).")
            self._sync_global_slider_to_average()
        else:
            self._rebuild_rows(readings, apply_saved=False)
            return
        if self.config.ambient_auto_enabled:
            self._handle_ambient_timer_tick()

    def _rebuild_rows(self, readings, apply_saved: bool) -> None:
        self._clear_monitor_rows()

        if not readings:
            self.info_label.setText(
                "No compatible displays detected. "
                "Enable DDC/CI in your monitor settings if needed."
            )
            self._update_link_mode_ui()
            self._refresh_visible_popup_geometry()
            return

        self.info_label.setText(f"Detected {len(readings)} display(s).")
        for monitor, current_level in readings:
            saved_level = self.config.monitor_levels.get(monitor.key)
            if apply_saved:
                initial_level = saved_level if saved_level is not None else (
                    current_level if current_level is not None else self.config.last_global_brightness
                )
            else:
                initial_level = current_level if current_level is not None else (
                    saved_level if saved_level is not None else self.config.last_global_brightness
                )

            row = MonitorSliderRow(monitor, initial_level)
            row.brightness_changed.connect(self._handle_monitor_slider_change)
            self.monitor_rows.append(row)
            self.monitor_rows_layout.addWidget(row)

        self._update_link_mode_ui()
        self._sync_monitor_scroll_height()
        self._refresh_visible_popup_geometry()

        if apply_saved:
            self.apply_saved_profile()
            if self.config.ambient_auto_enabled:
                self._handle_ambient_timer_tick()
            return
        self._sync_global_slider_to_average()
        if self.config.ambient_auto_enabled:
            self._handle_ambient_timer_tick()

    def _write_brightness(self, monitor: MonitorHandle, value: int) -> None:
        self._writer.submit(monitor, value)

    def _handle_write_failed(self, monitor_name: str) -> None:
        self.info_label.setText(
            f"Couldn't change brightness on {monitor_name}. Check that DDC/CI is enabled in its menu."
        )

    def _handle_write_recovered(self, monitor_name: str) -> None:
        del monitor_name
        self.info_label.setText(f"{len(self.monitor_rows)} display(s) connected.")

    def apply_saved_profile(self) -> None:
        if not self.monitor_rows:
            return

        if self.config.link_mode:
            self.apply_brightness_to_all(self.config.last_global_brightness, persist=False)
            return

        for row in self.monitor_rows:
            level = self.config.monitor_levels.get(row.monitor.key)
            if level is None:
                continue
            self._write_brightness(row.monitor, level)
            row.set_value(level)
        self._sync_global_slider_to_average()

    def apply_brightness_to_all(self, value: int, persist: bool = True) -> None:
        bounded = clamp_brightness(value)
        for row in self.monitor_rows:
            self._write_brightness(row.monitor, bounded)
            row.set_value(bounded)

        self._set_global_slider_value(bounded)
        if persist:
            self.config.last_global_brightness = bounded
            for row in self.monitor_rows:
                self.config.monitor_levels[row.monitor.key] = bounded
            self._persist_config_soon()

    def apply_brightness_map(self, values: dict[str, int], persist: bool = True) -> None:
        applied: list[int] = []
        for row in self.monitor_rows:
            if row.monitor.key not in values:
                continue
            level = clamp_brightness(values[row.monitor.key])
            self._write_brightness(row.monitor, level)
            row.set_value(level)
            self.config.monitor_levels[row.monitor.key] = level
            applied.append(level)

        if not applied:
            return

        average_level = round(sum(applied) / len(applied))
        self.config.last_global_brightness = clamp_brightness(average_level)
        self._set_global_slider_value(average_level)
        if persist:
            self._persist_config()

    def apply_schedule_targets(self, targets: dict[str, int], persist: bool = True) -> None:
        self.apply_brightness_map(targets, persist=persist)

    def set_schedule_status(self, text: str) -> None:
        if text == self._schedule_status_text:
            return
        self._schedule_status_text = text
        show_chip = bool(text) and text != "Schedule: off"
        chip_text = ""
        if show_chip:
            body = text.replace("Schedule: ", "", 1).rstrip(".")
            body = body[:1].upper() + body[1:]
            chip_text = QFontMetrics(self.status_chip.font()).elidedText(
                f"◷  {body}", Qt.TextElideMode.ElideRight, 250
            )
        self.status_chip.setText(chip_text)
        self.status_chip.setToolTip(text)
        if self.status_chip.isVisible() != show_chip:
            self.status_chip.setVisible(show_chip)
        self._refresh_visible_popup_geometry()

    def set_link_mode(
        self, linked: bool, persist: bool = True, apply_link_brightness: bool = True
    ) -> None:
        self.config.link_mode = linked
        if self.link_button.isChecked() != linked:
            self._internal_ui_update = True
            self.link_button.setChecked(linked)
            self._internal_ui_update = False
        self._update_link_mode_ui()

        if linked and apply_link_brightness:
            self.apply_brightness_to_all(self.global_slider.value(), persist=persist)
            return
        if persist:
            self._persist_config()

    def _load_refresh_icon(self) -> QIcon:
        return glyph_icon("refresh", active_theme().fg)

    def _build_settings_icon(self) -> QIcon:
        return glyph_icon("settings", active_theme().fg)

    def _refresh_toolbar_icons(self) -> None:
        self.refresh_button.setIcon(self._load_refresh_icon())
        self.settings_button.setIcon(self._build_settings_icon())

    def event(self, event) -> bool:  # type: ignore[override]
        if event.type() == QEvent.Type.StyleChange and hasattr(self, "settings_button"):
            self._refresh_toolbar_icons()
        return super().event(event)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        del event
        painter = QPainter(self)
        paint_glass(self, painter)
        painter.end()

    def _handle_refresh_button(self) -> None:
        self.refresh_monitors_async()

    def _handle_global_slider_change(self, value: int) -> None:
        bounded = clamp_brightness(value)
        _apply_brightness_slider_style(self.global_slider, bounded)
        self.global_value_label.setText(f"{bounded}%")
        self.global_value_label.setStyleSheet(_big_value_style(bounded))
        if self._internal_ui_update:
            return
        if not self.config.link_mode:
            return
        self.apply_brightness_to_all(bounded, persist=True)

    def _handle_link_toggle(self, checked: bool) -> None:
        if self._internal_ui_update:
            return
        self.set_link_mode(checked, persist=True, apply_link_brightness=checked)

    def _handle_ambient_toggle(self, checked: bool) -> None:
        if self._internal_ui_update:
            return
        self._set_ambient_enabled(checked, persist=True)

    def _set_ambient_enabled(self, enabled: bool, persist: bool) -> None:
        if enabled:
            self._begin_ambient_enable(persist)
            return

        self.config.ambient_auto_enabled = False
        self._ambient_timer.stop()
        self._ambient_service.stop()
        self._ambient_smoothed_target = None
        self._last_ambient_applied = None
        self._last_ambient_persist_ts = 0.0
        self._update_link_mode_ui()
        if persist:
            self._persist_config()

    def _begin_ambient_enable(self, persist: bool) -> None:
        """Probe the light sensor off the GUI thread (PowerShell can take a couple of seconds)."""
        if self._ambient_probe_in_flight:
            return
        self._ambient_probe_in_flight = True
        self.ambient_button.setEnabled(False)
        self.ambient_button.setToolTip("Checking the ambient light sensor…")
        run_async(
            self,
            self._ambient_service.probe_sensor,
            lambda lux, error: self._finish_ambient_enable(lux, error, persist),
        )

    def _finish_ambient_enable(self, detected_lux, error, persist: bool) -> None:
        self._ambient_probe_in_flight = False
        self.ambient_button.setEnabled(True)
        if error is not None or detected_lux is None:
            self.config.ambient_auto_enabled = False
            self._internal_ui_update = True
            self.ambient_button.setChecked(False)
            self._internal_ui_update = False
            self.info_label.setText(
                "Ambient light sensor is unavailable. Auto Light was not enabled."
            )
            self._update_link_mode_ui()
            if persist:
                self._persist_config()
            return

        self.config.ambient_auto_enabled = True
        if not self.ambient_button.isChecked():
            self._internal_ui_update = True
            self.ambient_button.setChecked(True)
            self._internal_ui_update = False
        self._ambient_smoothed_target = None
        self._last_ambient_applied = None
        self._last_ambient_persist_ts = 0.0
        self._ambient_service.start()
        self._ambient_timer.start()
        self._handle_ambient_timer_tick()
        self._update_link_mode_ui()
        if persist:
            self._persist_config()

    def _handle_ambient_timer_tick(self) -> None:
        if not self.config.ambient_auto_enabled:
            return
        if not self.monitor_rows:
            return

        lux = self._ambient_service.latest_lux()
        if lux is None:
            return

        raw_target = self._map_lux_to_brightness(lux)
        if self._ambient_smoothed_target is None:
            self._ambient_smoothed_target = float(raw_target)
        else:
            self._ambient_smoothed_target += self._AMBIENT_SMOOTHING_ALPHA * (
                float(raw_target) - self._ambient_smoothed_target
            )

        desired_level = clamp_brightness(round(self._ambient_smoothed_target))
        if self._last_ambient_applied is None:
            next_level = desired_level
        else:
            delta = desired_level - self._last_ambient_applied
            if abs(delta) < self._AMBIENT_APPLY_DEADBAND:
                return
            step = max(-self._AMBIENT_MAX_STEP, min(self._AMBIENT_MAX_STEP, delta))
            next_level = clamp_brightness(self._last_ambient_applied + step)

        target_rows = self._ambient_target_rows()
        if not target_rows:
            return

        applied_any = False
        for row in target_rows:
            if row.slider.value() == next_level:
                continue
            self._write_brightness(row.monitor, next_level)
            row.set_value(next_level)
            self.config.monitor_levels[row.monitor.key] = next_level
            applied_any = True

        if not applied_any and self._last_ambient_applied is not None:
            return

        self._last_ambient_applied = next_level
        if self.config.link_mode:
            self.config.last_global_brightness = next_level
            self._set_global_slider_value(next_level)
        else:
            self._sync_global_slider_to_average()

        now_seconds = wall_time.monotonic()
        if (
            self._last_ambient_persist_ts <= 0.0
            or now_seconds - self._last_ambient_persist_ts >= self._AMBIENT_PERSIST_INTERVAL_SEC
        ):
            self._persist_config()
            self._last_ambient_persist_ts = now_seconds

    def _ambient_target_rows(self) -> list[MonitorSliderRow]:
        if not self.monitor_rows:
            return []
        if self.config.link_mode:
            return list(self.monitor_rows)

        for row in self.monitor_rows:
            method_name = (row.monitor.method_name or "").strip().lower()
            if "wmi" in method_name:
                return [row]
        return [self.monitor_rows[0]]

    @staticmethod
    def _map_lux_to_brightness(lux: float) -> int:
        safe_lux = max(0.0, float(lux))
        normalized = math.log10(safe_lux + 1.0) / math.log10(801.0)
        normalized = max(0.0, min(1.0, normalized))
        minimum_brightness = 6.0
        maximum_brightness = 100.0
        target = minimum_brightness + normalized * (maximum_brightness - minimum_brightness)
        return clamp_brightness(target)

    def _handle_monitor_slider_change(self, monitor_key: str, value: int) -> None:
        if self._internal_ui_update:
            return

        if self.config.link_mode:
            self.apply_brightness_to_all(value, persist=True)
            return

        target_row = next((row for row in self.monitor_rows if row.monitor.key == monitor_key), None)
        if target_row is None:
            return

        self._write_brightness(target_row.monitor, value)
        self.config.monitor_levels[monitor_key] = clamp_brightness(value)
        self._sync_global_slider_to_average()
        self._persist_config_soon()

    def _sync_global_slider_to_average(self) -> None:
        if not self.monitor_rows:
            return
        average_level = round(
            sum(row.slider.value() for row in self.monitor_rows) / len(self.monitor_rows)
        )
        self.config.last_global_brightness = clamp_brightness(average_level)
        self._set_global_slider_value(average_level)

    def _set_global_slider_value(self, value: int) -> None:
        bounded = clamp_brightness(value)
        self._internal_ui_update = True
        self.global_slider.setValue(bounded)
        _apply_brightness_slider_style(self.global_slider, bounded)
        self.global_value_label.setText(f"{bounded}%")
        self.global_value_label.setStyleSheet(_big_value_style(bounded))
        self._internal_ui_update = False

    def _update_link_mode_ui(self) -> None:
        link_toggle_visible = len(self.monitor_rows) > 1
        linked = self._is_link_mode_effective()
        self.link_button.setVisible(link_toggle_visible)
        self.link_button.setText("Linked" if linked else "Unlinked")
        self.combined_group.setVisible(linked)
        self.monitors_group.setVisible(not linked)
        for row in self.monitor_rows:
            row.setVisible(not linked)
        self._sync_monitor_scroll_height()
        if self.config.ambient_auto_enabled:
            if linked:
                self.ambient_button.setToolTip("Auto Light controls all linked displays.")
            else:
                self.ambient_button.setToolTip("Auto Light controls only your computer display.")
        else:
            self.ambient_button.setToolTip("")
        self._refresh_visible_popup_geometry()

    def _clear_monitor_rows(self) -> None:
        while self.monitor_rows_layout.count():
            item = self.monitor_rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.monitor_rows = []
        self._sync_monitor_scroll_height()

    def _sync_monitor_scroll_height(self) -> None:
        if self._is_link_mode_effective():
            self.monitor_scroll.setFixedHeight(1)
            self.monitor_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            return
        if not self.monitor_rows:
            self.monitor_scroll.setFixedHeight(1)
            self.monitor_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            return
        spacing = self.monitor_rows_layout.spacing()
        margins = self.monitor_rows_layout.contentsMargins()
        content_height = margins.top() + margins.bottom()
        content_height += sum(row.sizeHint().height() for row in self.monitor_rows)
        content_height += max(0, len(self.monitor_rows) - 1) * spacing
        content_height += self.monitor_scroll.frameWidth() * 2
        max_height = 420
        needs_scroll = content_height > max_height
        self.monitor_scroll.setFixedHeight(max(1, min(max_height, content_height)))
        self.monitor_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
            if needs_scroll
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

    def _is_link_mode_effective(self) -> bool:
        return bool(self.config.link_mode) and len(self.monitor_rows) > 1

    def _refresh_visible_popup_geometry(self) -> None:
        if not self.isVisible():
            return
        self._apply_popup_geometry()

    def _open_settings_dialog(self) -> None:
        monitor_labels = [row.monitor.name for row in self.monitor_rows]
        monitor_handles = [row.monitor for row in self.monitor_rows]
        dialog = SettingsDialog(
            self.config,
            monitor_labels=monitor_labels,
            brightness_service=self.service,
            monitor_handles=monitor_handles,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if dialog.updated_config is None:
            return

        self.config = dialog.updated_config
        set_blur_enabled(self.config.blur_enabled)
        apply_backdrop(self)
        self._refresh_toolbar_icons()
        self._set_global_slider_value(self.config.last_global_brightness)
        self.set_link_mode(
            self.config.link_mode,
            persist=True,
            apply_link_brightness=self.config.link_mode,
        )
        self.settings_changed.emit(self.config)

    def _persist_config(self) -> None:
        self._persist_timer.stop()
        self.config_store.save(self.config)

    def _persist_config_soon(self) -> None:
        """Coalesce rapid slider drags into a single disk write."""
        self._persist_timer.start()

    def flush_pending_save(self) -> None:
        if self._persist_timer.isActive():
            self._persist_config()
        self._writer.stop()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.hide()
        event.accept()

    def hideEvent(self, event) -> None:  # type: ignore[override]
        self._last_popup_hide_monotonic = wall_time.monotonic()
        if self._persist_timer.isActive():
            self._persist_config()
        super().hideEvent(event)
