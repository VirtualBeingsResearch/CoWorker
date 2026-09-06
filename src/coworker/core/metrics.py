"""In-process runtime metrics registry with Prometheus text rendering.

Holds transport-level telemetry (HTTP requests, stream sessions, Relay
connections): process-lifetime counters, gauges and one histogram. These
metrics legitimately reset when the process restarts; anything that must
survive restarts belongs to the usage-statistics aggregate instead.

The exposition text follows the Prometheus text format version 0.0.4. Family
declarations are explicit (name, help, label names) so label dimensions stay
bounded by construction.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Literal, TypeVar

#: Upper bounds for HTTP request duration observations, in seconds.
DEFAULT_DURATION_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

MetricType = Literal["counter", "gauge", "histogram"]
Labels = tuple[tuple[str, str], ...]


def escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def escape_help_text(help: str) -> str:
    return help.replace("\\", "\\\\").replace("\n", "\\n")


def format_value(value: float) -> str:
    if value != value:  # NaN
        return "NaN"
    if value == float("inf"):
        return "+Inf"
    if value == float("-inf"):
        return "-Inf"
    if value.is_integer():
        return str(int(value))
    return format(value, ".15g")


def format_labels(labels: Labels) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{name}="{escape_label_value(value)}"' for name, value in labels)
    return f"{{{inner}}}"


@dataclass(frozen=True)
class _Family:
    name: str
    help: str
    type: MetricType
    label_names: tuple[str, ...] = ()
    buckets: tuple[float, ...] = ()


@dataclass
class _HistogramState:
    """Cumulative bucket counts plus the running sum and observation count."""

    counts: list[int] = field(default_factory=list)
    total: float = 0.0
    observations: int = 0


def _canonical_labels(
    label_names: tuple[str, ...],
    values: dict[str, str],
) -> Labels:
    if set(values) != set(label_names):
        raise ValueError(
            f"expected labels {sorted(label_names)}, got {sorted(values)}"
        )
    return tuple(sorted((name, str(value)) for name, value in values.items()))


class _SeriesHandle:
    """Shared base for metric handles bound to a registry."""

    def __init__(self, registry: MetricsRegistry, family: _Family) -> None:
        self._registry = registry
        self._family = family

    def _labels(self, values: dict[str, str]) -> Labels:
        return _canonical_labels(self._family.label_names, values)

    def _add(self, labels: dict[str, str], delta: float) -> None:
        canonical = self._labels(labels)
        with self._registry._lock:
            series = self._registry._series.setdefault(self._family.name, {})
            series[canonical] = series.get(canonical, 0.0) + delta

    def _set(self, labels: dict[str, str], value: float) -> None:
        canonical = self._labels(labels)
        with self._registry._lock:
            self._registry._series.setdefault(self._family.name, {})[canonical] = value


_SeriesHandleT = TypeVar("_SeriesHandleT", bound=_SeriesHandle)


class Counter(_SeriesHandle):
    """Monotonic counter."""

    def inc(self, value: float = 1.0, **labels: str) -> None:
        self._add(labels, value)


class Gauge(_SeriesHandle):
    """Point-in-time value; supports ``set``, ``inc`` and ``dec``."""

    def set(self, value: float, **labels: str) -> None:
        self._set(labels, value)

    def inc(self, value: float = 1.0, **labels: str) -> None:
        self._add(labels, value)

    def dec(self, value: float = 1.0, **labels: str) -> None:
        self._add(labels, -value)


class Histogram(_SeriesHandle):
    """Cumulative-bucket histogram for latency-style observations."""

    def observe(self, value: float, **labels: str) -> None:
        canonical = self._labels(labels)
        with self._registry._lock:
            state = self._registry._histograms.setdefault(self._family.name, {})
            entry = state.setdefault(
                canonical,
                _HistogramState(counts=[0] * len(self._family.buckets)),
            )
            position = 0
            while (
                position < len(self._family.buckets)
                and value > self._family.buckets[position]
            ):
                position += 1
            for index in range(position, len(entry.counts)):
                entry.counts[index] += 1
            entry.total += value
            entry.observations += 1


class MetricsRegistry:
    """Declares metric families and renders them as Prometheus text."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._families: dict[str, _Family] = {}
        self._series: dict[str, dict[Labels, float]] = {}
        self._histograms: dict[str, dict[Labels, _HistogramState]] = {}

    def _declare(
        self,
        family: _Family,
        handle_cls: type[_SeriesHandleT],
    ) -> _SeriesHandleT:
        with self._lock:
            if family.name in self._families:
                raise ValueError(f"metric family already declared: {family.name}")
            self._families[family.name] = family
        return handle_cls(self, family)

    def counter(
        self,
        name: str,
        help: str,
        label_names: tuple[str, ...] = (),
    ) -> Counter:
        family = _Family(name, help, "counter", tuple(sorted(label_names)))
        return self._declare(family, Counter)

    def gauge(
        self,
        name: str,
        help: str,
        label_names: tuple[str, ...] = (),
    ) -> Gauge:
        family = _Family(name, help, "gauge", tuple(sorted(label_names)))
        return self._declare(family, Gauge)

    def histogram(
        self,
        name: str,
        help: str,
        buckets: tuple[float, ...] = DEFAULT_DURATION_BUCKETS,
    ) -> Histogram:
        family = _Family(name, help, "histogram", (), tuple(buckets))
        return self._declare(family, Histogram)

    def render(self) -> str:
        """Render every family with at least one observation, sorted by name."""
        lines: list[str] = []
        with self._lock:
            for name in sorted(self._families):
                family = self._families[name]
                if family.type == "histogram":
                    lines.extend(self._render_histogram(family))
                else:
                    lines.extend(self._render_series(family))
        return "\n".join(lines) + ("\n" if lines else "")

    def _render_series(self, family: _Family) -> list[str]:
        series = self._series.get(family.name) or {}
        if not series:
            return []
        lines = [
            f"# HELP {family.name} {escape_help_text(family.help)}",
            f"# TYPE {family.name} {family.type}",
        ]
        for labels in sorted(series):
            lines.append(
                f"{family.name}{format_labels(labels)} {format_value(series[labels])}"
            )
        return lines

    def _render_histogram(self, family: _Family) -> list[str]:
        state = self._histograms.get(family.name) or {}
        if not state:
            return []
        lines = [
            f"# HELP {family.name} {escape_help_text(family.help)}",
            f"# TYPE {family.name} histogram",
        ]
        for labels in sorted(state):
            entry = state[labels]
            for bound, count in zip(family.buckets, entry.counts, strict=True):
                bucket_labels = labels + (("le", format_value(bound)),)
                lines.append(
                    f"{family.name}_bucket{format_labels(bucket_labels)} {count}"
                )
            infinite_labels = labels + (("le", "+Inf"),)
            lines.append(
                f"{family.name}_bucket{format_labels(infinite_labels)}"
                f" {entry.observations}"
            )
            lines.append(
                f"{family.name}_sum{format_labels(labels)} {format_value(entry.total)}"
            )
            lines.append(
                f"{family.name}_count{format_labels(labels)} {entry.observations}"
            )
        return lines
