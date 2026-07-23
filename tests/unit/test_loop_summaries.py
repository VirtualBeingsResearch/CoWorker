from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import coworker.agent.loop as loop_module
from coworker.agent.loop import AgentLoop
from coworker.core.types import IncomingEvent, LLMResponse, Message, ToolCall
from coworker.memory.short_term import ShortTermMemory


def _make_loop(brain, mem, events=None):
    """Build a minimal AgentLoop-like namespace for _cycle() testing."""
    inbox = MagicMock()
    inbox.get_pending = AsyncMock(return_value=events or [])
    inbox.push = AsyncMock()
    inbox.message_event = AsyncMock()

    identity = MagicMock()
    identity.load = MagicMock()
    identity.name = "Sora"

    prompt_builder = MagicMock()
    prompt_builder.build = MagicMock(return_value="system prompt")
    prompt_builder.refresh = MagicMock()
    prompt_builder.consume_skill_load_warnings = MagicMock(return_value=[])

    config = MagicMock()
    config.agent.idle_sleep_seconds = 0
    config.agent.inbox_batch_max = 10

    state = MagicMock()
    state.tick = False
    state.setup_mode = False
    state.cycle_count = 0
    state.current_provider = brain.current_provider_name
    state.current_model = brain.current_model

    loop = AgentLoop.__new__(AgentLoop)
    loop._brain = brain
    loop._short_term = mem
    lt = MagicMock()
    lt._mem = None  # disable auto-recall in unit tests
    loop._long_term = lt
    loop._tools = MagicMock()
    loop._tools.get_schemas = MagicMock(return_value=[])
    loop._identity = identity
    loop._prompt_builder = prompt_builder
    loop._inbox = inbox
    loop._config = config
    loop._ilog = None
    loop._snapshot_path = None
    loop._stop_event = MagicMock()
    loop._stop_event.is_set = MagicMock(return_value=False)
    loop.state = state
    loop._task_store = None
    loop._task_reminder_interval = 10
    loop._task_reminder_seconds = 300.0
    loop._last_task_reminder_cycle = 0
    loop._last_task_reminder_time = 0.0
    loop._bubble_store = None
    loop._subconscious = None
    loop._recent_activity = None
    loop._last_compress_generation = getattr(mem, "compress_generation", 0)
    return loop


@pytest.mark.asyncio
async def test_setup_mode_waits_without_consuming_inbox_or_calling_model():
    mem = ShortTermMemory()
    brain = _make_brain()
    loop = _make_loop(brain, mem, events=[IncomingEvent(participant_id="alice", content="hi")])
    loop.state.setup_mode = True
    loop._rest = AsyncMock()

    await loop._cycle()

    loop._rest.assert_awaited_once()
    loop._inbox.get_pending.assert_not_awaited()
    brain.think.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_restart_wakes_waiter_without_saving_snapshot(tmp_path):
    mem = ShortTermMemory()
    brain = _make_brain()
    loop = _make_loop(brain, mem)
    loop._stop_event = asyncio.Event()
    loop._inbox.message_event = asyncio.Event()
    loop._snapshot_path = tmp_path / "short_term_snapshot.json"
    loop.state.setup_mode = True
    loop.state.restart_requested = False
    loop.state.restart_reason = ""

    waiter = asyncio.create_task(loop.wait_until_stopped())
    await asyncio.sleep(0)
    assert not waiter.done()

    loop.request_restart(reason="bootstrap")
    await waiter

    assert loop.state.restart_requested is True
    assert loop.state.restart_reason == "bootstrap"
    assert not loop._snapshot_path.exists()


def _make_brain(content="ok", tool_calls=None, stop_reason="end_turn", usage=None):
    response = LLMResponse(
        content=content,
        tool_calls=tool_calls or [],
        stop_reason=stop_reason,
        model="mock-model",
        usage=usage or {},
    )
    brain = MagicMock()
    brain.think = AsyncMock(return_value=response)
    brain.current_provider_name = "mock"
    brain.current_model = "mock-model"
    brain.summarize = AsyncMock(return_value="s")
    return brain


@pytest.mark.asyncio
async def test_cycle_records_latest_main_response_input_tokens():
    mem = ShortTermMemory()
    brain = _make_brain(usage={"input_tokens": 321, "output_tokens": 12})
    loop = _make_loop(brain, mem)
    loop._short_term.compress_if_needed = AsyncMock()

    await loop._cycle()

    assert loop.state.last_main_response_usage["input_tokens"] == 321
    assert loop.state.last_main_response_usage["provider"] == "mock"
    assert loop.state.last_main_response_usage["model"] == "mock-model"
    assert loop.state.last_main_response_usage["measured_at"]
    assert mem.primary[-1].usage == {"input_tokens": 321, "output_tokens": 12}


@pytest.mark.asyncio
async def test_user_event_appended_to_primary():
    mem = ShortTermMemory()
    brain = _make_brain()
    event = IncomingEvent(participant_id="alice", content="hello", source="wecom")
    loop = _make_loop(brain, mem, events=[event])
    loop._short_term.compress_if_needed = AsyncMock()

    await loop._cycle()

    user_msgs = [m for m in mem.primary if m.role == "user"]
    assert len(user_msgs) == 1
    assert user_msgs[0].content == "[来自企业微信][alice]的消息:\nhello"
    assert user_msgs[0].source == "wecom"


@pytest.mark.asyncio
async def test_prompt_refreshed_only_after_compression():
    mem = ShortTermMemory()
    brain = _make_brain()
    loop = _make_loop(brain, mem)
    loop._short_term.compress_if_needed = AsyncMock()

    # 无压缩发生：generation 未变，不应刷新系统提示词输入
    await loop._cycle()
    loop._prompt_builder.refresh.assert_not_called()

    # 模拟一次实际压缩使 generation 自增
    mem.compress_generation += 1
    await loop._cycle()
    loop._prompt_builder.refresh.assert_called_once()


@pytest.mark.asyncio
async def test_recent_activity_syncs_only_after_compression_generation_changes():
    mem = ShortTermMemory()
    brain = _make_brain()
    loop = _make_loop(brain, mem)
    loop._short_term.compress_if_needed = AsyncMock()
    recent = MagicMock()
    recent.schedule_sync_compressed_from_log = MagicMock()
    loop._recent_activity = recent

    await loop._cycle()
    recent.schedule_sync_compressed_from_log.assert_not_called()

    mem.compress_generation += 1
    await loop._cycle()

    recent.schedule_sync_compressed_from_log.assert_called_once_with(mem.raw_primary_boundary())

    # 再次无压缩：不应重复刷新
    await loop._cycle()
    loop._prompt_builder.refresh.assert_called_once()


@pytest.mark.asyncio
async def test_assistant_response_appended_to_primary():
    mem = ShortTermMemory()
    brain = _make_brain(content="my reply")
    event = IncomingEvent(participant_id="alice", content="hi")
    loop = _make_loop(brain, mem, events=[event])
    loop._short_term.compress_if_needed = AsyncMock()

    await loop._cycle()

    asst_msgs = [m for m in mem.primary if m.role == "assistant"]
    assert len(asst_msgs) == 1
    assert asst_msgs[0].content == "my reply"


def _make_rest_loop(*, passive: bool, idle_sleep_seconds: int = 0) -> AgentLoop:
    """构造仅满足 _rest() 依赖的最小 AgentLoop。"""
    loop = AgentLoop.__new__(AgentLoop)
    loop.state = MagicMock()
    loop.state.is_sleeping = False
    loop._config = MagicMock()
    loop._config.agent.passive_mode = passive
    loop._config.agent.idle_sleep_seconds = idle_sleep_seconds
    return loop


@pytest.mark.asyncio
async def test_rest_passive_waits_for_event_without_timeout(monkeypatch):
    """passive 模式：_rest() 无超时，等到 message_event 才返回。"""
    loop = _make_rest_loop(passive=True, idle_sleep_seconds=999)
    event = asyncio.Event()
    loop._inbox = MagicMock()
    loop._inbox.message_event = event
    log = MagicMock()
    monkeypatch.setattr(loop_module, "logger", log)

    async def set_event_soon():
        await asyncio.sleep(0.05)
        event.set()

    asyncio.create_task(set_event_soon())
    await asyncio.wait_for(loop._rest(), timeout=5.0)
    assert loop.state.is_sleeping is False
    messages = [call.args[0] for call in log.info.call_args_list]
    assert messages == [
        "Agent entering passive rest; waiting for an external event",
        "Agent woke from passive rest after an external event",
    ]


@pytest.mark.asyncio
async def test_rest_passive_does_not_idle_timeout():
    """passive 模式：event 未 set 时 _rest() 不会因 idle_sleep_seconds 超时而返回。"""
    loop = _make_rest_loop(passive=True, idle_sleep_seconds=0)  # 若走超时路径会立即返回
    event = asyncio.Event()
    loop._inbox = MagicMock()
    loop._inbox.message_event = event

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(loop._rest(), timeout=0.2)


@pytest.mark.asyncio
async def test_rest_active_uses_idle_sleep_timeout(monkeypatch):
    """active 模式：_rest() 走 idle_sleep_seconds 超时路径，idle_sleep=0 立即返回不挂起。"""
    loop = _make_rest_loop(passive=False, idle_sleep_seconds=0)
    event = asyncio.Event()  # 不 set
    loop._inbox = MagicMock()
    loop._inbox.message_event = event
    log = MagicMock()
    monkeypatch.setattr(loop_module, "logger", log)

    await loop._rest()  # idle_sleep=0 -> 立即超时返回
    assert loop.state.is_sleeping is False
    messages = [call.args[0] for call in log.info.call_args_list]
    assert messages == [
        "Agent entering rest for 0s",
        "Agent rest timed out after 0s",
    ]


@pytest.mark.asyncio
async def test_tool_results_appended_to_primary():
    mem = ShortTermMemory()
    tc = ToolCall(id="t1", name="sleep", arguments={"seconds": 1})
    brain = _make_brain(tool_calls=[tc], stop_reason="tool_use")

    tool_result = MagicMock()
    tool_result.content = "slept"
    tool_result.content_blocks = None
    tool_result.is_error = False
    tool_result.recalled_memory_ids = []
    tools = MagicMock()
    tools.get_schemas = MagicMock(return_value=[])
    tools.execute = AsyncMock(return_value=tool_result)

    loop = _make_loop(brain, mem, events=[IncomingEvent(participant_id="alice", content="do it")])
    loop._tools = tools
    loop._short_term.compress_if_needed = AsyncMock()

    await loop._cycle()

    tool_msgs = [m for m in mem.primary if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].content == "slept"


@pytest.mark.asyncio
async def test_compress_called_when_idle():
    mem = ShortTermMemory()
    brain = _make_brain()
    loop = _make_loop(brain, mem, events=[])
    loop._short_term.compress_if_needed = AsyncMock()

    await loop._cycle()

    loop._short_term.compress_if_needed.assert_awaited_once()


@pytest.mark.asyncio
async def test_compress_not_called_when_event_received():
    mem = ShortTermMemory()
    brain = _make_brain()
    event = IncomingEvent(participant_id="alice", content="hi")
    loop = _make_loop(brain, mem, events=[event])
    loop._short_term.compress_if_needed = AsyncMock()

    await loop._cycle()

    loop._short_term.compress_if_needed.assert_not_awaited()


@pytest.mark.asyncio
async def test_extra_events_pushed_back():
    mem = ShortTermMemory()
    brain = _make_brain()
    ev1 = IncomingEvent(participant_id="alice", content="first")
    ev2 = IncomingEvent(participant_id="bob", content="second")

    inbox = MagicMock()
    inbox.get_pending = AsyncMock(return_value=[ev1, ev2])
    inbox.push = AsyncMock()
    inbox.message_event = AsyncMock()

    loop = _make_loop(brain, mem)
    loop._inbox = inbox
    loop._config.agent.inbox_batch_max = 1  # 只处理第一条，其余入队
    loop._short_term.compress_if_needed = AsyncMock()

    await loop._cycle()

    loop._inbox.push.assert_awaited_once_with(ev2)
    # only first event's message in primary
    user_msgs = [m for m in mem.primary if m.role == "user"]
    assert len(user_msgs) == 1
    assert "alice" in user_msgs[0].content


@pytest.mark.asyncio
async def test_tick_not_injected_after_tool_use():
    mem = ShortTermMemory()
    mem.primary.append(
        Message(role="assistant", content="", stop_reason="tool_use")
    )
    brain = _make_brain()
    loop = _make_loop(brain, mem, events=[])
    loop.state.tick = True
    loop._short_term.compress_if_needed = AsyncMock()

    await loop._cycle()

    tick_msgs = [m for m in mem.primary if m.role == "user"]
    assert not tick_msgs


@pytest.mark.asyncio
async def test_tick_injected_after_end_turn():
    from coworker.core.constants import TICK_TAG
    mem = ShortTermMemory()
    mem.primary.append(
        Message(role="assistant", content="done", stop_reason="end_turn")
    )
    brain = _make_brain()
    loop = _make_loop(brain, mem, events=[])
    loop.state.tick = True
    loop._short_term.compress_if_needed = AsyncMock()

    await loop._cycle()

    tick_msgs = [m for m in mem.primary if m.role == "user" and TICK_TAG in m.content]
    assert len(tick_msgs) == 1


@pytest.mark.asyncio
async def test_tick_suppressed_when_pins_reinjected():
    """pin 被压缩后重注入时，tick 不应同时触发。"""
    from coworker.core.constants import TICK_TAG
    mem = ShortTermMemory()
    mem.primary.append(Message(role="assistant", content="done", stop_reason="end_turn"))
    # pin() 不立即写 primary，reinject_missing_pins() 会在 cycle 开头补入
    mem.pin("rules", "规范", "不要用 print")

    brain = _make_brain()
    loop = _make_loop(brain, mem, events=[])
    loop.state.tick = True
    loop._short_term.compress_if_needed = AsyncMock()

    await loop._cycle()

    tick_msgs = [m for m in mem.primary if m.role == "user" and TICK_TAG in str(m.content)]
    assert len(tick_msgs) == 0
    # pin 应已重注入
    pin_msgs = [m for m in mem.primary if m.pin_id == "rules"]
    assert len(pin_msgs) == 1


@pytest.mark.asyncio
async def test_tick_fires_normally_when_no_pins_reinjected():
    """没有 pin 需要重注入时，tick 正常触发。"""
    from coworker.core.constants import TICK_TAG
    mem = ShortTermMemory()
    mem.primary.append(Message(role="assistant", content="done", stop_reason="end_turn"))

    brain = _make_brain()
    loop = _make_loop(brain, mem, events=[])
    loop.state.tick = True
    loop._short_term.compress_if_needed = AsyncMock()

    await loop._cycle()

    tick_msgs = [m for m in mem.primary if m.role == "user" and TICK_TAG in str(m.content)]
    assert len(tick_msgs) == 1


@pytest.mark.asyncio
async def test_auto_recall_skips_empty_query():
    """纯附件消息（空文本）不触发自动回忆。"""
    mem = ShortTermMemory()
    brain = _make_brain()
    event = IncomingEvent(participant_id="alice", content="")
    loop = _make_loop(brain, mem, events=[event])
    loop._short_term.compress_if_needed = AsyncMock()

    await loop._cycle()

    # 不应有 [自动回忆] 消息
    assert not any("[自动回忆]" in str(m.content) for m in mem.primary)


@pytest.mark.asyncio
async def test_same_user_events_batched_into_one_message():
    """同一用户的多条消息合并成一个 user message。"""
    mem = ShortTermMemory()
    brain = _make_brain()
    ev1 = IncomingEvent(participant_id="alice", content="第一条")
    ev2 = IncomingEvent(participant_id="alice", content="第二条")

    inbox = MagicMock()
    inbox.get_pending = AsyncMock(return_value=[ev1, ev2])
    inbox.push = AsyncMock()
    inbox.message_event = AsyncMock()

    loop = _make_loop(brain, mem)
    loop._inbox = inbox
    loop._short_term.compress_if_needed = AsyncMock()

    await loop._cycle()

    # 两条同用户消息不应再入队
    inbox.push.assert_not_awaited()

    user_msgs = [m for m in mem.primary if m.role == "user"]
    assert len(user_msgs) == 1
    content = user_msgs[0].content
    # 合并后是 blocks 格式
    assert isinstance(content, list)
    texts = [b["text"] for b in content if b["type"] == "text"]
    assert any("第一条" in t for t in texts)
    assert any("第二条" in t for t in texts)


@pytest.mark.asyncio
async def test_mixed_users_batched_together():
    """不同用户的消息在 inbox_batch_max 内一起合并，不再按用户拆分。"""
    mem = ShortTermMemory()
    brain = _make_brain()
    ev_alice1 = IncomingEvent(participant_id="alice", content="alice 第一")
    ev_bob = IncomingEvent(participant_id="bob", content="bob 消息")
    ev_alice2 = IncomingEvent(participant_id="alice", content="alice 第二")

    inbox = MagicMock()
    inbox.get_pending = AsyncMock(return_value=[ev_alice1, ev_bob, ev_alice2])
    inbox.push = AsyncMock()
    inbox.message_event = AsyncMock()

    loop = _make_loop(brain, mem)
    loop._inbox = inbox
    loop._short_term.compress_if_needed = AsyncMock()

    await loop._cycle()

    # 都在 batch 上限内，无需重新入队
    inbox.push.assert_not_awaited()

    user_msgs = [m for m in mem.primary if m.role == "user"]
    assert len(user_msgs) == 1
    content = user_msgs[0].content
    # 三条消息（含 bob）都被合并进同一条 user message
    assert isinstance(content, list)
    texts = [b["text"] for b in content if b["type"] == "text"]
    assert any("alice 第一" in t for t in texts)
    assert any("bob 消息" in t for t in texts)
    assert any("alice 第二" in t for t in texts)


@pytest.mark.asyncio
async def test_auto_recall_injects_and_deduplicates():
    """自动回忆注入记忆，第二次不重复注入相同 ID。"""

    mem = ShortTermMemory()
    brain = _make_brain()

    fake_memory = {
        "id": "mem-001",
        "content": "用户偏好 Python",
        "category": "knowledge",
        "tags": [],
        "timestamp": "",
        "relevance": 0.9,
    }

    loop = _make_loop(brain, mem, events=[IncomingEvent(participant_id="alice", content="Python")])
    loop._short_term.compress_if_needed = AsyncMock()

    # 激活 auto_recall：给 _mem 设一个非 None 值，query 返回 fake_memory
    loop._long_term._mem = MagicMock()
    loop._long_term.query = AsyncMock(return_value=[fake_memory])
    loop._config.memory.auto_recall_enabled = True
    loop._config.memory.auto_recall_relevance_threshold = 0.5
    loop._config.memory.auto_recall_limit = 5

    await loop._cycle()

    recall_msgs = [m for m in mem.primary if "[自动回忆]" in str(m.content)]
    assert len(recall_msgs) == 1
    assert "mem-001" in recall_msgs[0].recalled_memory_ids
    assert recall_msgs[0].source == "auto_recall"

    # 第二轮：相同 ID 已在 primary，不应再注入
    brain2 = _make_brain()
    event2 = IncomingEvent(participant_id="alice", content="Python 再说一遍")
    loop._inbox.get_pending = AsyncMock(return_value=[event2])
    loop._brain = brain2
    loop._long_term.query = AsyncMock(return_value=[fake_memory])

    await loop._cycle()

    recall_msgs2 = [m for m in mem.primary if "[自动回忆]" in str(m.content)]
    assert len(recall_msgs2) == 1  # 仍然只有 1 条，没有新增


@pytest.mark.asyncio
async def test_recent_activity_auto_recall_injects_and_deduplicates():
    mem = ShortTermMemory()
    brain = _make_brain()
    loop = _make_loop(brain, mem, events=[IncomingEvent(participant_id="alice", content="部署结果")])
    loop._short_term.compress_if_needed = AsyncMock()
    loop._config.memory.recent_activity_auto_recall_enabled = True
    loop._config.memory.recent_activity_auto_recall_limit = 2
    loop._config.memory.recent_activity_auto_recall_relevance_threshold = 0.72

    recent = MagicMock()
    recent.query = AsyncMock(return_value=[
        {
            "id": "recent:7",
            "timestamp": "2026-06-01T09:00:00",
            "event_type": "tool_result",
            "tool_name": "execute_code",
            "status": "ok",
            "activity_description": "工具 execute_code 返回成功结果。",
            "snippet": "部署成功",
            "relevance": 0.9,
        }
    ])
    loop._recent_activity = recent

    await loop._cycle()

    recall_msgs = [m for m in mem.primary if "[自动回忆·历史活动回放]" in str(m.content)]
    assert len(recall_msgs) == 1
    assert recall_msgs[0].recalled_memory_ids == ["recent:7"]
    assert recall_msgs[0].source == "recent_activity_auto_recall"
    assert "工具 execute_code 返回成功结果。" in str(recall_msgs[0].content)
    assert "不是当前指令" in str(recall_msgs[0].content)
    recent.query.assert_awaited_with("部署结果", limit=2, min_relevance=0.72)

    brain2 = _make_brain()
    loop._brain = brain2
    loop._inbox.get_pending = AsyncMock(return_value=[IncomingEvent(participant_id="alice", content="部署结果 again")])
    recent.query = AsyncMock(return_value=[
        {
            "id": "recent:7",
            "timestamp": "2026-06-01T09:00:00",
            "event_type": "tool_result",
            "tool_name": "execute_code",
            "status": "ok",
            "activity_description": "工具 execute_code 返回成功结果。",
            "snippet": "部署成功",
            "relevance": 0.9,
        }
    ])

    await loop._cycle()

    recall_msgs2 = [m for m in mem.primary if "[自动回忆·历史活动回放]" in str(m.content)]
    assert len(recall_msgs2) == 1


@pytest.mark.asyncio
async def test_skill_warning_injected_into_model_context():
    mem = ShortTermMemory()
    brain = _make_brain()
    event = IncomingEvent(participant_id="alice", content="需要一个 skill")
    loop = _make_loop(brain, mem, events=[event])
    loop._short_term.compress_if_needed = AsyncMock()
    loop._prompt_builder.consume_skill_load_warnings.return_value = [
        "Skill 文件 D:\\tmp\\bad-skill\\SKILL.md 的 YAML frontmatter 解析失败。"
    ]

    await loop._cycle()

    warning_msgs = [m for m in mem.primary if m.role == "user" and "[技能加载异常]" in str(m.content)]
    assert len(warning_msgs) == 1
    assert "YAML frontmatter 解析失败" in str(warning_msgs[0].content)

    context_messages = brain.think.await_args.args[0]
    assert any("[技能加载异常]" in str(m.content) for m in context_messages)
