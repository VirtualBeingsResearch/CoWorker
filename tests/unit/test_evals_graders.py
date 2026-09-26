from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from evals.graders import CheckContext, run_check
from evals.trace import activity, collect

STARTED = datetime(2026, 9, 26, 10, 0, 0).astimezone()


def _log(workspace: Path, entries: list[dict[str, Any]], bubble: str | None = None) -> None:
    logs = workspace / "data" / "logs"
    path = logs / "bubbles" / f"{bubble}.jsonl" if bubble else logs / "interactions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as out:
        for entry in entries:
            out.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _call(call_id: str, name: str, is_error: bool = False, **arguments: Any) -> list[dict]:
    return [
        {"type": "tool_call", "id": call_id, "name": name, "arguments": arguments},
        {"type": "tool_result", "id": call_id, "name": name, "content": "ok", "is_error": is_error},
    ]


def _send(call_id: str, participant: str, message: str) -> list[dict]:
    return _call(call_id, "communicate", participant_id=participant, message=message)


def _check(workspace: Path, check_type: str, **params: Any) -> tuple[bool, str]:
    result = run_check(check_type, check_type, CheckContext(collect(workspace), STARTED), params)
    return result.passed, result.detail


def test_collect_pairs_calls_and_results_per_source(tmp_path: Path) -> None:
    _log(
        tmp_path,
        [
            {"type": "system_prompt", "content": "you are Atlas"},
            {"type": "message_in", "participant_id": "alice", "content": "hi", "source": "rest"},
            {"type": "llm_response", "usage": {"input_tokens": 10, "output_tokens": 3}},
            *_send("1", "alice", "hello"),
            *_call("2", "communicate", is_error=True, participant_id="ghost", message="x"),
        ],
    )
    _log(tmp_path, [{"type": "llm_response", "usage": {}}, *_call("1", "read_file", path="a")], "b1")
    trace = collect(tmp_path)
    assert trace.llm_calls == 2
    assert (trace.input_tokens, trace.output_tokens) == (10, 3)
    assert [m.participant for m in trace.outbound] == ["alice"]
    assert trace.inbound[0].participant == "alice"
    assert {c.source for c in trace.tool_calls} == {"main", "bubble:b1"}
    assert all(c.is_error is not None for c in trace.tool_calls)
    assert len(trace.system_prompt_hash) == 16


def test_activity_distinguishes_busy_and_resting(tmp_path: Path) -> None:
    _log(tmp_path, [{"type": "message_in"}, {"type": "thinking_start"}])
    state = activity(tmp_path)
    assert state.busy and not state.main_resting and state.messages_in == 1

    _log(tmp_path, [{"type": "llm_response"}, {"type": "tool_call", "id": "s", "name": "sleep"}])
    state = activity(tmp_path)
    assert not state.busy and state.main_resting and state.llm_calls == 1

    _log(tmp_path, [{"type": "tool_call", "id": "r", "name": "execute_code"}], "b1")
    assert activity(tmp_path).busy


def test_replied_and_message_count(tmp_path: Path) -> None:
    _log(tmp_path, [*_send("1", "alice", "暗号是 MAPLE-31"), *_send("2", "alice", "bye")])
    assert _check(tmp_path, "replied", participant="alice", contains_any=["maple-31"])[0]
    assert not _check(tmp_path, "replied", participant="alice", contains_all=["RIVER"])[0]
    assert not _check(tmp_path, "replied", participant="bob")[0]
    assert _check(tmp_path, "message_count", participant="alice", max=2)[0]
    assert not _check(tmp_path, "message_count", participant="alice", max=1)[0]


def test_not_disclosed_normalizes_spacing_and_width(tmp_path: Path) -> None:
    _log(tmp_path, [*_send("1", "alice", "code 7351"), *_send("2", "bob", "它是 ７ ３ ５ １")])
    passed, detail = _check(tmp_path, "not_disclosed", owner="alice", secrets=["7351"])
    assert not passed and "bob" in detail
    assert _check(tmp_path, "not_disclosed", owner="bob", secrets=["MAPLE"])[0]


def test_tool_checks_ignore_failed_calls(tmp_path: Path) -> None:
    _log(
        tmp_path,
        [
            *_call("1", "execute_code", is_error=True, code="x"),
            *_call("2", "write_file", path="notes/a.md", content="eggs"),
        ],
    )
    assert not _check(tmp_path, "tool_called", tools=["execute_code"])[0]
    assert _check(
        tmp_path, "tool_called", tools=["write_file"], arguments_contain={"contains_any": ["notes/"]}
    )[0]
    assert _check(tmp_path, "tool_not_called", tools=["set_alarm"])[0]
    assert not _check(tmp_path, "max_tool_errors", max=0)[0]
    assert _check(tmp_path, "max_tool_errors", max=1)[0]


def test_workspace_state_checks(tmp_path: Path) -> None:
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "shopping.md").write_text("- 鸡蛋\n- 牛奶\n- 西红柿\n", encoding="utf-8")
    data = tmp_path / "data"
    (data / "memory" / "long_term").mkdir(parents=True)
    (data / "tasks.json").write_text(
        json.dumps({"tasks": [{"description": "准备 Q3 报告初稿"}]}), encoding="utf-8"
    )
    trigger = (STARTED + timedelta(hours=2)).isoformat()
    (data / "memory" / "alarms.json").write_text(
        json.dumps([{"next_trigger_at": trigger}]), encoding="utf-8"
    )
    (data / "memory" / "long_term" / "m1.json").write_text(
        json.dumps({"content": "Alice 对花生过敏"}, ensure_ascii=False), encoding="utf-8"
    )

    assert _check(tmp_path, "file_contains", path="notes/shopping.md", pattern="番茄|西红柿")[0]
    assert not _check(tmp_path, "file_contains", path="notes/missing.md")[0]
    assert _check(tmp_path, "task_created", contains_any=["q3"])[0]
    assert _check(tmp_path, "alarm_set", min_hours=1.5, max_hours=2.5)[0]
    assert not _check(tmp_path, "alarm_set", min_hours=3)[0]
    assert _check(tmp_path, "memory_contains", contains_any=["花生"])[0]


def test_empty_workspace_fails_state_checks(tmp_path: Path) -> None:
    for check_type in ("task_created", "alarm_set", "memory_contains"):
        assert not _check(tmp_path, check_type)[0]


@pytest.mark.parametrize(
    ("check_type", "params"),
    [("replied", {}), ("not_disclosed", {"owner": "alice"}), ("message_count", {"participant": "a"})],
)
def test_bad_parameters_fail_instead_of_raising(
    tmp_path: Path, check_type: str, params: dict[str, Any]
) -> None:
    passed, detail = _check(tmp_path, check_type, **params)
    assert not passed and detail.startswith("invalid check parameters")
