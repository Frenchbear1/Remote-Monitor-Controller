from __future__ import annotations

import sys
from datetime import datetime

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QAction, QCursor
from PySide6.QtWidgets import (
    QApplication,
    QMenu,
    QMessageBox,
    QStyle,
    QSystemTrayIcon,
)

from .brightness_service import BrightnessService
from .config_store import ConfigStore
from .location import detect_location_from_ip
from .models import AppConfig, ScheduleRule
from .startup import set_startup_enabled
from .sun_schedule import SunScheduleEngine
from .themes import build_stylesheet, normalize_theme_name
from .ui import BrightnessControlWindow


class TrayController(QObject):
    def __init__(self, app: QApplication):
        super().__init__()
        self.app = app

        self.config_store = ConfigStore()
        self.config = self.config_store.load()
        self._apply_theme()
        self.brightness_service = BrightnessService()
        self.schedule_engine = SunScheduleEngine()

        self.window = BrightnessControlWindow(
            service=self.brightness_service,
            config_store=self.config_store,
            config=self.config,
        )
        self.window.settings_changed.connect(self._handle_settings_changed)

        self._expected_auto_targets: dict[str, int] = {}

        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(app.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        self.tray_icon.setToolTip("Brightness Tray")
        self.tray_icon.setContextMenu(self._build_menu())
        self.tray_icon.activated.connect(self._handle_tray_activation)
        self.tray_icon.show()

        self._apply_startup_setting()
        self._resolve_location_if_needed()
        self._update_schedule_now(force_apply=True)

        self.schedule_timer = QTimer(self)
        self.schedule_timer.setInterval(1_000)
        self.schedule_timer.timeout.connect(self._update_schedule_now)
        self._refresh_schedule_timer_interval()
        self.schedule_timer.start()

    def _build_menu(self) -> QMenu:
        menu = QMenu()

        open_action = QAction("Show Popup", self)
        open_action.triggered.connect(self._show_popup)
        menu.addAction(open_action)

        refresh_action = QAction("Refresh Monitors", self)
        refresh_action.triggered.connect(
            lambda _checked=False: self.window.refresh_monitors(apply_saved=False)
        )
        menu.addAction(refresh_action)

        schedule_action = QAction("Apply Schedule Now", self)
        schedule_action.triggered.connect(
            lambda _checked=False: self._update_schedule_now(force_apply=True)
        )
        menu.addAction(schedule_action)

        menu.addSeparator()

        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)
        return menu

    def _handle_tray_activation(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason not in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            return
        if self.window.has_open_popups():
            self.window.close_all_popups()
            return
        if self.window.was_recently_hidden(0.35):
            return
        self._show_popup()

    def _show_popup(self, _checked: bool = False) -> None:
        self.window.refresh_monitors(apply_saved=False)
        self.window.show_as_popup(QCursor.pos())

    def _quit(self, _checked: bool = False) -> None:
        self.tray_icon.hide()
        self.app.quit()

    def _handle_settings_changed(self, updated_config: object) -> None:
        if not isinstance(updated_config, AppConfig):
            return
        self.config = updated_config
        self.window.config = updated_config
        self._apply_theme()
        self._expected_auto_targets = {}
        self._apply_startup_setting()
        self._resolve_location_if_needed()
        self._refresh_schedule_timer_interval()
        self._update_schedule_now(force_apply=True)

    def _apply_startup_setting(self) -> None:
        if not set_startup_enabled(self.config.startup_enabled):
            self.window.info_label.setText(
                "Warning: could not update startup registration. "
                "Try running the app once as administrator."
            )

    def _schedule_has_sun_rules(self) -> bool:
        return any(rule.anchor in ("sunrise", "sunset") for rule in self.config.schedule.rules)

    def _refresh_schedule_timer_interval(self) -> None:
        if self.config.schedule.enabled:
            interval_ms = 1_000
        else:
            interval_ms = 60_000
        if self.schedule_timer.interval() != interval_ms:
            self.schedule_timer.setInterval(interval_ms)

    def _resolve_location_if_needed(self) -> None:
        if not self.config.schedule.enabled:
            return
        if not self._schedule_has_sun_rules():
            return

        coords = detect_location_from_ip()
        if coords is None:
            return

        latitude, longitude = coords
        if (
            self.config.schedule.latitude is not None
            and self.config.schedule.longitude is not None
            and abs(self.config.schedule.latitude - latitude) < 0.0001
            and abs(self.config.schedule.longitude - longitude) < 0.0001
        ):
            return

        self.config.schedule.latitude = latitude
        self.config.schedule.longitude = longitude
        self.config_store.save(self.config)

    def _rules_for_display_index(self, display_index: int) -> list[ScheduleRule]:
        if display_index == 0:
            allowed_targets = {"display1", "both"}
        elif display_index == 1:
            allowed_targets = {"display2", "both"}
        else:
            allowed_targets = {"both"}
        return [rule for rule in self.config.schedule.rules if rule.target in allowed_targets]

    def _calculate_schedule_targets(self) -> dict[str, int]:
        targets: dict[str, int] = {}
        now = datetime.now().astimezone()
        for display_index, row in enumerate(self.window.monitor_rows):
            scoped_rules = self._rules_for_display_index(display_index)
            if not scoped_rules:
                continue
            value = self.schedule_engine.target_brightness(
                self.config.schedule, rules=scoped_rules, now=now
            )
            if value is None:
                continue
            targets[row.monitor.key] = value
        return targets

    def _format_target_summary(self, targets: dict[str, int]) -> str:
        parts: list[str] = []
        for display_index, row in enumerate(self.window.monitor_rows):
            value = targets.get(row.monitor.key)
            if value is None:
                continue
            parts.append(f"D{display_index + 1} {value}%")
        return ", ".join(parts)

    def _has_link_mode_conflict(self, targets: dict[str, int]) -> bool:
        active_values: list[int] = []
        for row in self.window.monitor_rows:
            value = targets.get(row.monitor.key)
            if value is None:
                continue
            active_values.append(value)
        return len(active_values) >= 2 and len(set(active_values)) > 1

    def _update_schedule_now(self, force_apply: bool = False) -> None:
        if self.window.is_ambient_auto_enabled():
            if self.config.schedule.enabled:
                self.window.set_schedule_status(
                    "Schedule: paused (Auto Light is active)."
                )
            self._expected_auto_targets = {}
            return

        if not self.config.schedule.enabled:
            self.window.set_schedule_status("Schedule: off")
            self._expected_auto_targets = {}
            return

        if self._schedule_has_sun_rules() and (
            self.config.schedule.latitude is None or self.config.schedule.longitude is None
        ):
            self._resolve_location_if_needed()

        targets = self._calculate_schedule_targets()
        if not targets:
            if self._schedule_has_sun_rules() and (
                self.config.schedule.latitude is None or self.config.schedule.longitude is None
            ):
                self.window.set_schedule_status(
                    "Schedule: waiting for location fix."
                )
            else:
                self.window.set_schedule_status(
                    "Schedule: enabled, but no valid rule target for connected displays."
                )
            return

        target_text = self._format_target_summary(targets)
        if self.config.link_mode and self._has_link_mode_conflict(targets):
            self.window.set_link_mode(False, persist=True, apply_link_brightness=False)
            self.config = self.window.config

        # Only re-apply when schedule target actually changes (or force requested).
        if not force_apply and targets == self._expected_auto_targets:
            self.window.set_schedule_status(f"Schedule: active ({target_text})")
            return

        self.window.apply_schedule_targets(targets, persist=True)
        self._expected_auto_targets = dict(targets)
        self.window.set_schedule_status(f"Schedule: active ({target_text})")

    def _apply_theme(self) -> None:
        theme_name = normalize_theme_name(self.config.theme)
        self.config.theme = theme_name
        self.app.setStyleSheet(build_stylesheet(theme_name))


def run() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Brightness Tray")
    app.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(
            None,
            "System Tray Unavailable",
            "Windows system tray is not available in this session.",
        )
        raise SystemExit(1)

    controller = TrayController(app)
    app._brightness_tray_controller = controller  # type: ignore[attr-defined]

    raise SystemExit(app.exec())
