"""Parse scenario durations: ``+3h``, ``+1d+2h``, ``PT3H``, ``P2D``."""

from __future__ import annotations

import re

_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
_OFFSET = re.compile(r"([+-])(\d+(?:\.\d+)?)(d|h|m|s)", re.IGNORECASE)
_ISO = re.compile(
    r"^P(?:(\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?$",
    re.IGNORECASE,
)


def parse_duration(text: str) -> float:
    """Return seconds for an ``at:`` / ``clock.horizon`` / ``clock.jitter`` value."""
    raw = str(text).strip()
    if not raw:
        raise ValueError("empty duration")
    if raw[:1] in "Pp":
        return _parse_iso(raw)
    if raw[0] in "+-" or raw[0].isdigit():
        return _parse_offset(raw if raw[0] in "+-" else f"+{raw}")
    raise ValueError(f"unsupported duration: {text!r}")


def _parse_offset(raw: str) -> float:
    total = 0.0
    pos = 0
    for match in _OFFSET.finditer(raw):
        if match.start() != pos:
            raise ValueError(f"unsupported duration: {raw!r}")
        sign = 1.0 if match.group(1) == "+" else -1.0
        total += sign * float(match.group(2)) * _UNITS[match.group(3).lower()]
        pos = match.end()
    if pos != len(raw) or pos == 0:
        raise ValueError(f"unsupported duration: {raw!r}")
    return total


def _parse_iso(raw: str) -> float:
    match = _ISO.fullmatch(raw)
    if match is None:
        raise ValueError(f"unsupported duration: {raw!r}")
    days, hours, minutes, seconds = match.groups()
    if not any((days, hours, minutes, seconds)):
        raise ValueError(f"unsupported duration: {raw!r}")
    total = 0.0
    if days:
        total += float(days) * 86400.0
    if hours:
        total += float(hours) * 3600.0
    if minutes:
        total += float(minutes) * 60.0
    if seconds:
        total += float(seconds)
    return total
