from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from coworker.agent.loop import AgentLoop
from coworker.core.types import AgentState, LLMResponse, ToolCall
from fastapi.testclient import TestClient

from explore_lab.assembly import Runtime
from explore_lab.branch_runner import BranchController, BranchStatus, ConflictError
from explore_lab.lab_communicate import LabCommunicateTool


def _make_brain(content: str = "ok", tool_calls=None, stop_reason: str = "end_turn"):
    response = LLMResponse(
        content=content,
        tool_calls=tool_calls or [],
        stop_reason=stop_reason,
        model="mock-model",
        usage={"input_tokens": 1, "output_tokens": 1},
    )
    brain = MagicMock()
    brain.current_provider_name = "mock"
    brain.current_model = "mock-model"
    brain.current_model_has_vision = False
    brain.thinking = False
    brain.think = AsyncMock(return_value=response)
    brain.switch_model = AsyncMock()
    brain.count_tokens = AsyncMock(return_value=10)
    brain.consume_fallback_switch = MagicMock(return_value=False)
    return brain


def _make_runtime(tmp_path: Path, brain) -> Runtime:
    from coworker.memory.short_term import ShortTermMemory
    from coworker.tools.communicate_tool import ListConnectionTool
    from coworker.tools.registry import ToolRegistry

    mem = ShortTermMemory()
    state = AgentState(
        current_provider=brain.current_provider_name, current_model=brain.current_model,
    )

    inbox = MagicMock()
    inbox.get_pending = AsyncMock(return_value=[])
    inbox.push = AsyncMock(return_value="event-id")
    inbox.message_event = asyncio.Event()  # _rest() 需要真的可 await 的 Event，不能用 MagicMock

    prompt_builder = MagicMock()
    prompt_builder.build = MagicMock(return_value="system prompt")
    prompt_builder.refresh = MagicMock()
    prompt_builder.consume_skill_load_warnings = MagicMock(return_value=[])

    config = MagicMock()
    config.agent.idle_sleep_seconds = 0
    config.agent.passive_mode = False
    config.agent.inbox_batch_max = 10
    config.agent.subconscious_thinking = False
    config.agent.subconscious_max_cycles = 5
    config.agent.bubble_max_concurrent = 5
    config.agent.logs_dir = str(tmp_path / "data" / "logs")
    config.memory.db_path = str(tmp_path / "data" / "memory")
    config.model_dump_json = MagicMock(
        side_effect=lambda indent=2: json.dumps(
            {"agent": {"subconscious_thinking": config.agent.subconscious_thinking}},
            indent=indent,
        )
    )

    long_term = MagicMock()
    long_term._mem = None

    communicate = LabCommunicateTool(str(tmp_path / "outbox"))
    tools = ToolRegistry()
    tools.register(communicate)
    tools.register(ListConnectionTool(communicate))
    clear_tool = MagicMock()
    clear_tool._subconscious = None

    loop = AgentLoop.__new__(AgentLoop)
    loop._brain = brain
    loop._short_term = mem
    loop._long_term = long_term
    loop._tools = tools
    loop._identity = MagicMock()
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
    loop._last_compress_generation = mem.compress_generation

    return Runtime(
        workdir=tmp_path,
        config=config,
        identity=MagicMock(),
        skill_loader=MagicMock(),
        palace_loader=MagicMock(),
        mode_loader=MagicMock(),
        log_store=MagicMock(),
        interaction_log=MagicMock(),
        event_collector=MagicMock(),
        usage_stats=MagicMock(snapshot=MagicMock(return_value={})),
        long_term=long_term,
        recent_activity=None,
        short_term=mem,
        brain=brain,
        agent_state=state,
        inbox_watcher=inbox,
        base_registry=tools,
        prompt_builder=prompt_builder,
        bubble_store=None,
        subconscious=None,
        agent_loop=loop,
        task_store=MagicMock(),
        snapshot_path=tmp_path / "snapshot.json",
        thinking_path=tmp_path / "data" / "thinking.md",
        browser_store=MagicMock(),
        communicate=communicate,
        tool_intercepts={},
        clear_short_term_memory_tool=clear_tool,
        stm_kwargs={},
    )


def _make_controller(tmp_path: Path, brain=None) -> BranchController:
    brain = brain or _make_brain()
    controller = BranchController()
    controller.runtime = _make_runtime(tmp_path, brain)
    controller._wrap_prompt_builder()
    controller.status = BranchStatus.PAUSED
    return controller


class TestStateMachine:
    def test_new_controller_reports_starting_before_runtime_is_ready(self):
        controller = BranchController()

        assert controller.state_snapshot()["status"] == BranchStatus.STARTING

    async def test_step_when_running_raises_conflict_with_current_status(self, tmp_path):
        controller = _make_controller(tmp_path)
        controller.status = BranchStatus.RUNNING
        with pytest.raises(ConflictError) as exc_info:
            await controller.step()
        assert exc_info.value.current_status == BranchStatus.RUNNING

    async def test_step_when_paused_succeeds_and_returns_to_paused(self, tmp_path):
        controller = _make_controller(tmp_path)
        result = await controller.step()
        assert result["ok"] is True
        assert controller.status == BranchStatus.PAUSED
        assert len(controller.undo) == 1
        assert controller.runtime.agent_state.cycle_count == 1

    async def test_resume_and_pause_allowed_only_from_expected_states(self, tmp_path):
        controller = _make_controller(tmp_path)
        controller.status = BranchStatus.PAUSED
        with pytest.raises(ConflictError):
            await controller.pause()

        controller.status = BranchStatus.RUNNING
        with pytest.raises(ConflictError):
            await controller.resume(None, None)

    async def test_concurrent_step_calls_second_one_conflicts(self, tmp_path):
        release = asyncio.Event()

        async def slow_think(*args, **kwargs):
            await release.wait()
            return LLMResponse(
                content="ok", tool_calls=[], stop_reason="end_turn",
                model="mock-model", usage={"input_tokens": 1, "output_tokens": 1},
            )

        brain = _make_brain()
        brain.think = slow_think
        controller = _make_controller(tmp_path, brain=brain)

        first = asyncio.create_task(controller.step())
        await asyncio.sleep(0)  # 让第一个 step 先把状态切到 STEPPING
        assert controller.status == BranchStatus.STEPPING

        with pytest.raises(ConflictError):
            await controller.step()

        release.set()
        result = await first
        assert result["ok"] is True
        assert controller.status == BranchStatus.PAUSED


class TestStepNUntilReply:
    async def test_stops_early_when_assistant_replies_without_tool_calls(self, tmp_path):
        responses = [
            LLMResponse(
                content="", tool_calls=[MagicMock(id="tc1", name="breathe", arguments={})],
                stop_reason="tool_use", model="m", usage={},
            ),
            LLMResponse(content="done", tool_calls=[], stop_reason="end_turn", model="m", usage={}),
            LLMResponse(
                content="should not run", tool_calls=[], stop_reason="end_turn",
                model="m", usage={},
            ),
        ]
        brain = _make_brain()
        brain.think = AsyncMock(side_effect=responses)
        controller = _make_controller(tmp_path, brain=brain)
        controller.runtime.agent_loop._tools.execute = AsyncMock(
            return_value=MagicMock(
                tool_call_id="tc1", content="ok", is_error=False, recalled_memory_ids=[],
            )
        )

        result = await controller.step_n(5, stop_condition="until_reply")

        assert result["ok"] is True
        assert result["completed"] == 2
        assert result["stopped_early"] == "until_reply"
        assert controller.status == BranchStatus.PAUSED


class TestExceptionRollback:
    async def test_cycle_exception_rolls_back_state_and_returns_to_paused(self, tmp_path):
        brain = _make_brain()
        brain.think = AsyncMock(side_effect=RuntimeError("boom"))
        controller = _make_controller(tmp_path, brain=brain)
        before_cycle_count = controller.runtime.agent_state.cycle_count
        before_messages = list(controller.runtime.short_term.primary)

        result = await controller.step()

        assert result["ok"] is False
        assert result["error"]["type"] == "RuntimeError"
        assert controller.status == BranchStatus.PAUSED
        assert controller.runtime.agent_state.cycle_count == before_cycle_count
        assert controller.runtime.short_term.primary == before_messages
        # 异常回滚不应该往 undo 栈里留下这一步（这一步等于没发生过）
        assert len(controller.undo) == 0


class TestUndoRedo:
    async def test_back_step_restores_previous_cycle_count(self, tmp_path):
        controller = _make_controller(tmp_path)
        await controller.step()
        await controller.step()
        assert controller.runtime.agent_state.cycle_count == 2

        result = await controller.back_step()
        assert result["ok"] is True
        assert controller.runtime.agent_state.cycle_count == 1

    async def test_back_step_on_empty_stack_raises_404_conflict(self, tmp_path):
        controller = _make_controller(tmp_path)
        with pytest.raises(Exception) as exc_info:
            await controller.back_step()
        assert getattr(exc_info.value, "status_code", None) == 409

    async def test_back_step_then_step_discards_old_future(self, tmp_path):
        controller = _make_controller(tmp_path)
        await controller.step()  # cycle_count -> 1
        await controller.step()  # cycle_count -> 2
        await controller.back_step()  # 回到 cycle_count == 1
        assert controller.runtime.agent_state.cycle_count == 1

        await controller.step()  # 在退回后的状态上继续跑，产生新的"未来"
        assert controller.runtime.agent_state.cycle_count == 2

        # 旧的"原始 cycle 2"已经不可恢复：栈里只剩 [回到 cycle 0 的快照, 回到 cycle 1 的快照]
        back = await controller.back_step()
        assert back["undo_depth"] == 1
        assert controller.runtime.agent_state.cycle_count == 1

        back = await controller.back_step()
        assert back["undo_depth"] == 0
        assert controller.runtime.agent_state.cycle_count == 0

        with pytest.raises(Exception) as exc_info:
            await controller.back_step()
        assert getattr(exc_info.value, "status_code", None) == 409


class TestFlushSnapshot:
    async def test_flush_snapshot_writes_current_short_term_to_disk(self, tmp_path):
        import json

        controller = _make_controller(tmp_path)
        await controller.step()

        result = controller.flush_snapshot()

        assert result["ok"] is True
        snapshot_path = controller.runtime.snapshot_path
        assert snapshot_path.is_file()
        data = json.loads(snapshot_path.read_text(encoding="utf-8"))
        # step() 里假 Brain 回复了一条 assistant 消息，落盘的快照应该包含它
        assert any(m.get("role") == "assistant" for m in data["primary"])


class TestSystemPrompt:
    def test_current_system_prompt_returns_base_and_effective_text(self, tmp_path):
        controller = _make_controller(tmp_path)

        snapshot = controller.current_system_prompt()
        assert snapshot["base_text"] == "system prompt"
        assert snapshot["effective_text"] == "system prompt"
        assert snapshot["override_active"] is False
        assert snapshot["override_text"] is None

        controller.set_system_prompt_override("override prompt")

        snapshot = controller.current_system_prompt()
        assert snapshot["base_text"] == "system prompt"
        assert snapshot["effective_text"] == "override prompt"
        assert snapshot["override_active"] is True
        assert snapshot["override_text"] == "override prompt"


class TestLabCommunicate:
    def test_default_ws_connection_is_visible_in_state(self, tmp_path):
        controller = _make_controller(tmp_path)

        snapshot = controller.state_snapshot()

        assert snapshot["ws_connections"] == ["explore_lab"]
        assert "communicate" not in snapshot["tool_intercepts"]
        assert "list_connections" not in snapshot["tool_intercepts"]

    async def test_communicate_to_virtual_connection_records_outbound_message(self, tmp_path):
        controller = _make_controller(tmp_path)
        tool_call = ToolCall(
            id="tc-communicate",
            name="communicate",
            arguments={"participant_id": "explore_lab", "message": "hello"},
        )

        result = await controller.runtime.base_registry.execute(tool_call)

        assert result.is_error is False
        assert "Explore Lab 模拟连接" in result.content
        snapshot = controller.state_snapshot()
        assert snapshot["outbound_messages"][0]["participant_id"] == "explore_lab"
        assert snapshot["outbound_messages"][0]["message"] == "hello"

    async def test_patch_config_replaces_virtual_ws_connections(self, tmp_path):
        controller = _make_controller(tmp_path)

        applied = await controller.patch_hot_config({"ws_connections": ["alice", "bob"]})

        assert applied["ws_connections"] == ["alice", "bob"]
        assert controller.state_snapshot()["ws_connections"] == ["alice", "bob"]


class TestSubconsciousToggle:
    def test_disable_disconnects_new_triggers_but_keeps_pending_visible(self, tmp_path):
        controller = _make_controller(tmp_path)
        scheduler = MagicMock()
        scheduler._active_by_mode = {"audit": "bbl_audit"}
        controller.runtime.subconscious = scheduler
        controller.runtime.config.agent.subconscious_thinking = True
        controller.runtime.agent_loop._subconscious = scheduler
        controller.runtime.clear_short_term_memory_tool._subconscious = scheduler
        bubble = SimpleNamespace(
            id="bbl_audit",
            goal="audit",
            status="running",
            cycles_used=1,
            max_cycles=5,
            participant_id="",
            created_at=datetime(2026, 7, 7, 10, 0, 0),
        )
        controller.runtime.bubble_store = MagicMock(list_active=MagicMock(return_value=[bubble]))

        result = controller.set_subconscious_enabled(False)
        snapshot = controller.state_snapshot()

        assert result == {"enabled": False, "pending": ["bbl_audit"]}
        assert snapshot["subconscious_enabled"] is False
        assert snapshot["subconscious_pending"] == ["bbl_audit"]
        assert snapshot["active_bubbles"][0]["kind"] == "subconscious"
        assert controller.runtime.agent_loop._subconscious is None
        assert controller.runtime.clear_short_term_memory_tool._subconscious is None
        assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8")) == {
            "agent": {"subconscious_thinking": False}
        }

    def test_enable_recreates_scheduler_and_persists_config(self, tmp_path, monkeypatch):
        import explore_lab.branch_runner as br_mod

        controller = _make_controller(tmp_path)
        scheduler = MagicMock()
        scheduler._active_by_mode = {}
        monkeypatch.setattr(br_mod, "create_subconscious_scheduler", lambda _rt: scheduler)

        result = controller.set_subconscious_enabled(True)

        assert result == {"enabled": True, "pending": []}
        assert controller.runtime.subconscious is scheduler
        assert controller.runtime.agent_loop._subconscious is scheduler
        assert controller.runtime.clear_short_term_memory_tool._subconscious is scheduler
        assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8")) == {
            "agent": {"subconscious_thinking": True}
        }

    async def test_patch_hot_config_uses_subconscious_toggle(self, tmp_path):
        controller = _make_controller(tmp_path)
        scheduler = MagicMock()
        scheduler._active_by_mode = {}
        controller.runtime.subconscious = scheduler
        controller.runtime.config.agent.subconscious_thinking = True
        controller.runtime.agent_loop._subconscious = scheduler
        controller.runtime.clear_short_term_memory_tool._subconscious = scheduler

        applied = await controller.patch_hot_config({"agent": {"subconscious_thinking": False}})

        assert applied["agent.subconscious_thinking"] is False
        assert controller.runtime.agent_loop._subconscious is None
        assert controller.runtime.clear_short_term_memory_tool._subconscious is None

    def test_trigger_route_rejects_when_subconscious_disabled(self, tmp_path, monkeypatch):
        import explore_lab.branch_runner as br_mod

        fake_controller = _make_controller(tmp_path)
        fake_controller.runtime.config.agent.subconscious_thinking = False
        fake_controller.runtime.subconscious = MagicMock()
        fake_controller.start = AsyncMock()
        monkeypatch.setattr(br_mod, "controller", fake_controller)

        app = br_mod.create_app(tmp_path)
        with TestClient(app) as client:
            resp = client.post("/subconscious/trigger", json={"mode": "audit"})

        assert resp.status_code == 503

