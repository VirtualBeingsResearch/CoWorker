"""The environment sensing channel — a receive-only ``BaseChannel``.

Environment sources feed external-world signals into the agent inbox through
this channel.  It is intentionally one-directional: ``send()`` returns a clear
"not supported" error because environment sources are sensors, not
conversational endpoints.  Inbound signals travel the standard
``publish_inbound`` path and inherit access-control + traffic recording for
free.

The channel also contributes to the system prompt via
:meth:`agent_instructions`, telling the agent which sources are live and how
to adjust them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from coworker.channels.base import BaseChannel, ConnectionInfo
from coworker.core.types import CommunicateRequest, ToolResult
from coworker.i18n import tr

from .loader import EnvironmentLoader

if TYPE_CHECKING:
    from .runtime import EnvironmentRuntime


class EnvironmentChannel(BaseChannel):
    """Receive-only channel that surfaces environment-source signals."""

    name = "environment"
    participant_prefix = "env:"
    requires_known_participant = False

    def __init__(
        self,
        loader: EnvironmentLoader,
        runtime: EnvironmentRuntime | None = None,
    ) -> None:
        super().__init__()
        self._loader = loader
        self._env_runtime = runtime

    async def send(self, request: CommunicateRequest) -> ToolResult:
        return ToolResult(
            tool_call_id="",
            content=tr(
                "channel.environment.send_not_supported",
                participant=request.participant_id,
            ),
            is_error=True,
        )

    def agent_instructions(self) -> str:
        sources = self._loader.list_enabled()
        if not sources:
            return ""
        lines = [tr("channel.environment.instructions_intro")]
        for src in sources:
            schedule = _describe_schedule(src)
            desc = f" — {src.description}" if src.description else ""
            lines.append(
                tr(
                    "channel.environment.source_entry",
                    name=src.name,
                    schedule=schedule,
                    description=desc,
                )
            )
        lines.append(tr("channel.environment.adjust_hint"))
        return "\n".join(lines)

    def list_connections(self) -> list[ConnectionInfo]:
        connections: list[ConnectionInfo] = []
        for src in self._loader.list_enabled():
            connections.append(
                ConnectionInfo(
                    participant_id=f"env:{src.name}",
                    channel=self.name,
                    kind=src.language,
                    display_name=src.description or src.name,
                    active=True,
                )
            )
        return connections


def _describe_schedule(src) -> str:
    """Human-readable schedule description for the prompt."""
    parts: list[str] = []
    if src.every_seconds:
        parts.append(tr("channel.environment.schedule.every_seconds", n=src.every_seconds))
    if src.interval_seconds:
        parts.append(
            tr("channel.environment.schedule.interval_seconds", n=int(src.interval_seconds))
        )
    if src.every_n_cycles:
        parts.append(tr("channel.environment.schedule.every_n_cycles", n=src.every_n_cycles))
    if src.every_n_tool_calls:
        parts.append(
            tr("channel.environment.schedule.every_n_tool_calls", n=src.every_n_tool_calls)
        )
    if src.cron:
        parts.append(tr("channel.environment.schedule.cron", expr=src.cron))
    if src.cold_floor_seconds:
        parts.append(
            tr("channel.environment.schedule.cold_floor", n=src.cold_floor_seconds)
        )
    if not parts:
        parts.append(tr("channel.environment.schedule.manual"))
    return " · ".join(parts)
