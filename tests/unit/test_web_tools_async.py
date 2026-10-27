"""Contracts for the inline/async split of the web tools.

Search and fetch may take tens of seconds.  The main loop only waits one second
for them: a result that is ready in time is returned as the tool result, and a
slower one keeps running in the background and is delivered as an inbound
asynchronous message instead of parking the whole cycle.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from coworker.agent.incoming_content import format_event_text
from coworker.agent.loop import AgentLoop
from coworker.core.types import IncomingEvent, ToolCall
from coworker.i18n import locale_context
from coworker.tools import web_tools
from coworker.tools.registry import ToolRegistry
from coworker.tools.web_tools import FetchURLTool, SearchWebTool
from coworker.web_search import SearchError


class RecordingInbox:
    """Minimal InboxWatcher stand-in that records pushed events."""

    def __init__(self) -> None:
        self.events: list[IncomingEvent] = []
        self.pushed = asyncio.Event()

    async def push(self, event: IncomingEvent, *, wake: bool = True) -> str:
        self.events.append(event)
        self.pushed.set()
        return "event-id"

    async def next_event(self, timeout: float = 3.0) -> IncomingEvent:
        await asyncio.wait_for(self.pushed.wait(), timeout=timeout)
        event = self.events[-1]
        self.pushed.clear()
        return event


@pytest.fixture
def short_inline_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_tools, "_WEB_TOOL_INLINE_WAIT", 0.05)


async def test_search_returns_inline_without_async_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_search(*args: object, **kwargs: object) -> str:
        return "quick result"

    monkeypatch.setattr(web_tools, "run_search", fake_search)
    inbox = RecordingInbox()

    result = await SearchWebTool(None, inbox=inbox).execute(query="fast query")

    assert result.content == "quick result"
    assert result.is_error is False
    assert inbox.events == []


async def test_slow_search_defers_and_delivers_later(
    monkeypatch: pytest.MonkeyPatch, short_inline_wait: None
) -> None:
    async def fake_search(*args: object, **kwargs: object) -> str:
        await asyncio.sleep(0.3)
        return "slow result"

    monkeypatch.setattr(web_tools, "run_search", fake_search)
    inbox = RecordingInbox()

    started = time.perf_counter()
    result = await SearchWebTool(None, inbox=inbox).execute(query="slow query")
    elapsed = time.perf_counter() - started

    assert elapsed < 0.25
    assert result.is_error is False
    assert "slow query" in result.content
    assert "后台" in result.content
    assert inbox.events == []

    event = await inbox.next_event()
    assert event.source == "web_async"
    assert event.participant_id == "search_web"
    assert "slow result" in event.content
    assert "slow query" in event.content
    # 自描述来源必须原样透传，不能再套一层「来自 X 的消息」外壳。
    assert format_event_text(event) == event.content


async def test_slow_search_failure_is_delivered_as_async_result(
    monkeypatch: pytest.MonkeyPatch, short_inline_wait: None
) -> None:
    async def fake_search(*args: object, **kwargs: object) -> str:
        await asyncio.sleep(0.3)
        raise SearchError("api key rejected")

    monkeypatch.setattr(web_tools, "run_search", fake_search)
    inbox = RecordingInbox()

    result = await SearchWebTool(None, inbox=inbox).execute(query="failing query")

    assert result.is_error is False
    assert "后台" in result.content

    event = await inbox.next_event()
    assert event.source == "web_async"
    assert "api key rejected" in event.content
    assert "failing query" in event.content


async def test_fetch_runs_off_the_event_loop_and_defers(
    monkeypatch: pytest.MonkeyPatch, short_inline_wait: None
) -> None:
    class SlowDDGS:
        def extract(self, url: str, fmt: str) -> dict[str, str]:
            time.sleep(0.3)
            return {"content": "page body"}

    monkeypatch.setattr("ddgs.DDGS", SlowDDGS)
    inbox = RecordingInbox()

    started = time.perf_counter()
    result = await FetchURLTool(inbox=inbox).execute(url="https://example.com/page")
    elapsed = time.perf_counter() - started

    assert elapsed < 0.25
    assert result.is_error is False
    assert "https://example.com/page" in result.content
    assert inbox.events == []

    event = await inbox.next_event()
    assert event.source == "web_async"
    assert event.participant_id == "fetch_url"
    assert "page body" in event.content


async def test_main_loop_act_records_placeholder_and_keeps_going(
    monkeypatch: pytest.MonkeyPatch, short_inline_wait: None
) -> None:
    """The main line stores a pending tool result instead of awaiting the search."""

    async def fake_search(*args: object, **kwargs: object) -> str:
        await asyncio.sleep(0.3)
        return "late result"

    monkeypatch.setattr(web_tools, "run_search", fake_search)
    inbox = RecordingInbox()
    registry = ToolRegistry()
    registry.register(SearchWebTool(None, inbox=inbox))
    loop = AgentLoop.__new__(AgentLoop)
    loop._short_term = SimpleNamespace(primary=[])
    loop.state = SimpleNamespace(tool_call_counts={})
    loop._ilog = None
    loop._tools = registry

    started = time.perf_counter()
    await loop._act([ToolCall(id="call_1", name="search_web", arguments={"query": "q"})])
    elapsed = time.perf_counter() - started

    assert elapsed < 0.25
    assert len(loop._short_term.primary) == 1
    message = loop._short_term.primary[0]
    assert message.role == "tool"
    assert message.tool_call_id == "call_1"
    assert "后台" in message.content

    event = await inbox.next_event()
    assert "late result" in event.content


async def test_without_inbox_the_tool_still_returns_the_real_result(
    monkeypatch: pytest.MonkeyPatch, short_inline_wait: None
) -> None:
    """Bubbles and ad-hoc registries have no inbound sink; keep them blocking."""

    async def fake_search(*args: object, **kwargs: object) -> str:
        await asyncio.sleep(0.1)
        return "inline result"

    monkeypatch.setattr(web_tools, "run_search", fake_search)

    result = await SearchWebTool().execute(query="bubble query")

    assert result.content == "inline result"


@pytest.mark.parametrize(
    ("locale", "marker"),
    [("zh-CN", "后台运行中"), ("en", "running in background")],
)
async def test_pending_message_follows_locale(
    monkeypatch: pytest.MonkeyPatch,
    short_inline_wait: None,
    locale: str,
    marker: str,
) -> None:
    async def fake_search(*args: object, **kwargs: object) -> str:
        await asyncio.sleep(0.3)
        return "done"

    monkeypatch.setattr(web_tools, "run_search", fake_search)
    inbox = RecordingInbox()

    with locale_context(locale):
        result = await SearchWebTool(None, inbox=inbox).execute(query="q")

    assert marker in result.content
    await inbox.next_event()
