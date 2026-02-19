from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


AnchorType = Literal["sunrise", "sunset", "time"]
RuleTarget = Literal["display1", "display2", "both"]


def clamp_brightness(value: int | float) -> int:
    return max(0, min(100, int(round(value))))


@dataclass
class ScheduleRule:
    anchor: AnchorType
    offset_minutes: int
    brightness: int
    target: RuleTarget = "both"
    specific_time: str | None = None


def default_schedule_rules() -> list[ScheduleRule]:
    return [
        ScheduleRule(anchor="sunrise", offset_minutes=-60, brightness=50, target="both"),
        ScheduleRule(anchor="sunrise", offset_minutes=-30, brightness=75, target="both"),
        ScheduleRule(anchor="sunrise", offset_minutes=0, brightness=100, target="both"),
        ScheduleRule(anchor="sunset", offset_minutes=0, brightness=100, target="both"),
        ScheduleRule(anchor="sunset", offset_minutes=30, brightness=75, target="both"),
        ScheduleRule(anchor="sunset", offset_minutes=60, brightness=50, target="both"),
    ]


@dataclass
class ScheduleSettings:
    enabled: bool = False
    gradual: bool = True
    auto_location: bool = True
    latitude: float | None = None
    longitude: float | None = None
    rules: list[ScheduleRule] = field(default_factory=default_schedule_rules)


@dataclass
class AppConfig:
    version: int = 1
    theme: str = "dark"
    link_mode: bool = True
    ambient_auto_enabled: bool = False
    last_global_brightness: int = 100
    monitor_levels: dict[str, int] = field(default_factory=dict)
    startup_enabled: bool = True
    schedule: ScheduleSettings = field(default_factory=ScheduleSettings)
