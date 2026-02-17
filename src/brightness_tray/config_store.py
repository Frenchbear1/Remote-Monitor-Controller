from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .models import (
    AppConfig,
    ScheduleRule,
    ScheduleSettings,
    clamp_brightness,
    default_schedule_rules,
)


APP_FOLDER_NAME = "BrightnessTrayScheduler"
CONFIG_FILE_NAME = "config.json"


def get_default_config_path() -> Path:
    appdata = os.getenv("APPDATA")
    if appdata:
        return Path(appdata) / APP_FOLDER_NAME / CONFIG_FILE_NAME
    return Path.home() / ".config" / APP_FOLDER_NAME / CONFIG_FILE_NAME


class ConfigStore:
    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path or get_default_config_path()

    def load(self) -> AppConfig:
        if not self.config_path.exists():
            config = AppConfig()
            self.save(config)
            return config

        try:
            raw_data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            config = AppConfig()
            self.save(config)
            return config

        return self._parse(raw_data)

    def save(self, config: AppConfig) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": config.version,
            "link_mode": bool(config.link_mode),
            "ambient_auto_enabled": bool(config.ambient_auto_enabled),
            "last_global_brightness": clamp_brightness(config.last_global_brightness),
            "monitor_levels": {
                key: clamp_brightness(value) for key, value in config.monitor_levels.items()
            },
            "startup_enabled": bool(config.startup_enabled),
            "schedule": {
                "enabled": bool(config.schedule.enabled),
                "gradual": bool(config.schedule.gradual),
                "auto_location": bool(config.schedule.auto_location),
                "latitude": config.schedule.latitude,
                "longitude": config.schedule.longitude,
                "rules": [
                    {
                        "anchor": rule.anchor,
                        "offset_minutes": int(rule.offset_minutes),
                        "brightness": clamp_brightness(rule.brightness),
                        "target": rule.target,
                        "specific_time": rule.specific_time,
                    }
                    for rule in config.schedule.rules
                ],
            },
        }
        self.config_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _parse(self, data: dict[str, Any]) -> AppConfig:
        config = AppConfig()
        config.version = int(data.get("version", 1))
        config.link_mode = bool(data.get("link_mode", True))
        config.ambient_auto_enabled = bool(data.get("ambient_auto_enabled", False))
        config.last_global_brightness = clamp_brightness(
            data.get("last_global_brightness", 100)
        )
        config.startup_enabled = bool(data.get("startup_enabled", True))

        monitor_levels = data.get("monitor_levels", {})
        if isinstance(monitor_levels, dict):
            config.monitor_levels = {
                str(key): clamp_brightness(value)
                for key, value in monitor_levels.items()
            }

        schedule_data = data.get("schedule", {})
        if isinstance(schedule_data, dict):
            schedule = ScheduleSettings()
            schedule.enabled = bool(schedule_data.get("enabled", False))
            schedule.gradual = bool(schedule_data.get("gradual", True))
            schedule.auto_location = bool(schedule_data.get("auto_location", True))
            schedule.latitude = self._optional_float(schedule_data.get("latitude"))
            schedule.longitude = self._optional_float(schedule_data.get("longitude"))
            schedule.rules = self._parse_rules(schedule_data.get("rules"))
            config.schedule = schedule

        return config

    def _parse_rules(self, raw_rules: Any) -> list[ScheduleRule]:
        if not isinstance(raw_rules, list):
            return default_schedule_rules()

        parsed: list[ScheduleRule] = []
        for raw_rule in raw_rules:
            if not isinstance(raw_rule, dict):
                continue
            anchor = str(raw_rule.get("anchor", "")).strip().lower()
            if anchor not in ("sunrise", "sunset", "time"):
                continue

            try:
                offset_minutes = int(raw_rule.get("offset_minutes", 0))
            except (TypeError, ValueError):
                offset_minutes = 0
            offset_minutes = max(-1440, min(1440, offset_minutes))

            target = str(raw_rule.get("target", "both")).strip().lower()
            if target not in ("display1", "display2", "both"):
                target = "both"

            specific_time: str | None = None
            if anchor == "time":
                specific_time = self._normalize_time_text(raw_rule.get("specific_time"))
                if specific_time is None:
                    continue
                offset_minutes = 0

            parsed.append(
                ScheduleRule(
                    anchor=anchor,
                    offset_minutes=offset_minutes,
                    brightness=clamp_brightness(raw_rule.get("brightness", 100)),
                    target=target,
                    specific_time=specific_time,
                )
            )

        if not parsed:
            return default_schedule_rules()
        return parsed

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_time_text(value: Any) -> str | None:
        text = str(value or "").strip()
        if not re.fullmatch(r"\d{1,2}:\d{2}", text):
            return None
        try:
            hour, minute = [int(piece) for piece in text.split(":")]
        except (TypeError, ValueError):
            return None
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            return None
        return f"{hour:02d}:{minute:02d}"
