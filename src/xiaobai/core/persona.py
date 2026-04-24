"""Time-of-day persona hints per-user.

Bots that respond with identical energy at every hour of the day feel
robotic. A real friend at 3 AM is half-asleep; at noon they're sharp;
at 10 PM they're winding down. This module lets ingress inject the
target user's local ``hour_bucket`` into ``meta`` so Claude can modulate
pacing and tone.

Resolution path:

1. Try the person record's explicit ``timezone`` field
   (IANA identifier like ``America/Vancouver``).
2. Fall back to a ``location`` → IANA lookup for the cities we commonly
   handle (Boss/Mom/extended family).
3. If neither resolves, return ``None`` — ingress just won't set the key.

Buckets are coarse (``deep_night``, ``morning``, ``day``, ``evening``,
``late_night``) because finer grain would encourage Claude to over-react
to single-hour crossings, whereas tone should shift gradually.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

HourBucket = Literal["deep_night", "morning", "day", "evening", "late_night"]


_LOCATION_TO_TZ: dict[str, str] = {
    # North America
    "vancouver": "America/Vancouver",
    "seattle": "America/Los_Angeles",
    "sf": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles",
    "la": "America/Los_Angeles",
    "los angeles": "America/Los_Angeles",
    "nyc": "America/New_York",
    "new york": "America/New_York",
    "toronto": "America/Toronto",
    # Greater China
    "福州": "Asia/Shanghai",
    "烟台": "Asia/Shanghai",
    "青岛": "Asia/Shanghai",
    "威海": "Asia/Shanghai",
    "莱阳": "Asia/Shanghai",
    "北京": "Asia/Shanghai",
    "上海": "Asia/Shanghai",
    "深圳": "Asia/Shanghai",
    "广州": "Asia/Shanghai",
    "杭州": "Asia/Shanghai",
    "hong kong": "Asia/Hong_Kong",
    "香港": "Asia/Hong_Kong",
    "taipei": "Asia/Taipei",
    "台北": "Asia/Taipei",
    # Rest of world
    "tokyo": "Asia/Tokyo",
    "singapore": "Asia/Singapore",
    "london": "Europe/London",
    "paris": "Europe/Paris",
    "berlin": "Europe/Berlin",
    "sydney": "Australia/Sydney",
}


def resolve_timezone(tz_str: str = "", location: str = "") -> str | None:
    """Resolve a timezone identifier from explicit ``tz_str`` or a location name."""
    if tz_str:
        try:
            ZoneInfo(tz_str)
            return tz_str
        except ZoneInfoNotFoundError:
            pass
    if location:
        loc = location.lower().strip()
        if loc in _LOCATION_TO_TZ:
            return _LOCATION_TO_TZ[loc]
        # Substring fallback — handle "温哥华" or "人在福州" style entries.
        # CJK keys: direct substring (no word boundaries in Chinese text).
        # Latin keys: require a word boundary to avoid e.g. "atlantis"
        # accidentally matching "la".
        for key, tz in _LOCATION_TO_TZ.items():
            if any("一" <= ch <= "鿿" for ch in key):
                if key in loc:
                    return tz
            else:
                if re.search(rf"\b{re.escape(key)}\b", loc):
                    return tz
    return None


def hour_bucket(hour: int) -> HourBucket:
    """Map 0-23 → coarse tone bucket."""
    if 2 <= hour < 6:
        return "deep_night"
    if 6 <= hour < 12:
        return "morning"
    if 12 <= hour < 19:
        return "day"
    if 19 <= hour < 23:
        return "evening"
    return "late_night"  # 23 and 0-1


def current_hour(tz: str, now: datetime | None = None) -> int | None:
    """Return the current hour (0-23) in ``tz`` or ``None`` on bad tz."""
    try:
        zi = ZoneInfo(tz)
    except ZoneInfoNotFoundError:
        return None
    ref = now.astimezone(zi) if now is not None else datetime.now(zi)
    return ref.hour


def persona_signal(
    tz_str: str = "", location: str = "", now: datetime | None = None
) -> dict[str, object]:
    """Build the persona meta keys for a target user.

    Returns a dict with ``user_local_hour`` and ``hour_bucket`` when a
    timezone could be resolved; empty dict otherwise.
    """
    tz = resolve_timezone(tz_str=tz_str, location=location)
    if not tz:
        return {}
    hour = current_hour(tz, now=now)
    if hour is None:
        return {}
    return {
        "user_local_hour": hour,
        "hour_bucket": hour_bucket(hour),
        "user_timezone": tz,
    }
