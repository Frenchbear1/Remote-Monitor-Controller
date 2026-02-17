from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LocationContext:
    latitude: float
    longitude: float
    region: str
    timezone_name: str | None = None


def detect_location_context_from_ip() -> LocationContext | None:
    try:
        import geocoder
    except Exception:
        return None

    try:
        result = geocoder.ip("me")
    except Exception:
        return None

    if not result:
        return None

    latlng = getattr(result, "latlng", None)
    if not latlng or len(latlng) != 2:
        return None

    try:
        latitude = float(latlng[0])
        longitude = float(latlng[1])
    except (TypeError, ValueError):
        return None

    payload = getattr(result, "json", {})
    if not isinstance(payload, dict):
        payload = {}
    raw_payload = payload.get("raw", {})
    if not isinstance(raw_payload, dict):
        raw_payload = {}

    city = _first_non_empty(
        getattr(result, "city", None),
        payload.get("city"),
        raw_payload.get("city"),
    )
    state = _first_non_empty(
        getattr(result, "state", None),
        payload.get("state"),
        raw_payload.get("region"),
    )
    country = _first_non_empty(
        getattr(result, "country", None),
        payload.get("country"),
        raw_payload.get("country"),
    )
    timezone_name = _first_non_empty(
        getattr(result, "timezone", None),
        payload.get("timezone"),
        raw_payload.get("timezone"),
    )
    region = _build_region_label(city, state, country)

    return LocationContext(
        latitude=latitude,
        longitude=longitude,
        region=region,
        timezone_name=timezone_name,
    )


def detect_location_from_ip() -> tuple[float, float] | None:
    context = detect_location_context_from_ip()
    if context is None:
        return None
    return (context.latitude, context.longitude)


def _first_non_empty(*values) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _build_region_label(city: str | None, state: str | None, country: str | None) -> str:
    parts: list[str] = []
    for value in (city, state, country):
        if not value:
            continue
        if value in parts:
            continue
        parts.append(value)
    if parts:
        return ", ".join(parts)
    return "Location unavailable"
