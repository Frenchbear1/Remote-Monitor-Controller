from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import math
from pathlib import Path
import sys
import time as wall_time
from zoneinfo import ZoneInfo

from PySide6.QtCore import QPoint, QPointF, QRegularExpression, QSize, QTimer, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QGuiApplication,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
    QRegularExpressionValidator,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGraphicsOpacityEffect,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStyle,
    QTableWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from astral import LocationInfo
from astral.sun import sun
from tzlocal import get_localzone_name

from .ambient_light import AmbientLightService
from .brightness_service import BrightnessService, MonitorHandle, PictureControl
from .config_store import ConfigStore
from .location import LocationContext, detect_location_context_from_ip
from .models import AppConfig, ScheduleRule, clamp_brightness, default_schedule_rules
from .themes import (
    THEME_DARK,
    THEME_GRAY,
    THEME_LIGHT,
    THEME_SAND,
    build_stylesheet,
    normalize_theme_name,
)

def _resolve_icon_path(filename: str) -> Path | None:
    module_dir = Path(__file__).resolve().parent
    candidate_paths = [
        module_dir / "assets" / "icons" / filename,
    ]

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        pyinstaller_root = Path(meipass)
        # Support both legacy and corrected PyInstaller data targets.
        candidate_paths.extend(
            [
                pyinstaller_root / "src" / "brightness_tray" / "assets" / "icons" / filename,
                pyinstaller_root / "brightness_tray" / "assets" / "icons" / filename,
            ]
        )

    for candidate in candidate_paths:
        if candidate.exists():
            return candidate
    return None


REFRESH_ICON_PATH = _resolve_icon_path("refresh.png")
SETTINGS_ICON_PATH = _resolve_icon_path("settings.png")
POPUP_EDGE_MARGIN_PX = 10
POPUP_TASKBAR_CLEARANCE_PX = 8


def _bottom_right_popup_position(widget: QWidget, available) -> QPoint:
    frame = widget.frameGeometry()
    desired_x = available.right() - frame.width() - POPUP_EDGE_MARGIN_PX + 1
    desired_y = (
        available.bottom()
        - frame.height()
        - (POPUP_EDGE_MARGIN_PX + POPUP_TASKBAR_CLEARANCE_PX)
        + 1
    )
    max_x = available.right() - frame.width() + 1
    max_y = available.bottom() - frame.height() + 1
    target_x = max(available.left(), min(desired_x, max_x))
    target_y = available.top() if desired_y < available.top() else min(desired_y, max_y)
    return QPoint(target_x, target_y)


def _apply_native_rounded_corners(widget: QWidget) -> None:
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return

    try:
        hwnd = int(widget.winId())
    except Exception:
        return
    if hwnd <= 0:
        return

    # DWM window corner preference (Windows 11+).
    dwm_window_corner_preference = 33
    dwmcp_round = 2
    corner_pref = ctypes.c_int(dwmcp_round)
    try:
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd),
            ctypes.c_uint(dwm_window_corner_preference),
            ctypes.byref(corner_pref),
            ctypes.sizeof(corner_pref),
        )
    except Exception:
        return


def _apply_rounded_popup_chrome(widget: QWidget, object_name: str) -> None:
    widget.setObjectName(object_name)
    _apply_native_rounded_corners(widget)


class MonitorSliderRow(QWidget):
    brightness_changed = Signal(str, int)

    def __init__(self, monitor: MonitorHandle, initial_value: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.monitor = monitor

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.name_label = QLabel(monitor.name)
        self.name_label.setMinimumWidth(120)
        layout.addWidget(self.name_label)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setValue(clamp_brightness(initial_value))
        layout.addWidget(self.slider, stretch=1)

        self.value_label = QLabel(f"{self.slider.value()}%")
        self.value_label.setMinimumWidth(40)
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.value_label)

        self.slider.valueChanged.connect(self._handle_slider_change)

    def set_value(self, value: int) -> None:
        bounded = clamp_brightness(value)
        self.slider.blockSignals(True)
        self.slider.setValue(bounded)
        self.slider.blockSignals(False)
        self.value_label.setText(f"{bounded}%")

    def _handle_slider_change(self, value: int) -> None:
        bounded = clamp_brightness(value)
        self.value_label.setText(f"{bounded}%")
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

        self.name_label = QLabel(f"{control.name} (0x{control.code:02X})")
        self.name_label.setMinimumWidth(190)
        layout.addWidget(self.name_label)

        self.slider = QSlider(Qt.Orientation.Horizontal)
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
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setMinimumWidth(620)
        self.resize(700, 420)
        _apply_rounded_popup_chrome(self, "pictureControlsDialog")

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(6, 6, 6, 6)
        root_layout.setSpacing(6)

        toolbar_layout = QHBoxLayout()
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_layout.setSpacing(8)
        self.refresh_button = QPushButton("Refresh Controls (Deep Scan)")
        toolbar_layout.addWidget(self.refresh_button)
        toolbar_layout.addStretch(1)
        root_layout.addLayout(toolbar_layout)

        self.content_scroll = QScrollArea()
        self.content_scroll.setWidgetResizable(True)
        root_layout.addWidget(self.content_scroll, stretch=1)

        self.content_widget = QWidget()
        self.content_layout = QVBoxLayout(self.content_widget)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(10)
        self.content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.content_scroll.setWidget(self.content_widget)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        footer_layout = QHBoxLayout()
        footer_layout.setContentsMargins(0, 0, 0, 0)
        footer_layout.setSpacing(8)
        footer_layout.addWidget(self.status_label, stretch=1)
        self.close_button = QPushButton("Close")
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
            + self.refresh_button.sizeHint().height()
            + max(self.status_label.sizeHint().height(), self.close_button.sizeHint().height())
            + (spacing * 2)
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
            group_layout.setContentsMargins(8, 4, 8, 8)
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
                failure_label.setWordWrap(True)
                group_layout.addWidget(failure_label)

            if not controls:
                unsupported_label = QLabel(
                    "No supported picture controls were reported by this display."
                )
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
        self.setWindowTitle("Brightness Tray Settings")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setMinimumWidth(760)
        _apply_rounded_popup_chrome(self, "settingsDialog")

        self._source_config = deepcopy(current_config)
        self._initial_theme = normalize_theme_name(current_config.theme)
        self._selected_theme = self._initial_theme
        self.updated_config: AppConfig | None = None
        self.monitor_labels = monitor_labels or []
        self.brightness_service = brightness_service
        self.monitor_handles = monitor_handles or []
        self._detected_location: LocationContext | None = None
        self._location_timezone_name: str | None = None
        self._sun_times_cache_key: tuple[str, float, float, str] | None = None

        root_layout = QVBoxLayout(self)
        self.content_scroll = QScrollArea()
        self.content_scroll.setObjectName("settingsContentScroll")
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root_layout.addWidget(self.content_scroll, stretch=1)

        self.content_widget = QWidget()
        self.content_widget.setObjectName("settingsContentWidget")
        content_layout = QVBoxLayout(self.content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)

        appearance_box = QGroupBox("Appearance")
        appearance_layout = QHBoxLayout(appearance_box)
        self.theme_button_group = QButtonGroup(self)
        self.theme_button_group.setExclusive(True)
        self.theme_buttons: dict[str, QPushButton] = {}
        for label, theme_name in (
            ("Light", THEME_LIGHT),
            ("Dark", THEME_DARK),
            ("Gray", THEME_GRAY),
            ("Sand", THEME_SAND),
        ):
            button = QPushButton(label)
            button.setCheckable(True)
            self.theme_button_group.addButton(button)
            self.theme_buttons[theme_name] = button
            button.toggled.connect(
                lambda checked, selected_theme=theme_name: self._handle_theme_toggle(
                    selected_theme,
                    checked,
                )
            )
            appearance_layout.addWidget(button)
        appearance_layout.addStretch(1)
        content_layout.addWidget(appearance_box)

        startup_box = QGroupBox("Startup")
        startup_layout = QVBoxLayout(startup_box)
        self.startup_checkbox = QCheckBox("Launch app automatically when Windows starts")
        startup_layout.addWidget(self.startup_checkbox)
        content_layout.addWidget(startup_box)

        picture_controls_box = QGroupBox("Display Picture Controls")
        picture_controls_layout = QVBoxLayout(picture_controls_box)
        self.picture_controls_help_label = QLabel(
            "Open monitor-specific picture sliders (for example contrast, sharpness, "
            "black level, and color controls) detected through DDC/CI."
        )
        self.picture_controls_help_label.setWordWrap(True)
        picture_controls_layout.addWidget(self.picture_controls_help_label)
        picture_controls_row = QHBoxLayout()
        self.picture_controls_button = QPushButton("Open Picture Controls")
        picture_controls_row.addWidget(self.picture_controls_button)
        picture_controls_row.addStretch(1)
        picture_controls_layout.addLayout(picture_controls_row)
        content_layout.addWidget(picture_controls_box)

        schedule_box = QGroupBox("Sunrise/Sunset Scheduling")
        schedule_layout = QVBoxLayout(schedule_box)

        schedule_top_layout = QHBoxLayout()
        schedule_toggle_layout = QVBoxLayout()
        self.schedule_enabled_checkbox = QCheckBox("Enable automatic schedule")
        self.gradual_checkbox = QCheckBox("Use gradual transitions between schedule points")
        schedule_toggle_layout.addWidget(self.schedule_enabled_checkbox)
        schedule_toggle_layout.addWidget(self.gradual_checkbox)
        schedule_top_layout.addLayout(schedule_toggle_layout, stretch=1)

        self.location_status_widget = QWidget()
        self.location_status_widget.setObjectName("scheduleTimeCard")
        location_status_layout = QVBoxLayout(self.location_status_widget)
        location_status_layout.setContentsMargins(8, 6, 8, 6)
        location_status_layout.setSpacing(6)
        self.location_time_label = QLabel("--:--")
        self.location_time_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.sunrise_time_label = QLabel("Sunrise --:--")
        self.sunrise_time_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.sunset_time_label = QLabel("Sunset --:--")
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
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight,
        )
        schedule_layout.addLayout(schedule_top_layout)

        self.rules_header_label = QLabel(
            "Rules: choose a display target, then anchor by sunrise/sunset (with offset) "
            "or use a fixed daily time (HH:MM, 24-hour)."
        )
        self.rules_header_label.setWordWrap(True)
        schedule_layout.addWidget(self.rules_header_label)

        self.rules_table = QTableWidget(0, 5)
        self.rules_table.setHorizontalHeaderLabels(
            [
                "Display Target",
                "Anchor",
                "Specific Time (HH:MM)",
                "Offset (min)",
                "Brightness (%)",
            ]
        )
        self.rules_table.verticalHeader().setVisible(False)
        self.rules_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.rules_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.rules_table.setShowGrid(False)
        self.rules_table.setStyleSheet(
            """
            QTableWidget::item:selected {
                background-color: rgba(59, 130, 246, 28);
                border-top: 1px solid rgba(59, 130, 246, 120);
                border-bottom: 1px solid rgba(59, 130, 246, 120);
            }
            """
        )
        self.rules_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.rules_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.rules_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.rules_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.rules_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        schedule_layout.addWidget(self.rules_table)

        self.rule_buttons_widget = QWidget()
        rule_button_layout = QHBoxLayout(self.rule_buttons_widget)
        rule_button_layout.setContentsMargins(0, 0, 0, 0)
        self.add_rule_button = QPushButton("Add Rule")
        self.remove_rule_button = QPushButton("Remove Selected Rule")
        self.load_default_sun_rules_button = QPushButton("Load Default Sunrise/Sunset Rules")
        self.add_rule_button.clicked.connect(lambda _checked=False: self._add_empty_rule())
        self.remove_rule_button.clicked.connect(lambda _checked=False: self._remove_selected_rule())
        self.load_default_sun_rules_button.clicked.connect(
            lambda _checked=False: self._apply_default_sunrise_sunset_rules()
        )
        self.load_default_sun_rules_button.setToolTip(
            "Add the preset sunrise/sunset ramp profile to the end of the list."
        )
        rule_button_layout.addWidget(self.add_rule_button)
        rule_button_layout.addWidget(self.remove_rule_button)
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
        root_layout.addWidget(button_box)
        self.button_box = button_box

        self.picture_controls_button.clicked.connect(
            lambda _checked=False: self._open_picture_controls_dialog()
        )
        self.schedule_enabled_checkbox.toggled.connect(self._handle_schedule_enabled_toggled)
        self._load_from_config()
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
        self.schedule_enabled_checkbox.setChecked(self._source_config.schedule.enabled)
        self.gradual_checkbox.setChecked(self._source_config.schedule.gradual)
        controls_available = self.brightness_service is not None and bool(self.monitor_handles)
        self.picture_controls_button.setEnabled(controls_available)
        if not controls_available:
            self.picture_controls_button.setToolTip("Refresh monitors to enable picture controls")
        else:
            self.picture_controls_button.setToolTip("")
        self._refresh_auto_location(refresh_detection=True)

        rules = self._source_config.schedule.rules or default_schedule_rules()
        for rule in rules:
            self._add_rule_row(rule)
        self._refresh_default_rules_button_state()
        self._update_schedule_controls_visibility(self.schedule_enabled_checkbox.isChecked())
        self._refresh_dialog_size()

    def _handle_schedule_enabled_toggled(self, checked: bool) -> None:
        self._update_schedule_controls_visibility(checked)
        self._refresh_dialog_size()
        QTimer.singleShot(0, self._refresh_dialog_size)

    def _update_schedule_controls_visibility(self, schedule_enabled: bool) -> None:
        dim_opacity = 1.0 if schedule_enabled else 0.5
        self.gradual_checkbox.setVisible(True)
        self.location_status_widget.setVisible(True)
        self.rules_header_label.setVisible(True)
        self.rules_table.setVisible(True)
        self.rule_buttons_widget.setVisible(True)
        for widget in (
            self.gradual_checkbox,
            self.location_status_widget,
            self.rules_header_label,
            self.rules_table,
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
        self._update_sun_event_labels(current_time, timezone_name)

    def _update_sun_event_labels(self, current_time: datetime, timezone_name: str | None) -> None:
        latitude = self._source_config.schedule.latitude
        longitude = self._source_config.schedule.longitude
        if latitude is None or longitude is None:
            self.sunrise_time_label.setText("Sunrise --:--")
            self.sunset_time_label.setText("Sunset --:--")
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
            self.sunrise_time_label.setText("Sunrise --:--")
            self.sunset_time_label.setText("Sunset --:--")
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
        self.sunrise_time_label.setText(f"Sunrise {sunrise_text}")
        self.sunset_time_label.setText(f"Sunset {sunset_text}")
        self._sun_times_cache_key = cache_key

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        _apply_native_rounded_corners(self)
        self._refresh_auto_location(refresh_detection=True)
        self._position_bottom_right()
        QTimer.singleShot(0, self._refresh_dialog_size)

    def _rules_table_content_height(self) -> int:
        self.rules_table.resizeRowsToContents()
        header_height = self.rules_table.horizontalHeader().height()
        row_heights = sum(self.rules_table.rowHeight(index) for index in range(self.rules_table.rowCount()))
        frame_height = self.rules_table.frameWidth() * 2
        return header_height + row_heights + frame_height + 2

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
        table_height = self._rules_table_content_height()
        self.rules_table.setFixedHeight(table_height)
        self.rules_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

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

        static_height = margins.top() + margins.bottom() + layout_spacing + button_height
        preferred_height = static_height + content_size.height()
        max_height = self._max_dialog_height()
        max_content_height = max(160, max_height - static_height)
        needs_scroll = content_size.height() > max_content_height
        content_height = min(content_size.height(), max_content_height)
        target_height = static_height + content_height

        self.content_scroll.setFixedHeight(content_height)
        target_width = max(self.minimumWidth(), content_size.width() + margins.left() + margins.right())
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

    def _add_empty_rule(self) -> None:
        self._add_rule_row(ScheduleRule(anchor="sunrise", offset_minutes=0, brightness=100, target="both"))
        self._refresh_dialog_size()

    @staticmethod
    def _default_sunrise_sunset_ramp_rules() -> list[ScheduleRule]:
        return [
            ScheduleRule(anchor="sunrise", offset_minutes=0, brightness=0, target="both"),
            ScheduleRule(anchor="sunrise", offset_minutes=15, brightness=10, target="both"),
            ScheduleRule(anchor="sunrise", offset_minutes=40, brightness=50, target="both"),
            ScheduleRule(anchor="sunrise", offset_minutes=60, brightness=100, target="both"),
            ScheduleRule(anchor="sunset", offset_minutes=-40, brightness=100, target="both"),
            ScheduleRule(anchor="sunset", offset_minutes=-15, brightness=50, target="both"),
            ScheduleRule(anchor="sunset", offset_minutes=0, brightness=15, target="both"),
            ScheduleRule(anchor="sunset", offset_minutes=30, brightness=0, target="both"),
        ]

    def _apply_default_sunrise_sunset_rules(self) -> None:
        for rule in self._default_sunrise_sunset_ramp_rules():
            self._add_rule_row(rule)
        self.schedule_enabled_checkbox.setChecked(True)
        self.gradual_checkbox.setChecked(True)
        self._refresh_default_rules_button_state()
        self._refresh_dialog_size()

    def _rule_from_row(self, row_index: int) -> ScheduleRule | None:
        target_widget = self.rules_table.cellWidget(row_index, 0)
        anchor_widget = self.rules_table.cellWidget(row_index, 1)
        time_widget = self.rules_table.cellWidget(row_index, 2)
        offset_widget = self.rules_table.cellWidget(row_index, 3)
        brightness_widget = self.rules_table.cellWidget(row_index, 4)
        if not isinstance(target_widget, QComboBox):
            return None
        if not isinstance(anchor_widget, QComboBox):
            return None
        if not isinstance(time_widget, QLineEdit):
            return None
        if not isinstance(offset_widget, QSpinBox):
            return None
        if not isinstance(brightness_widget, QSpinBox):
            return None

        target = str(target_widget.currentData() or "").strip().lower()
        if target not in ("display1", "display2", "both"):
            target = "both"

        anchor_text = anchor_widget.currentText().strip().lower()
        if anchor_text == "specific time":
            return ScheduleRule(
                anchor="time",
                offset_minutes=0,
                brightness=clamp_brightness(brightness_widget.value()),
                target=target,
                specific_time=self._normalize_time_text(time_widget.text()),
            )
        if anchor_text in ("sunrise", "sunset"):
            return ScheduleRule(
                anchor=anchor_text,
                offset_minutes=int(offset_widget.value()),
                brightness=clamp_brightness(brightness_widget.value()),
                target=target,
                specific_time=None,
            )
        return None

    def _rules_match_exact_slice(self, start_index: int, expected_rules: list[ScheduleRule]) -> bool:
        for offset, expected_rule in enumerate(expected_rules):
            actual_rule = self._rule_from_row(start_index + offset)
            if actual_rule is None or actual_rule != expected_rule:
                return False
        return True

    def _has_untouched_default_sunrise_sunset_block(self) -> bool:
        expected_rules = self._default_sunrise_sunset_ramp_rules()
        expected_count = len(expected_rules)
        row_count = self.rules_table.rowCount()
        if row_count < expected_count:
            return False

        for start_index in range(0, row_count - expected_count + 1):
            if self._rules_match_exact_slice(start_index, expected_rules):
                return True
        return False

    def _refresh_default_rules_button_state(self) -> None:
        has_untouched_default_block = self._has_untouched_default_sunrise_sunset_block()
        self.load_default_sun_rules_button.setVisible(not has_untouched_default_block)
        self.load_default_sun_rules_button.setText("Add Default Sunrise/Sunset Rules to List")
        self.load_default_sun_rules_button.setToolTip(
            "Add the preset sunrise/sunset ramp profile to the end of the list."
        )

    def _add_rule_row(self, rule: ScheduleRule) -> None:
        row_index = self.rules_table.rowCount()
        self.rules_table.insertRow(row_index)

        target_combo = QComboBox()
        for value, label in self._build_target_choices():
            target_combo.addItem(label, userData=value)
        target_combo.setCurrentIndex(max(0, target_combo.findData(rule.target)))

        anchor_combo = QComboBox()
        anchor_combo.addItems(["sunrise", "sunset", "specific time"])
        if rule.anchor == "time":
            anchor_combo.setCurrentText("specific time")
        else:
            anchor_combo.setCurrentText(rule.anchor)

        time_edit = QLineEdit()
        time_edit.setPlaceholderText("HH:MM")
        time_regex = QRegularExpression(r"^([01]?\d|2[0-3]):[0-5]\d$")
        time_edit.setValidator(QRegularExpressionValidator(time_regex, self))
        if rule.specific_time:
            time_edit.setText(rule.specific_time)

        offset_spin = QSpinBox()
        offset_spin.setRange(-1440, 1440)
        offset_spin.setValue(int(rule.offset_minutes))

        brightness_spin = QSpinBox()
        brightness_spin.setRange(0, 100)
        brightness_spin.setValue(clamp_brightness(rule.brightness))

        self.rules_table.setCellWidget(row_index, 0, target_combo)
        self.rules_table.setCellWidget(row_index, 1, anchor_combo)
        self.rules_table.setCellWidget(row_index, 2, time_edit)
        self.rules_table.setCellWidget(row_index, 3, offset_spin)
        self.rules_table.setCellWidget(row_index, 4, brightness_spin)

        anchor_combo.currentTextChanged.connect(
            lambda _text: self._sync_rule_anchor_mode(anchor_combo, time_edit, offset_spin)
        )
        target_combo.currentIndexChanged.connect(
            lambda _index: self._refresh_default_rules_button_state()
        )
        anchor_combo.currentIndexChanged.connect(
            lambda _index: self._refresh_default_rules_button_state()
        )
        time_edit.textChanged.connect(lambda _text: self._refresh_default_rules_button_state())
        offset_spin.valueChanged.connect(lambda _value: self._refresh_default_rules_button_state())
        brightness_spin.valueChanged.connect(
            lambda _value: self._refresh_default_rules_button_state()
        )
        self._sync_rule_anchor_mode(anchor_combo, time_edit, offset_spin)
        self._refresh_default_rules_button_state()

    def _sync_rule_anchor_mode(
        self, anchor_combo: QComboBox, time_edit: QLineEdit, offset_spin: QSpinBox
    ) -> None:
        anchor_text = anchor_combo.currentText().strip().lower()
        is_specific_time = anchor_text == "specific time"

        time_edit.setEnabled(is_specific_time)
        offset_spin.setEnabled(not is_specific_time)
        if is_specific_time:
            offset_spin.setValue(0)
            if not time_edit.text().strip():
                time_edit.setText("12:00")
        else:
            time_edit.clear()

    def _remove_selected_rule(self) -> None:
        current_row = self.rules_table.currentRow()
        if current_row < 0:
            return
        self.rules_table.removeRow(current_row)
        self._refresh_default_rules_button_state()
        self._refresh_dialog_size()

    @staticmethod
    def _normalize_time_text(raw_text: str) -> str | None:
        text = raw_text.strip()
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
        return f"{hour:02d}:{minute:02d}"

    def _collect_rules(self) -> tuple[list[ScheduleRule], str | None]:
        rules: list[ScheduleRule] = []
        for row_index in range(self.rules_table.rowCount()):
            target_widget = self.rules_table.cellWidget(row_index, 0)
            anchor_widget = self.rules_table.cellWidget(row_index, 1)
            time_widget = self.rules_table.cellWidget(row_index, 2)
            offset_widget = self.rules_table.cellWidget(row_index, 3)
            brightness_widget = self.rules_table.cellWidget(row_index, 4)
            if not isinstance(target_widget, QComboBox):
                continue
            if not isinstance(anchor_widget, QComboBox):
                continue
            if not isinstance(time_widget, QLineEdit):
                continue
            if not isinstance(offset_widget, QSpinBox):
                continue
            if not isinstance(brightness_widget, QSpinBox):
                continue

            target = str(target_widget.currentData() or "").strip().lower()
            if target not in ("display1", "display2", "both"):
                target = "both"

            anchor_text = anchor_widget.currentText().strip().lower()
            if anchor_text == "specific time":
                specific_time = self._normalize_time_text(time_widget.text())
                if specific_time is None:
                    return ([], f"Rule {row_index + 1}: specific time must be HH:MM (24-hour).")
                rules.append(
                    ScheduleRule(
                        anchor="time",
                        offset_minutes=0,
                        brightness=clamp_brightness(brightness_widget.value()),
                        target=target,
                        specific_time=specific_time,
                    )
                )
                continue

            if anchor_text not in ("sunrise", "sunset"):
                continue

            rules.append(
                ScheduleRule(
                    anchor=anchor_text,
                    offset_minutes=int(offset_widget.value()),
                    brightness=clamp_brightness(brightness_widget.value()),
                    target=target,
                    specific_time=None,
                )
            )
        return (rules, None)

    def _save_and_close(self) -> None:
        self._refresh_auto_location(refresh_detection=True)

        rules, error = self._collect_rules()
        if error:
            QMessageBox.warning(self, "Invalid Rule", error)
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
        updated.schedule.enabled = self.schedule_enabled_checkbox.isChecked()
        updated.schedule.gradual = self.gradual_checkbox.isChecked()
        updated.schedule.auto_location = True
        if self._detected_location is not None:
            updated.schedule.latitude = self._detected_location.latitude
            updated.schedule.longitude = self._detected_location.longitude
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
            app = QApplication.instance()
            if app is not None:
                app.setStyleSheet(build_stylesheet(normalized_theme))

    def reject(self) -> None:  # type: ignore[override]
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(build_stylesheet(self._initial_theme))
        super().reject()


class BrightnessControlWindow(QWidget):
    settings_changed = Signal(object)
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

        self.setWindowTitle("Brightness Tray")
        self.setWindowFlag(Qt.WindowType.Popup, True)
        self.setMinimumWidth(420)
        self.resize(440, 260)
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

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(10, 10, 10, 10)

        self.info_label = QLabel("")
        self.info_label.setWordWrap(True)
        root_layout.addWidget(self.info_label)

        control_row = QHBoxLayout()
        self.link_button = QPushButton("Unlinked")
        self.link_button.setCheckable(True)
        self.link_button.setMinimumWidth(92)
        control_row.addWidget(self.link_button)
        self.ambient_button = QPushButton("Auto Light")
        self.ambient_button.setCheckable(True)
        self.ambient_button.setMinimumWidth(96)
        control_row.addWidget(self.ambient_button)
        control_row.addStretch(1)
        self.refresh_button = QToolButton()
        self.refresh_button.setIcon(self._load_refresh_icon())
        self.refresh_button.setIconSize(QSize(18, 18))
        self.refresh_button.setToolTip("Refresh monitors")
        self.settings_button = QToolButton()
        self.settings_button.setIcon(self._build_settings_icon())
        self.settings_button.setIconSize(QSize(18, 18))
        self.settings_button.setToolTip("Settings")
        control_row.addWidget(self.refresh_button)
        control_row.addWidget(self.settings_button)
        root_layout.addLayout(control_row)

        self.combined_group = QGroupBox("Combined Brightness")
        combined_layout = QHBoxLayout(self.combined_group)
        self.combined_label = QLabel("All displays")
        self.global_slider = QSlider(Qt.Orientation.Horizontal)
        self.global_slider.setRange(0, 100)
        self.global_value_label = QLabel("0%")
        self.global_value_label.setMinimumWidth(40)
        self.global_value_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        combined_layout.addWidget(self.combined_label)
        combined_layout.addWidget(self.global_slider, stretch=1)
        combined_layout.addWidget(self.global_value_label)
        root_layout.addWidget(self.combined_group)

        self.monitors_group = QGroupBox("Per-Monitor Brightness")
        monitors_layout = QVBoxLayout(self.monitors_group)
        self.monitor_scroll = QScrollArea()
        self.monitor_scroll.setObjectName("monitorScroll")
        self.monitor_scroll.setWidgetResizable(True)
        self.monitor_scroll.setMinimumHeight(1)
        self.monitor_scroll_content = QWidget()
        self.monitor_scroll_content.setObjectName("monitorScrollContent")
        self.monitor_rows_layout = QVBoxLayout(self.monitor_scroll_content)
        self.monitor_rows_layout.setContentsMargins(0, 0, 0, 0)
        self.monitor_rows_layout.setSpacing(8)
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
        width = max(420, min(560, preferred_size.width()))
        min_height = 118 if self.config.link_mode else 160
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
        try:
            monitors = self.service.refresh_monitors()
        except Exception as error:
            self.info_label.setText(f"Could not access monitors: {error}")
            monitors = []

        self._clear_monitor_rows()

        if not monitors:
            self.info_label.setText(
                "No compatible displays detected. "
                "Enable DDC/CI in your monitor settings if needed."
            )
            self._update_link_mode_ui()
            self._refresh_visible_popup_geometry()
            return

        self.info_label.setText(f"Detected {len(monitors)} display(s).")
        for monitor in monitors:
            saved_level = self.config.monitor_levels.get(monitor.key)
            current_level = self.service.get_brightness(monitor)
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
            self.service.set_brightness(row.monitor, level)
            row.set_value(level)
        self._sync_global_slider_to_average()

    def apply_brightness_to_all(self, value: int, persist: bool = True) -> None:
        bounded = clamp_brightness(value)
        for row in self.monitor_rows:
            self.service.set_brightness(row.monitor, bounded)
            row.set_value(bounded)

        self._set_global_slider_value(bounded)
        if persist:
            self.config.last_global_brightness = bounded
            for row in self.monitor_rows:
                self.config.monitor_levels[row.monitor.key] = bounded
            self._persist_config()

    def apply_brightness_map(self, values: dict[str, int], persist: bool = True) -> None:
        applied: list[int] = []
        for row in self.monitor_rows:
            if row.monitor.key not in values:
                continue
            level = clamp_brightness(values[row.monitor.key])
            self.service.set_brightness(row.monitor, level)
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
        self._schedule_status_text = text

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
        if REFRESH_ICON_PATH is not None:
            source = QPixmap(str(REFRESH_ICON_PATH))
            if source.isNull():
                return QIcon(str(REFRESH_ICON_PATH))
            if normalize_theme_name(self.config.theme) == THEME_DARK:
                tinted = QPixmap(source.size())
                tinted.fill(Qt.GlobalColor.transparent)
                painter = QPainter(tinted)
                painter.drawPixmap(0, 0, source)
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
                painter.fillRect(tinted.rect(), QColor(255, 255, 255))
                painter.end()
                return QIcon(tinted)
            return QIcon(source)
        return self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)

    def _build_settings_icon(self) -> QIcon:
        if SETTINGS_ICON_PATH is not None:
            source = QPixmap(str(SETTINGS_ICON_PATH))
            if source.isNull():
                return QIcon(str(SETTINGS_ICON_PATH))
            if normalize_theme_name(self.config.theme) == THEME_DARK:
                tinted = QPixmap(source.size())
                tinted.fill(Qt.GlobalColor.transparent)
                painter = QPainter(tinted)
                painter.drawPixmap(0, 0, source)
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
                painter.fillRect(tinted.rect(), QColor(255, 255, 255))
                painter.end()
                return QIcon(tinted)
            return QIcon(source)

        size = 18
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)

        if normalize_theme_name(self.config.theme) == THEME_DARK:
            bar_color = QColor(255, 255, 255)
        else:
            palette = self.palette()
            bar_color = QColor(palette.buttonText().color())

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        track_left = 3.0
        track_right = 15.0
        line_ys = (5.0, 9.0, 13.0)
        knob_offsets = (-2.0, 2.0, -2.0)
        center_x = (track_left + track_right) / 2.0
        knob_radius = 1.7

        line_pen = QPen(bar_color, 1.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(line_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for y in line_ys:
            painter.drawLine(QPointF(track_left, y), QPointF(track_right, y))

        painter.setPen(QPen(bar_color, 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for y, offset in zip(line_ys, knob_offsets):
            painter.drawEllipse(QPointF(center_x + offset, y), knob_radius, knob_radius)

        painter.end()
        return QIcon(pixmap)

    def _refresh_toolbar_icons(self) -> None:
        self.refresh_button.setIcon(self._load_refresh_icon())
        self.settings_button.setIcon(self._build_settings_icon())

    def _handle_refresh_button(self) -> None:
        self.refresh_monitors(apply_saved=False)

    def _handle_global_slider_change(self, value: int) -> None:
        bounded = clamp_brightness(value)
        self.global_value_label.setText(f"{bounded}%")
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
        target_enabled = bool(enabled)
        if target_enabled:
            detected_lux = self._ambient_service.probe_sensor()
            if detected_lux is None:
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
            self._ambient_smoothed_target = None
            self._last_ambient_applied = None
            self._last_ambient_persist_ts = 0.0
            self._ambient_service.start()
            self._ambient_timer.start()
            self._handle_ambient_timer_tick()
        else:
            self.config.ambient_auto_enabled = False
            self._ambient_timer.stop()
            self._ambient_service.stop()
            self._ambient_smoothed_target = None
            self._last_ambient_applied = None
            self._last_ambient_persist_ts = 0.0

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
            self.service.set_brightness(row.monitor, next_level)
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

        self.service.set_brightness(target_row.monitor, value)
        self.config.monitor_levels[monitor_key] = clamp_brightness(value)
        self._sync_global_slider_to_average()
        self._persist_config()

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
        self.global_value_label.setText(f"{bounded}%")
        self._internal_ui_update = False

    def _update_link_mode_ui(self) -> None:
        linked = self.config.link_mode
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
        if self.config.link_mode:
            self.monitor_scroll.setFixedHeight(56)
            return
        if not self.monitor_rows:
            self.monitor_scroll.setFixedHeight(56)
            return
        spacing = self.monitor_rows_layout.spacing()
        content_height = sum(row.sizeHint().height() for row in self.monitor_rows)
        content_height += max(0, len(self.monitor_rows) - 1) * spacing
        content_height += 8
        self.monitor_scroll.setFixedHeight(max(56, min(420, content_height)))

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
        self._refresh_toolbar_icons()
        self._set_global_slider_value(self.config.last_global_brightness)
        self.set_link_mode(
            self.config.link_mode,
            persist=True,
            apply_link_brightness=self.config.link_mode,
        )
        self.settings_changed.emit(self.config)

    def _persist_config(self) -> None:
        self.config_store.save(self.config)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.hide()
        event.accept()

    def hideEvent(self, event) -> None:  # type: ignore[override]
        self._last_popup_hide_monotonic = wall_time.monotonic()
        super().hideEvent(event)
