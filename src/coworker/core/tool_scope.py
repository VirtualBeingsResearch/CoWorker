from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coworker.agent.inbox_watcher import InboxWatcher
    from coworker.brain.brain import Brain
    from coworker.memory.short_term import ShortTermMemory
    from coworker.tools.code_tools import BackgroundJobStore
    from coworker.tools.reasoning_tools import TaskStore


@dataclass
class ToolScope:
    """Per-caller resource container.

    Passed to ``ToolRegistry.scoped(scope)`` to produce a registry whose
    scope-sensitive tools (task store, job store, inbox, brain) are wired to
    the caller's own resources rather than the shared main-loop ones.
    """

    task_store: TaskStore
    job_store: BackgroundJobStore
    inbox: InboxWatcher | None
    scope_id: str = "main"
    allow_block: bool = False
    brain: Brain | None = None
    short_term: ShortTermMemory | None = None
