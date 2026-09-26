"""Collect a sample's observable behavior from its workspace after the run."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

LOGS_DIR = Path("data/logs")
MEMORY_DIR = Path("data/memory")


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    source: str
    ts: str = ""
    is_error: bool | None = None
    result: str = ""


@dataclass
class OutboundMessage:
    participant: str
    message: str
    source: str
    ts: str = ""


@dataclass
class InboundMessage:
    participant: str
    content: str
    source: str
    ts: str = ""


@dataclass
class Trace:
    workspace: Path
    inbound: list[InboundMessage] = field(default_factory=list)
    outbound: list[OutboundMessage] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    system_prompt_hash: str = ""

    def messages_to(self, participant: str) -> list[OutboundMessage]:
        return [m for m in self.outbound if m.participant == participant]

    def calls_of(self, *names: str) -> list[ToolCall]:
        wanted = set(names)
        return [call for call in self.tool_calls if call.name in wanted]

    def read_json(self, relative: str | Path) -> Any:
        path = self.workspace / relative
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["workspace"] = str(self.workspace)
        return data


def log_files(workspace: Path) -> list[Path]:
    logs = workspace / LOGS_DIR
    files = sorted(logs.glob("interactions*.jsonl"))
    files.extend(sorted((logs / "bubbles").glob("*.jsonl")))
    return files


RESTING_TOOLS = frozenset({"sleep"})


@dataclass(frozen=True)
class Activity:
    llm_calls: int
    messages_in: int
    busy: bool
    main_resting: bool


def activity(workspace: Path) -> Activity:
    """Summarize whether any line of thought is mid-call or mid-tool.

    A log whose last entry is ``thinking_start`` is waiting on the model; a trailing
    ``tool_call`` other than ``sleep`` is still executing. The main line rests when its
    last entry is a pending ``sleep`` call (the idle rest is reported by ``/status``).
    """
    llm_calls = 0
    messages_in = 0
    tails: dict[str, dict[str, Any]] = {}
    for source, entry in _entries(workspace):
        kind = entry.get("type")
        if kind == "llm_response":
            llm_calls += 1
        elif kind == "message_in":
            messages_in += 1
        tails[source] = entry
    busy = False
    for tail in tails.values():
        kind = tail.get("type")
        if kind == "thinking_start":
            busy = True
        elif kind == "tool_call" and tail.get("name") not in RESTING_TOOLS:
            busy = True
    main_tail = tails.get("main", {})
    main_resting = main_tail.get("type") == "tool_call" and main_tail.get("name") in RESTING_TOOLS
    return Activity(
        llm_calls=llm_calls, messages_in=messages_in, busy=busy, main_resting=main_resting
    )


def _entries(workspace: Path) -> list[tuple[str, dict[str, Any]]]:
    entries: list[tuple[str, dict[str, Any]]] = []
    for path in log_files(workspace):
        source = "main" if path.parent.name != "bubbles" else f"bubble:{path.stem}"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                entries.append((source, entry))
    return entries


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) else 0


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def collect(workspace: Path) -> Trace:
    trace = Trace(workspace=workspace)
    pending: dict[tuple[str, str], ToolCall] = {}
    for source, entry in _entries(workspace):
        kind = entry.get("type")
        ts = str(entry.get("ts") or "")
        if kind == "system_prompt" and not trace.system_prompt_hash and source == "main":
            content = str(entry.get("content") or "")
            trace.system_prompt_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        elif kind == "message_in" and source == "main":
            trace.inbound.append(
                InboundMessage(
                    participant=str(entry.get("participant_id") or ""),
                    content=str(entry.get("content") or ""),
                    source=str(entry.get("source") or ""),
                    ts=ts,
                )
            )
        elif kind == "llm_response":
            trace.llm_calls += 1
            usage = _as_dict(entry.get("usage"))
            trace.input_tokens += _as_int(usage.get("input_tokens"))
            trace.output_tokens += _as_int(usage.get("output_tokens"))
        elif kind == "tool_call":
            arguments = _as_dict(entry.get("arguments"))
            call = ToolCall(
                name=str(entry.get("name") or ""),
                arguments=arguments,
                source=source,
                ts=ts,
            )
            trace.tool_calls.append(call)
            pending[(source, str(entry.get("id") or ""))] = call
        elif kind == "tool_result":
            finished = pending.pop((source, str(entry.get("id") or "")), None)
            if finished is not None:
                finished.is_error = bool(entry.get("is_error"))
                finished.result = str(entry.get("content") or "")[:2000]

    for call in trace.tool_calls:
        if call.name != "communicate" or call.is_error:
            continue
        participant = str(call.arguments.get("participant_id") or "").strip()
        message = str(call.arguments.get("message") or "")
        if participant and message:
            trace.outbound.append(
                OutboundMessage(
                    participant=participant, message=message, source=call.source, ts=call.ts
                )
            )
    return trace


def load_tasks(trace: Trace) -> list[dict[str, Any]]:
    data = trace.read_json("data/tasks.json")
    tasks = data.get("tasks") if isinstance(data, dict) else None
    return [t for t in tasks or [] if isinstance(t, dict)]


def load_alarms(trace: Trace) -> list[dict[str, Any]]:
    data = trace.read_json(MEMORY_DIR / "alarms.json")
    return [a for a in data or [] if isinstance(a, dict)] if isinstance(data, list) else []


def load_long_term_memories(trace: Trace) -> list[str]:
    directory = trace.workspace / MEMORY_DIR / "long_term"
    memories: list[str] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("content"):
            memories.append(str(data["content"]))
    return memories
