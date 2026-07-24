from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coworker.agent.bubble import Bubble, BubbleStore
from coworker.agent.bubble_communication import BubbleCommunicateTool
from coworker.agent.bubble_handoff import BubbleHandoffMatcher, BubbleHandoffNotifier
from coworker.agent.bubble_loop import BubbleMiniLoop, _build_merge_message
from coworker.agent.usage_stats import UsageStatsCollector
from coworker.channels.base import BaseChannel, ChannelCapabilities
from coworker.channels.system import create_channel_system
from coworker.core.types import (
    AttachmentData,
    IncomingEvent,
    LLMResponse,
    Message,
    ToolCall,
    ToolResult,
)
from coworker.i18n import locale_context
from coworker.tools.bubble_tools import (
    BubbleCancelTool,
    BubbleCheckTool,
    BubbleListTool,
    BubbleSendTool,
    BubbleSpawnTool,
)
from coworker.tools.file_tools import WriteFileTool

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store():
    return BubbleStore(max_concurrent=3)


@pytest.fixture
def messages():
    return [Message(role="user", content="初始消息")]


@pytest.fixture
def mock_brain():
    provider = MagicMock()
    provider.provider_name = "mock"
    provider.default_model = "mock-model"
    provider.supports_tool_use.return_value = True
    provider.can_use_tools.side_effect = provider.supports_tool_use
    provider.supports_vision.return_value = False
    provider.complete = AsyncMock(return_value=_make_response(content="done"))
    brain = MagicMock()
    brain.current_model_has_vision = False
    brain.current_provider_name = "mock"
    brain.current_model = "mock-model"
    brain._providers = {"mock": provider}
    return brain


@pytest.fixture
def mock_inbox():
    inbox = MagicMock()
    inbox.push = AsyncMock()
    return inbox


@pytest.fixture
def mock_registry():
    registry = MagicMock()
    registry.get_schemas.return_value = []
    registry.execute = AsyncMock(return_value=MagicMock(content="tool result", is_error=False))
    registry.scoped.return_value = registry  # scoped() returns itself in tests
    registry.intercept.return_value = registry  # intercept() returns itself in tests
    return registry


@pytest.fixture
def mock_short_term(messages):
    st = MagicMock()
    st.primary = list(messages)
    return st


@pytest.fixture
def mock_prompt_builder():
    pb = MagicMock()
    pb.build.return_value = "system prompt"
    return pb


@pytest.fixture(autouse=True)
async def cancel_tasks():
    yield
    current = asyncio.current_task()
    tasks = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# BubbleStore
# ---------------------------------------------------------------------------


class TestBubbleStore:
    def test_create_returns_bubble(self, store, messages):
        result = store.create("goal", messages, max_cycles=5)
        assert isinstance(result, Bubble)
        assert result.id.startswith("bbl_")
        assert result.goal == "goal"
        assert result.status == "running"
        assert len(result.forked_context) == 1

    def test_create_copies_context(self, store, messages):
        b = store.create("goal", messages, max_cycles=5)
        assert isinstance(b, Bubble)
        messages.append(Message(role="user", content="extra"))
        assert len(b.forked_context) == 1  # snapshot, not reference

    def test_create_respects_max_concurrent(self, messages):
        store = BubbleStore(max_concurrent=2)
        b1 = store.create("g1", messages, 5)
        b2 = store.create("g2", messages, 5)
        b3 = store.create("g3", messages, 5)
        assert isinstance(b1, Bubble)
        assert isinstance(b2, Bubble)
        assert isinstance(b3, str)
        assert "已达到最大并发泡泡数" in b3

    def test_get_active_bubble(self, store, messages):
        b = store.create("goal", messages, 5)
        assert isinstance(b, Bubble)
        found = store.get(b.id)
        assert found is b

    def test_get_history_bubble(self, store, messages):
        b = store.create("goal", messages, 5)
        assert isinstance(b, Bubble)
        store.mark_done(b)
        assert store.get(b.id) is b
        assert len(store.list_active()) == 0

    def test_get_nonexistent_returns_none(self, store):
        assert store.get("bbl_missing") is None

    def test_mark_done_updates_finished_at(self, store, messages):
        b = store.create("goal", messages, 5)
        assert isinstance(b, Bubble)
        assert b.finished_at is None
        store.mark_done(b)
        assert b.finished_at is not None

    def test_history_capped_at_max(self, messages):
        store = BubbleStore(max_concurrent=50)
        store._MAX_HISTORY = 3
        for i in range(5):
            b = store.create(f"goal {i}", messages, 5)
            assert isinstance(b, Bubble)
            store.mark_done(b)
        assert len(store._history) == 3

    def test_cancel_all_cancels_tasks(self, store, messages):
        b = store.create("goal", messages, 5)
        assert isinstance(b, Bubble)
        task = MagicMock()
        task.done.return_value = False
        b.task = task
        store.cancel_all()
        task.cancel.assert_called_once()

    def test_list_active_excludes_done(self, store, messages):
        b1 = store.create("g1", messages, 5)
        b2 = store.create("g2", messages, 5)
        assert isinstance(b1, Bubble) and isinstance(b2, Bubble)
        store.mark_done(b1)
        active = store.list_active()
        assert len(active) == 1
        assert active[0] is b2

    def test_resume_reactivates_recent_timeout_with_extra_cycles(self, store, messages):
        b = store.create("goal", messages, max_cycles=2)
        assert isinstance(b, Bubble)
        b.status = "timeout"
        b.cycles_used = 2
        store.mark_done(b)

        resumed = store.resume(
            b.id,
            additional_cycles=3,
            max_cycles_cap=50,
        )

        assert resumed is b
        assert b.status == "running"
        assert b.finished_at is None
        assert b.max_cycles == 5
        assert b.resume_count == 1
        assert store.list_active() == [b]
        assert b not in store._history

    def test_resume_rejects_expired_timeout(self, messages):
        store = BubbleStore(timeout_resume_seconds=10)
        b = store.create("goal", messages, max_cycles=2)
        assert isinstance(b, Bubble)
        b.status = "timeout"
        store.mark_done(b)
        b.finished_at = datetime.now() - timedelta(seconds=11)

        result = store.resume(b.id, additional_cycles=2, max_cycles_cap=50)

        assert isinstance(result, str)
        assert "超过可续跑窗口" in result
        assert b.status == "timeout"
        assert store.list_active() == []

    def test_resume_respects_concurrent_capacity(self, messages):
        store = BubbleStore(max_concurrent=1)
        timed_out = store.create("timed out", messages, max_cycles=2)
        assert isinstance(timed_out, Bubble)
        timed_out.status = "timeout"
        store.mark_done(timed_out)
        active = store.create("active", messages, max_cycles=2)
        assert isinstance(active, Bubble)

        result = store.resume(timed_out.id, additional_cycles=2, max_cycles_cap=50)

        assert isinstance(result, str)
        assert "最大并发泡泡数" in result


class TestBubbleHandoffNotifier:
    async def test_completion_requires_successful_takeover_notice(self):
        communicate = MagicMock()
        communicate.supports_message_extra.return_value = False
        communicate.execute = AsyncMock(
            return_value=ToolResult(
                tool_call_id="",
                content="delivery failed",
                is_error=True,
            )
        )
        bubble = Bubble(
            id="bbl_test",
            goal="reply",
            participant_id="wecom:alice",
            handoff_transparency=True,
        )
        notifier = BubbleHandoffNotifier(communicate)

        assert await notifier.announce_started(bubble) is False
        assert await notifier.announce_finished(bubble) is False
        assert bubble.handoff_notice_active is False
        communicate.execute.assert_awaited_once()


# ---------------------------------------------------------------------------
# Bubble helpers
# ---------------------------------------------------------------------------


class TestBubble:
    def test_is_terminal_running(self):
        b = Bubble(id="bbl_x", goal="g")
        assert not b.is_terminal()

    @pytest.mark.parametrize("status", ["done", "error", "cancelled", "timeout"])
    def test_is_terminal_states(self, status):
        b = Bubble(id="bbl_x", goal="g", status=status)
        assert b.is_terminal()

    def test_elapsed_seconds_running(self):
        b = Bubble(id="bbl_x", goal="g")
        assert b.elapsed_seconds() >= 0

    def test_elapsed_seconds_done(self):
        from datetime import timedelta
        b = Bubble(id="bbl_x", goal="g")
        b.finished_at = b.created_at + timedelta(seconds=5)
        assert abs(b.elapsed_seconds() - 5.0) < 0.01


class TestBubbleHandoff:
    def test_unconfigured_matcher_fails_closed_but_config_factory_uses_defaults(self):
        unconfigured = BubbleHandoffMatcher()
        configured = BubbleHandoffMatcher.from_config()
        local = "coworker-desktop:desk:local:cw_default:abcd1234"

        assert not unconfigured.matches("wecom:alice")
        assert not unconfigured.matches(local)
        assert not unconfigured.matches("web-client", stream_transport="websocket")
        assert configured.matches("wecom:alice")
        assert configured.matches(local)
        assert configured.matches("web-client", stream_transport="websocket")
        assert configured.matches("sse-client", stream_transport="sse")

    def test_matcher_combines_glob_stream_and_desktop_rules(self):
        matcher = BubbleHandoffMatcher.from_config(
            participant_matches=["wecom:*", "coworker-desktop:*:local:*"],
            stream_transports=["websocket", "sse"],
        )

        local = "coworker-desktop:desk:local:cw_default:abcd1234"
        claude = "coworker-desktop:desk:claude:cw_default:abcd1234"
        codex = "coworker-desktop:desk:codex:cw_default:abcd1234"

        assert matcher.matches("wecom:alice")
        assert not matcher.matches("other-wecom:alice")
        assert matcher.matches("web-client", stream_transport="websocket")
        assert matcher.matches("sse-client", stream_transport="sse")
        assert not matcher.matches("web-client")
        assert matcher.matches(local, stream_transport="websocket")
        assert not matcher.matches(claude, stream_transport="websocket")
        assert not matcher.matches(codex, stream_transport="sse")

    def test_matcher_uses_exact_match_without_glob_and_allows_empty_override(self):
        exact = BubbleHandoffMatcher.from_config(participant_matches=["wecom:alice"])
        disabled = BubbleHandoffMatcher.from_config(participant_matches=[])
        streams_disabled = BubbleHandoffMatcher.from_config(stream_transports=[])

        assert exact.matches("wecom:alice")
        assert not exact.matches("wecom:bob")
        assert not disabled.matches("wecom:alice")
        assert not disabled.matches("coworker-desktop:desk:local:cw_default:abcd1234")
        assert not streams_disabled.matches("web-client", stream_transport="websocket")

    def test_explicit_desktop_glob_takes_precedence_over_stream_guard(self):
        matcher = BubbleHandoffMatcher.from_config(
            participant_matches=["coworker-desktop:*:claude:*"],
            stream_transports=["websocket"],
        )

        assert matcher.matches("coworker-desktop:desk:claude:cw_default:abcd1234")
        assert not matcher.matches(
            "coworker-desktop:desk:codex:cw_default:abcd1234",
            stream_transport="websocket",
        )


# ---------------------------------------------------------------------------
# _build_merge_message
# ---------------------------------------------------------------------------


class TestBuildMergeMessage:
    def test_done_with_result(self):
        b = Bubble(id="bbl_abc", goal="分析X", status="done", result="结论Y", cycles_used=2, max_cycles=10)
        msg = _build_merge_message(b)
        assert "bbl_abc" in msg
        assert "成功完成" in msg
        assert "结论Y" in msg
        assert "分析X" in msg

    def test_error_state(self):
        b = Bubble(id="bbl_abc", goal="g", status="error", error="连接超时", cycles_used=1, max_cycles=5)
        msg = _build_merge_message(b)
        assert "执行出错" in msg
        assert "连接超时" in msg

    def test_includes_thinking_path(self):
        b = Bubble(id="bbl_abc", goal="g", status="done", result="ok", cycles_used=2, max_cycles=10)
        b.inner_messages = [
            Message(role="assistant", content="", tool_calls=[
                {"id": "1", "type": "function", "function": {"name": "search_web", "arguments": "{}"}}
            ]),
            Message(role="tool", content="data", tool_call_id="1"),
        ]
        msg = _build_merge_message(b)
        assert "search_web" in msg
        assert "轮次1" in msg

    def test_no_result_shows_placeholder(self):
        b = Bubble(id="bbl_abc", goal="g", status="cancelled", cycles_used=0, max_cycles=5)
        msg = _build_merge_message(b)
        assert "无结论" in msg


# ---------------------------------------------------------------------------
# BubbleMiniLoop
# ---------------------------------------------------------------------------


def _make_mini_loop(
    bubble,
    brain,
    registry,
    inbox,
    store,
    logs_dir,
    *,
    communicate=None,
):
    return BubbleMiniLoop(
        bubble=bubble,
        brain=brain,
        tool_registry=registry,
        system_prompt="sys",
        bubble_store=store,
        inbox_watcher=inbox,
        logs_dir=str(logs_dir),
        communicate=communicate,
    )


def _make_response(content="", tool_calls=None, stop_reason="end_turn", usage=None, model="mock"):
    return LLMResponse(
        content=content,
        tool_calls=tool_calls or [],
        stop_reason=stop_reason,
        model=model,
        usage=usage or {},
    )


class TestBubbleMiniLoop:
    async def test_end_turn_without_tools_nudges_then_done(
        self, store, messages, mock_brain, mock_inbox, mock_registry, tmp_path
    ):
        """No tool_calls → system nudge injected; model calls bubble_done next cycle."""
        done_tc = ToolCall(id="c1", name="bubble_done", arguments={"result": "完成了"})
        mock_brain.think = AsyncMock(side_effect=[
            _make_response(content="我在思考"),  # no tools → nudge
            _make_response(tool_calls=[done_tc], stop_reason="tool_use"),
        ])
        b = store.create("goal", messages, max_cycles=5)
        assert isinstance(b, Bubble)
        loop = _make_mini_loop(b, mock_brain, mock_registry, mock_inbox, store, tmp_path)
        await loop.run()

        assert b.status == "done"
        assert "完成了" in b.result
        # nudge message should be in inner_messages
        nudge_msgs = [m for m in b.inner_messages if isinstance(m.content, str) and "bubble_done" in m.content]
        assert nudge_msgs

    async def test_bubble_done_tool_call(self, store, messages, mock_brain, mock_inbox, mock_registry, tmp_path):
        tc = ToolCall(id="c1", name="bubble_done", arguments={"result": "分析结论"})
        done_resp = _make_response(tool_calls=[tc], stop_reason="tool_use")
        mock_brain.think = AsyncMock(return_value=done_resp)
        b = store.create("goal", messages, max_cycles=5)
        assert isinstance(b, Bubble)
        loop = _make_mini_loop(b, mock_brain, mock_registry, mock_inbox, store, tmp_path)
        await loop.run()

        assert b.status == "done"
        assert b.result == "分析结论"

    async def test_usage_stats_listener_counts_bubble_llm_and_tools(
        self, store, messages, mock_brain, mock_inbox, mock_registry, tmp_path
    ):
        stats = UsageStatsCollector(now_fn=lambda: datetime(2026, 6, 29))
        tc = ToolCall(id="c1", name="bubble_done", arguments={"result": "分析结论"})
        mock_brain.think = AsyncMock(return_value=_make_response(
            tool_calls=[tc],
            stop_reason="tool_use",
            usage={"input_tokens": 12, "output_tokens": 3, "cached_tokens": 4},
            model="mock-model",
        ))
        b = store.create("goal", messages, max_cycles=5)
        assert isinstance(b, Bubble)
        loop = BubbleMiniLoop(
            bubble=b,
            brain=mock_brain,
            tool_registry=mock_registry,
            system_prompt="sys",
            bubble_store=store,
            inbox_watcher=mock_inbox,
            logs_dir=str(tmp_path),
            usage_stats=stats,
            usage_logs_root=str(tmp_path),
        )
        await loop.run()

        today = stats.snapshot()["lifetime"]
        assert today["llm_calls"] == 1
        assert today["total_tokens"] == 15
        assert today["cached_tokens"] == 4
        assert today["tool_calls"] == 1
        assert today["tools"] == {"bubble_done": 1}
        assert today["by_provider_model"]["mock/mock-model"]["total_tokens"] == 15
        assert today["by_provider_model"]["mock/mock-model"]["cache_rate"] == 4 / 12
        assert today["by_scope"]["bubble"]["total_tokens"] == 15
        assert today["by_scope"]["bubble"]["tools"] == {"bubble_done": 1}
        assert today["by_scope"]["main"]["total_tokens"] == 0

    async def test_usage_stats_listener_counts_subconscious_bubble(
        self, store, messages, mock_brain, mock_inbox, mock_registry, tmp_path
    ):
        from coworker.agent.subconscious import SubconsciousMiniLoop

        stats = UsageStatsCollector(now_fn=lambda: datetime(2026, 6, 29))
        tc = ToolCall(id="c1", name="bubble_done", arguments={"result": "潜意识结论"})
        mock_brain.think = AsyncMock(return_value=_make_response(
            tool_calls=[tc],
            stop_reason="tool_use",
            usage={"input_tokens": 5, "output_tokens": 7},
            model="sub-model",
        ))
        b = store.create("goal", messages, max_cycles=5)
        assert isinstance(b, Bubble)
        loop = SubconsciousMiniLoop(
            mode="audit",
            identity_body="subconscious {bubble_id} {goal} {max_cycles}",
            intercepts={},
            bubble=b,
            brain=mock_brain,
            tool_registry=mock_registry,
            system_prompt="sys",
            bubble_store=store,
            inbox_watcher=mock_inbox,
            logs_dir=str(tmp_path / "subconscious"),
            usage_stats=stats,
            usage_logs_root=str(tmp_path),
        )
        await loop.run()

        today = stats.snapshot()["lifetime"]
        assert today["llm_calls"] == 1
        assert today["total_tokens"] == 12
        assert today["tool_calls"] == 1
        assert today["by_model"]["sub-model"]["total_tokens"] == 12
        assert today["by_provider_model"]["mock/sub-model"]["total_tokens"] == 12
        assert today["by_scope"]["subconscious"]["total_tokens"] == 12
        assert today["by_scope"]["subconscious"]["tools"] == {"bubble_done": 1}
        assert today["by_scope"]["bubble"]["total_tokens"] == 0

    async def test_auto_merge_pushes_to_inbox(self, store, messages, mock_brain, mock_inbox, mock_registry, tmp_path):
        """Merge is routed through inbox to avoid mid-tool-execution insertion."""
        done_tc = ToolCall(id="c1", name="bubble_done", arguments={"result": "完成了"})
        mock_brain.think = AsyncMock(return_value=_make_response(tool_calls=[done_tc], stop_reason="tool_use"))
        b = store.create("goal", messages, max_cycles=5)
        assert isinstance(b, Bubble)
        loop = _make_mini_loop(b, mock_brain, mock_registry, mock_inbox, store, tmp_path)
        await loop.run()

        push_contents = [call[0][0].content for call in mock_inbox.push.call_args_list]
        assert any("[泡泡思考结果]" in c for c in push_contents)

    async def test_notify_inbox_called(self, store, messages, mock_brain, mock_inbox, mock_registry, tmp_path):
        done_tc = ToolCall(id="c1", name="bubble_done", arguments={"result": "完成"})
        mock_brain.think = AsyncMock(return_value=_make_response(tool_calls=[done_tc], stop_reason="tool_use"))
        b = store.create("goal", messages, max_cycles=5)
        assert isinstance(b, Bubble)
        loop = _make_mini_loop(b, mock_brain, mock_registry, mock_inbox, store, tmp_path)
        await loop.run()

        push_contents = [call[0][0].content for call in mock_inbox.push.call_args_list]
        assert any("[泡泡思考结果]" in c for c in push_contents)

    async def test_burst_warning_on_last_cycle(
        self, store, messages, mock_brain, mock_inbox, mock_registry, tmp_path
    ):
        """On the final cycle the bubble gets a heads-up, letting it call bubble_done before timeout."""
        captured: list[list] = []

        tc = ToolCall(id="c1", name="some_tool", arguments={})
        done_tc = ToolCall(id="c2", name="bubble_done", arguments={"result": "及时收尾"})
        call_count = 0

        async def capture_think(messages, system_prompt, tools):
            nonlocal call_count
            call_count += 1
            captured.append(list(messages))
            if call_count == 1:
                return _make_response(tool_calls=[tc], stop_reason="tool_use")
            return _make_response(tool_calls=[done_tc], stop_reason="tool_use")

        mock_brain.think = capture_think
        mock_registry.execute = AsyncMock(return_value=MagicMock(content="ok", is_error=False))
        mock_registry.get_schemas.return_value = [{"name": "some_tool", "description": "", "parameters": {}}]

        b = store.create("goal", messages, max_cycles=2)
        assert isinstance(b, Bubble)
        loop = _make_mini_loop(b, mock_brain, mock_registry, mock_inbox, store, tmp_path)
        await loop.run()

        # First cycle: no warning yet. Last cycle: warning present.
        first_texts = [m.content for m in captured[0] if isinstance(m.content, str)]
        assert not any("即将破灭" in t for t in first_texts)
        last_texts = [m.content for m in captured[1] if isinstance(m.content, str)]
        assert any("即将破灭" in t for t in last_texts)
        # Heads-up let it finish cleanly instead of timing out.
        assert b.status == "done"
        assert b.result == "及时收尾"

    async def test_timeout_via_max_cycles(self, store, messages, mock_brain, mock_inbox, mock_registry, tmp_path):
        # Always returns a tool call that is NOT bubble_done → eventually times out
        tc = ToolCall(id="c1", name="some_tool", arguments={})
        resp = _make_response(tool_calls=[tc], stop_reason="tool_use")
        mock_brain.think = AsyncMock(return_value=resp)
        mock_brain.summarize = AsyncMock(return_value=json.dumps({"summary": "超时摘要", "memories": []}))
        mock_registry.execute = AsyncMock(return_value=MagicMock(content="tool result", is_error=False))
        mock_registry.get_schemas.return_value = [{"name": "some_tool", "description": "", "parameters": {}}]

        b = store.create("goal", messages, max_cycles=2)
        assert isinstance(b, Bubble)
        loop = _make_mini_loop(b, mock_brain, mock_registry, mock_inbox, store, tmp_path)
        await loop.run()

        assert b.status == "timeout"
        assert b.cycles_used == 2
        assert "超时摘要" in b.result
        merged = [call.args[0].content for call in mock_inbox.push.call_args_list]
        assert any("bubble_spawn" in content and "bubble_id=" in content for content in merged)

    async def test_resume_keeps_transcript_and_continues_cycle_budget(
        self, store, messages, mock_brain, mock_inbox, mock_registry, tmp_path
    ):
        """A resumed bubble starts at its consumed cycle count and sees prior work."""
        work_tc = ToolCall(id="work", name="some_tool", arguments={})
        done_tc = ToolCall(id="done", name="bubble_done", arguments={"result": "续跑完成"})
        responses = [
            _make_response(tool_calls=[work_tc], stop_reason="tool_use"),
            _make_response(tool_calls=[work_tc], stop_reason="tool_use"),
            _make_response(tool_calls=[done_tc], stop_reason="tool_use"),
        ]
        captured: list[list] = []

        async def capture_think(messages, system_prompt, tools):
            captured.append(list(messages))
            return responses.pop(0)

        mock_brain.think = capture_think
        mock_brain.summarize = AsyncMock(
            return_value=json.dumps({"summary": "第一阶段超时摘要", "memories": []})
        )
        mock_registry.execute = AsyncMock(
            return_value=MagicMock(content="第一阶段工具结果", is_error=False)
        )
        mock_registry.get_schemas.return_value = [{"name": "some_tool", "description": "", "parameters": {}}]

        b = store.create("goal", messages, max_cycles=2)
        assert isinstance(b, Bubble)
        await _make_mini_loop(b, mock_brain, mock_registry, mock_inbox, store, tmp_path).run()
        assert b.status == "timeout"
        assert b.cycles_used == 2

        resumed = store.resume(b.id, additional_cycles=2, max_cycles_cap=50)
        assert resumed is b
        await _make_mini_loop(b, mock_brain, mock_registry, mock_inbox, store, tmp_path).run()

        assert b.status == "done"
        assert b.cycles_used == 3
        assert b.max_cycles == 4
        assert b.result == "续跑完成"
        resumed_context = [m.content for m in captured[2] if isinstance(m.content, str)]
        assert any("第一阶段工具结果" in text for text in resumed_context)
        assert any("第 1 次续跑" in text for text in resumed_context)
        assert not any("第一阶段超时摘要" in text for text in resumed_context)

    async def test_checkpoint_extends_budget_even_before_last_cycle(
        self, store, messages, mock_brain, mock_inbox, mock_registry, tmp_path
    ):
        checkpoint_tc = ToolCall(
            id="c1", name="bubble_done", arguments={"result": "阶段结论", "checkpoint": True}
        )
        done_tc = ToolCall(id="c2", name="bubble_done", arguments={"result": "最终结论"})
        mock_brain.think = AsyncMock(side_effect=[
            _make_response(tool_calls=[checkpoint_tc], stop_reason="tool_use"),
            _make_response(tool_calls=[done_tc], stop_reason="tool_use"),
        ])

        b = store.create("goal", messages, max_cycles=3)
        assert isinstance(b, Bubble)
        loop = _make_mini_loop(b, mock_brain, mock_registry, mock_inbox, store, tmp_path)
        await loop.run()

        assert b.status == "done"
        assert b.result == "最终结论"
        assert b.partial_results == ["阶段结论"]
        assert b.checkpoint_count == 1
        assert b.cycles_used == 2
        assert b.initial_max_cycles == 3
        assert b.max_cycles == 6
        push_contents = [call.args[0].content for call in mock_inbox.push.call_args_list]
        assert any("[泡泡检查点]" in c and "阶段结论" in c for c in push_contents)
        assert any("[泡泡思考结果]" in c and "最终结论" in c for c in push_contents)

    def test_base_intercepts_cover_idle_and_main_tools(self, store, mock_brain, mock_inbox, mock_registry, tmp_path):
        b = store.create("goal", [Message(role="user", content="x")], max_cycles=5)
        assert isinstance(b, Bubble)
        loop = _make_mini_loop(b, mock_brain, mock_registry, mock_inbox, store, tmp_path)
        ic = loop._tool_intercepts()
        for name in ("sleep", "restart_self", "clear_short_term_memory", "compress_memory"):
            assert name in ic
        # breathe is a ritual transition (not idle spinning) → stays available for bubbles
        assert "breathe" not in ic
        # manage_pinned_context is scoped to the bubble's own STM → must stay available
        assert "manage_pinned_context" not in ic
        # 泡泡通过 bubble_send 通信，不直接调用主线 communicate。
        assert "communicate" in ic
        assert "bubble_spawn" not in ic

    def test_participant_bound_bubble_can_use_communicate(
        self, store, mock_brain, mock_inbox, mock_registry, tmp_path
    ):
        b = store.create("reply to user", [Message(role="user", content="x")], max_cycles=5)
        assert isinstance(b, Bubble)
        b.participant_id = "wecom:alice"
        b.conversation_id = "conv-1"
        loop = _make_mini_loop(
            b,
            mock_brain,
            mock_registry,
            mock_inbox,
            store,
            tmp_path,
            communicate=MagicMock(),
        )

        assert "communicate" not in loop._tool_intercepts()
        identity = loop._build_identity_content(b)
        assert "wecom:alice" in identity
        assert (
            "communicate(participant_id='wecom:alice', "
            "conversation_id='conv-1', message=...)"
        ) in identity

    async def test_participant_bound_bubble_sends_direct_reply_only_to_its_binding(
        self, store, messages, mock_brain, mock_inbox, tmp_path
    ):
        from coworker.core.types import CommunicateRequest, ToolResult
        from coworker.tools.communicate_tool import CommunicateTool
        from coworker.tools.registry import ToolRegistry

        sent: list[CommunicateRequest] = []

        async def sender(request: CommunicateRequest):
            sent.append(request)
            return ToolResult(tool_call_id="", content="sent")

        registry = ToolRegistry()
        channel_system = create_channel_system(tmp_path / "outbox")
        communicate = CommunicateTool(channel_system.registry)
        channel_system.registry.register(
            BaseChannel.from_sender(
                "wecom:",
                sender,
                capabilities=ChannelCapabilities(conversation_id=True),
            )
        )
        registry.register(communicate)
        mock_brain.think = AsyncMock(
            side_effect=[
                _make_response(
                    tool_calls=[
                        ToolCall(
                            id="reply",
                            name="communicate",
                            arguments={"message": "已经处理"},
                        )
                    ],
                    stop_reason="tool_use",
                ),
                _make_response(
                    tool_calls=[
                        ToolCall(
                            id="done",
                            name="bubble_done",
                            arguments={"result": "已回复用户"},
                        )
                    ],
                    stop_reason="tool_use",
                ),
            ]
        )
        b = store.create("reply", messages, max_cycles=5)
        assert isinstance(b, Bubble)
        b.participant_id = "wecom:alice"
        b.conversation_id = "conv-1"

        loop = _make_mini_loop(
            b,
            mock_brain,
            registry,
            mock_inbox,
            store,
            tmp_path,
            communicate=communicate,
        )
        await loop.run()

        assert sent == [
            CommunicateRequest(
                participant_id="wecom:alice",
                message="已经处理",
                conversation_id="conv-1",
            )
        ]

    @pytest.mark.parametrize(
        "participant_id",
        ["wecom:alice", "coworker-desktop:desk:local:cw_default:abcd1234"],
    )
    @pytest.mark.parametrize(("resume_count", "resumed"), [(0, False), (1, True)])
    async def test_transparent_bound_bubble_starts_handoff_on_first_reply(
        self,
        store,
        messages,
        mock_brain,
        mock_inbox,
        tmp_path,
        participant_id,
        resume_count,
        resumed,
    ):
        from coworker.agent.bubble_handoff import (
            BUBBLE_REPLY_PREFIX,
            bubble_handoff_message_extra,
            bubble_reply_message_extra,
            format_handoff_end_message,
            format_handoff_start_message,
        )
        from coworker.core.types import CommunicateRequest, ToolResult
        from coworker.tools.communicate_tool import CommunicateTool
        from coworker.tools.registry import ToolRegistry

        sent: list[CommunicateRequest] = []

        async def sender(request: CommunicateRequest):
            sent.append(request)
            return ToolResult(tool_call_id="", content="sent")

        registry = ToolRegistry()
        channel_system = create_channel_system(tmp_path / "outbox")
        communicate = CommunicateTool(channel_system.registry)
        supports_extra = participant_id.startswith("coworker-desktop:")
        channel_system.registry.register(BaseChannel.from_sender(
            f"{participant_id.split(':', 1)[0]}:",
            sender,
            capabilities=ChannelCapabilities(
                conversation_id=True,
                extra=supports_extra,
            ),
        ))
        registry.register(communicate)
        mock_brain.think = AsyncMock(
            side_effect=[
                _make_response(
                    tool_calls=[
                        ToolCall(
                            id="reply",
                            name="communicate",
                            arguments={"message": "已经处理"},
                        )
                    ],
                    stop_reason="tool_use",
                ),
                _make_response(
                    tool_calls=[
                        ToolCall(
                            id="done",
                            name="bubble_done",
                            arguments={"result": "已回复用户"},
                        )
                    ],
                    stop_reason="tool_use",
                ),
            ]
        )
        bubble = store.create("reply", messages, max_cycles=5)
        assert isinstance(bubble, Bubble)
        bubble.participant_id = participant_id
        bubble.conversation_id = "conv-1"
        bubble.handoff_transparency = True
        bubble.resume_count = resume_count

        loop = BubbleMiniLoop(
            bubble=bubble,
            brain=mock_brain,
            tool_registry=registry,
            system_prompt="sys",
            bubble_store=store,
            inbox_watcher=mock_inbox,
            logs_dir=str(tmp_path),
            communicate=communicate,
        )
        await loop.run()

        expected_reply = (
            "已经处理"
            if participant_id.startswith("coworker-desktop:")
            else f"{BUBBLE_REPLY_PREFIX}已经处理"
        )
        start_extra = (
            bubble_handoff_message_extra(
                bubble.id,
                phase="start",
                resumed=resumed,
            )
            if supports_extra
            else {}
        )
        reply_extra = bubble_reply_message_extra(bubble.id) if supports_extra else {}
        end_extra = (
            bubble_handoff_message_extra(bubble.id, phase="end") if supports_extra else {}
        )
        assert sent == [
            CommunicateRequest(
                participant_id=participant_id,
                message=format_handoff_start_message(bubble.id, resumed=resumed),
                conversation_id="conv-1",
                extra=start_extra,
            ),
            CommunicateRequest(
                participant_id=participant_id,
                message=expected_reply,
                conversation_id="conv-1",
                extra=reply_extra,
            ),
            CommunicateRequest(
                participant_id=participant_id,
                message=format_handoff_end_message(bubble.id),
                conversation_id="conv-1",
                extra=end_extra,
            ),
        ]

    @pytest.mark.parametrize(
        ("has_inbound_message", "expected_phases"),
        [
            (False, []),
            (True, ["start", "end"]),
        ],
    )
    async def test_transparent_handoff_notices_require_real_inbound_session(
        self,
        store,
        messages,
        mock_brain,
        mock_inbox,
        tmp_path,
        has_inbound_message,
        expected_phases,
    ):
        from coworker.core.types import CommunicateRequest, ToolResult
        from coworker.tools.communicate_tool import CommunicateTool
        from coworker.tools.registry import ToolRegistry

        sent: list[CommunicateRequest] = []

        async def sender(request: CommunicateRequest):
            sent.append(request)
            return ToolResult(tool_call_id="", content="sent")

        participant_id = "coworker-desktop:desk:local:cw:123"
        registry = ToolRegistry()
        channel_system = create_channel_system(tmp_path / "outbox")
        communicate = CommunicateTool(channel_system.registry)
        channel_system.registry.register(
            BaseChannel.from_sender(
                "coworker-desktop:",
                sender,
                capabilities=ChannelCapabilities(conversation_id=True, extra=True),
            )
        )
        registry.register(communicate)
        mock_brain.think = AsyncMock(
            return_value=_make_response(
                tool_calls=[
                    ToolCall(
                        id="done",
                        name="bubble_done",
                        arguments={"result": "无需回复"},
                    )
                ],
                stop_reason="tool_use",
            )
        )
        bubble = store.create("inspect", messages, max_cycles=2)
        assert isinstance(bubble, Bubble)
        bubble.participant_id = participant_id
        bubble.conversation_id = "conv-1"
        bubble.handoff_transparency = True
        if has_inbound_message:
            bubble.inbox.put_nowait(
                IncomingEvent(
                    participant_id=participant_id,
                    conversation_id="conv-1",
                    content="有新进展吗？",
                    source="websocket",
                )
            )

        loop = BubbleMiniLoop(
            bubble=bubble,
            brain=mock_brain,
            tool_registry=registry,
            system_prompt="sys",
            bubble_store=store,
            inbox_watcher=mock_inbox,
            logs_dir=str(tmp_path),
            communicate=communicate,
        )
        await loop.run()

        phases = [
            request.extra["bubble"]["phase"]
            for request in sent
            if request.extra["bubble"]["kind"] == "handoff"
        ]
        assert phases == expected_phases
        assert bubble.handoff_notice_active is False

    async def test_cancellation(self, store, messages, mock_brain, mock_inbox, mock_registry, tmp_path):
        async def slow_think(*args, **kwargs):
            await asyncio.sleep(10)
        mock_brain.think = slow_think

        b = store.create("goal", messages, max_cycles=5)
        assert isinstance(b, Bubble)
        loop = _make_mini_loop(b, mock_brain, mock_registry, mock_inbox, store, tmp_path)
        task = asyncio.create_task(loop.run())
        await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert b.status == "cancelled"

    async def test_inbox_message_injected_before_think(self, store, messages, mock_brain, mock_inbox, mock_registry, tmp_path):
        injected_messages = []

        async def capture_think(messages, system_prompt, tools):
            injected_messages.extend(messages)
            return _make_response(content="done")

        mock_brain.think = capture_think
        b = store.create("goal", messages, max_cycles=5)
        assert isinstance(b, Bubble)
        await b.inbox.put(("主线", "请关注点A"))
        loop = _make_mini_loop(b, mock_brain, mock_registry, mock_inbox, store, tmp_path)
        await loop.run()

        texts = [m.content for m in injected_messages if isinstance(m.content, str)]
        assert any("来自 主线" in t and "请关注点A" in t for t in texts)

    async def test_directly_routed_communication_is_injected_with_metadata(
        self, store, messages, mock_brain, mock_inbox, mock_registry, tmp_path
    ):
        injected_messages = []

        async def capture_think(messages, system_prompt, tools):
            injected_messages.extend(messages)
            return _make_response(content="done")

        mock_brain.think = capture_think
        b = store.create("handle follow-up", messages, max_cycles=5)
        assert isinstance(b, Bubble)
        await b.inbox.put(
            IncomingEvent(
                participant_id="wecom:alice",
                conversation_id="conv-7",
                content="请看附件",
                source="wecom",
                attachments=[
                    AttachmentData(
                        filename="photo.jpg",
                        media_type="image/jpeg",
                        saved_path="data/attachments/photo.jpg",
                        data="base64-image",
                    )
                ],
            )
        )

        loop = _make_mini_loop(b, mock_brain, mock_registry, mock_inbox, store, tmp_path)
        await loop.run()

        direct_message = next(
            message
            for message in injected_messages
            if isinstance(message.content, list)
            and any(block.get("type") == "image" for block in message.content)
        )
        text_block = next(block for block in direct_message.content if block["type"] == "text")
        assert "wecom:alice" in text_block["text"]
        assert "conversation:conv-7" in text_block["text"]

    async def test_finalizing_bubble_returns_undrained_direct_message_to_main(
        self, store, messages, mock_brain, mock_inbox, mock_registry, tmp_path
    ):
        b = store.create("finish", messages, max_cycles=5)
        assert isinstance(b, Bubble)
        b.status = "done"
        direct_event = IncomingEvent(
            participant_id="alice",
            content="一个稍晚到达的追问",
            source="websocket",
        )
        await b.inbox.put(direct_event)
        loop = _make_mini_loop(b, mock_brain, mock_registry, mock_inbox, store, tmp_path)

        await loop._auto_merge()

        pushed = [call.args[0] for call in mock_inbox.push.call_args_list]
        assert pushed[0].source == "bubble"
        assert pushed[1] is direct_event
        assert store.get(b.id) is b

    async def test_bubble_send_to_main(self, store, messages, mock_brain, mock_inbox, mock_registry, tmp_path):
        tc = ToolCall(id="c1", name="bubble_send", arguments={"target": "main", "message": "发现了X"})
        done_tc = ToolCall(id="c2", name="bubble_done", arguments={"result": "完成"})
        responses = [
            _make_response(tool_calls=[tc], stop_reason="tool_use"),
            _make_response(tool_calls=[done_tc], stop_reason="tool_use"),
        ]
        mock_brain.think = AsyncMock(side_effect=responses)
        b = store.create("goal", messages, max_cycles=5)
        assert isinstance(b, Bubble)
        loop = _make_mini_loop(b, mock_brain, mock_registry, mock_inbox, store, tmp_path)
        await loop.run()

        # inbox.push should have been called for the bubble_send + completion notify
        push_calls = [call[0][0].content for call in mock_inbox.push.call_args_list]
        assert any("发现了X" in c for c in push_calls)

    async def test_bubble_send_to_other_bubble(self, store, messages, mock_brain, mock_inbox, mock_registry, tmp_path):
        b_target = store.create("target goal", messages, max_cycles=5)
        assert isinstance(b_target, Bubble)

        tc = ToolCall(id="c1", name="bubble_send", arguments={"target": b_target.id, "message": "hello"})
        done_tc = ToolCall(id="c2", name="bubble_done", arguments={"result": "sent"})
        responses = [
            _make_response(tool_calls=[tc], stop_reason="tool_use"),
            _make_response(tool_calls=[done_tc], stop_reason="tool_use"),
        ]
        mock_brain.think = AsyncMock(side_effect=responses)
        b_sender = store.create("sender goal", messages, max_cycles=5)
        assert isinstance(b_sender, Bubble)
        loop = _make_mini_loop(b_sender, mock_brain, mock_registry, mock_inbox, store, tmp_path)
        await loop.run()

        assert not b_target.inbox.empty()
        sender_id, msg_text = b_target.inbox.get_nowait()
        assert sender_id == b_sender.id
        assert msg_text == "hello"

    async def test_log_file_created(self, store, messages, mock_brain, mock_inbox, mock_registry, tmp_path):
        mock_brain.think = AsyncMock(return_value=_make_response(content="done"))
        b = store.create("goal", messages, max_cycles=5)
        assert isinstance(b, Bubble)
        loop = _make_mini_loop(b, mock_brain, mock_registry, mock_inbox, store, tmp_path)
        await loop.run()

        log_path = tmp_path / "bubbles" / f"{b.id}.jsonl"
        assert log_path.exists()
        lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        # __meta__ is the last line, written by InteractionLogger._write() so it has ts
        assert lines[-1]["__meta__"] is True
        assert lines[-1]["id"] == b.id
        assert lines[-1]["goal"] == "goal"
        assert lines[-1]["provider"] == ""
        assert lines[-1]["model"] == ""
        assert "ts" in lines[-1]
        # first line is the identity message_in entry
        assert lines[0]["type"] == "message_in"
        assert "泡泡模式" in lines[0]["content"]
        assert "ts" in lines[0]

    async def test_log_realtime_llm_response_written_immediately(
        self, store, messages, mock_brain, mock_inbox, mock_registry, tmp_path
    ):
        """LLM response is logged immediately after think() returns, before next cycle."""
        log_sizes_after_think: list[int] = []
        bubbles_dir = tmp_path / "bubbles"

        tc = ToolCall(id="c1", name="some_tool", arguments={})
        done_tc = ToolCall(id="c2", name="bubble_done", arguments={"result": "完成"})
        call_count = 0

        async def capture_think(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # Capture log size BEFORE think returns (sizes are appended after, see below)
            if call_count == 1:
                return _make_response(tool_calls=[tc], stop_reason="tool_use")
            # At cycle 2 start, capture size — llm_response from cycle 1 already written
            files = list(bubbles_dir.glob("*.jsonl")) if bubbles_dir.exists() else []
            log_sizes_after_think.append(sum(f.stat().st_size for f in files))
            return _make_response(tool_calls=[done_tc], stop_reason="tool_use")

        mock_brain.think = capture_think
        mock_registry.execute = AsyncMock(return_value=MagicMock(content="ok", is_error=False))
        mock_registry.get_schemas.return_value = [{"name": "some_tool", "description": "", "parameters": {}}]

        b = store.create("goal", messages, max_cycles=5)
        assert isinstance(b, Bubble)
        loop = _make_mini_loop(b, mock_brain, mock_registry, mock_inbox, store, tmp_path)
        await loop.run()

        # By the time cycle 2's think() is called, cycle 1's llm_response + tool results are written
        assert log_sizes_after_think and log_sizes_after_think[0] > 0

    async def test_log_writes_thinking_start_for_each_bubble_cycle(
        self, store, messages, mock_brain, mock_inbox, mock_registry, tmp_path
    ):
        tc = ToolCall(id="c1", name="some_tool", arguments={})
        done_tc = ToolCall(id="c2", name="bubble_done", arguments={"result": "完成"})
        mock_brain.think = AsyncMock(side_effect=[
            _make_response(tool_calls=[tc], stop_reason="tool_use"),
            _make_response(tool_calls=[done_tc], stop_reason="tool_use"),
        ])
        mock_registry.execute = AsyncMock(return_value=MagicMock(content="ok", is_error=False))
        mock_registry.get_schemas.return_value = [{"name": "some_tool", "description": "", "parameters": {}}]

        b = store.create("goal", messages, max_cycles=5)
        assert isinstance(b, Bubble)
        loop = _make_mini_loop(b, mock_brain, mock_registry, mock_inbox, store, tmp_path)
        await loop.run()

        log_path = tmp_path / "bubbles" / f"{b.id}.jsonl"
        entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        thinking_starts = [e for e in entries if e.get("type") == "thinking_start"]
        llm_responses = [e for e in entries if e.get("type") == "llm_response"]

        assert len(thinking_starts) == 2
        assert [e["cycle"] for e in thinking_starts] == [0, 1]
        assert all(e["thinking"] is True for e in thinking_starts)
        assert len(llm_responses) == 2
        assert all(e["thinking"] is True for e in llm_responses)
        assert all(e["provider"] == "mock" for e in llm_responses)

    async def test_log_writes_non_thinking_flag_for_bubble_cycle(
        self, store, messages, mock_brain, mock_inbox, mock_registry, tmp_path
    ):
        done_tc = ToolCall(id="c1", name="bubble_done", arguments={"result": "完成"})
        mock_brain.think = AsyncMock(return_value=_make_response(tool_calls=[done_tc], stop_reason="tool_use"))
        mock_brain.thinking = False

        b = store.create("goal", messages, max_cycles=5)
        assert isinstance(b, Bubble)
        loop = _make_mini_loop(b, mock_brain, mock_registry, mock_inbox, store, tmp_path)
        await loop.run()

        log_path = tmp_path / "bubbles" / f"{b.id}.jsonl"
        entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        thinking_start = next(e for e in entries if e.get("type") == "thinking_start")
        llm_response = next(e for e in entries if e.get("type") == "llm_response")

        assert thinking_start["thinking"] is False
        assert llm_response["thinking"] is False
        assert llm_response["provider"] == "mock"


# ---------------------------------------------------------------------------
# Bubble tools
# ---------------------------------------------------------------------------


class TestBubbleSpawnTool:
    def _make_tool(
        self,
        store,
        short_term,
        brain,
        registry,
        prompt_builder,
        inbox,
        tmp_path,
        **kwargs,
    ):
        return BubbleSpawnTool(
            store=store,
            short_term=short_term,
            parent_brain=brain,
            full_registry=registry,
            system_prompt_builder=prompt_builder,
            inbox=inbox,
            logs_dir=str(tmp_path),
            **kwargs,
        )

    async def test_spawn_creates_task(
        self, store, mock_short_term, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
    ):
        mock_brain.think = AsyncMock(return_value=_make_response(content="done"))
        tool = self._make_tool(store, mock_short_term, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path)
        result = await tool.execute(goal="测试目标", max_cycles=3)

        assert not result.is_error
        assert "bbl_" in result.content
        assert len(store.list_active()) == 1

    async def test_spawn_binds_participant_and_conversation(
        self,
        store,
        mock_short_term,
        mock_brain,
        mock_registry,
        mock_prompt_builder,
        mock_inbox,
        tmp_path,
    ):
        mock_brain.think = AsyncMock(return_value=_make_response(content="done"))
        tool = self._make_tool(
            store,
            mock_short_term,
            mock_brain,
            mock_registry,
            mock_prompt_builder,
            mock_inbox,
            tmp_path,
        )

        await tool.execute(
            goal="处理这个会话",
            participant_id="wecom:alice",
            conversation_id="conv-3",
        )

        bubble = store.list_active()[0]
        assert bubble.participant_id == "wecom:alice"
        assert bubble.conversation_id == "conv-3"
        assert not bubble.handoff_transparency

    async def test_spawn_resolves_shorthand_participant_before_binding(
        self,
        store,
        mock_short_term,
        mock_brain,
        mock_registry,
        mock_prompt_builder,
        mock_inbox,
        tmp_path,
    ):
        from coworker.core.types import CommunicateRequest, ToolResult
        from coworker.tools.communicate_tool import CommunicateTool

        async def sender(request: CommunicateRequest):
            return ToolResult(tool_call_id="", content="sent")

        channel_system = create_channel_system(tmp_path / "outbox")
        communicate = CommunicateTool(channel_system.registry)
        channel_system.registry.register(BaseChannel.from_sender(
            "wecom:",
            sender,
            lambda pid: f"wecom:single:{pid}" if pid == "alice" else None,
            capabilities=ChannelCapabilities(conversation_id=True),
        ))
        tool = self._make_tool(
            store,
            mock_short_term,
            mock_brain,
            mock_registry,
            mock_prompt_builder,
            mock_inbox,
            tmp_path,
            communicate=communicate,
            handoff_matcher=BubbleHandoffMatcher.from_config(participant_matches=["wecom:*"]),
        )

        with patch.object(tool, "start_existing"):
            result = await tool.execute(goal="处理这个会话", participant_id=" alice ")

        assert not result.is_error
        bubble = store.list_active()[0]
        assert bubble.participant_id == "wecom:single:alice"
        assert bubble.handoff_transparency
        assert "通信绑定：wecom:single:alice" in result.content

    async def test_spawn_rejects_ambiguous_shorthand_participant(
        self,
        store,
        mock_short_term,
        mock_brain,
        mock_registry,
        mock_prompt_builder,
        mock_inbox,
        tmp_path,
    ):
        from coworker.core.types import CommunicateRequest, ToolResult
        from coworker.tools.communicate_tool import CommunicateTool

        async def sender(request: CommunicateRequest):
            return ToolResult(tool_call_id="", content="sent")

        channel_system = create_channel_system(tmp_path / "outbox")
        communicate = CommunicateTool(channel_system.registry)
        channel_system.registry.register(
            BaseChannel.from_sender("chan_a:", sender, lambda pid: f"chan_a:{pid}")
        )
        channel_system.registry.register(
            BaseChannel.from_sender("chan_b:", sender, lambda pid: f"chan_b:{pid}")
        )
        tool = self._make_tool(
            store,
            mock_short_term,
            mock_brain,
            mock_registry,
            mock_prompt_builder,
            mock_inbox,
            tmp_path,
            communicate=communicate,
        )

        result = await tool.execute(goal="处理这个会话", participant_id="alice")

        assert result.is_error
        assert "多个信道" in result.content
        assert store.list_active() == []

    async def test_transparent_binding_defers_handoff_notice_to_bubble_loop(
        self,
        store,
        mock_short_term,
        mock_brain,
        mock_registry,
        mock_prompt_builder,
        mock_inbox,
        tmp_path,
    ):
        from coworker.core.types import CommunicateRequest, ToolResult
        from coworker.tools.communicate_tool import CommunicateTool

        sent: list[CommunicateRequest] = []
        order: list[str] = []

        async def sender(request: CommunicateRequest):
            sent.append(request)
            order.append("notice")
            return ToolResult(tool_call_id="", content="sent")

        channel_system = create_channel_system(tmp_path / "outbox")
        communicate = CommunicateTool(channel_system.registry)
        channel_system.registry.register(
            BaseChannel.from_sender(
                "wecom:",
                sender,
                capabilities=ChannelCapabilities(conversation_id=True),
            )
        )
        tool = self._make_tool(
            store,
            mock_short_term,
            mock_brain,
            mock_registry,
            mock_prompt_builder,
            mock_inbox,
            tmp_path,
            communicate=communicate,
            handoff_matcher=BubbleHandoffMatcher.from_config(participant_matches=["wecom:*"]),
        )

        with patch.object(tool, "start_existing") as start_existing:
            start_existing.side_effect = lambda _bubble: order.append("start")
            result = await tool.execute(
                goal="处理这个会话",
                participant_id="wecom:alice",
                conversation_id="conv-3",
            )

        assert not result.is_error
        bubble = store.list_active()[0]
        assert bubble.handoff_transparency
        assert order == ["start"]
        assert not sent

    @pytest.mark.parametrize("transport", ["websocket", "sse"])
    async def test_configured_stream_transport_enables_transparent_handoff(
        self,
        transport,
        store,
        mock_short_term,
        mock_brain,
        mock_registry,
        mock_prompt_builder,
        mock_inbox,
        tmp_path,
    ):
        from coworker.core.types import CommunicateRequest
        from coworker.tools.communicate_tool import CommunicateTool

        participant_id = f"{transport}-client"
        outbound: asyncio.Queue[CommunicateRequest] = asyncio.Queue()
        channel_system = create_channel_system(tmp_path / "outbox")
        communicate = CommunicateTool(channel_system.registry)
        assert channel_system.stream_runtime.register_session(
            participant_id, outbound, transport=transport
        )
        tool = self._make_tool(
            store,
            mock_short_term,
            mock_brain,
            mock_registry,
            mock_prompt_builder,
            mock_inbox,
            tmp_path,
            communicate=communicate,
            stream_runtime=channel_system.stream_runtime,
            handoff_matcher=BubbleHandoffMatcher.from_config(stream_transports=[transport]),
        )

        with patch.object(tool, "start_existing"):
            result = await tool.execute(goal="处理流式会话", participant_id=participant_id)

        assert not result.is_error
        bubble = store.list_active()[0]
        assert bubble.handoff_transparency
        assert outbound.empty()

    async def test_unconfigured_stream_transport_keeps_handoff_silent(
        self,
        store,
        mock_short_term,
        mock_brain,
        mock_registry,
        mock_prompt_builder,
        mock_inbox,
        tmp_path,
    ):
        from coworker.tools.communicate_tool import CommunicateTool

        outbound: asyncio.Queue = asyncio.Queue()
        channel_system = create_channel_system(tmp_path / "outbox")
        communicate = CommunicateTool(channel_system.registry)
        assert channel_system.stream_runtime.register_session(
            "web-client", outbound, transport="websocket"
        )
        tool = self._make_tool(
            store,
            mock_short_term,
            mock_brain,
            mock_registry,
            mock_prompt_builder,
            mock_inbox,
            tmp_path,
            communicate=communicate,
            stream_runtime=channel_system.stream_runtime,
            handoff_matcher=BubbleHandoffMatcher.from_config(stream_transports=["sse"]),
        )

        with patch.object(tool, "start_existing"):
            await tool.execute(goal="处理流式会话", participant_id="web-client")

        bubble = store.list_active()[0]
        assert not bubble.handoff_transparency
        assert outbound.empty()

    @pytest.mark.parametrize(
        ("actor_id", "transparent"),
        [("local", True), ("claude", False), ("codex", False)],
    )
    async def test_local_desktop_rule_excludes_claude_and_codex_from_stream_rule(
        self,
        actor_id,
        transparent,
        store,
        mock_short_term,
        mock_brain,
        mock_registry,
        mock_prompt_builder,
        mock_inbox,
        tmp_path,
    ):
        from coworker.core.types import CommunicateRequest
        from coworker.tools.communicate_tool import CommunicateTool

        participant_id = f"coworker-desktop:desk:{actor_id}:cw_default:abcd1234"
        outbound: asyncio.Queue[CommunicateRequest] = asyncio.Queue()
        channel_system = create_channel_system(tmp_path / "outbox")
        communicate = CommunicateTool(channel_system.registry)
        assert channel_system.stream_runtime.register_session(
            participant_id, outbound, transport="websocket"
        )
        tool = self._make_tool(
            store,
            mock_short_term,
            mock_brain,
            mock_registry,
            mock_prompt_builder,
            mock_inbox,
            tmp_path,
            communicate=communicate,
            stream_runtime=channel_system.stream_runtime,
            handoff_matcher=BubbleHandoffMatcher.from_config(stream_transports=["websocket"]),
        )

        with patch.object(tool, "start_existing"):
            await tool.execute(goal="处理 Desktop 会话", participant_id=participant_id)

        bubble = store.list_active()[0]
        assert bubble.handoff_transparency is transparent
        assert outbound.empty()

    async def test_spawn_at_capacity_returns_error(
        self, mock_short_term, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
    ):
        store = BubbleStore(max_concurrent=1)
        tool = self._make_tool(store, mock_short_term, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path)
        # Fill up
        msg = Message(role="user", content="x")
        store.create("g1", [msg], 5)
        result = await tool.execute(goal="another", max_cycles=3)
        assert result.is_error
        assert "已达到最大并发泡泡数" in result.content

    async def test_spawn_max_cycles_capped(
        self, store, mock_short_term, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
    ):
        mock_brain.think = AsyncMock(return_value=_make_response(content="done"))
        tool = self._make_tool(store, mock_short_term, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path)
        await tool.execute(goal="g", max_cycles=999)
        b = store.list_active()[0]
        assert b.max_cycles == 50

    async def test_resume_timeout_with_bubble_id(
        self, store, messages, mock_short_term, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
    ):
        bubble = store.create("继续处理", messages, max_cycles=2)
        assert isinstance(bubble, Bubble)
        bubble.status = "timeout"
        bubble.cycles_used = 2
        store.mark_done(bubble)
        tool = self._make_tool(
            store, mock_short_term, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
        )

        with patch.object(tool, "start_existing") as start_existing:
            result = await tool.execute(
                bubble_id=bubble.id,
                max_cycles=3,
                goal="请完成剩余验证。",
            )

        assert not result.is_error
        start_existing.assert_called_once_with(bubble)
        assert bubble.status == "running"
        assert bubble.max_cycles == 5
        assert bubble.resume_count == 1
        sender, message = bubble.inbox.get_nowait()
        assert sender == "主线"
        assert message == "请完成剩余验证。"

    async def test_transparent_resume_defers_handoff_notice_to_bubble_loop(
        self,
        store,
        messages,
        mock_short_term,
        mock_brain,
        mock_registry,
        mock_prompt_builder,
        mock_inbox,
        tmp_path,
    ):
        from coworker.core.types import CommunicateRequest, ToolResult
        from coworker.tools.communicate_tool import CommunicateTool

        bubble = store.create("继续处理", messages, max_cycles=2)
        assert isinstance(bubble, Bubble)
        bubble.participant_id = "wecom:alice"
        bubble.conversation_id = "conv-3"
        bubble.handoff_transparency = True
        bubble.status = "timeout"
        bubble.cycles_used = 2
        store.mark_done(bubble)

        sent: list[CommunicateRequest] = []
        order: list[str] = []

        async def sender(request: CommunicateRequest):
            sent.append(request)
            order.append("notice")
            return ToolResult(tool_call_id="", content="sent")

        channel_system = create_channel_system(tmp_path / "outbox")
        communicate = CommunicateTool(channel_system.registry)
        channel_system.registry.register(
            BaseChannel.from_sender(
                "wecom:",
                sender,
                capabilities=ChannelCapabilities(conversation_id=True),
            )
        )
        tool = self._make_tool(
            store,
            mock_short_term,
            mock_brain,
            mock_registry,
            mock_prompt_builder,
            mock_inbox,
            tmp_path,
            communicate=communicate,
        )

        with patch.object(tool, "start_existing") as start_existing:
            start_existing.side_effect = lambda _bubble: order.append("start")
            result = await tool.execute(bubble_id=bubble.id, max_cycles=3)

        assert not result.is_error
        assert order == ["start"]
        assert not sent

    async def test_resume_rejects_non_timeout_bubble(
        self, store, messages, mock_short_term, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
    ):
        bubble = store.create("仍在运行", messages, max_cycles=2)
        assert isinstance(bubble, Bubble)
        tool = self._make_tool(
            store, mock_short_term, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
        )

        result = await tool.execute(bubble_id=bubble.id)

        assert result.is_error
        assert "只有超时泡泡" in result.content

    async def test_spawn_requires_goal_without_bubble_id(
        self, store, mock_short_term, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
    ):
        tool = self._make_tool(
            store, mock_short_term, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
        )

        result = await tool.execute()

        assert result.is_error
        assert "goal" in result.content

    def test_definition_exposes_resume_as_spawn_mode(
        self, store, mock_short_term, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
    ):
        tool = self._make_tool(
            store, mock_short_term, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
        )

        definition = tool.definition

        assert definition.name == "bubble_spawn"
        assert definition.parameters["required"] == []
        assert "bubble_id" in definition.parameters["properties"]
        assert "conversation_id" in definition.parameters["properties"]
        assert "message" not in definition.parameters["properties"]

    async def test_spawn_thinking_default_is_true(
        self, store, mock_short_term, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
    ):
        mock_brain.think = AsyncMock(return_value=_make_response(content="done"))
        mock_brain.max_tokens = 8192
        mock_brain.message_time_prefix = True
        tool = self._make_tool(store, mock_short_term, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path)
        await tool.execute(goal="g")
        b = store.list_active()[0]
        assert b.brain._thinking is True

    async def test_spawn_thinking_false_creates_non_thinking_brain(
        self, store, mock_short_term, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
    ):
        mock_brain.think = AsyncMock(return_value=_make_response(content="done"))
        mock_brain.max_tokens = 8192
        mock_brain.message_time_prefix = True
        tool = self._make_tool(store, mock_short_term, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path)
        result = await tool.execute(goal="g", thinking=False)
        b = store.list_active()[0]
        assert b.brain._thinking is False
        assert "非思考" in result.content

    async def test_spawn_inherits_current_provider_and_model_by_default(
        self, store, mock_short_term, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
    ):
        mock_brain.think = AsyncMock(return_value=_make_response(content="done"))
        mock_brain.max_tokens = 8192
        mock_brain.message_time_prefix = True
        tool = self._make_tool(
            store, mock_short_term, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
        )

        result = await tool.execute(goal="g")

        b = store.list_active()[0]
        assert b.provider == "mock"
        assert b.model == "mock-model"
        assert "模型：mock/mock-model" in result.content

    async def test_spawn_uses_explicit_provider_and_model(
        self, store, mock_short_term, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
    ):
        other_provider = MagicMock()
        other_provider.default_model = "other-default"
        other_provider.supports_tool_use.return_value = True
        mock_brain._providers["other"] = other_provider
        mock_brain.think = AsyncMock(return_value=_make_response(content="done"))
        mock_brain.max_tokens = 8192
        mock_brain.message_time_prefix = True
        tool = self._make_tool(
            store, mock_short_term, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
        )

        await tool.execute(goal="g", provider="other", model="other-model")

        b = store.list_active()[0]
        assert b.provider == "other"
        assert b.model == "other-model"
        assert b.brain.current_provider_name == "other"
        assert b.brain.current_model == "other-model"

    async def test_spawn_uses_provider_default_model_when_model_omitted(
        self, store, mock_short_term, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
    ):
        other_provider = MagicMock()
        other_provider.default_model = "other-default"
        other_provider.supports_tool_use.return_value = True
        mock_brain._providers["other"] = other_provider
        mock_brain.think = AsyncMock(return_value=_make_response(content="done"))
        mock_brain.max_tokens = 8192
        mock_brain.message_time_prefix = True
        tool = self._make_tool(
            store, mock_short_term, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
        )

        result = await tool.execute(goal="g", provider="other")

        b = store.list_active()[0]
        assert b.provider == "other"
        assert b.model == "other-default"
        assert "模型：other/other-default" in result.content

    async def test_spawn_returns_error_for_unsupported_model(
        self, store, mock_short_term, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
    ):
        mock_brain._providers["mock"].supports_tool_use.return_value = False
        tool = self._make_tool(
            store, mock_short_term, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
        )

        result = await tool.execute(goal="g", model="bad-model")

        assert result.is_error
        assert "不支持" in result.content
        assert len(store.list_active()) == 0

    async def test_spawn_returns_error_when_provider_has_no_resolved_model(
        self, store, mock_short_term, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
    ):
        other_provider = MagicMock()
        other_provider.default_model = ""
        other_provider.supports_tool_use.return_value = True
        mock_brain._providers["other"] = other_provider
        tool = self._make_tool(
            store, mock_short_term, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
        )

        result = await tool.execute(goal="g", provider="other")

        assert result.is_error
        assert "显式传入 model" in result.content
        assert len(store.list_active()) == 0

    async def test_spawn_synthesizes_tool_results_in_forked_context(
        self, store, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
    ):
        """Forked context gets synthetic tool results so the conversation is structurally valid."""
        from unittest.mock import MagicMock
        st = MagicMock()
        # Simulate primary ending with a multi-tool assistant message (current cycle)
        st.primary = [
            Message(role="user", content="用户问题"),
            Message(role="assistant", content="", tool_calls=[
                {"id": "c_other", "type": "function", "function": {"name": "search_web", "arguments": "{}"}},
                {"id": "c_spawn", "type": "function", "function": {"name": "bubble_spawn", "arguments": "{\"goal\": \"子任务\"}"}},
            ]),
        ]
        mock_brain.think = AsyncMock(return_value=_make_response(content="done"))
        tool = self._make_tool(store, st, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path)
        await tool.execute(goal="子任务", max_cycles=3)

        b = store.list_active()[0]
        # forked_context = 1 user + 1 assistant + 2 synthetic tool results
        assert len(b.forked_context) == 4
        tool_results = [m for m in b.forked_context if m.role == "tool"]
        assert len(tool_results) == 2
        # bubble_spawn result mentions the bubble id
        spawn_result = next(m for m in tool_results if m.tool_call_id == "c_spawn")
        assert b.id in spawn_result.content
        assert "后台" not in spawn_result.content
        # other tool result notes it runs in main loop
        other_result = next(m for m in tool_results if m.tool_call_id == "c_other")
        assert "主线程" in other_result.content


    async def test_fresh_start_uses_only_pins(
        self, store, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
    ):
        from coworker.core.types import PinnedItem
        st = MagicMock()
        st.primary = [
            Message(role="user", content="長い会話"),
            Message(role="user", content="続き"),
        ]
        pin = PinnedItem(pin_id="p1", label="重要メモ", content="ピン内容")
        st.pinned_items = [pin]
        st.pinned_as_messages.return_value = [
            Message(role="user", content="[重要メモ]\nピン内容", pin_id="p1")
        ]
        mock_brain.think = AsyncMock(return_value=_make_response(content="done"))
        tool = self._make_tool(store, st, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path)
        result = await tool.execute(goal="fresh task", max_cycles=3, fresh_start=True)

        assert not result.is_error
        b = store.list_active()[0]
        # Only the pinned message, no conversation history
        assert len(b.forked_context) == 1
        assert b.forked_context[0].pin_id == "p1"
        assert "全新" in result.content

    async def test_fresh_start_empty_when_no_pins(
        self, store, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
    ):
        st = MagicMock()
        st.primary = [Message(role="user", content="history")]
        st.pinned_as_messages.return_value = []
        mock_brain.think = AsyncMock(return_value=_make_response(content="done"))
        tool = self._make_tool(store, st, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path)
        await tool.execute(goal="clean task", fresh_start=True)
        b = store.list_active()[0]
        assert len(b.forked_context) == 0


class TestBubbleSpawnPalaces:
    def _make_loaders(self, tmp_path):
        from coworker.palaces.loader import PalaceLoader
        from coworker.skills.loader import SkillLoader

        skills_dir = tmp_path / "skills"
        (skills_dir / "bug-create").mkdir(parents=True)
        (skills_dir / "bug-create" / "SKILL.md").write_text(
            "---\nname: bug-create\ndescription: 提单\n---\n提单的详细步骤正文。",
            encoding="utf-8",
        )
        skill_loader = SkillLoader(str(skills_dir))
        skill_loader.load_all()

        palaces_dir = tmp_path / "palaces"
        (palaces_dir / "product-bug").mkdir(parents=True)
        (palaces_dir / "product-bug" / "PALACE.md").write_text(
            "---\nname: product-bug\nwhen_to_attach: 反馈缺陷\n"
            "critical_skills: [bug-create]\nmemory_tags: [product, bug]\n---\n这是宫殿速记卡。",
            encoding="utf-8",
        )
        palace_loader = PalaceLoader(str(palaces_dir))
        palace_loader.load_all()
        return skill_loader, palace_loader

    def _make_tool(self, tmp_path, store, short_term, brain, registry, prompt_builder, inbox, long_term=None):
        skill_loader, palace_loader = self._make_loaders(tmp_path)
        return BubbleSpawnTool(
            store=store,
            short_term=short_term,
            parent_brain=brain,
            full_registry=registry,
            system_prompt_builder=prompt_builder,
            inbox=inbox,
            logs_dir=str(tmp_path),
            palace_loader=palace_loader,
            skill_loader=skill_loader,
            long_term=long_term,
        )

    async def test_unknown_palace_returns_error(
        self, store, mock_short_term, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
    ):
        tool = self._make_tool(tmp_path, store, mock_short_term, mock_brain, mock_registry, mock_prompt_builder, mock_inbox)
        result = await tool.execute(goal="g", palaces=["does-not-exist"], fresh_start=True)
        assert result.is_error
        assert "不存在" in result.content
        assert len(store.list_active()) == 0

    async def test_palace_card_and_critical_skill_injected(
        self, store, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
    ):
        st = MagicMock()
        st.pinned_as_messages.return_value = []
        mock_brain.think = AsyncMock(return_value=_make_response(content="done"))
        tool = self._make_tool(tmp_path, store, st, mock_brain, mock_registry, mock_prompt_builder, mock_inbox)
        result = await tool.execute(goal="提个 bug", palaces=["product-bug"], fresh_start=True)

        assert not result.is_error
        assert "已挂宫殿：product-bug" in result.content
        b = store.list_active()[0]
        texts = [m.content for m in b.forked_context if isinstance(m.content, str)]
        assert any("[宫殿:product-bug]" in t and "这是宫殿速记卡" in t for t in texts)
        assert any("skill:bug-create" in t and "提单的详细步骤正文" in t for t in texts)
        assert b.palace_tags == ["product", "bug"]

    async def test_spawn_sets_participant_and_palaces(
        self, store, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
    ):
        st = MagicMock()
        st.pinned_as_messages.return_value = []
        mock_brain.think = AsyncMock(return_value=_make_response(content="done"))
        tool = self._make_tool(tmp_path, store, st, mock_brain, mock_registry, mock_prompt_builder, mock_inbox)
        await tool.execute(goal="g", palaces=["product-bug"], participant_id="alice", fresh_start=True)
        b = store.list_active()[0]
        assert b.participant_id == "alice"
        assert b.palaces == ["product-bug"]

    async def test_tag_filtered_recall_injected(
        self, store, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
    ):
        st = MagicMock()
        st.pinned_as_messages.return_value = []
        mock_brain.think = AsyncMock(return_value=_make_response(content="done"))

        long_term = MagicMock()
        long_term._mem = object()  # not None → recall runs
        # query_by_tags does the tag filtering (tested in test_long_term); here it returns the matched set
        long_term.query_by_tags = AsyncMock(return_value=[
            {"id": "m1", "category": "experience", "content": "登录页 bug 复现要点", "tags": ["product", "bug"], "relevance": 0.9},
        ])
        tool = self._make_tool(tmp_path, store, st, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, long_term=long_term)
        await tool.execute(goal="提个 bug", palaces=["product-bug"], fresh_start=True)

        b = store.list_active()[0]
        recall_msgs = [m for m in b.forked_context if isinstance(m.content, str) and "[宫殿记忆]" in m.content]
        assert len(recall_msgs) == 1
        assert "登录页 bug 复现要点" in recall_msgs[0].content
        # query_by_tags was called with the palace's tags
        long_term.query_by_tags.assert_awaited_once()
        assert long_term.query_by_tags.await_args.args[1] == ["product", "bug"]

    async def test_palace_injection_summary_captured(
        self, store, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, tmp_path
    ):
        st = MagicMock()
        st.pinned_as_messages.return_value = []
        mock_brain.think = AsyncMock(return_value=_make_response(content="done"))
        long_term = MagicMock()
        long_term._mem = object()
        long_term.query_by_tags = AsyncMock(return_value=[
            {"id": "m1", "category": "experience", "content": "登录页 bug 复现要点",
             "tags": ["product", "bug"], "relevance": 0.9},
        ])
        tool = self._make_tool(tmp_path, store, st, mock_brain, mock_registry, mock_prompt_builder, mock_inbox, long_term=long_term)
        await tool.execute(goal="提个 bug", palaces=["product-bug"], fresh_start=True)

        b = store.list_active()[0]
        inj = b.palace_injection
        assert inj is not None
        assert inj["palaces"] == ["product-bug"]
        assert inj["tags"] == ["product", "bug"]
        assert inj["critical_skills"] == ["bug-create"]
        assert [m["id"] for m in inj["recalled"]] == ["m1"]


class TestPalaceWriteBack:
    async def test_write_back_on_done_with_tags(
        self, store, messages, mock_brain, mock_inbox, mock_registry, tmp_path
    ):
        long_term = MagicMock()
        long_term._mem = object()
        long_term.write = AsyncMock(return_value="new_id")

        done_tc = ToolCall(id="c1", name="bubble_done", arguments={"result": "bug 单已提交"})
        mock_brain.think = AsyncMock(return_value=_make_response(tool_calls=[done_tc], stop_reason="tool_use"))

        b = store.create("goal", messages, max_cycles=5)
        assert isinstance(b, Bubble)
        b.palace_tags = ["product", "bug"]
        loop = BubbleMiniLoop(
            bubble=b, brain=mock_brain, tool_registry=mock_registry, system_prompt="sys",
            bubble_store=store, inbox_watcher=mock_inbox, logs_dir=str(tmp_path), long_term=long_term,
        )
        await loop.run()

        assert b.status == "done"
        long_term.write.assert_awaited_once()
        kwargs = long_term.write.await_args.kwargs
        assert kwargs["tags"] == ["product", "bug"]
        assert kwargs["content"] == "bug 单已提交"

    async def test_no_write_back_without_palace_tags(
        self, store, messages, mock_brain, mock_inbox, mock_registry, tmp_path
    ):
        long_term = MagicMock()
        long_term._mem = object()
        long_term.write = AsyncMock()

        done_tc = ToolCall(id="c1", name="bubble_done", arguments={"result": "结论"})
        mock_brain.think = AsyncMock(return_value=_make_response(tool_calls=[done_tc], stop_reason="tool_use"))

        b = store.create("goal", messages, max_cycles=5)
        assert isinstance(b, Bubble)
        # no palace_tags → no write-back
        loop = BubbleMiniLoop(
            bubble=b, brain=mock_brain, tool_registry=mock_registry, system_prompt="sys",
            bubble_store=store, inbox_watcher=mock_inbox, logs_dir=str(tmp_path), long_term=long_term,
        )
        await loop.run()

        long_term.write.assert_not_awaited()


class TestPalaceLogging:
    async def test_injection_and_meta_logged(
        self, store, messages, mock_brain, mock_inbox, mock_registry, tmp_path
    ):
        done_tc = ToolCall(id="c1", name="bubble_done", arguments={"result": "ok"})
        mock_brain.think = AsyncMock(return_value=_make_response(tool_calls=[done_tc], stop_reason="tool_use"))

        b = store.create("goal", messages, max_cycles=5)
        assert isinstance(b, Bubble)
        b.participant_id = "alice"
        b.palaces = ["product-bug"]
        b.palace_tags = ["product", "bug"]
        b.palace_injection = {
            "palaces": ["product-bug"], "tags": ["product", "bug"],
            "critical_skills": ["bug-create"], "related_skills": ["issue-tracker"],
            "recalled": [{"id": "m1", "category": "experience", "content": "复现要点", "relevance": 0.9}],
        }
        loop = BubbleMiniLoop(
            bubble=b, brain=mock_brain, tool_registry=mock_registry, system_prompt="sys",
            bubble_store=store, inbox_watcher=mock_inbox, logs_dir=str(tmp_path),
        )
        await loop.run()

        log_path = Path(tmp_path) / "bubbles" / f"{b.id}.jsonl"
        entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]

        inj = [e for e in entries if e.get("type") == "palace_injection"]
        assert len(inj) == 1
        assert inj[0]["palaces"] == ["product-bug"]
        assert inj[0]["critical_skills"] == ["bug-create"]
        assert inj[0]["recalled"][0]["id"] == "m1"

        meta = [e for e in entries if e.get("__meta__")]
        assert len(meta) == 1
        assert meta[0]["palaces"] == ["product-bug"]
        assert meta[0]["participant_id"] == "alice"
        assert meta[0]["palace_tags"] == ["product", "bug"]
        assert meta[0]["provider"] == ""
        assert meta[0]["model"] == ""


class TestBubbleCheckTool:
    async def test_check_existing(self, store, messages):
        b = store.create("目标A", messages, 5)
        assert isinstance(b, Bubble)
        b.cycles_used = 2
        tool = BubbleCheckTool(store)
        result = await tool.execute(bubble_id=b.id)
        assert not result.is_error
        assert b.id in result.content
        assert "running" in result.content

    async def test_check_with_result_preview(self, store, messages):
        b = store.create("g", messages, 5)
        assert isinstance(b, Bubble)
        b.result = "A" * 600
        store.mark_done(b)
        b.status = "done"
        tool = BubbleCheckTool(store)
        result = await tool.execute(bubble_id=b.id)
        assert "..." in result.content

    async def test_check_unknown_id(self, store):
        tool = BubbleCheckTool(store)
        result = await tool.execute(bubble_id="bbl_missing")
        assert result.is_error


class TestBubbleSendTool:
    async def test_send_to_bubble(self, store, messages, mock_inbox):
        b = store.create("g", messages, 5)
        assert isinstance(b, Bubble)
        tool = BubbleSendTool(store, mock_inbox)
        result = await tool.execute(target=b.id, message="hello")
        assert not result.is_error
        assert not b.inbox.empty()
        sender, text = b.inbox.get_nowait()
        assert sender == "主线"
        assert text == "hello"

    async def test_send_to_main(self, store, mock_inbox):
        tool = BubbleSendTool(store, mock_inbox)
        result = await tool.execute(target="main", message="通知主线")
        assert not result.is_error
        mock_inbox.push.assert_called_once()

    async def test_send_to_unknown_bubble(self, store, mock_inbox):
        tool = BubbleSendTool(store, mock_inbox)
        result = await tool.execute(target="bbl_missing", message="hi")
        assert result.is_error

    async def test_send_to_terminal_bubble(self, store, messages, mock_inbox):
        b = store.create("g", messages, 5)
        assert isinstance(b, Bubble)
        b.status = "done"
        store.mark_done(b)
        tool = BubbleSendTool(store, mock_inbox)
        result = await tool.execute(target=b.id, message="hi")
        assert result.is_error


class TestBubbleCancelTool:
    async def test_cancel_running(self, store, messages):
        b = store.create("g", messages, 5)
        assert isinstance(b, Bubble)
        mock_task = MagicMock()
        mock_task.done.return_value = False
        b.task = mock_task
        tool = BubbleCancelTool(store)
        result = await tool.execute(bubble_id=b.id)
        assert not result.is_error
        mock_task.cancel.assert_called_once()

    async def test_cancel_already_done(self, store, messages):
        b = store.create("g", messages, 5)
        assert isinstance(b, Bubble)
        b.status = "done"
        store.mark_done(b)
        tool = BubbleCancelTool(store)
        result = await tool.execute(bubble_id=b.id)
        assert not result.is_error
        assert "终态" in result.content

    async def test_cancel_unknown(self, store):
        tool = BubbleCancelTool(store)
        result = await tool.execute(bubble_id="bbl_ghost")
        assert result.is_error


class TestBubbleListTool:
    async def test_list_empty(self, store):
        tool = BubbleListTool(store)
        result = await tool.execute()
        assert "没有活跃的泡泡" in result.content

    async def test_list_active(self, store, messages):
        b1 = store.create("task A", messages, 5)
        b2 = store.create("task B", messages, 5)
        assert isinstance(b1, Bubble) and isinstance(b2, Bubble)
        tool = BubbleListTool(store)
        result = await tool.execute()
        assert b1.id in result.content
        assert b2.id in result.content
        assert "task A" in result.content

    async def test_list_shows_participant_and_palaces(self, store, messages):
        b = store.create("提单", messages, 5)
        assert isinstance(b, Bubble)
        b.participant_id = "alice"
        b.palaces = ["product-bug"]
        result = await BubbleListTool(store).execute()
        assert "alice" in result.content
        assert "product-bug" in result.content


class TestBubbleCheckMetadata:
    async def test_check_shows_participant_and_palaces(self, store, messages):
        b = store.create("提单", messages, 5)
        assert isinstance(b, Bubble)
        b.participant_id = "alice"
        b.palaces = ["product-bug"]
        result = await BubbleCheckTool(store).execute(bubble_id=b.id)
        assert "alice" in result.content
        assert "product-bug" in result.content

    async def test_check_and_list_show_model(self, store, messages):
        b = store.create("提单", messages, 5, provider="mock", model="mock-model")
        assert isinstance(b, Bubble)
        check_result = await BubbleCheckTool(store).execute(bubble_id=b.id)
        list_result = await BubbleListTool(store).execute()
        assert "模型：mock/mock-model" in check_result.content
        assert "模型=mock/mock-model" in list_result.content


# ---------------------------------------------------------------------------
# Ephemeral task store (bubble-local, does not touch shared TaskStore)
# ---------------------------------------------------------------------------


class TestToolScope:
    async def test_task_create_uses_scope_store_not_shared(self, tmp_path):
        """fork() wires tool to bubble's task_store; shared store untouched."""
        from coworker.core.tool_scope import ToolScope
        from coworker.tools.code_tools import BackgroundJobStore
        from coworker.tools.reasoning_tools import TaskCreateTool, TaskStore

        shared = TaskStore(str(tmp_path / "shared.json"))
        bubble_store = TaskStore(store_path=None)
        scope = ToolScope(task_store=bubble_store, job_store=BackgroundJobStore(), inbox=None)

        forked = TaskCreateTool(shared).fork(scope)
        await forked.execute(description="泡泡子任务")

        assert shared.list() == []
        assert len(bubble_store.list()) == 1
        assert bubble_store.list()[0].description == "泡泡子任务"

    async def test_task_crud_via_scope(self, tmp_path):
        """Full create/get/list/update cycle through forked tools."""
        from coworker.core.tool_scope import ToolScope
        from coworker.tools.code_tools import BackgroundJobStore
        from coworker.tools.reasoning_tools import (
            TaskCreateTool,
            TaskGetTool,
            TaskListTool,
            TaskStore,
            TaskUpdateTool,
        )

        shared = TaskStore(store_path=None)
        bubble_store = TaskStore(store_path=None)
        scope = ToolScope(task_store=bubble_store, job_store=BackgroundJobStore(), inbox=None)

        result = await TaskCreateTool(shared).fork(scope).execute(description="研究X")
        task_id = result.content.split("]")[0].strip("[")

        list_result = await TaskListTool(shared).fork(scope).execute()
        assert "研究X" in list_result.content

        update_result = await TaskUpdateTool(shared).fork(scope).execute(task_id=task_id, status="in_progress")
        assert "in_progress" in update_result.content

        get_result = await TaskGetTool(shared).fork(scope).execute(task_id=task_id)
        assert "in_progress" in get_result.content

    async def test_bubble_loop_scope_isolates_tasks(
        self, store, messages, mock_brain, mock_inbox, tmp_path
    ):
        """BubbleMiniLoop creates a scoped task store; task_create does not touch shared store."""
        from coworker.tools.reasoning_tools import TaskCreateTool, TaskListTool, TaskStore
        from coworker.tools.registry import ToolRegistry

        shared = TaskStore(str(tmp_path / "shared_tasks.json"))
        registry = ToolRegistry()
        registry.register(TaskCreateTool(shared))
        registry.register(TaskListTool(shared))

        tc_create = ToolCall(id="c1", name="task_create", arguments={"description": "泡泡子任务"})
        tc_done = ToolCall(id="c2", name="bubble_done", arguments={"result": "完成"})
        mock_brain.think = AsyncMock(side_effect=[
            _make_response(tool_calls=[tc_create], stop_reason="tool_use"),
            _make_response(tool_calls=[tc_done], stop_reason="tool_use"),
        ])

        b = store.create("goal", messages, max_cycles=5)
        assert isinstance(b, Bubble)
        loop = _make_mini_loop(b, mock_brain, registry, mock_inbox, store, tmp_path)
        await loop.run()

        assert shared.list() == [], "bubble tasks must not appear in shared TaskStore"

    async def test_registry_scoped_forks_tools(self, tmp_path):
        """ToolRegistry.scoped() forks scope-sensitive tools, leaves others unchanged."""
        from coworker.core.tool_scope import ToolScope
        from coworker.tools.code_tools import BackgroundJobStore
        from coworker.tools.file_tools import ReadFileTool
        from coworker.tools.reasoning_tools import TaskCreateTool, TaskStore
        from coworker.tools.registry import ToolRegistry

        shared_task = TaskStore(store_path=None)
        bubble_task = TaskStore(store_path=None)
        scope = ToolScope(task_store=bubble_task, job_store=BackgroundJobStore(), inbox=None)

        registry = ToolRegistry()
        original_task_tool = TaskCreateTool(shared_task)
        read_tool = ReadFileTool()
        registry.register(original_task_tool)
        registry.register(read_tool)

        scoped = registry.scoped(scope)

        # task_create is forked to bubble_task
        forked_task = scoped._tools["task_create"]
        assert forked_task is not original_task_tool
        assert forked_task._store is bubble_task

        # read_file is stateless, returns self
        assert scoped._tools["read_file"] is read_tool


# ---------------------------------------------------------------------------
# WriteFileTool per-path lock
# ---------------------------------------------------------------------------


class TestWriteFileToolLock:
    async def test_concurrent_writes_to_same_path_are_serialized(self, tmp_path):
        tool = WriteFileTool()
        path = str(tmp_path / "shared.txt")
        order = []

        async def write(content):
            order.append(f"start-{content}")
            await tool.execute(path=path, content=content)
            order.append(f"end-{content}")

        await asyncio.gather(write("A"), write("B"))
        # Each write should start and end without interleaving with the other
        # i.e., we should NOT see start-A, start-B, end-A, end-B
        assert order.index("end-A") < order.index("start-B") or \
               order.index("end-B") < order.index("start-A")

    async def test_write_creates_file(self, tmp_path):
        tool = WriteFileTool()
        path = str(tmp_path / "new.txt")
        result = await tool.execute(path=path, content="hello")
        assert not result.is_error
        assert Path(path).read_text() == "hello"

    async def test_patch_respects_lock(self, tmp_path):
        tool = WriteFileTool()
        path = str(tmp_path / "patch.txt")
        Path(path).write_text("old content")
        result = await tool.execute(path=path, old_string="old", new_string="new")
        assert not result.is_error
        assert Path(path).read_text() == "new content"


# ---------------------------------------------------------------------------
# Tool fork() behaviour for bubble-scoped resources
# ---------------------------------------------------------------------------


class TestToolForkBubbleScope:
    async def test_bubble_communicate_limits_delivery_to_bound_recipient(self, tmp_path):
        from coworker.core.types import CommunicateRequest, ToolResult
        from coworker.tools.communicate_tool import CommunicateTool

        seen: list[CommunicateRequest] = []

        async def sender(request: CommunicateRequest):
            seen.append(request)
            return ToolResult(tool_call_id="", content="sent")

        channel_system = create_channel_system(tmp_path / "outbox")
        tool = CommunicateTool(channel_system.registry)
        channel_system.registry.register(
            BaseChannel.from_sender(
                "wecom:",
                sender,
                capabilities=ChannelCapabilities(conversation_id=True),
            )
        )
        bubble = Bubble(
            id="bbl_bound",
            goal="reply",
            participant_id="wecom:alice",
            conversation_id="conv-1",
        )
        bubble_tool = BubbleCommunicateTool.from_tool(
            tool,
            bubble,
            BubbleHandoffNotifier(tool),
        )

        result = await bubble_tool.execute(message="已处理")
        rejected = await bubble_tool.execute(
            participant_id="wecom:bob",
            conversation_id="conv-2",
            message="不应发送",
            extra=["invalid"],
        )
        with locale_context("en"):
            rejected_en = await bubble_tool.execute(
                participant_id="wecom:bob",
                conversation_id="conv-2",
                message="must not send",
                extra=["invalid"],
            )

        assert not result.is_error
        assert rejected.is_error
        assert "存在以下问题" in rejected.content
        assert "不能改用其他 participant_id" in rejected.content
        assert "只能向已绑定的 conversation_id" in rejected.content
        assert "extra 必须是对象" in rejected.content
        assert "participant_id='wecom:alice', conversation_id='conv-1'" in rejected.content
        assert rejected_en.is_error
        assert "arguments are invalid" in rejected_en.content
        assert "participant_id='wecom:alice', conversation_id='conv-1'" in rejected_en.content
        assert bubble_tool.definition.to_schema() == tool.definition.to_schema()
        assert seen == [
            CommunicateRequest(
                participant_id="wecom:alice",
                message="已处理",
                conversation_id="conv-1",
            )
        ]

    def test_sleep_fork_returns_no_inbox(self):
        from unittest.mock import MagicMock

        from coworker.core.tool_scope import ToolScope
        from coworker.tools.code_tools import BackgroundJobStore
        from coworker.tools.reasoning_tools import TaskStore
        from coworker.tools.system_tools import SleepTool

        inbox = MagicMock()
        tool = SleepTool(inbox_watcher=inbox)
        scope = ToolScope(task_store=TaskStore(store_path=None), job_store=BackgroundJobStore(), inbox=None)

        forked = tool.fork(scope)
        assert forked is not tool
        assert forked._inbox is None

    async def test_sleep_fork_sleeps_without_wakeup(self):
        from coworker.tools.system_tools import SleepTool

        tool = SleepTool(inbox_watcher=None)
        result = await tool.execute(seconds=0)
        assert not result.is_error

    def test_get_context_fork_uses_scope_brain(self):
        from coworker.core.tool_scope import ToolScope
        from coworker.tools.code_tools import BackgroundJobStore
        from coworker.tools.reasoning_tools import TaskStore
        from coworker.tools.system_tools import GetContextTool

        main_brain = MagicMock()
        bubble_brain = MagicMock()
        bubble_brain.current_provider_name = "bubble_provider"
        bubble_brain.current_model = "bubble_model"

        tool = GetContextTool(main_brain, MagicMock(), MagicMock())
        scope = ToolScope(
            task_store=TaskStore(store_path=None),
            job_store=BackgroundJobStore(),
            inbox=None,
            brain=bubble_brain,
        )
        forked = tool.fork(scope)
        assert forked._brain is bubble_brain

    def test_manage_pinned_fork_uses_scope_short_term(self):
        from coworker.core.tool_scope import ToolScope
        from coworker.memory.short_term import ShortTermMemory
        from coworker.tools.code_tools import BackgroundJobStore
        from coworker.tools.pinned_context_tool import ManagePinnedContextTool
        from coworker.tools.reasoning_tools import TaskStore

        main_st = MagicMock()
        tool = ManagePinnedContextTool(main_st)
        bubble_stm = ShortTermMemory()
        scope = ToolScope(
            task_store=TaskStore(store_path=None),
            job_store=BackgroundJobStore(),
            inbox=None,
            short_term=bubble_stm,
        )
        forked = tool.fork(scope)
        assert forked is not tool
        assert forked._short_term is bubble_stm

    def test_manage_pinned_fork_no_scope_short_term_returns_self(self):
        from coworker.core.tool_scope import ToolScope
        from coworker.tools.code_tools import BackgroundJobStore
        from coworker.tools.pinned_context_tool import ManagePinnedContextTool
        from coworker.tools.reasoning_tools import TaskStore

        tool = ManagePinnedContextTool(MagicMock())
        scope = ToolScope(
            task_store=TaskStore(store_path=None),
            job_store=BackgroundJobStore(),
            inbox=None,
        )
        assert tool.fork(scope) is tool

    async def test_bubble_loop_pinned_items_injected(
        self, store, messages, mock_brain, mock_inbox, tmp_path
    ):
        """manage_pinned_context pin injects into bubble's own ShortTermMemory; main stm untouched."""
        from coworker.memory.short_term import ShortTermMemory
        from coworker.tools.pinned_context_tool import ManagePinnedContextTool
        from coworker.tools.registry import ToolRegistry

        main_stm = ShortTermMemory()
        registry = ToolRegistry()
        registry.register(ManagePinnedContextTool(main_stm))

        captured: list[list] = []

        async def capture_think(messages, system_prompt, tools):
            captured.append(list(messages))
            if len(captured) == 1:
                return _make_response(
                    tool_calls=[ToolCall(
                        id="p1", name="manage_pinned_context",
                        arguments={"action": "pin", "pin_id": "note1", "label": "Note", "content": "记住这个"},
                    )],
                    stop_reason="tool_use",
                )
            return _make_response(content="done")

        mock_brain.think = capture_think
        b = store.create("goal", messages, max_cycles=5)
        assert isinstance(b, Bubble)
        loop = _make_mini_loop(b, mock_brain, registry, mock_inbox, store, tmp_path)
        await loop.run()

        second_call_msgs = captured[1]
        texts = [m.content for m in second_call_msgs if isinstance(m.content, str)]
        assert any("记住这个" in t for t in texts)
        assert main_stm.pinned_items == []
