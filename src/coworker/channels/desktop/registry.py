from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from coworker.channels.desktop.detail_store import DetailStore, _safe
from coworker.i18n import tr
from coworker.memory.short_term import ShortTermMemory

_PIN_ID = "coworker_desktop_registry"
_RECENT_CONVERSATIONS_IN_PIN = 5


@dataclass
class DesktopActorState:
    desktop_id: str
    display_name: str
    actor_id: str
    participant_id: str
    protocol_version: int
    snapshot: dict[str, Any]


class DesktopRegistry:
    """Tracks connected CoWorker Desktop actors and renders their pinned context.

    Folded-prompt detail persistence is delegated to :class:`DetailStore`.
    Inbound envelope routing (consume vs. wake) is the dispatcher's job.
    """

    def __init__(
        self,
        short_term: ShortTermMemory,
        registry_dir: str | Path,
    ) -> None:
        self._short_term = short_term
        self._dir = Path(registry_dir)
        self._actors: dict[str, DesktopActorState] = {}
        self._connections: set[str] = set()
        self._details = DetailStore(self._dir)

    @property
    def actors(self) -> dict[str, DesktopActorState]:
        return dict(self._actors)

    def update_connections(self, participant_ids: set[str]) -> None:
        self._connections = set(participant_ids)
        stale = [
            key
            for key, actor in self._actors.items()
            if actor.participant_id not in self._connections
        ]
        for key in stale:
            self._actors.pop(key, None)
        self._refresh_pin()

    def ingest_snapshot(self, payload: Any, participant_id: str) -> bool:
        """Validate and store a ``desktop.actor.snapshot`` payload.

        Returns ``True`` (consume) for both valid and recognized-but-invalid
        snapshots so a malformed snapshot never leaks into the agent's inbox;
        only a non-snapshot desktop envelope returns ``False`` upstream.
        """
        if not isinstance(payload, dict):
            return True
        desktop_id = str(payload.get("desktop_id") or "").strip()
        actor_id = str(payload.get("actor_id") or "").strip()
        if not desktop_id or actor_id not in {"local", "codex", "claude"}:
            logger.warning("Ignored invalid CoWorker Desktop actor snapshot")
            return True
        state = DesktopActorState(
            desktop_id=desktop_id,
            display_name=str(payload.get("display_name") or desktop_id),
            actor_id=actor_id,
            participant_id=participant_id,
            protocol_version=1,
            snapshot=payload,
        )
        self._actors[f"{desktop_id}:{actor_id}"] = state
        self._persist(state)
        self._refresh_pin()
        return True

    def render_pinned_context(self) -> str:
        lines = [*tr("channel.desktop.pin_intro").splitlines(), ""]
        for state in sorted(
            self._actors.values(), key=lambda item: (item.desktop_id, item.actor_id)
        ):
            lines.extend(
                [
                    f"- {state.display_name} / {state.actor_id}",
                    f"  participant_id: {state.participant_id}",
                    f"  desktop_id: {state.desktop_id}",
                    "  status: connected",
                ]
            )
            projects = _dict_list(state.snapshot.get("projects"))
            if not projects:
                lines.append(tr("channel.desktop.projects_none"))
                continue
            for project in projects:
                conversation_only = project.get("scope") == "conversation"
                if conversation_only:
                    lines.append(tr("channel.desktop.conversations"))
                else:
                    name = str(project.get("name") or tr("channel.desktop.unknown_project"))
                    project_id = str(project.get("project_id") or "unknown")
                    lines.append(tr("channel.desktop.project", name=name, id=project_id))
                    path = project.get("path")
                    if isinstance(path, str) and path:
                        lines.append(f"    path: {path}")
                counts = _project_counts(project)
                if counts:
                    lines.append(f"    conversation_count: {counts}")
                conversations = _dict_list(project.get("recent_conversations"))
                _append_conversations(lines, conversations, "    ")
        return "\n".join(lines)

    def _persist(self, state: DesktopActorState) -> None:
        root = self._dir / _safe(state.desktop_id) / state.actor_id
        root.mkdir(parents=True, exist_ok=True)
        destination = root / "latest.json"
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(state.snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(destination)

    def _refresh_pin(self) -> None:
        if not self._actors:
            self._short_term.unpin(_PIN_ID)
        else:
            self._short_term.pin(
                _PIN_ID,
                tr("channel.desktop.pin_label"),
                self.render_pinned_context(),
            )

    # --------------------------------------------------- detail store delegates

    def detail_path(self, key: str) -> Path:
        return self._details.detail_path(key)

    def write_detail(self, key: str, text: str) -> Path:
        return self._details.write_detail(key, text)

    def _prune_details(self) -> None:
        self._details.prune()


def _dict_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _project_counts(project: dict[str, Any]) -> str:
    fields = (
        ("shown", "shown_conversation_count"),
        ("matched", "matched_conversation_count"),
        ("complete", "complete"),
        ("truncated", "truncated"),
    )
    parts = []
    for label, key in fields:
        value = project.get(key)
        if isinstance(value, bool):
            parts.append(f"{label}={str(value).lower()}")
        elif isinstance(value, int):
            parts.append(f"{label}={value}")
    return ", ".join(parts)


def _append_conversations(
    lines: list[str], conversations: list[dict[str, Any]], indent: str
) -> None:
    if not conversations:
        lines.append(tr("channel.desktop.recent_none", indent=indent))
        return
    for conversation in conversations[:_RECENT_CONVERSATIONS_IN_PIN]:
        conversation_id = str(conversation.get("conversation_id") or "unknown")
        title = " ".join(
            str(conversation.get("title") or tr("channel.desktop.unnamed_conversation")).split()
        )
        title = title if len(title) <= 12 else f"{title[:11]}…"
        details = []
        mode = conversation.get("mode")
        if isinstance(mode, str) and mode:
            details.append(f"mode={mode}")
        updated_at = conversation.get("updated_at")
        if isinstance(updated_at, str) and updated_at:
            details.append(f"updated_at={updated_at}")
        suffix = (
            tr("channel.desktop.conversation_details", details=", ".join(details))
            if details
            else ""
        )
        lines.append(f"{indent}- {conversation_id} {title}{suffix}")
