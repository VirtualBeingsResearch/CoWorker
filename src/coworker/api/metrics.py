"""Authenticated Prometheus metrics endpoint on the existing API port.

Composes three sources into one ``text/plain; version=0.0.4`` body:

- the runtime registry (HTTP requests, sessions, Relay, model switches),
- persisted lifetime aggregates from :meth:`UsageStatsCollector.metrics_snapshot`,
- channel traffic totals and agent cycle/uptime gauges.

The endpoint follows the API's communication-token model: when
``API__COMMUNICATION_TOKEN`` is configured, scrapes must present a valid
Bearer token; without a configured token it answers like the rest of the
unauthenticated surface (the API binds to loopback by default).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, Header, HTTPException, Request, Response

from coworker.core.metrics import (
    escape_help_text,
    format_labels,
    format_value,
)

if TYPE_CHECKING:
    from coworker.agent.usage_stats import UsageStatsCollector
    from coworker.api.routes import AgentLoop
    from coworker.channels.traffic import ChannelTrafficStore
    from coworker.core.metrics import MetricsRegistry

router = APIRouter()

_PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

_usage_stats: UsageStatsCollector | None = None
_registry: MetricsRegistry | None = None
_agent: AgentLoop | None = None
_channel_traffic: ChannelTrafficStore | None = None


def setup(
    *,
    usage_stats: UsageStatsCollector | None = None,
    registry: MetricsRegistry | None = None,
    agent: AgentLoop | None = None,
    channel_traffic: ChannelTrafficStore | None = None,
) -> None:
    global _usage_stats, _registry, _agent, _channel_traffic
    _usage_stats = usage_stats
    _registry = registry
    _agent = agent
    _channel_traffic = channel_traffic


@router.get("/metrics")
async def get_metrics(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    from coworker.api.routes import (
        communication_token_required,
        verify_communication_authorization,
    )

    if _registry is None:
        raise HTTPException(status_code=404)
    if communication_token_required():
        verify_communication_authorization(authorization)

    body = render_metrics()
    return Response(
        content=body,
        media_type=_PROMETHEUS_CONTENT_TYPE,
        headers={"Cache-Control": "no-store"},
    )


def render_metrics() -> str:
    """Compose the full exposition body from all injected metric sources."""
    lines: list[str] = []
    if _registry is not None:
        lines.append(_registry.render())
    if _usage_stats is not None:
        lines.append(_render_counter_families(_usage_stats.metrics_snapshot()))
    if _channel_traffic is not None:
        lines.append(
            _render_counter_families(
                _channel_traffic_series(_channel_traffic.totals())
            )
        )
    gauges: dict[str, list[tuple[dict[str, str], float]]] = {}
    if _agent is not None:
        gauges["coworker_agent_cycles_total"] = [
            ({}, float(_agent.state.cycle_count))
        ]
    gauges["coworker_uptime_seconds"] = [
        ({}, max(0.0, time.monotonic() - _uptime_start()))
    ]
    lines.append(_render_gauge_families(gauges))
    return "\n".join(part for part in lines if part)


def _uptime_start() -> float:
    from coworker.api.app import metrics_started_monotonic

    return metrics_started_monotonic()


def _render_counter_families(
    families: dict[str, list[tuple[dict[str, str], float]]],
) -> str:
    return _render_families(families, "counter")


def _render_gauge_families(
    families: dict[str, list[tuple[dict[str, str], float]]],
) -> str:
    return _render_families(families, "gauge")


def _render_families(
    families: dict[str, list[tuple[dict[str, str], float]]],
    metric_type: str,
) -> str:
    lines: list[str] = []
    for name in sorted(families):
        series = families[name]
        if not series:
            continue
        lines.append(f"# HELP {name} {escape_help_text(_HELP_TEXTS.get(name, name))}")
        lines.append(f"# TYPE {name} {metric_type}")
        for labels, value in sorted(
            series, key=lambda item: sorted(item[0].items())
        ):
            canonical = tuple(sorted(labels.items()))
            lines.append(f"{name}{format_labels(canonical)} {format_value(value)}")
    return "\n".join(lines) + ("\n" if lines else "")


def _channel_traffic_series(
    totals: dict[str, dict[str, dict[str, int]]],
) -> dict[str, list[tuple[dict[str, str], float]]]:
    series: list[tuple[dict[str, str], float]] = []
    for channel, directions in totals.items():
        for direction, statuses in directions.items():
            for status, count in statuses.items():
                series.append(
                    (
                        {"channel": channel, "direction": direction, "status": status},
                        float(count),
                    )
                )
    return {"coworker_channel_messages_total": series}


_HELP_TEXTS: dict[str, str] = {
    "coworker_llm_calls_total": "LLM calls by provider, model and scope.",
    "coworker_llm_tokens_total": (
        "LLM tokens by provider, model, scope and kind "
        "(input_tokens, output_tokens, cached_tokens)."
    ),
    "coworker_tool_calls_total": "Tool invocations by tool name.",
    "coworker_tool_results_total": "Tool outcomes by tool name and outcome.",
    "coworker_skill_loads_total": "Skill loads by skill and mode.",
    "coworker_bubble_runs_total": "Bubble runs by outcome.",
    "coworker_memory_compressions_total": "Memory compressions by trigger.",
    "coworker_messages_in_total": "Inbound messages by source.",
    "coworker_task_reminders_total": "Background task reminders.",
    "coworker_auto_recalls_total": "Automatic memory recalls.",
    "coworker_auto_recall_memories_total": "Memories surfaced by automatic recall.",
    "coworker_subconscious_spawned_total": "Subconscious bubbles started.",
    "coworker_subconscious_done_total": "Subconscious bubbles finished.",
    "coworker_channel_messages_total": (
        "Channel messages by channel, direction and status over the retained "
        "traffic window."
    ),
    "coworker_agent_cycles_total": "Agent loop cycles since process start.",
    "coworker_uptime_seconds": "Seconds since the API metrics collection started.",
}
