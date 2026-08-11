"""Core data types for the environment sensing channel.

Environment sources are scriptable plugins that let the agent perceive the
external world — GitHub issues, RSS feeds, system metrics, or anything a
``poll`` function can reach.  Each source produces :class:`EnvironmentSignal`
values that are deduplicated by fingerprint and pushed to the agent inbox as
``IncomingEvent`` payloads.

This module is dependency-light on purpose so it can be imported from both the
runtime and test layers without pulling in the full channel stack.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

Severity = Literal["info", "warning", "critical"]


@dataclass
class EnvironmentSignal:
    """One piece of environment information emitted by a source.

    ``fingerprint`` is the deduplication key — sources are responsible for
    making it stable across polls (e.g. ``"issue:142:2026-08-11T10:00Z"``).
    The runtime drops signals whose fingerprint was already pushed.
    """

    source_id: str
    title: str
    content: str
    fingerprint: str
    timestamp: datetime = field(default_factory=datetime.now)
    url: str | None = None
    severity: Severity = "info"


# ---------------------------------------------------------------------------
# Source definitions (parsed from SOURCE.md frontmatter)
# ---------------------------------------------------------------------------

ScheduleTrigger = Literal["periodic", "cold_floor", "manual"]
ExecutionMode = Literal["inline", "subprocess"]


@dataclass
class EnvironmentSourceDef:
    """A loaded environment source definition.

    ``name`` doubles as the source identifier and the participant suffix —
    signals from source ``github-issues`` arrive as
    ``participant_id="env:github-issues"``.
    """

    name: str
    description: str = ""
    mode: ExecutionMode = "inline"
    language: str = "python"
    script: str = "source.py"
    enabled: bool = True
    protected: bool = False
    # --- scheduling (mirrors SubconsciousMode triggers) ---
    schedule_trigger: ScheduleTrigger = "periodic"
    interval_seconds: float = 0.0
    every_seconds: int = 0
    every_n_cycles: int = 0
    every_n_tool_calls: int = 0
    cold_floor_seconds: int = 0
    min_interval_seconds: int = 0
    cron: str = ""
    # --- execution ---
    timeout_seconds: float = 60.0
    params: dict[str, Any] = field(default_factory=dict)
    # filesystem origin (for executor)
    source_dir: str = ""

    @property
    def participant_id(self) -> str:
        """The ``env:`` prefixed participant id used for routing."""
        return f"env:{self.name}"


# ---------------------------------------------------------------------------
# Runtime state (persisted per source)
# ---------------------------------------------------------------------------


@dataclass
class SourceScheduleState:
    """Mutable per-source runtime state, persisted to ``state.json``."""

    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str = ""
    run_count: int = 0
    success_count: int = 0
    cursor: str | None = None
    known_fingerprints: set[str] = field(default_factory=set)
    enabled_override: bool | None = None  # None = follow definition

    def is_enabled(self, definition: EnvironmentSourceDef) -> bool:
        if self.enabled_override is not None:
            return self.enabled_override
        return definition.enabled

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_success_at": (
                self.last_success_at.isoformat() if self.last_success_at else None
            ),
            "last_error": self.last_error,
            "run_count": self.run_count,
            "success_count": self.success_count,
            "cursor": self.cursor,
            "known_fingerprints": sorted(self.known_fingerprints),
            "enabled_override": self.enabled_override,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> SourceScheduleState:
        if not data:
            return cls()
        raw_fps = data.get("known_fingerprints") or []
        fingerprints = {str(fp) for fp in raw_fps} if isinstance(raw_fps, list) else set()

        def _parse_dt(value: Any) -> datetime | None:
            if not value or not isinstance(value, str):
                return None
            try:
                return datetime.fromisoformat(value)
            except (ValueError, TypeError):
                return None

        override = data.get("enabled_override")
        return cls(
            last_run_at=_parse_dt(data.get("last_run_at")),
            last_success_at=_parse_dt(data.get("last_success_at")),
            last_error=str(data.get("last_error") or ""),
            run_count=int(data.get("run_count") or 0),
            success_count=int(data.get("success_count") or 0),
            cursor=data.get("cursor"),
            known_fingerprints=fingerprints,
            enabled_override=bool(override) if override is not None else None,
        )


@dataclass
class PollOutcome:
    """Result of a single ``poll`` invocation for one source."""

    source_id: str
    emitted: int = 0
    deduplicated: int = 0
    error: str | None = None
    duration_seconds: float = 0.0
