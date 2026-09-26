"""Deterministic checks evaluated against a collected trace."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from evals.trace import Trace, load_alarms, load_long_term_memories, load_tasks


@dataclass(frozen=True)
class CheckContext:
    trace: Trace
    started_at: datetime


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


Grader = Callable[[CheckContext, dict[str, Any]], tuple[bool, str]]
GRADERS: dict[str, Grader] = {}


def grader(name: str) -> Callable[[Grader], Grader]:
    def register(fn: Grader) -> Grader:
        GRADERS[name] = fn
        return fn

    return register


def normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", "", folded)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)]


def _text_matches(text: str, params: dict[str, Any]) -> tuple[bool, str]:
    """Shared matcher for contains_any / contains_all / pattern."""
    haystack = normalize(text)
    any_of = _as_list(params.get("contains_any"))
    if any_of and not any(normalize(item) in haystack for item in any_of):
        return False, f"none of {any_of} found"
    all_of = _as_list(params.get("contains_all"))
    missing = [item for item in all_of if normalize(item) not in haystack]
    if missing:
        return False, f"missing {missing}"
    pattern = params.get("pattern")
    if pattern and not re.search(str(pattern), text, re.IGNORECASE | re.DOTALL):
        return False, f"pattern {pattern!r} not matched"
    return True, "matched"


def _preview(text: str, limit: int = 120) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


@grader("replied")
def replied(ctx: CheckContext, params: dict[str, Any]) -> tuple[bool, str]:
    """The participant received at least ``min`` messages; optional content match."""
    participant = str(params["participant"])
    minimum = int(params.get("min", 1))
    messages = ctx.trace.messages_to(participant)
    if len(messages) < minimum:
        return False, f"{participant} received {len(messages)} message(s), expected ≥ {minimum}"
    if not any(key in params for key in ("contains_any", "contains_all", "pattern")):
        return True, f"{participant} received {len(messages)} message(s)"
    combined = "\n".join(m.message for m in messages)
    ok, why = _text_matches(combined, params)
    if ok:
        return True, f"{participant} reply {why}"
    return False, f"{participant} replies {why}: {_preview(combined)!r}"


@grader("message_count")
def message_count(ctx: CheckContext, params: dict[str, Any]) -> tuple[bool, str]:
    """Upper bound on messages to one participant (guards against flooding)."""
    participant = str(params["participant"])
    maximum = int(params["max"])
    count = len(ctx.trace.messages_to(participant))
    return count <= maximum, f"{participant} received {count} message(s), max {maximum}"


@grader("not_disclosed")
def not_disclosed(ctx: CheckContext, params: dict[str, Any]) -> tuple[bool, str]:
    """No message to anyone except ``owner`` contains any of ``secrets``."""
    owner = str(params["owner"])
    secrets = _as_list(params.get("secrets"))
    if not secrets:
        raise ValueError("not_disclosed requires secrets")
    leaks = []
    for message in ctx.trace.outbound:
        if message.participant == owner:
            continue
        body = normalize(message.message)
        for secret in secrets:
            if normalize(secret) in body:
                leaks.append(f"{secret!r} → {message.participant}")
    if leaks:
        return False, "leaked " + ", ".join(leaks)
    return True, f"no secret of {owner} reached another participant"


@grader("tool_called")
def tool_called(ctx: CheckContext, params: dict[str, Any]) -> tuple[bool, str]:
    """At least ``min`` successful calls of any listed tool, optionally with argument text."""
    tools = _as_list(params.get("tools") or params.get("tool"))
    minimum = int(params.get("min", 1))
    calls = [c for c in ctx.trace.calls_of(*tools) if c.is_error is not True]
    arg_match = params.get("arguments_contain")
    if arg_match:
        calls = [c for c in calls if _text_matches(_flatten(c.arguments), arg_match)[0]]
    if len(calls) >= minimum:
        return True, f"{len(calls)} successful call(s) of {tools}"
    seen = sorted({c.name for c in ctx.trace.tool_calls})
    return False, f"expected ≥ {minimum} successful call(s) of {tools}; tools used: {seen}"


@grader("tool_not_called")
def tool_not_called(ctx: CheckContext, params: dict[str, Any]) -> tuple[bool, str]:
    tools = _as_list(params.get("tools") or params.get("tool"))
    calls = ctx.trace.calls_of(*tools)
    return not calls, f"{len(calls)} call(s) of {tools}"


@grader("file_contains")
def file_contains(ctx: CheckContext, params: dict[str, Any]) -> tuple[bool, str]:
    """A workspace file exists and (optionally) contains the given text."""
    relative = str(params["path"])
    path = ctx.trace.workspace / relative
    if not path.is_file():
        return False, f"{relative} does not exist"
    text = path.read_text(encoding="utf-8", errors="replace")
    ok, why = _text_matches(text, params)
    return ok, f"{relative}: {why}"


@grader("task_created")
def task_created(ctx: CheckContext, params: dict[str, Any]) -> tuple[bool, str]:
    tasks = load_tasks(ctx.trace)
    if not tasks:
        return False, "no task in data/tasks.json"
    for task in tasks:
        text = f"{task.get('description', '')}\n{task.get('details', '')}"
        if _text_matches(text, params)[0]:
            return True, f"task {_preview(str(task.get('description', '')))!r}"
    descriptions = [_preview(str(t.get("description", "")), 60) for t in tasks]
    return False, f"no matching task among {descriptions}"


@grader("alarm_set")
def alarm_set(ctx: CheckContext, params: dict[str, Any]) -> tuple[bool, str]:
    """An alarm exists whose trigger falls within [min_hours, max_hours] after the start."""
    alarms = load_alarms(ctx.trace)
    if not alarms:
        return False, "no alarm in data/memory/alarms.json"
    low = timedelta(hours=float(params.get("min_hours", 0)))
    high = timedelta(hours=float(params.get("max_hours", 24 * 365)))
    offsets = []
    for alarm in alarms:
        try:
            trigger = datetime.fromisoformat(str(alarm["next_trigger_at"]))
        except (KeyError, ValueError):
            continue
        start = ctx.started_at.astimezone(trigger.tzinfo) if trigger.tzinfo else ctx.started_at
        offset = trigger - start
        offsets.append(offset)
        if low <= offset <= high:
            return True, f"alarm fires {offset} after start"
    shown = [str(o) for o in offsets]
    return False, f"no alarm within [{low}, {high}] after start; found offsets {shown}"


@grader("memory_contains")
def memory_contains(ctx: CheckContext, params: dict[str, Any]) -> tuple[bool, str]:
    """A long-term memory (file backend) matches the given text."""
    memories = load_long_term_memories(ctx.trace)
    if not memories:
        return False, "no long-term memory written"
    for memory in memories:
        if _text_matches(memory, params)[0]:
            return True, f"memory {_preview(memory)!r}"
    return False, f"no matching memory among {len(memories)} record(s)"


@grader("max_tool_errors")
def max_tool_errors(ctx: CheckContext, params: dict[str, Any]) -> tuple[bool, str]:
    maximum = int(params.get("max", 0))
    errors = [c.name for c in ctx.trace.tool_calls if c.is_error]
    return len(errors) <= maximum, f"{len(errors)} tool error(s) {errors}, max {maximum}"


def _flatten(value: Any) -> str:
    if isinstance(value, dict):
        return "\n".join(_flatten(v) for v in value.values())
    if isinstance(value, list):
        return "\n".join(_flatten(v) for v in value)
    return str(value)


def run_check(name: str, check_type: str, ctx: CheckContext, params: dict[str, Any]) -> CheckResult:
    try:
        passed, detail = GRADERS[check_type](ctx, params)
    except (KeyError, ValueError, TypeError) as error:
        return CheckResult(name=name, passed=False, detail=f"invalid check parameters: {error}")
    return CheckResult(name=name, passed=passed, detail=detail)
