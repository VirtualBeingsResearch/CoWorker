"""Declared agent-event counters aggregated from interaction-log entries.

The interaction log already records these entry types; this module only
declares which of them become aggregate counters, what they are called in the
bucket's ``events`` sub-dict and how they surface as metric series. Adding a
new counter means adding one :class:`EventSpec` row.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EventSpec:
    """One aggregate counter derived from an interaction-log entry type.

    ``key`` is the counter's storage key inside the bucket's ``events`` dict.
    When ``label`` is set, per-label counts are kept under
    ``{key}_by_{label}``. ``count_field`` derives the increment from an entry
    field (list length) instead of counting one per entry. Metric series are
    exported as ``metric_name`` with the optional ``metric_label`` dimension.
    """

    entry_type: str
    key: str
    label: str | None = None
    count_field: str | None = None
    metric_name: str = ""

    @property
    def label_key(self) -> str:
        return f"{self.key}_by_{self.label}"


EVENT_SPECS: tuple[EventSpec, ...] = (
    EventSpec(
        entry_type="message_in",
        key="messages_in",
        label="source",
        metric_name="coworker_messages_in_total",
    ),
    EventSpec(entry_type="task_reminder", key="task_reminders", metric_name="coworker_task_reminders_total"),
    EventSpec(entry_type="auto_recall", key="auto_recalls", metric_name="coworker_auto_recalls_total"),
    EventSpec(
        entry_type="auto_recall",
        key="auto_recall_memories",
        count_field="memories",
        metric_name="coworker_auto_recall_memories_total",
    ),
    EventSpec(
        entry_type="subconscious_spawned",
        key="subconscious_spawned",
        metric_name="coworker_subconscious_spawned_total",
    ),
    EventSpec(
        entry_type="subconscious_done",
        key="subconscious_done",
        metric_name="coworker_subconscious_done_total",
    ),
)

EVENTS_BY_TYPE: dict[str, tuple[EventSpec, ...]] = {}
for _spec in EVENT_SPECS:
    EVENTS_BY_TYPE[_spec.entry_type] = (*EVENTS_BY_TYPE.get(_spec.entry_type, ()), _spec)


def record_entry_events(bucket: dict[str, Any], entry: dict[str, Any]) -> None:
    """Aggregate one interaction-log entry into the bucket's ``events`` dict."""
    events = bucket["events"]
    for spec in EVENTS_BY_TYPE.get(str(entry.get("type")), ()):
        increment = 1
        if spec.count_field:
            value = entry.get(spec.count_field)
            if isinstance(value, (list, tuple, dict)):
                increment = len(value)
            elif isinstance(value, (int, float)) and value > 0:
                increment = int(value)
            else:
                increment = 0
        if increment <= 0:
            continue
        events[spec.key] = events.get(spec.key, 0) + increment
        if spec.label is not None:
            label_map = events.setdefault(spec.label_key, {})
            label = str(entry.get(spec.label) or "").strip() or "unknown"
            label_map[label] = label_map.get(label, 0) + increment


def metrics_series(events: dict[str, Any]) -> list[tuple[str, dict[str, str], float]]:
    """Flatten event counters into ``(metric_name, labels, value)`` series.

    Labeled counters export only their per-label series; Prometheus recovers
    the total via ``sum()``. Mixing unlabeled and labeled series under one
    metric name would produce inconsistent label dimensions.
    """
    series: list[tuple[str, dict[str, str], float]] = []
    for spec in EVENT_SPECS:
        if spec.label is None:
            value = events.get(spec.key, 0)
            if isinstance(value, dict):
                value = 0
            if value:
                series.append((spec.metric_name, {}, float(value)))
            continue
        label_map = events.get(spec.label_key)
        if isinstance(label_map, dict):
            for label, count in sorted(label_map.items()):
                if count:
                    series.append(
                        (spec.metric_name, {spec.label: str(label)}, float(count))
                    )
    return series
