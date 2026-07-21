from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from coworker.core.types import ToolResult
from coworker.i18n import tr
from coworker.tools.base import Tool, ToolDefinition

_TASK_STATUSES = ("pending", "in_progress", "completed", "deleted")
_DETAILS_UPDATE_MODES = ("replace", "append", "patch")
_TASK_KIND_PREFIXES = {
    "[growth]": "growth",
    "[maintenance]": "maintenance",
    "[subconscious]": "subconscious",
    # Read-only compatibility for tasks created before the stable ASCII protocol.
    "[成长]": "growth",
    "[维护]": "maintenance",
    "[潜意识]": "subconscious",
}
_HUNK_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)
_UNSUPPORTED_PATCH_PREFIXES = (
    "diff --git ",
    "Binary files ",
    "rename ",
    "new file mode ",
    "deleted file mode ",
    "old mode ",
    "new mode ",
)


class DetailsPatchError(ValueError):
    pass


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def infer_task_kind(description: str) -> str:
    """Return the stable framework task kind, including legacy-prefix inference."""

    stripped = description.lstrip()
    for prefix, kind in _TASK_KIND_PREFIXES.items():
        if stripped.startswith(prefix):
            return kind
    return ""


def _timestamp_from_path(path: Path) -> str | None:
    try:
        return (
            datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
        )
    except OSError:
        return None


def _parse_task_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone()


def _relative_time(value: datetime, *, now: datetime | None = None) -> str:
    now = (now or datetime.now().astimezone()).astimezone()
    value = value.astimezone()
    seconds = int((now - value).total_seconds())
    future = seconds < 0
    seconds = abs(seconds)

    if seconds < 60:
        return tr("tool_result.task.soon" if future else "tool_result.task.just_now")
    if seconds < 3600:
        return tr(
            "tool_result.task.minutes_future" if future else "tool_result.task.minutes_past",
            count=seconds // 60,
        )
    if seconds < 86400:
        return tr(
            "tool_result.task.hours_future" if future else "tool_result.task.hours_past",
            count=seconds // 3600,
        )

    day_delta = (now.date() - value.date()).days
    if future:
        day_delta = -day_delta
        if day_delta == 1:
            return tr("tool_result.task.tomorrow")
        if day_delta == 2:
            return tr("tool_result.task.day_after_tomorrow")
        if day_delta < 30:
            return tr("tool_result.task.days_future", count=day_delta)
        if day_delta < 365:
            return tr("tool_result.task.months_future", count=day_delta // 30)
        return tr("tool_result.task.years_future", count=day_delta // 365)

    if day_delta == 0:
        return tr("tool_result.task.today")
    if day_delta == 1:
        return tr("tool_result.task.yesterday")
    if day_delta == 2:
        return tr("tool_result.task.day_before_yesterday")
    if day_delta < 30:
        return tr("tool_result.task.days_past", count=day_delta)
    if day_delta < 365:
        return tr("tool_result.task.months_past", count=day_delta // 30)
    return tr("tool_result.task.years_past", count=day_delta // 365)


def _should_show_absolute_date(value: datetime, *, now: datetime | None = None) -> bool:
    now = (now or datetime.now().astimezone()).astimezone()
    value = value.astimezone()
    return abs((now.date() - value.date()).days) >= 30


def format_task_time(value: str, *, now: datetime | None = None) -> str:
    parsed = _parse_task_time(value)
    if parsed is None:
        return value or tr("tool_result.task.unknown_time")
    relative = _relative_time(parsed, now=now)
    if not _should_show_absolute_date(parsed, now=now):
        return relative
    return tr(
        "tool_result.task.absolute",
        relative=relative,
        date=parsed.strftime("%Y-%m-%d"),
    )


def format_task_times(task: Task, *, now: datetime | None = None) -> str:
    return tr(
        "tool_result.task.times",
        created=format_task_time(task.created_at, now=now),
        updated=format_task_time(task.updated_at, now=now),
    )


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _split_text_lines(text: str) -> tuple[list[str], bool]:
    text = _normalize_newlines(text)
    if text == "":
        return [], False
    trailing_newline = text.endswith("\n")
    lines = text.split("\n")
    if trailing_newline:
        lines = lines[:-1]
    return lines, trailing_newline


def _join_text_lines(lines: list[str], trailing_newline: bool) -> str:
    if not lines:
        return "\n" if trailing_newline else ""
    return "\n".join(lines) + ("\n" if trailing_newline else "")


def _parse_hunk_header(line: str) -> tuple[int, int, int, int]:
    m = _HUNK_RE.match(line)
    if not m:
        raise DetailsPatchError(tr("tool_result.task_patch.invalid_header", line=line))
    old_start = int(m.group("old_start"))
    old_count = int(m.group("old_count") or "1")
    new_start = int(m.group("new_start"))
    new_count = int(m.group("new_count") or "1")
    return old_start, old_count, new_start, new_count


def _apply_unified_diff(text: str, patch: str) -> str:
    """Apply a small, single-document unified diff to task details.

    This intentionally supports only ordinary text hunks. It rejects multi-file and
    metadata patches so task details stay a plain Markdown field, not a filesystem.
    """
    patch = _normalize_newlines(patch)
    if not patch.strip():
        raise DetailsPatchError(tr("tool_result.task_patch.empty"))

    source, source_trailing_newline = _split_text_lines(text)
    patch_lines = patch.split("\n")
    if patch_lines and patch_lines[-1] == "":
        patch_lines = patch_lines[:-1]

    out: list[str] = []
    source_pos = 0
    i = 0
    seen_file_header = False
    seen_hunk = False
    result_trailing_newline = source_trailing_newline

    while i < len(patch_lines):
        line = patch_lines[i]
        if line.startswith(_UNSUPPORTED_PATCH_PREFIXES):
            raise DetailsPatchError(tr("tool_result.task_patch.unsupported_metadata"))
        if line.startswith("--- "):
            if seen_file_header or seen_hunk:
                raise DetailsPatchError(tr("tool_result.task_patch.multi_file"))
            if i + 1 >= len(patch_lines) or not patch_lines[i + 1].startswith("+++ "):
                raise DetailsPatchError(tr("tool_result.task_patch.missing_plus_header"))
            seen_file_header = True
            i += 2
            continue
        if line.startswith("+++ "):
            raise DetailsPatchError(tr("tool_result.task_patch.missing_minus_header"))
        if not line.startswith("@@ "):
            raise DetailsPatchError(tr("tool_result.task_patch.unexpected", line=line))

        seen_hunk = True
        old_start, old_count, _new_start, new_count = _parse_hunk_header(line)
        i += 1

        target_pos = 0 if old_start == 0 and old_count == 0 else old_start - 1
        if target_pos < source_pos or target_pos > len(source):
            raise DetailsPatchError(tr("tool_result.task_patch.out_of_bounds"))
        out.extend(source[source_pos:target_pos])
        source_pos = target_pos

        old_seen = 0
        new_seen = 0
        while i < len(patch_lines) and not patch_lines[i].startswith("@@ "):
            hline = patch_lines[i]
            if hline == r"\ No newline at end of file":
                result_trailing_newline = False
                i += 1
                continue
            if hline.startswith("--- ") or hline.startswith("+++ "):
                raise DetailsPatchError(tr("tool_result.task_patch.multi_file"))
            if not hline:
                raise DetailsPatchError(tr("tool_result.task_patch.missing_prefix"))
            op = hline[0]
            value = hline[1:]
            if op == " ":
                if source_pos >= len(source) or source[source_pos] != value:
                    raise DetailsPatchError(
                        tr("tool_result.task_patch.context_mismatch", value=value)
                    )
                out.append(value)
                source_pos += 1
                old_seen += 1
                new_seen += 1
            elif op == "-":
                if source_pos >= len(source) or source[source_pos] != value:
                    raise DetailsPatchError(
                        tr("tool_result.task_patch.deletion_mismatch", value=value)
                    )
                source_pos += 1
                old_seen += 1
            elif op == "+":
                out.append(value)
                new_seen += 1
                result_trailing_newline = True
            else:
                raise DetailsPatchError(tr("tool_result.task_patch.invalid_prefix", op=op))
            i += 1

        if old_seen != old_count or new_seen != new_count:
            raise DetailsPatchError(
                tr(
                    "tool_result.task_patch.count_mismatch",
                    old_seen=old_seen,
                    old_count=old_count,
                    new_seen=new_seen,
                    new_count=new_count,
                )
            )

    if not seen_hunk:
        raise DetailsPatchError(tr("tool_result.task_patch.no_hunk"))
    out.extend(source[source_pos:])
    return _join_text_lines(out, result_trailing_newline)


@dataclass
class Task:
    id: str
    description: str
    status: str = "pending"
    details: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def kind(self) -> str:
        return infer_task_kind(self.description)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "kind": self.kind,
            "status": self.status,
            "details": self.details,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any], *, default_timestamp: str | None = None) -> Task:
        created_at = d.get("created_at") or d.get("updated_at") or default_timestamp or _now_iso()
        updated_at = d.get("updated_at") or created_at
        return cls(
            id=d["id"],
            description=d["description"],
            status=d.get("status", "pending"),
            details=d.get("details", ""),
            created_at=created_at,
            updated_at=updated_at,
        )


class TaskStore:
    def __init__(self, store_path: str | Path | None = "data/tasks.json") -> None:
        self._path = Path(store_path) if store_path is not None else None
        self._tasks: dict[str, Task] = {}
        self._load()

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            legacy_timestamp = _timestamp_from_path(self._path)
            needs_save = False
            for t in data.get("tasks", []):
                task = Task.from_dict(t, default_timestamp=legacy_timestamp)
                if "created_at" not in t or "updated_at" not in t:
                    needs_save = True
                self._tasks[task.id] = task
            if needs_save:
                self._save()
        except Exception as e:
            logger.warning(f"Failed to load tasks from {self._path}: {e}")

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"tasks": [t.to_dict() for t in self._tasks.values()]}
            self._path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"Failed to save tasks to {self._path}: {e}")

    def create(self, description: str, details: str = "") -> Task:
        now = _now_iso()
        task = Task(
            id=uuid.uuid4().hex[:8],
            description=description,
            details=details,
            created_at=now,
            updated_at=now,
        )
        self._tasks[task.id] = task
        self._save()
        return task

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def list(self) -> list[Task]:
        return list(self._tasks.values())

    def purge_completed(self) -> int:
        completed_ids = [tid for tid, t in self._tasks.items() if t.status == "completed"]
        for tid in completed_ids:
            del self._tasks[tid]
        if completed_ids:
            self._save()
        return len(completed_ids)

    def update(
        self,
        task_id: str,
        *,
        status: str | None = None,
        description: str | None = None,
        details: str | None = None,
        details_update_mode: str | None = None,
    ) -> Task | None:
        task = self._tasks.get(task_id)
        if task is None:
            return None
        if status == "deleted":
            task.status = "deleted"
            task.updated_at = _now_iso()
            del self._tasks[task_id]
            self._save()
            return task

        new_details = task.details
        if details is not None:
            mode = details_update_mode or "replace"
            if mode == "replace":
                new_details = details
            elif mode == "append":
                new_details = task.details
                if new_details and not new_details.endswith("\n"):
                    new_details += "\n"
                new_details += details
            elif mode == "patch":
                new_details = _apply_unified_diff(task.details, details)
            else:
                raise ValueError(tr("tool_result.task.unknown_details_mode", mode=mode))

        if status is not None:
            task.status = status
        if description is not None:
            task.description = description
        if details is not None:
            task.details = new_details
        if status is not None or description is not None or details is not None:
            task.updated_at = _now_iso()
        self._save()
        return task


class TaskCreateTool(Tool):
    def __init__(self, store: TaskStore) -> None:
        self._store = store

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="task_create",
            description="创建新任务。返回任务 ID。创建后状态为 pending。",
            parameters={
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": (
                            "任务内容。若任务来自某个用户，请在开头注明来源，"
                            "如「[alice] 整理本周报告」，避免多用户任务混淆。"
                        ),
                    },
                    "details": {
                        "type": "string",
                        "description": "可选，任务相关信息，建议使用 Markdown。",
                    },
                },
                "required": ["description"],
            },
        )

    def fork(self, scope) -> TaskCreateTool:
        return TaskCreateTool(scope.task_store)

    async def execute(self, description: str, details: str = "", **_) -> ToolResult:
        task = self._store.create(description, details=details)
        details_suffix = "has_details=true" if task.details.strip() else ""
        return ToolResult(
            tool_call_id="",
            content=(
                f"[{task.id}]({details_suffix}) [{task.status}] [{format_task_times(task)}] {task.description} "
            ),
        )


class TaskGetTool(Tool):
    def __init__(self, store: TaskStore) -> None:
        self._store = store

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="task_get",
            description="按 ID 获取任务的完整信息。",
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "任务 ID"},
                },
                "required": ["task_id"],
            },
        )

    def fork(self, scope) -> TaskGetTool:
        return TaskGetTool(scope.task_store)

    async def execute(self, task_id: str, **_) -> ToolResult:
        task = self._store.get(task_id)
        if task is None:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.task.missing", id=task_id),
                is_error=True,
            )
        return ToolResult(
            tool_call_id="",
            content=(
                f"id: {task.id}\n"
                f"status: {task.status}\n"
                f"time: {format_task_times(task)}\n"
                f"created_at: {task.created_at}\n"
                f"updated_at: {task.updated_at}\n"
                f"description: {task.description}\n"
                f"has_details: {str(bool(task.details.strip())).lower()}\n"
                f"details:\n{task.details}"
            ),
        )


class TaskListTool(Tool):
    def __init__(self, store: TaskStore) -> None:
        self._store = store

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="task_list",
            description="列出所有任务（不含已删除）。使用 task_get 获取单个任务完整信息。",
            parameters={"type": "object", "properties": {}, "required": []},
        )

    def fork(self, scope) -> TaskListTool:
        return TaskListTool(scope.task_store)

    async def execute(self, **_) -> ToolResult:
        tasks = self._store.list()
        if not tasks:
            return ToolResult(tool_call_id="", content=tr("tool_result.task.empty"))
        lines = [tr("tool_result.task.list_title", count=len(tasks))]
        for t in tasks:
            suffix = " has_details=true" if t.details.strip() else ""
            lines.append(
                f"- [{t.id}] [{t.status}] [{format_task_times(t)}] {t.description}{suffix} "
            )
        return ToolResult(tool_call_id="", content="\n".join(lines))


class TaskUpdateTool(Tool):
    def __init__(self, store: TaskStore) -> None:
        self._store = store

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="task_update",
            description=(
                "更新任务。只需传入要修改的字段。\n"
                "状态工作流：pending → in_progress → completed；deleted 表示永久删除该任务。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "要更新的任务 ID"},
                    "status": {
                        "type": "string",
                        "enum": list(_TASK_STATUSES),
                        "description": "新状态",
                    },
                    "description": {"type": "string", "description": "新的任务描述"},
                    "details": {
                        "type": "string",
                        "description": (
                            "任务恢复上下文更新内容。replace=完整新 Markdown；append=追加内容；"
                            "patch=单文档 unified diff。"
                        ),
                    },
                    "details_update_mode": {
                        "type": "string",
                        "enum": list(_DETAILS_UPDATE_MODES),
                        "description": (
                            "details 更新模式：replace（替换）、append（追加）、"
                            "patch（details 为 unified diff）。"
                            "传 details 但不传该字段时默认 replace。"
                        ),
                    },
                },
                "required": ["task_id"],
            },
        )

    def fork(self, scope) -> TaskUpdateTool:
        return TaskUpdateTool(scope.task_store)

    async def execute(
        self,
        task_id: str,
        status: str | None = None,
        description: str | None = None,
        details: str | None = None,
        details_update_mode: str | None = None,
        **_,
    ) -> ToolResult:
        try:
            task = self._store.update(
                task_id,
                status=status,
                description=description,
                details=details,
                details_update_mode=details_update_mode,
            )
        except (ValueError, DetailsPatchError) as e:
            return ToolResult(tool_call_id="", content=str(e), is_error=True)
        if task is None:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.task.missing", id=task_id),
                is_error=True,
            )
        suffix = " has_details=true" if task.details.strip() else ""
        return ToolResult(
            tool_call_id="",
            content=(
                f"[{task.id}] [{task.status}] [{format_task_times(task)}] {task.description}{suffix} "
            ),
        )
