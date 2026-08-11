from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from coworker.core.config import I18NConfig
from coworker.core.timezone import (
    as_local,
    configure_timezone,
    local_now,
    local_wall_now,
    timezone_description,
)
from coworker.i18n import locale_context


@pytest.fixture(autouse=True)
def reset_runtime_timezone():
    configure_timezone("")
    yield
    configure_timezone("")


def test_i18n_config_accepts_and_normalizes_iana_timezone() -> None:
    assert I18NConfig(timezone="  Asia/Shanghai ").timezone == "Asia/Shanghai"
    assert I18NConfig(timezone="").timezone == ""


def test_i18n_config_rejects_unknown_timezone_in_runtime_language() -> None:
    with locale_context("en"), pytest.raises(ValueError, match="Unknown IANA timezone"):
        I18NConfig(timezone="Mars/Olympus_Mons")


def test_runtime_timezone_controls_current_and_naive_wall_times() -> None:
    configure_timezone("Asia/Shanghai")

    now = local_now()
    wall_now = local_wall_now()
    wall_time = as_local(datetime(2026, 1, 2, 9, 30))

    assert now.utcoffset() == timedelta(hours=8)
    assert wall_now.tzinfo is None
    assert abs(wall_now - now.replace(tzinfo=None)) < timedelta(seconds=1)
    assert getattr(wall_time.tzinfo, "key", None) == "Asia/Shanghai"
    assert wall_time.isoformat() == "2026-01-02T09:30:00+08:00"
    assert timezone_description() == "Asia/Shanghai (UTC+8)"


def test_runtime_timezone_converts_aware_instants() -> None:
    configure_timezone("America/New_York")

    converted = as_local(datetime(2026, 1, 2, 12, 0, tzinfo=UTC))

    assert converted.isoformat() == "2026-01-02T07:00:00-05:00"
