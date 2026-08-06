from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_usage_analytics_uses_the_shared_admin_type_scale() -> None:
    css = (REPOSITORY_ROOT / "web/src/admin/admin.css").read_text(encoding="utf-8")
    marker = "/* Usage analytics follows the shared admin scale; dense charts keep the micro floor. */"
    typography = css.split(marker, 1)[1].split("/* Desktop releases", 1)[0]

    assert "--admin-type-small" in typography
    assert "--admin-type-caption" in typography
    assert "--admin-type-micro" in typography

    for selector in (
        ".usage-analytics-windows button",
        ".usage-scope-filter button",
        ".usage-analytics-metric > span",
        ".usage-analytics-metric > small",
        ".usage-model-table td",
        ".usage-source-cards > button > small",
        ".usage-execution-list article strong",
        ".usage-trend-chart article > span",
        ".usage-intraday-bars > button > small",
    ):
        assert selector in typography
