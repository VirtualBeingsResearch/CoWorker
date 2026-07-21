from __future__ import annotations

import json

import pytest

from coworker.__main__ import _append_recovered_tool_result, _diff_runtime_locale
from coworker.agent.interaction_log import InteractionLogger
from coworker.core.types import Message
from coworker.i18n import locale_context
from coworker.memory.short_term import ShortTermMemory


def _assistant_tool_call(tool_call_id: str, name: str) -> Message:
    return Message(
        role="assistant",
        content="",
        tool_calls=[
            {
                "id": tool_call_id,
                "type": "function",
                "function": {"name": name, "arguments": "{}"},
            }
        ],
        stop_reason="tool_use",
    )


@pytest.mark.parametrize("tool_name", ["restart_self", "sleep"])
def test_recovered_tool_result_is_appended_and_logged(tmp_path, tool_name):
    short_term = ShortTermMemory()
    short_term.primary.append(_assistant_tool_call("tc-1", tool_name))
    log = InteractionLogger(str(tmp_path / "interactions.jsonl"))

    recovered = _append_recovered_tool_result(
        short_term,
        log,
        tool_name=tool_name,
        content="recovered",
    )

    assert recovered is True
    assert short_term.primary[-1].role == "tool"
    assert short_term.primary[-1].tool_call_id == "tc-1"
    assert short_term.primary[-1].content == "recovered"

    entries = [
        json.loads(line)
        for line in (tmp_path / "interactions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert entries == [
        {
            "type": "tool_result",
            "id": "tc-1",
            "name": tool_name,
            "content": "recovered",
            "is_error": False,
            "seq": 0,
            "ts": entries[0]["ts"],
        }
    ]


def test_recovered_tool_result_noops_without_pending_call(tmp_path):
    short_term = ShortTermMemory()
    log = InteractionLogger(str(tmp_path / "interactions.jsonl"))

    recovered = _append_recovered_tool_result(
        short_term,
        log,
        tool_name="sleep",
        content="recovered",
    )

    assert recovered is False
    assert short_term.primary == []
    assert not (tmp_path / "interactions.jsonl").exists()


def test_runtime_locale_change_notice_is_emitted_only_for_an_actual_change():
    assert _diff_runtime_locale({}, {"runtime_locale": "en"}) is None
    assert (
        _diff_runtime_locale(
            {"runtime_locale": "zh-CN"},
            {"runtime_locale": "zh-CN"},
        )
        is None
    )

    with locale_context("en"):
        notice = _diff_runtime_locale(
            {"runtime_locale": "zh-CN"},
            {"runtime_locale": "en"},
        )

    assert notice is not None
    assert "runtime language changed from zh-CN to en" in notice
    assert "existing user content and memories remain" in notice
