"""Tests for the runtime metrics registry and its Prometheus exposition."""

from __future__ import annotations

import threading

import pytest

from coworker.core.metrics import MetricsRegistry


def test_counter_renders_labels_sorted_and_escaped():
    registry = MetricsRegistry()
    # chr(92) == backslash，避免在断言里叠加多层字符串转义。
    backslash = chr(92)
    requests = registry.counter(
        "coworker_test_total",
        'Help with "quotes" and ' + backslash + " marks.",
        ("route", "status"),
    )

    requests.inc(route="/ws/{id}", status="200")
    tricky = 'a"b' + backslash + "c"
    requests.inc(route=tricky, status="200", value=2)

    rendered = registry.render()
    assert "# TYPE coworker_test_total counter" in rendered
    assert 'coworker_test_total{route="/ws/{id}",status="200"} 1' in rendered
    assert (
        'coworker_test_total{route="a' + backslash + '"b' + backslash * 2
        + 'c",status="200"} 2'
    ) in rendered
    assert 'Help with "quotes" and ' + backslash * 2 + " marks." in rendered
    assert rendered.endswith("\n")


def test_gauge_supports_set_inc_and_dec():
    registry = MetricsRegistry()
    active = registry.gauge("coworker_test_active", "Active things.", ("transport",))

    active.set(5, transport="sse")
    active.inc(2, transport="sse")
    active.dec(transport="sse")

    assert 'coworker_test_active{transport="sse"} 6' in registry.render()


def test_histogram_renders_cumulative_buckets_sum_and_count():
    registry = MetricsRegistry()
    duration = registry.histogram(
        "coworker_test_duration_seconds",
        "Observed durations.",
        buckets=(0.1, 0.5, 1.0),
    )

    duration.observe(0.05)
    duration.observe(0.4)
    duration.observe(3.0)

    rendered = registry.render()
    assert "# TYPE coworker_test_duration_seconds histogram" in rendered
    assert 'coworker_test_duration_seconds_bucket{le="0.1"} 1' in rendered
    assert 'coworker_test_duration_seconds_bucket{le="0.5"} 2' in rendered
    assert 'coworker_test_duration_seconds_bucket{le="1"} 2' in rendered
    assert 'coworker_test_duration_seconds_bucket{le="+Inf"} 3' in rendered
    assert "coworker_test_duration_seconds_sum 3.45" in rendered
    assert "coworker_test_duration_seconds_count 3" in rendered


def test_families_without_observations_are_omitted():
    registry = MetricsRegistry()
    registry.counter("coworker_test_declared_only", "Never incremented.")

    assert registry.render() == ""


def test_duplicate_family_declaration_and_label_mismatch_raise():
    registry = MetricsRegistry()
    registry.counter("coworker_test_dup", "First.", ("a",))
    with pytest.raises(ValueError, match="already declared"):
        registry.gauge("coworker_test_dup", "Second.")

    counter = registry.counter("coworker_test_labels", "Label check.", ("a",))
    with pytest.raises(ValueError, match="expected labels"):
        counter.inc(b="wrong")


def test_families_render_sorted_by_name():
    registry = MetricsRegistry()
    registry.counter("coworker_test_b_total", "B.").inc()
    registry.gauge("coworker_test_a_gauge", "A.").set(1)

    rendered = registry.render()
    assert rendered.index("coworker_test_a_gauge") < rendered.index(
        "coworker_test_b_total"
    )


def test_counter_is_thread_safe_under_concurrent_increments():
    registry = MetricsRegistry()
    counter = registry.counter("coworker_test_threads_total", "Concurrent.", ("worker",))
    workers = 8
    rounds = 500

    def hammer(worker: str) -> None:
        for _ in range(rounds):
            counter.inc(worker=worker)

    threads = [
        threading.Thread(target=hammer, args=(f"w{index}",))
        for index in range(workers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    rendered = registry.render()
    for index in range(workers):
        assert f'coworker_test_threads_total{{worker="w{index}"}} {rounds}' in rendered
