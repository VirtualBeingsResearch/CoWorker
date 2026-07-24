from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from coworker.channels.stream.desktop.detail_store import DetailStore, _safe
from coworker.i18n import tr
from coworker.memory.short_term import ShortTermMemory

_PIN_ID = "coworker_desktop_registry"
_RECENT_CONVERSATIONS_PER_ACTOR = 4
_CONVERSATION_TITLE_LIMIT = 24


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
        for desktop_states in _group_by_desktop(self._actors.values()):
            lines.append(
                tr(
                    "channel.desktop.desktop",
                    name=desktop_states[0].display_name,
                )
            )
            for state in desktop_states:
                lines.append(
                    tr(
                        "channel.desktop.actor",
                        actor=state.actor_id,
                        participant=state.participant_id,
                    )
                )
                _append_actor_projects(lines, state.snapshot)
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


def _group_by_desktop(
    actors: Iterable[DesktopActorState],
) -> list[list[DesktopActorState]]:
    grouped: dict[str, list[DesktopActorState]] = {}
    for actor in sorted(actors, key=lambda item: (item.desktop_id, item.actor_id)):
        grouped.setdefault(actor.desktop_id, []).append(actor)
    return list(grouped.values())


def _append_actor_projects(lines: list[str], snapshot: dict[str, Any]) -> None:
    projects = _dict_list(snapshot.get("projects"))
    visible = _recent_projects(projects)
    if not visible:
        lines.append(tr("channel.desktop.projects_none"))
        return
    for project, conversations in visible:
        _append_project(lines, project, len(conversations))
        _append_conversations(lines, conversations)


def _recent_projects(
    projects: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    ranked: list[tuple[str, int, int, dict[str, Any]]] = []
    for project_index, project in enumerate(projects):
        for conversation_index, conversation in enumerate(
            _dict_list(project.get("recent_conversations"))
        ):
            updated_at = conversation.get("updated_at")
            rank = updated_at if isinstance(updated_at, str) else ""
            ranked.append((rank, project_index, conversation_index, conversation))
    selected = sorted(
        ranked,
        key=lambda item: (item[0], -item[1], -item[2]),
        reverse=True,
    )[:_RECENT_CONVERSATIONS_PER_ACTOR]
    conversations_by_project: dict[int, list[dict[str, Any]]] = {}
    for _, project_index, _, conversation in selected:
        conversations_by_project.setdefault(project_index, []).append(conversation)
    return [
        (projects[project_index], conversations)
        for project_index, conversations in conversations_by_project.items()
    ]


def _append_project(
    lines: list[str],
    project: dict[str, Any],
    visible_conversation_count: int,
) -> None:
    if project.get("scope") == "conversation":
        lines.append(tr("channel.desktop.conversations"))
        return
    name = str(project.get("name") or tr("channel.desktop.unknown_project"))
    project_id = str(project.get("project_id") or "unknown")
    path = project.get("path")
    path_note = (
        tr("channel.desktop.project_path", path=path)
        if isinstance(path, str)
        and path
        and _normalize_path(path) != _normalize_path(project_id)
        else ""
    )
    summary = _project_summary(project, visible_conversation_count)
    lines.append(
        tr(
            "channel.desktop.project",
            name=name,
            id=project_id,
            path=path_note,
            summary=summary,
        )
    )


def _project_summary(
    project: dict[str, Any],
    visible_conversation_count: int,
) -> str:
    available = len(_dict_list(project.get("recent_conversations")))
    matched = project.get("matched_conversation_count")
    total = max(matched, available) if isinstance(matched, int) else available
    if total > visible_conversation_count:
        return tr(
            "channel.desktop.project_partial",
            shown=visible_conversation_count,
            matched=total,
        )
    if project.get("truncated") is True or project.get("complete") is False:
        return tr("channel.desktop.project_incomplete")
    return ""


def _normalize_path(value: str) -> str:
    return value.replace("\\", "/").rstrip("/").casefold()


def _append_conversations(
    lines: list[str],
    conversations: list[dict[str, Any]],
) -> None:
    for conversation in conversations:
        conversation_id = str(conversation.get("conversation_id") or "unknown")
        title = " ".join(
            str(conversation.get("title") or tr("channel.desktop.unnamed_conversation")).split()
        )
        title = (
            title
            if len(title) <= _CONVERSATION_TITLE_LIMIT
            else f"{title[: _CONVERSATION_TITLE_LIMIT - 1]}…"
        )
        details = []
        mode = conversation.get("mode")
        if isinstance(mode, str) and mode and mode != "default":
            details.append(mode)
        updated_at = conversation.get("updated_at")
        if isinstance(updated_at, str) and updated_at:
            details.append(updated_at.split("T", 1)[0])
        suffix = (
            tr("channel.desktop.conversation_details", details=", ".join(details))
            if details
            else ""
        )
        lines.append(
            tr(
                "channel.desktop.conversation",
                id=conversation_id,
                title=title,
                details=suffix,
            )
        )
