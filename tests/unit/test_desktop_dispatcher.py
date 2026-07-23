from __future__ import annotations

import os
import re
import time
from pathlib import Path

from coworker.channels.desktop import DesktopDispatcher, DesktopRegistry
from coworker.channels.desktop.dispatcher import DesktopEnvelope
from coworker.memory.short_term import ShortTermMemory

_PARTICIPANT = "cw-desktop:desk:claude:cw:p"


def _detail_path_from(content: str) -> str:
    match = re.search(r"见文件：(.*?)，可用 read_file", content)
    assert match, f"no detail-file pointer in content: {content!r}"
    return match.group(1)


def _envelope(
    event_type: str,
    payload: dict | None = None,
    *,
    conversation_id: str | None = None,
    request_id: str | None = None,
    protocol_version: int = 1,
) -> DesktopEnvelope:
    data: dict = {
        "protocol_version": protocol_version,
        "message_id": "msg-1",
        "created_at": "2026-07-15T00:00:00Z",
        "type": event_type,
        "payload": payload or {},
    }
    if request_id is not None:
        data["request_id"] = request_id
    if conversation_id is not None:
        data["conversation_id"] = conversation_id
    return DesktopEnvelope.model_validate(data)


def _dispatcher(tmp_path) -> DesktopDispatcher:
    return DesktopDispatcher(DesktopRegistry(ShortTermMemory(), tmp_path))


def test_snapshot_is_consumed_and_feeds_registry(tmp_path):
    dispatcher = _dispatcher(tmp_path)
    payload = {"desktop_id": "desk-a", "actor_id": "claude", "display_name": "Desk A"}

    assert dispatcher.route(_envelope("desktop.actor.snapshot", payload), _PARTICIPANT) is None
    assert "desk-a:claude" in dispatcher._registry.actors


def test_command_result_ok_is_suppressed(tmp_path):
    dispatcher = _dispatcher(tmp_path)
    assert (
        dispatcher.route(
            _envelope("desktop.command.result", {"request_id": "r-1", "ok": True}), _PARTICIPANT
        )
        is None
    )


def test_command_result_failure_is_rendered_and_wakes_agent(tmp_path):
    dispatcher = _dispatcher(tmp_path)
    result = dispatcher.route(
        _envelope("desktop.command.result", {"request_id": "r-1", "ok": False}), _PARTICIPANT
    )
    assert result is not None
    assert "错误" in result


def test_server_request_resolved_is_suppressed(tmp_path):
    dispatcher = _dispatcher(tmp_path)
    assert (
        dispatcher.route(
            _envelope("desktop.server_request.resolved", {"server_request_id": "0", "params": {}}),
            _PARTICIPANT,
        )
        is None
    )


def test_error_is_rendered_and_wakes_agent(tmp_path):
    dispatcher = _dispatcher(tmp_path)
    result = dispatcher.route(_envelope("desktop.error", {"message": "boom"}), _PARTICIPANT)
    assert result is not None
    assert "错误" in result
    assert "boom" in result


def test_codex_approval_renders_server_request_id_template(tmp_path):
    dispatcher = _dispatcher(tmp_path)
    payload = {
        "codex_id": "codex-local",
        "server_request_id": "srv-123",
        "method": "commandExecution",
        "params": {"command": "git push"},
        "status": "pending",
    }
    result = dispatcher.route(
        _envelope("desktop.approval.requested", payload, conversation_id="thread-1"),
        _PARTICIPANT,
    )
    assert result is not None
    assert "审批请求" in result
    assert "Codex" in result
    assert "server_request_id" in result
    assert "srv-123" in result
    assert "decision" in result
    assert "communicate(" in result
    assert "thread-1" in result


def test_claude_approval_renders_request_id_template(tmp_path):
    dispatcher = _dispatcher(tmp_path)
    payload = {
        "actor_id": "claude",
        "request_id": "req-abc",
        "session_id": "session-1",
        "tool_name": "Bash",
        "input": {"command": "rm -rf /"},
    }
    result = dispatcher.route(
        _envelope("desktop.approval.requested", payload, conversation_id="session-1"),
        _PARTICIPANT,
    )
    assert result is not None
    assert "Claude" in result
    assert "request_id" in result
    assert "req-abc" in result
    assert "decision" in result


def test_claude_askuserquestion_renders_questions_and_answers_template(tmp_path):
    dispatcher = _dispatcher(tmp_path)
    payload = {
        "actor_id": "claude",
        "request_id": "q-1",
        "session_id": "session-1",
        "tool_name": "AskUserQuestion",
        "input": {
            "questions": [
                {
                    "question": "Which database should we use?",
                    "options": [{"label": "SQLite", "description": "local file"}],
                }
            ]
        },
    }
    result = dispatcher.route(
        _envelope("desktop.user_input.requested", payload, conversation_id="session-1"),
        _PARTICIPANT,
    )
    assert result is not None
    assert "提问请求" in result
    assert "Which database should we use?" in result
    assert "SQLite" in result
    assert "user_input_request_id" in result
    assert "q-1" in result
    assert "answers" in result


def test_codex_user_input_without_questions_renders_decision_template(tmp_path):
    # Non-AskUserQuestion input request (e.g. Codex requestUserInput): routed
    # through the approval-style decision template.
    dispatcher = _dispatcher(tmp_path)
    payload = {
        "codex_id": "codex-local",
        "server_request_id": "srv-in-1",
        "method": "requestUserInput",
        "params": {"prompt": "enter a value"},
    }
    result = dispatcher.route(
        _envelope("desktop.user_input.requested", payload, conversation_id="thread-1"),
        _PARTICIPANT,
    )
    assert result is not None
    assert "server_request_id" in result
    assert "srv-in-1" in result
    assert "decision" in result


def test_thread_event_returns_message_text(tmp_path):
    dispatcher = _dispatcher(tmp_path)
    result = dispatcher.route(
        _envelope("desktop.thread.event", {"message": "hello there"}), _PARTICIPANT
    )
    assert result == "hello there"


def test_unknown_desktop_type_renders_summary(tmp_path):
    dispatcher = _dispatcher(tmp_path)
    result = dispatcher.route(
        _envelope("desktop.something.new", {"foo": "bar"}), _PARTICIPANT
    )
    assert result is not None
    assert "desktop.something.new" in result


def test_unsupported_protocol_version_is_consumed(tmp_path):
    dispatcher = _dispatcher(tmp_path)
    assert (
        dispatcher.route(
            _envelope(
                "desktop.command.result",
                {"ok": True},
                protocol_version=99,
            ),
            _PARTICIPANT,
        )
        is None
    )


def test_short_error_is_not_folded(tmp_path):
    dispatcher = _dispatcher(tmp_path)
    result = dispatcher.route(
        _envelope("desktop.error", {"message": "boom"}, request_id="err-2"), _PARTICIPANT
    )
    assert result == "[CoWorker Desktop 错误]\n内容：boom"
    assert "read_file" not in result


def test_long_error_is_folded_with_read_file_pointer(tmp_path):
    dispatcher = _dispatcher(tmp_path)
    long_message = "boom-" * 200  # ~1000 chars, well past the fold threshold
    result = dispatcher.route(
        _envelope("desktop.error", {"message": long_message}, request_id="err-1"), _PARTICIPANT
    )
    assert result is not None
    assert "[CoWorker Desktop 错误]" in result
    assert "read_file" in result
    # inline keeps a prefix of the message but not the whole thing
    assert "boom-" in result
    assert long_message not in result

    path = _detail_path_from(result)
    full = Path(path).read_text(encoding="utf-8")
    assert "[CoWorker Desktop 错误]" in full
    assert long_message in full
    assert len(result) < len(full)


def test_long_askuserquestion_folds_descriptions_to_detail_file(tmp_path):
    dispatcher = _dispatcher(tmp_path)
    long_desc = "选项说明-" * 60  # ~300 chars per option
    questions = [
        {
            "question": f"第 {i + 1} 题：选什么？",
            "options": [
                {"label": f"选项A{i}", "description": long_desc},
                {"label": f"选项B{i}", "description": long_desc},
            ],
        }
        for i in range(4)
    ]
    payload = {
        "actor_id": "claude",
        "request_id": "q-long",
        "session_id": "session-1",
        "tool_name": "AskUserQuestion",
        "input": {"questions": questions},
    }
    result = dispatcher.route(
        _envelope("desktop.user_input.requested", payload, conversation_id="session-1"),
        _PARTICIPANT,
    )
    assert result is not None
    # folded: pointer present
    assert "read_file" in result
    # questions, labels and answers template stay inline so the coworker can answer
    assert "第 1 题" in result
    assert "选项A0" in result
    assert "answers" in result
    assert "user_input_request_id" in result
    # verbose descriptions are NOT inline...
    assert long_desc not in result
    # ...only in the detail file
    path = _detail_path_from(result)
    full = Path(path).read_text(encoding="utf-8")
    assert long_desc in full
    assert "第 1 题" in full
    assert len(result) < len(full)


def test_long_thread_event_message_is_folded(tmp_path):
    dispatcher = _dispatcher(tmp_path)
    long_message = "hello-" * 200
    result = dispatcher.route(
        _envelope("desktop.thread.event", {"message": long_message}, request_id="t-1"),
        _PARTICIPANT,
    )
    assert result is not None
    assert "read_file" in result
    assert long_message not in result
    assert "hello-" in result  # prefix survives
    path = _detail_path_from(result)
    assert Path(path).read_text(encoding="utf-8") == long_message


def test_detail_files_are_pruned_by_age(tmp_path):
    dispatcher = _dispatcher(tmp_path)
    registry = dispatcher._registry
    fresh = registry.write_detail("fresh", "fresh content")
    stale1 = registry.write_detail("stale1", "stale1 content")
    stale2 = registry.write_detail("stale2", "stale2 content")
    old = time.time() - (8 * 24 * 3600)  # past the 7-day retention window
    os.utime(stale1, (old, old))
    os.utime(stale2, (old, old))

    registry._prune_details()

    assert fresh.exists()
    assert not stale1.exists()
    assert not stale2.exists()


def test_detail_files_are_pruned_by_count(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "coworker.channels.desktop.detail_store._DETAIL_MAX_FILES", 2
    )
    dispatcher = _dispatcher(tmp_path)
    registry = dispatcher._registry
    paths = [registry.write_detail(f"k{i}", f"content {i}") for i in range(4)]

    existing = [path for path in paths if path.exists()]
    assert len(existing) == 2
    # the two newest survive; the oldest are dropped
    assert paths[-1].exists()
    assert paths[-2].exists()
    assert not paths[0].exists()
