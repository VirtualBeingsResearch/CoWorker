from __future__ import annotations

import pytest
from evals.duration import parse_duration


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("+0s", 0.0),
        ("+3h", 3 * 3600),
        ("+1d", 86400),
        ("+1d+2h", 86400 + 2 * 3600),
        ("+1d-25m", 86400 - 25 * 60),
        ("+10m", 600),
        ("PT3H", 3 * 3600),
        ("P2D", 2 * 86400),
        ("P1DT2H", 86400 + 2 * 3600),
        ("PT1H30M", 3600 + 30 * 60),
        ("3h", 3 * 3600),
    ],
)
def test_parse_duration(text: str, seconds: float) -> None:
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "soon", "P", "PT", "++1h", "+1x"])
def test_parse_duration_rejects_junk(text: str) -> None:
    with pytest.raises(ValueError):
        parse_duration(text)
