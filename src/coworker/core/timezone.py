from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from coworker.i18n import tr

_configured_timezone: ZoneInfo | None = None


def normalize_timezone(value: object) -> str:
    """Normalize and validate an optional IANA timezone name."""
    if value is None:
        return ""
    name = str(value).strip()
    if not name:
        return ""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(tr("config.timezone.invalid", timezone=name)) from exc
    return name


def configure_timezone(name: str) -> None:
    """Select the instance timezone; an empty name follows the host timezone."""
    global _configured_timezone
    normalized = normalize_timezone(name)
    _configured_timezone = ZoneInfo(normalized) if normalized else None


def runtime_timezone() -> tzinfo:
    if _configured_timezone is not None:
        return _configured_timezone
    return datetime.now().astimezone().tzinfo or UTC


def local_now() -> datetime:
    if _configured_timezone is None:
        return datetime.now().astimezone()
    return datetime.now(_configured_timezone)


def local_wall_now() -> datetime:
    """Return configured local wall time without tzinfo for legacy timestamp stores."""
    return local_now().replace(tzinfo=None)


def as_local(value: datetime) -> datetime:
    """Interpret naive wall times in, or convert aware values to, the instance timezone."""
    if _configured_timezone is None:
        return value.astimezone()
    zone = _configured_timezone
    if value.tzinfo is None:
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)


def local_from_timestamp(value: float) -> datetime:
    if _configured_timezone is None:
        return datetime.fromtimestamp(value).astimezone()
    return datetime.fromtimestamp(value, _configured_timezone)


def timezone_description() -> str:
    now = local_now()
    offset_seconds = int((now.utcoffset() or UTC.utcoffset(now)).total_seconds())
    hours, remainder = divmod(abs(offset_seconds), 3600)
    minutes = remainder // 60
    sign = "+" if offset_seconds >= 0 else "-"
    offset = f"UTC{sign}{hours}" if minutes == 0 else f"UTC{sign}{hours}:{minutes:02d}"
    name = getattr(now.tzinfo, "key", None) or now.tzname() or "UTC"
    return f"{name} ({offset})"
