from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from astral import LocationInfo
from astral.sun import sun
from tzlocal import get_localzone_name

from .models import ScheduleRule, ScheduleSettings, clamp_brightness


class SunScheduleEngine:
    def __init__(self) -> None:
        self.timezone_name = get_localzone_name()

    def target_brightness(
        self,
        schedule: ScheduleSettings,
        rules: list[ScheduleRule] | None = None,
        now: datetime | None = None,
    ) -> int | None:
        if not schedule.enabled:
            return None
        active_rules = rules if rules is not None else schedule.rules
        if not active_rules:
            return None

        uses_sun_events = any(rule.anchor in ("sunrise", "sunset") for rule in active_rules)
        if uses_sun_events and (schedule.latitude is None or schedule.longitude is None):
            return None

        timezone = ZoneInfo(self.timezone_name)
        if now is None:
            current_time = datetime.now(timezone)
        elif now.tzinfo is None:
            current_time = now.replace(tzinfo=timezone)
        else:
            current_time = now.astimezone(timezone)

        points: list[tuple[datetime, int]] = []
        for day_offset in (-1, 0, 1):
            target_date = (current_time + timedelta(days=day_offset)).date()
            sun_events: dict[str, datetime] | None = None
            if uses_sun_events:
                if schedule.latitude is None or schedule.longitude is None:
                    continue
                sun_events = self._get_sun_events(
                    target_date, schedule.latitude, schedule.longitude, timezone
                )

            for rule in active_rules:
                if rule.anchor == "time":
                    parsed_time = self._parse_time(rule.specific_time)
                    if parsed_time is None:
                        continue
                    anchor_time = datetime.combine(target_date, parsed_time, timezone)
                else:
                    if not sun_events:
                        continue
                    anchor_time = sun_events.get(rule.anchor)
                    if anchor_time is None:
                        continue
                    anchor_time = anchor_time + timedelta(minutes=rule.offset_minutes)
                points.append(
                    (
                        anchor_time,
                        clamp_brightness(rule.brightness),
                    )
                )

        if not points:
            return None

        points.sort(key=lambda point: point[0])
        previous = points[0]
        following = points[-1]
        for point in points:
            if point[0] <= current_time:
                previous = point
                continue
            following = point
            break

        if not schedule.gradual:
            return previous[1]

        return self._interpolate(current_time, previous, following)

    def _get_sun_events(
        self, target_date: Any, latitude: float, longitude: float, timezone: ZoneInfo
    ) -> dict[str, datetime] | None:
        location = LocationInfo(
            name="Local",
            region="Local",
            timezone=self.timezone_name,
            latitude=latitude,
            longitude=longitude,
        )
        try:
            sun_times = sun(location.observer, date=target_date, tzinfo=timezone)
        except Exception:
            return None

        sunrise = sun_times.get("sunrise")
        sunset = sun_times.get("sunset")
        if sunrise is None or sunset is None:
            return None
        return {"sunrise": sunrise, "sunset": sunset}

    @staticmethod
    def _parse_time(value: str | None) -> time | None:
        if not value:
            return None
        text = value.strip()
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
        return time(hour=hour, minute=minute)

    @staticmethod
    def _interpolate(
        current_time: datetime,
        previous: tuple[datetime, int],
        following: tuple[datetime, int],
    ) -> int:
        start_time, start_value = previous
        end_time, end_value = following
        if end_time <= start_time:
            return clamp_brightness(end_value)

        elapsed_seconds = (current_time - start_time).total_seconds()
        duration_seconds = (end_time - start_time).total_seconds()
        ratio = max(0.0, min(1.0, elapsed_seconds / duration_seconds))
        blended = start_value + (end_value - start_value) * ratio
        return clamp_brightness(blended)
