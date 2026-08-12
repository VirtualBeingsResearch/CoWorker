from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from coworker.agent.loop import AgentLoop
from coworker.brain.deepseek_provider import _parse_tool_arguments as deepseek_parse
from coworker.brain.minimax_provider import _parse_tool_arguments as minimax_parse
from coworker.brain.openai_provider import _parse_tool_arguments as openai_parse
from coworker.brain.qwen_provider import _parse_tool_arguments as qwen_parse
from coworker.brain.zhipu_provider import _parse_tool_arguments as zhipu_parse
from coworker.core.types import ToolCall

# --- _parse_tool_arguments ---

@pytest.mark.parametrize(
    "parse_fn",
    [deepseek_parse, minimax_parse, openai_parse, qwen_parse, zhipu_parse],
)
def test_parse_tool_arguments_valid_json(parse_fn):
    result = parse_fn('{"key": "value"}', "my_tool")
    assert result == {"key": "value"}


@pytest.mark.parametrize(
    "parse_fn",
    [deepseek_parse, minimax_parse, openai_parse, qwen_parse, zhipu_parse],
)
def test_parse_tool_arguments_invalid_json_returns_parse_error(parse_fn):
    raw = "not valid json{{{"
    result = parse_fn(raw, "my_tool")
    assert "__parse_error__" in result
    assert isinstance(result["__parse_error__"], str)
    assert result["__raw_arguments__"] == raw


@pytest.mark.parametrize(
    "parse_fn",
    [deepseek_parse, minimax_parse, openai_parse, qwen_parse, zhipu_parse],
)
def test_parse_tool_arguments_empty_string_returns_parse_error(parse_fn):
    result = parse_fn("", "my_tool")
    assert "__parse_error__" in result
    assert result["__raw_arguments__"] == ""


# --- AgentLoop._act() with __parse_error__ ---

def _make_minimal_loop():
    loop = AgentLoop.__new__(AgentLoop)
    loop._short_term = SimpleNamespace(primary=[])
    loop.state = SimpleNamespace(tool_call_counts={})
    loop._ilog = None
    loop._tools = MagicMock()
    loop._tools.execute = AsyncMock()
    loop._environment_runtime = None
    return loop


@pytest.mark.asyncio
async def test_act_injects_error_result_when_parse_error():
    loop = _make_minimal_loop()
    tc = ToolCall(
        id="call_1",
        name="my_tool",
        arguments={
            "__parse_error__": "Expecting value: line 1 column 1 (char 0)",
            "__raw_arguments__": "not valid json{{{",
        },
    )

    await loop._act([tc])

    loop._tools.execute.assert_not_called()
    assert len(loop._short_term.primary) == 1
    msg = loop._short_term.primary[0]
    assert msg.role == "tool"
    assert msg.tool_call_id == "call_1"
    assert "JSON 解析失败" in msg.content
    assert "Expecting value" in msg.content
    assert "原始参数" not in msg.content
    assert "not valid json{{{" not in msg.content


@pytest.mark.asyncio
async def test_act_calls_tool_normally_when_no_parse_error():
    from coworker.core.types import ToolResult

    loop = _make_minimal_loop()
    loop._tools.execute = AsyncMock(
        return_value=ToolResult(tool_call_id="call_2", content="done", is_error=False)
    )
    tc = ToolCall(id="call_2", name="my_tool", arguments={"key": "value"})

    await loop._act([tc])

    loop._tools.execute.assert_awaited_once()
    assert loop._short_term.primary[0].content == "done"
