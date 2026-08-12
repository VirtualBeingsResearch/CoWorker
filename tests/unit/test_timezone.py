from __future__ import annotations

from datetime import UTC, datetime, timedelta

from coworker.core.timezone import (
    as_local,
    local_from_timestamp,
    local_now,
    local_wall_now,
    timezone_description,
)


def test_local_time_helpers_follow_the_host_timezone() -> None:
    naive = datetime(2026, 1, 2, 9, 30)

    now = local_now()
    wall_now = local_wall_now()
    wall_time = as_local(naive)

    assert now.tzinfo is not None
    assert wall_now.tzinfo is None
    assert abs(wall_now - now.replace(tzinfo=None)) < timedelta(seconds=1)
    assert wall_time == naive.astimezone()
    assert "UTC" in timezone_description()


def test_local_conversions_use_the_host_timezone() -> None:
    aware = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    timestamp = aware.timestamp()

    assert as_local(aware) == aware.astimezone()
    assert local_from_timestamp(timestamp) == datetime.fromtimestamp(timestamp).astimezone()
