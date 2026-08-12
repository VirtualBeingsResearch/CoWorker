from __future__ import annotations

from datetime import UTC, datetime


def local_now() -> datetime:
    """Return the current time in the host process timezone."""
    return datetime.now().astimezone()


def local_wall_now() -> datetime:
    """Return configured local wall time without tzinfo for legacy timestamp stores."""
    return local_now().replace(tzinfo=None)


def as_local(value: datetime) -> datetime:
    """Interpret naive wall times in, or convert aware values to, the host timezone."""
    return value.astimezone()


def local_from_timestamp(value: float) -> datetime:
    return datetime.fromtimestamp(value).astimezone()


def timezone_description() -> str:
    now = local_now()
    offset_seconds = int((now.utcoffset() or UTC.utcoffset(now)).total_seconds())
    hours, remainder = divmod(abs(offset_seconds), 3600)
    minutes = remainder // 60
    sign = "+" if offset_seconds >= 0 else "-"
    offset = f"UTC{sign}{hours}" if minutes == 0 else f"UTC{sign}{hours}:{minutes:02d}"
    name = getattr(now.tzinfo, "key", None) or now.tzname() or "UTC"
    return f"{name} ({offset})"
