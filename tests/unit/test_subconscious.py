from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coworker.agent.bubble import Bubble, BubbleStore
from coworker.agent.subconscious import (
    _SUBCONSCIOUS_EXTRA_INTERCEPTS,
    SubconsciousMiniLoop,
    SubconsciousScheduler,
)
from coworker.agent.subconscious_mode import SubconsciousMode, SubconsciousModeLoader
from coworker.core.types import LLMResponse, Message, ToolCall
from coworker.palaces.loader import Palace

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mode(
    name,
    *,
    body="",
    trigger="periodic",
    context_builder="short_term",
    n_cycles=0,
    every_seconds=0,
    n_tool_calls=0,
    pre_compress=False,
    n_compressions=0,
    pre_compress_min_interval=0,
    pre_compress_context="full",
    cold_floor=0,
    max_cycles=5,
    grants_task_store=False,
    inject_skill_anomalies=False,
    inject_telemetry=False,
    use_threshold=0,
    min_interval=0,
    enabled=True,
    purpose="",
    retire_after="",
    protected=False,
) -> SubconsciousMode:
    return SubconsciousMode(
        name=name,
        body=body or f"{name} {{bubble_id}} {{goal}} {{max_cycles}}",
        trigger=trigger,
        context_builder=context_builder,
        every_n_cycles=n_cycles,
        every_seconds=every_seconds,
        every_n_tool_calls=n_tool_calls,
        pre_compress=pre_compress,
        every_n_compressions=n_compressions,
        pre_compress_min_interval_seconds=pre_compress_min_interval,
        pre_compress_context=pre_compress_context,
        cold_floor_seconds=cold_floor,
        max_cycles=max_cycles,
        grants_task_store=grants_task_store,
        inject_skill_anomalies=inject_skill_anomalies,
        inject_telemetry=inject_telemetry,
        use_threshold=use_threshold,
        min_interval_seconds=min_interval,
        enabled=enabled,
        purpose=purpose,
        retire_after=retire_after,
        protected=protected,
    )


def _populate_loader(modes: list[SubconsciousMode]) -> SubconsciousModeLoader:
    loader = SubconsciousModeLoader("/nonexistent")
    for m in modes:
        loader._modes[m.name] = m
    return loader


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store():
    return BubbleStore(max_concurrent=5)


@pytest.fixture
def messages():
    return [Message(role="user", content="test")]


@pytest.fixture
def mock_brain():
    brain = MagicMock()
    brain.current_provider_name = "mock"
    brain.current_model = "mock-model"
    brain.current_model_has_vision = False
    brain._providers = {}
    return brain


@pytest.fixture
def mock_inbox():
    inbox = MagicMock()
    inbox.push = AsyncMock()
    return inbox


@pytest.fixture
def mock_registry():
    reg = MagicMock()
    reg.get_schemas.return_value = []
    reg.scoped.return_value = reg
    reg.intercept.return_value = reg
    return reg


@pytest.fixture
def mock_prompt_builder():
    pb = MagicMock()
    pb.build.return_value = "system prompt"
    return pb


@pytest.fixture
def mock_ilog():
    return MagicMock()


@pytest.fixture
def cfg_agent():
    a = MagicMock()
    a.subconscious_summarize_before_compress = True
    a.subconscious_max_cycles = 5
    a.skills_dir = ".coworker/skills"
    return a


@pytest.fixture
def mock_cfg(cfg_agent):
    cfg = MagicMock()
    cfg.agent = cfg_agent
    return cfg


@pytest.fixture
def mode_loader():
    """In-memory mode loader with all 5 modes, thresholds all 0 so tests control them via mutation."""
    return _populate_loader(
        [
            _make_mode("audit", pre_compress=True, n_compressions=1),
            _make_mode(
                "summarize",
                trigger="manual",
                pre_compress=True,
                n_compressions=1,
                pre_compress_context="slice",
            ),
            _make_mode("explore"),
            _make_mode(
                "introspect",
                grants_task_store=True,
                inject_skill_anomalies=True,
                pre_compress=True,
                n_compressions=3,
                pre_compress_min_interval=21600,
            ),
            _make_mode(
                "meta",
                trigger="cold_floor",
                context_builder="meta",
                inject_telemetry=True,
                grants_task_store=True,
            ),
        ]
    )


@pytest.fixture
def real_mode_loader():
    """Mode loader that reads from the actual .coworker/subconscious/ seed files."""
    loader = SubconsciousModeLoader(".coworker/subconscious")
    loader.load_all()
    return loader


@pytest.fixture
def scheduler(mock_cfg, store, mock_brain, mock_registry, mock_prompt_builder,
              mock_inbox, mock_ilog, tmp_path, mode_loader):
    return SubconsciousScheduler(
        cfg=mock_cfg,
        bubble_store=store,
        brain=mock_brain,
        tool_registry=mock_registry,
        prompt_builder=mock_prompt_builder,
        short_term=MagicMock(),
        inbox=mock_inbox,
        logs_dir=str(tmp_path),
        interaction_log=mock_ilog,
        mode_loader=mode_loader,
    )


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
# SubconsciousMiniLoop: identity content
# ---------------------------------------------------------------------------


class TestSubconsciousMiniLoopIdentity:
    def _make_bubble(self, goal="test goal", max_cycles=5):
        return Bubble(id="bbl_test", goal=goal, max_cycles=max_cycles)

    def _make_loop(self, mode, store, mock_brain, mock_registry, mock_inbox, tmp_path, real_mode_loader):
        bubble = self._make_bubble()
        m = real_mode_loader.get(mode)
        identity_body = m.body if m else f"{mode} identity {{bubble_id}} {{goal}} {{max_cycles}}"
        return SubconsciousMiniLoop(
            mode=mode,
            identity_body=identity_body,
            intercepts=dict(_SUBCONSCIOUS_EXTRA_INTERCEPTS),
            bubble=bubble,
            brain=mock_brain,
            tool_registry=mock_registry,
            system_prompt="sys",
            bubble_store=store,
            inbox_watcher=mock_inbox,
            logs_dir=str(tmp_path),
        )

    def test_audit_identity_contains_key_phrases(self, store, mock_brain, mock_registry, mock_inbox, tmp_path, real_mode_loader):
        loop = self._make_loop("audit", store, mock_brain, mock_registry, mock_inbox, tmp_path, real_mode_loader)
        bubble = self._make_bubble()
        content = loop._build_identity_content(bubble)
        assert "潜意识" in content
        assert "审计" in content
        assert "bubble_send" in content
        assert "不会传递给主线" in content

    def test_audit_identity_contains_alignment_content(self, store, mock_brain, mock_registry, mock_inbox, tmp_path, real_mode_loader):
        loop = self._make_loop("audit", store, mock_brain, mock_registry, mock_inbox, tmp_path, real_mode_loader)
        bubble = self._make_bubble()
        content = loop._build_identity_content(bubble)
        assert "自我对齐" in content
        assert "身份认同" in content or "价值观" in content

    def test_summarize_identity_contains_key_phrases(self, store, mock_brain, mock_registry, mock_inbox, tmp_path, real_mode_loader):
        loop = self._make_loop("summarize", store, mock_brain, mock_registry, mock_inbox, tmp_path, real_mode_loader)
        bubble = self._make_bubble()
        content = loop._build_identity_content(bubble)
        assert "潜意识" in content
        assert "总结" in content
        assert "manage_memory" in content
        assert "不要调用 bubble_send" in content

    def test_explore_identity_contains_key_phrases(self, store, mock_brain, mock_registry, mock_inbox, tmp_path, real_mode_loader):
        loop = self._make_loop("explore", store, mock_brain, mock_registry, mock_inbox, tmp_path, real_mode_loader)
        bubble = self._make_bubble()
        content = loop._build_identity_content(bubble)
        assert "潜意识" in content
        assert "发散" in content
        assert "manage_memory" in content
        assert "bubble_send" in content

    def test_all_three_modes_differ(self, store, mock_brain, mock_registry, mock_inbox, tmp_path, real_mode_loader):
        loops = {m: self._make_loop(m, store, mock_brain, mock_registry, mock_inbox, tmp_path, real_mode_loader)
                 for m in ("audit", "summarize", "explore")}
        bubble = self._make_bubble()
        contents = [loops[m]._build_identity_content(bubble) for m in ("audit", "summarize", "explore")]
        assert len(set(contents)) == 3

    def test_bubble_id_in_identity(self, store, mock_brain, mock_registry, mock_inbox, tmp_path, real_mode_loader):
        loop = self._make_loop("audit", store, mock_brain, mock_registry, mock_inbox, tmp_path, real_mode_loader)
        bubble = self._make_bubble()
        content = loop._build_identity_content(bubble)
        assert bubble.id in content

    def test_audit_identity_contains_completeness_lens(self, store, mock_brain, mock_registry, mock_inbox, tmp_path, real_mode_loader):
        loop = self._make_loop("audit", store, mock_brain, mock_registry, mock_inbox, tmp_path, real_mode_loader)
        content = loop._build_identity_content(self._make_bubble())
        assert "完成度" in content
        assert "半途而废" in content or "敷衍" in content

    def test_introspect_identity_contains_skill_audit(self, store, mock_brain, mock_registry, mock_inbox, tmp_path, real_mode_loader):
        loop = self._make_loop("introspect", store, mock_brain, mock_registry, mock_inbox, tmp_path, real_mode_loader)
        content = loop._build_identity_content(self._make_bubble())
        assert "能力" in content
        assert "技能库审视" in content
        assert "[维护]" in content
        assert "[SKILLS]" in content
        assert "task_create" in content
        assert "get_skill" in content

    def test_garden_identity_contains_key_phrases(self, store, mock_brain, mock_registry, mock_inbox, tmp_path, real_mode_loader):
        loop = self._make_loop("garden", store, mock_brain, mock_registry, mock_inbox, tmp_path, real_mode_loader)
        content = loop._build_identity_content(self._make_bubble())
        assert "园丁" in content
        assert "manage_memory" in content
        assert "delete" in content
        assert "保守" in content
        assert "bubble_send" in content
        assert "不能" in content
        assert "associate" in content
        assert "query_memory" in content
        assert "memory_tags" in content


class TestSubconsciousIntercepts:
    def _make_loop(self, mode, store, mock_brain, mock_registry, mock_inbox, tmp_path):
        bubble = Bubble(id="bbl_test", goal="g", max_cycles=5)
        return SubconsciousMiniLoop(
            mode=mode,
            identity_body=f"{mode} identity",
            intercepts=dict(_SUBCONSCIOUS_EXTRA_INTERCEPTS),
            bubble=bubble,
            brain=mock_brain,
            tool_registry=mock_registry,
            system_prompt="sys",
            bubble_store=store,
            inbox_watcher=mock_inbox,
            logs_dir=str(tmp_path),
        )

    def test_intercepts_block_idle_and_main_affecting_tools(self, store, mock_brain, mock_registry, mock_inbox, tmp_path):
        loop = self._make_loop("audit", store, mock_brain, mock_registry, mock_inbox, tmp_path)
        ic = loop._tool_intercepts()
        for name in ("sleep", "restart_self", "compress_memory",
                     "communicate", "bubble_spawn", "switch_model", "set_alarm"):
            assert name in ic, f"{name} should be intercepted"
        assert "breathe" not in ic

    def test_intercepts_keep_working_tools_available(self, store, mock_brain, mock_registry, mock_inbox, tmp_path):
        loop = self._make_loop("summarize", store, mock_brain, mock_registry, mock_inbox, tmp_path)
        ic = loop._tool_intercepts()
        for name in ("manage_memory", "query_memory", "bubble_done", "bubble_send"):
            assert name not in ic, f"{name} should remain available"

    def test_every_intercept_has_a_reason(self, store, mock_brain, mock_registry, mock_inbox, tmp_path):
        loop = self._make_loop("explore", store, mock_brain, mock_registry, mock_inbox, tmp_path)
        ic = loop._tool_intercepts()
        assert all(isinstance(r, str) and r for r in ic.values())


# ---------------------------------------------------------------------------
# SubconsciousMiniLoop: _auto_merge does NOT push to inbox
# ---------------------------------------------------------------------------


class TestSubconsciousMiniLoopAutoMerge:
    async def test_auto_merge_silent_no_inbox_push(self, store, mock_brain, mock_registry, mock_inbox, tmp_path):
        bubble = store.create("goal", [Message(role="user", content="x")], max_cycles=5)
        assert isinstance(bubble, Bubble)
        loop = SubconsciousMiniLoop(
            mode="audit",
            identity_body="audit identity",
            intercepts=dict(_SUBCONSCIOUS_EXTRA_INTERCEPTS),
            bubble=bubble,
            brain=mock_brain,
            tool_registry=mock_registry,
            system_prompt="sys",
            bubble_store=store,
            inbox_watcher=mock_inbox,
            logs_dir=str(tmp_path),
        )
        await loop._auto_merge()
        mock_inbox.push.assert_not_called()

    async def test_auto_merge_marks_done(self, store, mock_brain, mock_registry, mock_inbox, tmp_path):
        bubble = store.create("goal", [Message(role="user", content="x")], max_cycles=5)
        assert isinstance(bubble, Bubble)
        loop = SubconsciousMiniLoop(
            mode="summarize",
            identity_body="summarize identity",
            intercepts=dict(_SUBCONSCIOUS_EXTRA_INTERCEPTS),
            bubble=bubble,
            brain=mock_brain,
            tool_registry=mock_registry,
            system_prompt="sys",
            bubble_store=store,
            inbox_watcher=mock_inbox,
            logs_dir=str(tmp_path),
        )
        assert bubble.id in store._active
        await loop._auto_merge()
        assert bubble.id not in store._active
        assert any(b.id == bubble.id for b in store._history)


# ---------------------------------------------------------------------------
# SubconsciousScheduler: cycle triggers
# ---------------------------------------------------------------------------


class TestSchedulerCycleTriggers:
    async def test_audit_triggers_after_n_cycles(self, scheduler, mode_loader, messages):
        mode_loader.get("audit").every_n_cycles = 5
        spawn_calls = []

        async def fake_spawn(mode, ctx, goal_override=None):
            spawn_calls.append(mode.name)

        scheduler._spawn = fake_spawn
        await scheduler.notify_cycle_complete(5, messages)
        assert spawn_calls == ["audit"]

    async def test_audit_does_not_trigger_before_n_cycles(self, scheduler, mode_loader, messages):
        mode_loader.get("audit").every_n_cycles = 10
        spawn_calls = []

        async def fake_spawn(mode, ctx, goal_override=None):
            spawn_calls.append(mode.name)

        scheduler._spawn = fake_spawn
        await scheduler.notify_cycle_complete(5, messages)
        assert "audit" not in spawn_calls

    async def test_summarize_manual_mode_ignores_periodic_thresholds(self, scheduler, mode_loader, messages):
        mode_loader.get("summarize").every_n_cycles = 10
        mode_loader.get("summarize").every_seconds = 1
        mode_loader.get("summarize").every_n_tool_calls = 1
        mode_loader.get("audit").every_n_cycles = 0
        spawn_calls = []

        async def fake_spawn(mode, ctx, goal_override=None):
            spawn_calls.append(mode.name)

        scheduler._spawn = fake_spawn
        scheduler._last_time["summarize"] -= 2.0
        await scheduler.notify_cycle_complete(10, messages)
        assert "summarize" not in spawn_calls

    async def test_audit_disabled_when_n_is_zero(self, scheduler, mode_loader, messages):
        mode_loader.get("audit").every_n_cycles = 0
        mode_loader.get("summarize").every_n_cycles = 0
        mode_loader.get("explore").every_n_cycles = 0
        spawn_calls = []

        async def fake_spawn(mode, ctx, goal_override=None):
            spawn_calls.append(mode.name)

        scheduler._spawn = fake_spawn
        await scheduler.notify_cycle_complete(100, messages)
        assert spawn_calls == []

    async def test_audit_not_triggered_while_active(self, scheduler, store, mode_loader, messages):
        mode_loader.get("audit").every_n_cycles = 5
        bubble = store.create("audit bubble", messages, max_cycles=5)
        assert isinstance(bubble, Bubble)
        scheduler._active_by_mode["audit"] = bubble.id

        spawn_calls = []

        async def fake_spawn(mode, ctx, goal_override=None):
            spawn_calls.append(mode.name)

        scheduler._spawn = fake_spawn
        await scheduler.notify_cycle_complete(5, messages)
        assert "audit" not in spawn_calls

    async def test_last_cycle_counter_updated_after_trigger(self, scheduler, mode_loader, messages):
        mode_loader.get("audit").every_n_cycles = 5
        mode_loader.get("summarize").every_n_cycles = 0

        async def fake_spawn(mode, ctx, goal_override=None):
            pass

        scheduler._spawn = fake_spawn
        await scheduler.notify_cycle_complete(5, messages)
        assert scheduler._last_cycle.get("audit") == 5

    async def test_explore_triggers_after_k_cycles(self, scheduler, mode_loader, messages):
        mode_loader.get("explore").every_n_cycles = 10
        mode_loader.get("audit").every_n_cycles = 0
        mode_loader.get("summarize").every_n_cycles = 0
        spawn_calls = []

        async def fake_spawn(mode, ctx, goal_override=None):
            spawn_calls.append(mode.name)

        scheduler._spawn = fake_spawn
        await scheduler.notify_cycle_complete(10, messages)
        assert spawn_calls == ["explore"]

    async def test_explore_disabled_when_k_is_zero(self, scheduler, mode_loader, messages):
        mode_loader.get("explore").every_n_cycles = 0
        mode_loader.get("audit").every_n_cycles = 0
        mode_loader.get("summarize").every_n_cycles = 0
        spawn_calls = []

        async def fake_spawn(mode, ctx, goal_override=None):
            spawn_calls.append(mode.name)

        scheduler._spawn = fake_spawn
        await scheduler.notify_cycle_complete(100, messages)
        assert spawn_calls == []

    async def test_audit_triggers_by_time_when_cycles_not_met(self, scheduler, mode_loader, messages):
        mode_loader.get("audit").every_n_cycles = 100
        mode_loader.get("audit").every_seconds = 1
        mode_loader.get("summarize").every_n_cycles = 0
        mode_loader.get("explore").every_n_cycles = 0
        spawn_calls = []

        async def fake_spawn(mode, ctx, goal_override=None):
            spawn_calls.append(mode.name)

        scheduler._spawn = fake_spawn
        scheduler._last_time["audit"] -= 2.0
        await scheduler.notify_cycle_complete(1, messages)
        assert "audit" in spawn_calls

    async def test_audit_does_not_trigger_when_both_disabled(self, scheduler, mode_loader, messages):
        mode_loader.get("audit").every_n_cycles = 0
        mode_loader.get("audit").every_seconds = 0
        mode_loader.get("summarize").every_n_cycles = 0
        mode_loader.get("summarize").every_seconds = 0
        mode_loader.get("explore").every_n_cycles = 0
        mode_loader.get("explore").every_seconds = 0
        spawn_calls = []

        async def fake_spawn(mode, ctx, goal_override=None):
            spawn_calls.append(mode.name)

        scheduler._spawn = fake_spawn
        scheduler._last_time["audit"] -= 9999.0
        await scheduler.notify_cycle_complete(9999, messages)
        assert spawn_calls == []

    async def test_audit_triggers_by_tool_calls(self, scheduler, mode_loader, messages):
        mode_loader.get("audit").every_n_cycles = 0
        mode_loader.get("audit").every_n_tool_calls = 10
        mode_loader.get("summarize").every_n_cycles = 0
        mode_loader.get("explore").every_n_cycles = 0
        spawn_calls = []

        async def fake_spawn(mode, ctx, goal_override=None):
            spawn_calls.append(mode.name)

        scheduler._spawn = fake_spawn
        await scheduler.notify_cycle_complete(1, messages, tool_calls_this_cycle=6)
        assert "audit" not in spawn_calls
        await scheduler.notify_cycle_complete(2, messages, tool_calls_this_cycle=4)
        assert "audit" in spawn_calls

    async def test_tool_call_counter_resets_after_trigger(self, scheduler, mode_loader, messages):
        mode_loader.get("audit").every_n_cycles = 0
        mode_loader.get("audit").every_n_tool_calls = 5
        mode_loader.get("summarize").every_n_cycles = 0
        mode_loader.get("explore").every_n_cycles = 0
        spawn_count = []

        async def fake_spawn(mode, ctx, goal_override=None):
            spawn_count.append(mode.name)

        scheduler._spawn = fake_spawn
        await scheduler.notify_cycle_complete(1, messages, tool_calls_this_cycle=5)
        await scheduler.notify_cycle_complete(2, messages, tool_calls_this_cycle=4)
        assert spawn_count.count("audit") == 1

    async def test_audit_does_not_trigger_when_all_disabled(self, scheduler, mode_loader, messages):
        for name in ("audit", "summarize", "explore", "introspect", "meta"):
            m = mode_loader.get(name)
            if m:
                m.every_n_cycles = 0
                m.every_seconds = 0
                m.every_n_tool_calls = 0
                m.cold_floor_seconds = 0
        spawn_calls = []

        async def fake_spawn(mode, ctx, goal_override=None):
            spawn_calls.append(mode.name)

        scheduler._spawn = fake_spawn
        scheduler._last_time["audit"] -= 9999.0
        await scheduler.notify_cycle_complete(9999, messages, tool_calls_this_cycle=9999)
        assert spawn_calls == []

    async def test_audit_does_not_retrigger_immediately(self, scheduler, mode_loader, messages):
        mode_loader.get("audit").every_n_cycles = 5
        mode_loader.get("summarize").every_n_cycles = 0
        spawn_count = []

        async def fake_spawn(mode, ctx, goal_override=None):
            spawn_count.append(mode.name)

        scheduler._spawn = fake_spawn
        await scheduler.notify_cycle_complete(5, messages)
        await scheduler.notify_cycle_complete(6, messages)
        assert spawn_count.count("audit") == 1


# ---------------------------------------------------------------------------
# SubconsciousScheduler: skill-library audit (introspect)
# ---------------------------------------------------------------------------


class TestSkillAnomalyScan:
    def _skills_dir(self, tmp_path, *, loose=False, missing_skill_md=False, good=True):
        sd = tmp_path / "skills"
        sd.mkdir()
        if good:
            g = sd / "good"
            g.mkdir()
            (g / "SKILL.md").write_text("---\nname: good\n---\nbody", encoding="utf-8")
        if loose:
            (sd / "loose.md").write_text("orphan doc", encoding="utf-8")
        if missing_skill_md:
            (sd / "bad").mkdir()
        return sd

    def test_flags_loose_md_and_dir_without_skill_md(self, scheduler, tmp_path):
        scheduler._cfg.agent.skills_dir = str(
            self._skills_dir(tmp_path, loose=True, missing_skill_md=True)
        )
        anomalies = scheduler._scan_skill_anomalies()
        assert "loose.md" in anomalies
        assert any(a.startswith("bad/") for a in anomalies)
        assert all("good" not in a for a in anomalies)

    def test_clean_dir_yields_no_message(self, scheduler, tmp_path):
        scheduler._cfg.agent.skills_dir = str(self._skills_dir(tmp_path))
        assert scheduler._scan_skill_anomalies() == []
        assert scheduler._build_skill_anomaly_message() is None

    def test_message_lists_anomalies(self, scheduler, tmp_path):
        scheduler._cfg.agent.skills_dir = str(self._skills_dir(tmp_path, loose=True))
        msg = scheduler._build_skill_anomaly_message()
        assert msg is not None and "loose.md" in msg.content

    def test_missing_dir_is_safe(self, scheduler, tmp_path):
        scheduler._cfg.agent.skills_dir = str(tmp_path / "does_not_exist")
        assert scheduler._scan_skill_anomalies() == []
        assert scheduler._build_skill_anomaly_message() is None


class TestIntrospectSkillContext:
    async def test_introspect_appends_anomaly_message(self, scheduler, mode_loader, messages, tmp_path):
        mode_loader.get("introspect").every_n_cycles = 1
        mode_loader.get("audit").every_n_cycles = 0
        mode_loader.get("summarize").every_n_cycles = 0
        mode_loader.get("explore").every_n_cycles = 0
        sd = tmp_path / "skills"
        sd.mkdir()
        (sd / "loose.md").write_text("orphan", encoding="utf-8")
        scheduler._cfg.agent.skills_dir = str(sd)

        captured = {}

        async def fake_spawn(mode, ctx, goal_override=None):
            captured[mode.name] = ctx

        scheduler._spawn = fake_spawn
        await scheduler.notify_cycle_complete(1, messages)
        assert "introspect" in captured
        assert any("loose.md" in (getattr(m, "content", "") or "") for m in captured["introspect"])

    async def test_introspect_without_anomalies_passes_plain_snapshot(self, scheduler, mode_loader, messages, tmp_path):
        mode_loader.get("introspect").every_n_cycles = 1
        mode_loader.get("audit").every_n_cycles = 0
        mode_loader.get("summarize").every_n_cycles = 0
        mode_loader.get("explore").every_n_cycles = 0
        sd = tmp_path / "skills"
        sd.mkdir()
        scheduler._cfg.agent.skills_dir = str(sd)

        captured = {}

        async def fake_spawn(mode, ctx, goal_override=None):
            captured[mode.name] = ctx

        scheduler._spawn = fake_spawn
        await scheduler.notify_cycle_complete(1, messages)
        assert "introspect" in captured
        assert len(captured["introspect"]) == len(messages)


# ---------------------------------------------------------------------------
# SubconsciousScheduler: pre-compress
# ---------------------------------------------------------------------------


class TestSchedulerPreCompress:
    async def test_pre_compress_uses_slice_for_summarize_and_full_for_audit(
        self, scheduler, messages
    ):
        spawned = []
        full = [*messages, Message(role="assistant", content="full-only")]

        async def fake_spawn(mode, ctx, goal_override=None):
            spawned.append((mode.name, ctx, goal_override))

        scheduler._spawn = fake_spawn
        await scheduler.notify_pre_compress(messages, full_snapshot=full)
        assert [item[0] for item in spawned] == ["summarize", "audit"]
        assert spawned[0][0] == "summarize"
        assert spawned[0][1] == messages
        assert "压缩" in (spawned[0][2] or "")
        assert spawned[1][1] == full
        assert spawned[1][2] is None

    async def test_summary_switch_does_not_disable_other_pre_compress_modes(
        self, scheduler, cfg_agent, messages
    ):
        cfg_agent.subconscious_summarize_before_compress = False
        spawned = []

        async def fake_spawn(mode, ctx, goal_override=None):
            spawned.append(mode.name)

        scheduler._spawn = fake_spawn
        await scheduler.notify_pre_compress(messages)
        assert spawned == ["audit"]

    async def test_pre_compress_skips_if_summarize_active(self, scheduler, store, messages):
        bubble = store.create("running", messages, max_cycles=5)
        assert isinstance(bubble, Bubble)
        scheduler._active_by_mode["summarize"] = bubble.id
        spawned = []

        async def fake_spawn(mode, ctx, goal_override=None):
            spawned.append(mode.name)

        scheduler._spawn = fake_spawn
        await scheduler.notify_pre_compress(messages)
        assert spawned == ["audit"]

    async def test_introspect_requires_three_compressions_and_six_hours(self, scheduler, messages):
        scheduler._last_pre_compress_time["introspect"] -= 21601
        spawned = []

        async def fake_spawn(mode, ctx, goal_override=None):
            spawned.append(mode.name)

        scheduler._spawn = fake_spawn
        await scheduler.notify_pre_compress(messages)
        await scheduler.notify_pre_compress(messages)
        assert "introspect" not in spawned

        await scheduler.notify_pre_compress(messages)
        assert spawned.count("introspect") == 1

    async def test_introspect_waits_for_minimum_interval(self, scheduler, messages):
        spawned = []

        async def fake_spawn(mode, ctx, goal_override=None):
            spawned.append(mode.name)

        scheduler._spawn = fake_spawn
        for _ in range(3):
            await scheduler.notify_pre_compress(messages)
        assert "introspect" not in spawned

        scheduler._last_pre_compress_time["introspect"] -= 21601
        await scheduler.notify_pre_compress(messages)
        assert spawned.count("introspect") == 1

    async def test_pre_compress_resets_periodic_tool_fallback(self, scheduler, messages):
        audit = scheduler._mode_loader.get("audit")
        audit.every_n_tool_calls = 100
        spawned = []

        async def fake_spawn(mode, ctx, goal_override=None):
            spawned.append(mode.name)

        scheduler._spawn = fake_spawn
        scheduler._total_tool_calls = 75
        await scheduler.notify_pre_compress(messages)
        assert scheduler._last_tool_calls["audit"] == 75

        await scheduler.notify_cycle_complete(1, messages, tool_calls_this_cycle=99)
        assert spawned.count("audit") == 1
        await scheduler.notify_cycle_complete(2, messages, tool_calls_this_cycle=1)
        assert spawned.count("audit") == 2

    async def test_pre_compress_does_not_update_last_cycle(self, scheduler, messages):
        async def fake_spawn(mode, ctx, goal_override=None):
            pass

        scheduler._spawn = fake_spawn
        scheduler._last_cycle["summarize"] = 0
        await scheduler.notify_pre_compress(messages)
        assert scheduler._last_cycle.get("summarize") == 0


# ---------------------------------------------------------------------------
# SubconsciousScheduler: _has_active_mode
# ---------------------------------------------------------------------------


class TestHasActiveMode:
    def test_returns_false_when_no_bubble(self, scheduler):
        assert not scheduler._has_active_mode("audit")

    def test_returns_true_when_running(self, scheduler, store, messages):
        bubble = store.create("goal", messages, max_cycles=5)
        assert isinstance(bubble, Bubble)
        scheduler._active_by_mode["audit"] = bubble.id
        assert scheduler._has_active_mode("audit")

    def test_returns_false_and_clears_when_terminal(self, scheduler, store, messages):
        bubble = store.create("goal", messages, max_cycles=5)
        assert isinstance(bubble, Bubble)
        store.mark_done(bubble)
        scheduler._active_by_mode["audit"] = bubble.id
        assert not scheduler._has_active_mode("audit")
        assert scheduler._active_by_mode["audit"] is None

    def test_returns_false_for_unknown_bubble_id(self, scheduler):
        scheduler._active_by_mode["audit"] = "bbl_nonexistent"
        assert not scheduler._has_active_mode("audit")
        assert scheduler._active_by_mode["audit"] is None


# ---------------------------------------------------------------------------
# SubconsciousScheduler: _spawn creates bubble and task
# ---------------------------------------------------------------------------


class TestSchedulerSpawn:
    async def test_spawn_creates_bubble_in_store(self, scheduler, store, mode_loader, messages):
        with patch.object(SubconsciousMiniLoop, "run", new_callable=lambda: lambda self: asyncio.sleep(0)):
            await scheduler._spawn(mode_loader.get("audit"), messages)
        assert len(store._active) + len(store._history) >= 1

    async def test_spawn_persists_current_provider_and_model_in_index(
        self, scheduler, store, mode_loader, messages, mock_brain, tmp_path
    ):
        from coworker.agent.bubble_log_index import load_completed_bubble_index

        mock_brain.think = AsyncMock(
            return_value=LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="done", name="bubble_done", arguments={"result": "ok"})
                ],
                stop_reason="tool_use",
                model="mock-model",
                usage={},
            )
        )
        scheduler._create_brain = MagicMock(return_value=mock_brain)

        await scheduler._spawn(mode_loader.get("audit"), messages)
        bubble = next(iter(store._active.values()))
        assert bubble.provider == "mock"
        assert bubble.model == "mock-model"
        assert bubble.brain.current_provider_name == bubble.provider
        assert bubble.brain.current_model == bubble.model
        await bubble.task

        indexed = load_completed_bubble_index(tmp_path / "subconscious")
        assert indexed is not None
        assert indexed[0]["provider"] == "mock"
        assert indexed[0]["model"] == "mock-model"

    async def test_spawn_sets_active_mode(self, scheduler, store, mode_loader, messages):
        with patch.object(SubconsciousMiniLoop, "run", new_callable=lambda: lambda self: asyncio.sleep(0)):
            await scheduler._spawn(mode_loader.get("audit"), messages)
        assert scheduler._active_by_mode["audit"] is not None

    async def test_spawn_skips_when_store_at_capacity(self, mock_cfg, mock_brain, mock_registry,
                                                        mock_prompt_builder, mock_inbox, mock_ilog,
                                                        tmp_path, messages, mode_loader):
        small_store = BubbleStore(max_concurrent=1)
        existing = small_store.create("existing", messages, max_cycles=5)
        assert isinstance(existing, Bubble)

        sched = SubconsciousScheduler(
            cfg=mock_cfg,
            bubble_store=small_store,
            brain=mock_brain,
            tool_registry=mock_registry,
            prompt_builder=mock_prompt_builder,
            short_term=MagicMock(),
            inbox=mock_inbox,
            logs_dir=str(tmp_path),
            interaction_log=mock_ilog,
            mode_loader=mode_loader,
        )

        await sched._spawn(mode_loader.get("audit"), messages)
        assert len(small_store._active) == 1
        assert sched._active_by_mode["audit"] is None


# ---------------------------------------------------------------------------
# SubconsciousScheduler: _on_done callback
# ---------------------------------------------------------------------------


class TestOnDone:
    def test_on_done_logs_to_interaction_log(self, scheduler, store, messages, mock_ilog):
        bubble = store.create("goal", messages, max_cycles=5)
        assert isinstance(bubble, Bubble)
        bubble.result = "审计通过"
        bubble.cycles_used = 3
        store.mark_done(bubble)

        task = MagicMock()
        scheduler._active_by_mode["audit"] = bubble.id
        scheduler._on_done("audit", bubble.id, task)

        mock_ilog.log_subconscious_done.assert_called_once()
        call_kwargs = mock_ilog.log_subconscious_done.call_args
        assert call_kwargs.kwargs["mode"] == "audit" or call_kwargs.args[0] == "audit"

    def test_on_done_clears_active_slot(self, scheduler, store, messages):
        bubble = store.create("goal", messages, max_cycles=5)
        assert isinstance(bubble, Bubble)
        store.mark_done(bubble)
        scheduler._active_by_mode["audit"] = bubble.id

        task = MagicMock()
        scheduler._on_done("audit", bubble.id, task)
        assert scheduler._active_by_mode["audit"] is None

    def test_on_done_does_not_crash_if_bubble_missing(self, scheduler):
        task = MagicMock()
        scheduler._on_done("audit", "bbl_nonexistent", task)

    def test_on_done_accumulates_telemetry(self, scheduler, store, messages):
        bubble = store.create("goal", messages, max_cycles=5)
        assert isinstance(bubble, Bubble)
        bubble.result = "审计通过"
        store.mark_done(bubble)
        scheduler._active_by_mode["audit"] = bubble.id

        task = MagicMock()
        scheduler._on_done("audit", bubble.id, task)

        assert scheduler._mode_run_count.get("audit") == 1
        assert "审计通过" in scheduler._mode_recent_results.get("audit", [])
        assert scheduler._mode_last_run_wall.get("audit") is not None


# ---------------------------------------------------------------------------
# SubconsciousScheduler: state persistence across restart
# ---------------------------------------------------------------------------


class TestStatePersistence:
    def _make_scheduler(self, mock_cfg, store, mock_brain, mock_registry,
                        mock_prompt_builder, mock_inbox, mock_ilog, state_path, mode_loader):
        return SubconsciousScheduler(
            cfg=mock_cfg,
            bubble_store=store,
            brain=mock_brain,
            tool_registry=mock_registry,
            prompt_builder=mock_prompt_builder,
            short_term=MagicMock(),
            inbox=mock_inbox,
            logs_dir="logs",
            interaction_log=mock_ilog,
            state_path=state_path,
            mode_loader=mode_loader,
        )

    def test_save_and_load_tool_call_counters(self, mock_cfg, store, mock_brain, mock_registry,
                                               mock_prompt_builder, mock_inbox, mock_ilog,
                                               tmp_path, mode_loader):
        state_path = tmp_path / "subconscious_state.json"
        s1 = self._make_scheduler(mock_cfg, store, mock_brain, mock_registry,
                                  mock_prompt_builder, mock_inbox, mock_ilog, state_path, mode_loader)
        s1._total_tool_calls = 42
        s1._last_tool_calls["audit"] = 30
        s1.save_state()
        assert state_path.exists()

        s2 = self._make_scheduler(mock_cfg, store, mock_brain, mock_registry,
                                  mock_prompt_builder, mock_inbox, mock_ilog, state_path, mode_loader)
        assert s2._total_tool_calls == 42
        assert s2._last_tool_calls.get("audit") == 30

    def test_time_gap_preserved_across_restart(self, mock_cfg, store, mock_brain, mock_registry,
                                               mock_prompt_builder, mock_inbox, mock_ilog,
                                               tmp_path, mode_loader):
        import time as _time
        state_path = tmp_path / "subconscious_state.json"
        s1 = self._make_scheduler(mock_cfg, store, mock_brain, mock_registry,
                                  mock_prompt_builder, mock_inbox, mock_ilog, state_path, mode_loader)
        s1._last_time["audit"] = _time.monotonic() - 100.0
        s1.save_state()

        s2 = self._make_scheduler(mock_cfg, store, mock_brain, mock_registry,
                                  mock_prompt_builder, mock_inbox, mock_ilog, state_path, mode_loader)
        gap = _time.monotonic() - s2._last_time.get("audit", _time.monotonic())
        assert 95 < gap < 120

    def test_pre_compress_cadence_persists_across_restart(
        self,
        mock_cfg,
        store,
        mock_brain,
        mock_registry,
        mock_prompt_builder,
        mock_inbox,
        mock_ilog,
        tmp_path,
        mode_loader,
    ):
        import time as _time

        state_path = tmp_path / "subconscious_state.json"
        s1 = self._make_scheduler(
            mock_cfg,
            store,
            mock_brain,
            mock_registry,
            mock_prompt_builder,
            mock_inbox,
            mock_ilog,
            state_path,
            mode_loader,
        )
        s1._compression_count = 8
        s1._last_pre_compress_count["introspect"] = 6
        s1._last_pre_compress_time["introspect"] = _time.monotonic() - 7200
        s1.save_state()

        s2 = self._make_scheduler(
            mock_cfg,
            store,
            mock_brain,
            mock_registry,
            mock_prompt_builder,
            mock_inbox,
            mock_ilog,
            state_path,
            mode_loader,
        )
        assert s2._compression_count == 8
        assert s2._last_pre_compress_count["introspect"] == 6
        gap = _time.monotonic() - s2._last_pre_compress_time["introspect"]
        assert 7195 < gap < 7220

    def test_no_state_path_is_noop(self, mock_cfg, store, mock_brain, mock_registry,
                                   mock_prompt_builder, mock_inbox, mock_ilog, mode_loader):
        s = SubconsciousScheduler(
            cfg=mock_cfg, bubble_store=store, brain=mock_brain, tool_registry=mock_registry,
            prompt_builder=mock_prompt_builder, short_term=MagicMock(), inbox=mock_inbox,
            logs_dir="logs", interaction_log=mock_ilog, state_path=None, mode_loader=mode_loader,
        )
        s.save_state()

    def test_load_missing_file_is_noop(self, mock_cfg, store, mock_brain, mock_registry,
                                       mock_prompt_builder, mock_inbox, mock_ilog, tmp_path, mode_loader):
        state_path = tmp_path / "does_not_exist.json"
        s = self._make_scheduler(mock_cfg, store, mock_brain, mock_registry,
                                 mock_prompt_builder, mock_inbox, mock_ilog, state_path, mode_loader)
        assert s._total_tool_calls == 0

    def test_garden_index_persists(self, mock_cfg, store, mock_brain, mock_registry,
                                   mock_prompt_builder, mock_inbox, mock_ilog, tmp_path, mode_loader):
        state_path = tmp_path / "subconscious_state.json"
        s1 = self._make_scheduler(mock_cfg, store, mock_brain, mock_registry,
                                  mock_prompt_builder, mock_inbox, mock_ilog, state_path, mode_loader)
        s1._garden_index = 3
        s1.save_state()
        s2 = self._make_scheduler(mock_cfg, store, mock_brain, mock_registry,
                                  mock_prompt_builder, mock_inbox, mock_ilog, state_path, mode_loader)
        assert s2._garden_index == 3

    def test_telemetry_persists(self, mock_cfg, store, mock_brain, mock_registry,
                                mock_prompt_builder, mock_inbox, mock_ilog, tmp_path, mode_loader):
        state_path = tmp_path / "subconscious_state.json"
        s1 = self._make_scheduler(mock_cfg, store, mock_brain, mock_registry,
                                  mock_prompt_builder, mock_inbox, mock_ilog, state_path, mode_loader)
        s1._mode_run_count["audit"] = 7
        s1._mode_recent_results["audit"] = ["通过", "警告"]
        s1.save_state()
        s2 = self._make_scheduler(mock_cfg, store, mock_brain, mock_registry,
                                  mock_prompt_builder, mock_inbox, mock_ilog, state_path, mode_loader)
        assert s2._mode_run_count.get("audit") == 7
        assert s2._mode_recent_results.get("audit") == ["通过", "警告"]

    def test_old_format_migration(self, mock_cfg, store, mock_brain, mock_registry,
                                  mock_prompt_builder, mock_inbox, mock_ilog, tmp_path, mode_loader):
        """Old flat-key state format should be migrated without raising."""
        import json
        import time as _t
        state_path = tmp_path / "old_state.json"
        now_wall = __import__("datetime").datetime.now().timestamp()
        old_data = {
            "total_tool_calls": 15,
            "last_audit_tool_calls": 10,
            "last_summarize_tool_calls": 0,
            "last_explore_tool_calls": 0,
            "last_introspect_tool_calls": 0,
            "garden_index": 2,
            "palace_use_counts": {},
            "palace_last_garden_wall": {},
            "last_audit_wall": now_wall - 50.0,
            "last_summarize_wall": now_wall,
            "last_explore_wall": now_wall,
            "last_introspect_wall": now_wall,
        }
        state_path.write_text(json.dumps(old_data), encoding="utf-8")
        s = self._make_scheduler(mock_cfg, store, mock_brain, mock_registry,
                                 mock_prompt_builder, mock_inbox, mock_ilog, state_path, mode_loader)
        assert s._total_tool_calls == 15
        assert s._last_tool_calls.get("audit") == 10
        assert s._garden_index == 2
        # Time gap should be ~50s
        gap = _t.monotonic() - s._last_time.get("audit", _t.monotonic())
        assert 45 < gap < 65


# ---------------------------------------------------------------------------
# SubconsciousScheduler: mode change tracking
# ---------------------------------------------------------------------------


class TestModeChangeTracking:
    """Verify that _maybe_reload_modes detects content changes and that
    _build_telemetry_message reflects the change status correctly."""

    def _make_sched(self, mock_cfg, store, mock_brain, mock_registry,
                    mock_prompt_builder, mock_inbox, mock_ilog, tmp_path, ml):
        return SubconsciousScheduler(
            cfg=mock_cfg, bubble_store=store, brain=mock_brain, tool_registry=mock_registry,
            prompt_builder=mock_prompt_builder, short_term=MagicMock(), inbox=mock_inbox,
            logs_dir=str(tmp_path), interaction_log=mock_ilog, mode_loader=ml,
        )

    def test_initial_hashes_computed(self, mock_cfg, store, mock_brain, mock_registry,
                                     mock_prompt_builder, mock_inbox, mock_ilog, tmp_path, mode_loader):
        sched = self._make_sched(mock_cfg, store, mock_brain, mock_registry,
                                 mock_prompt_builder, mock_inbox, mock_ilog, tmp_path, mode_loader)
        for m in mode_loader.list_all():
            assert m.name in sched._mode_content_hash
            assert sched._mode_content_hash[m.name]  # non-empty hash

    def test_change_detected_on_reload(self, mock_cfg, store, mock_brain, mock_registry,
                                       mock_prompt_builder, mock_inbox, mock_ilog, tmp_path, mode_loader):
        import time as _t
        sched = self._make_sched(mock_cfg, store, mock_brain, mock_registry,
                                 mock_prompt_builder, mock_inbox, mock_ilog, tmp_path, mode_loader)
        old_hash = sched._mode_content_hash.get("audit")
        assert old_hash is not None
        assert sched._mode_last_changed_wall.get("audit") is None  # not changed yet

        # Simulate a content change.
        mode_loader.get("audit").every_n_cycles = 99
        # Force reload by moving the last_reload time back.
        from coworker.agent.subconscious import _MODE_RELOAD_INTERVAL
        sched._last_mode_reload -= _MODE_RELOAD_INTERVAL + 1
        sched._maybe_reload_modes(_t.monotonic())

        new_hash = sched._mode_content_hash.get("audit")
        assert new_hash != old_hash
        assert sched._mode_last_changed_wall.get("audit") is not None

    def test_no_change_leaves_timestamp_untouched(self, mock_cfg, store, mock_brain,
                                                   mock_registry, mock_prompt_builder, mock_inbox,
                                                   mock_ilog, tmp_path, mode_loader):
        import time as _t
        sched = self._make_sched(mock_cfg, store, mock_brain, mock_registry,
                                 mock_prompt_builder, mock_inbox, mock_ilog, tmp_path, mode_loader)
        from coworker.agent.subconscious import _MODE_RELOAD_INTERVAL
        sched._last_mode_reload -= _MODE_RELOAD_INTERVAL + 1
        sched._maybe_reload_modes(_t.monotonic())
        # Nothing changed, so no timestamp should appear.
        assert sched._mode_last_changed_wall.get("audit") is None

    def test_telemetry_shows_changed_star_marker(self, mock_cfg, store, mock_brain,
                                                  mock_registry, mock_prompt_builder, mock_inbox,
                                                  mock_ilog, tmp_path, mode_loader):
        sched = self._make_sched(mock_cfg, store, mock_brain, mock_registry,
                                 mock_prompt_builder, mock_inbox, mock_ilog, tmp_path, mode_loader)
        import datetime as _dt
        # Mark audit as changed after last meta run.
        sched._mode_last_changed_wall["audit"] = _dt.datetime.now().timestamp()
        sched._mode_last_run_wall["meta"] = _dt.datetime.now().timestamp() - 3600  # meta ran 1h ago

        msg = sched._build_telemetry_message()
        assert "★已变更" in msg.content
        assert "[变更摘要]" in msg.content
        assert "深度审视" in msg.content

    def test_telemetry_no_changes_shows_light_check(self, mock_cfg, store, mock_brain,
                                                     mock_registry, mock_prompt_builder, mock_inbox,
                                                     mock_ilog, tmp_path, mode_loader):
        sched = self._make_sched(mock_cfg, store, mock_brain, mock_registry,
                                 mock_prompt_builder, mock_inbox, mock_ilog, tmp_path, mode_loader)
        import datetime as _dt
        # meta ran recently, no mode has been changed since.
        sched._mode_last_run_wall["meta"] = _dt.datetime.now().timestamp() - 100
        # No _mode_last_changed_wall entries set.
        msg = sched._build_telemetry_message()
        assert "★" not in msg.content
        assert "轻量健康检查" in msg.content

    def test_telemetry_shows_protected_and_purpose(self, mock_cfg, store, mock_brain,
                                                     mock_registry, mock_prompt_builder, mock_inbox,
                                                     mock_ilog, tmp_path):
        protected_mode = _make_mode("core", body="body", protected=True,
                                    purpose="核心安全机制", retire_after="")
        retire_mode = _make_mode("optional", body="body", protected=False,
                                 purpose="可选功能", retire_after="场景切换后")
        ml = _populate_loader([protected_mode, retire_mode])
        sched = self._make_sched(mock_cfg, store, mock_brain, mock_registry,
                                 mock_prompt_builder, mock_inbox, mock_ilog, tmp_path, ml)
        msg = sched._build_telemetry_message()
        assert "🔒受保护" in msg.content
        assert "⚠退休条件" in msg.content
        assert "场景切换后" in msg.content
        assert "核心安全机制" in msg.content
        assert "可选功能" in msg.content

    def test_change_tracking_persists(self, mock_cfg, store, mock_brain, mock_registry,
                                      mock_prompt_builder, mock_inbox, mock_ilog, tmp_path, mode_loader):
        import datetime as _dt
        state_path = tmp_path / "state.json"
        s1 = SubconsciousScheduler(
            cfg=mock_cfg, bubble_store=store, brain=mock_brain, tool_registry=mock_registry,
            prompt_builder=mock_prompt_builder, short_term=MagicMock(), inbox=mock_inbox,
            logs_dir=str(tmp_path), state_path=state_path, mode_loader=mode_loader,
        )
        now_wall = _dt.datetime.now().timestamp()
        s1._mode_content_hash["audit"] = "abc123"
        s1._mode_last_changed_wall["audit"] = now_wall
        s1.save_state()

        s2 = SubconsciousScheduler(
            cfg=mock_cfg, bubble_store=store, brain=mock_brain, tool_registry=mock_registry,
            prompt_builder=mock_prompt_builder, short_term=MagicMock(), inbox=mock_inbox,
            logs_dir=str(tmp_path), state_path=state_path, mode_loader=mode_loader,
        )
        # Hash is restored from state; mode was in-memory so re-hash may differ — but
        # last_changed_wall should be restored.
        assert s2._mode_last_changed_wall.get("audit") == now_wall


# ---------------------------------------------------------------------------
# SubconsciousScheduler: palace gardener
# ---------------------------------------------------------------------------


def _palace(name: str, tags: list[str]) -> Palace:
    return Palace(name=name, when_to_attach=f"挂载 {name}", body=f"{name} 卡片", memory_tags=tags)


def _garden_scheduler(mock_cfg, store, mock_brain, mock_registry, mock_prompt_builder,
                      mock_inbox, mock_ilog, tmp_path, palaces, mems,
                      every_seconds=0, use_threshold=0, min_interval_seconds=0):
    garden_mode = _make_mode(
        "garden",
        body="garden {bubble_id} {goal} {max_cycles}",
        trigger="garden",
        context_builder="garden",
        every_seconds=every_seconds,
        use_threshold=use_threshold,
        min_interval=min_interval_seconds,
    )
    ml = _populate_loader([garden_mode])

    palace_loader = MagicMock()
    palace_loader.list_all.return_value = palaces
    palace_loader.list_names.return_value = [p.name for p in palaces]
    long_term = MagicMock()
    long_term._mem = object()
    long_term.query_by_tags = AsyncMock(return_value=mems)
    return SubconsciousScheduler(
        cfg=mock_cfg, bubble_store=store, brain=mock_brain, tool_registry=mock_registry,
        prompt_builder=mock_prompt_builder, short_term=MagicMock(), inbox=mock_inbox,
        logs_dir=str(tmp_path), interaction_log=mock_ilog,
        palace_loader=palace_loader, long_term=long_term, mode_loader=ml,
    )


class TestGardenScheduler:
    async def test_round_robin_selects_each_palace(self, mock_cfg, store, mock_brain,
                                                   mock_registry, mock_prompt_builder, mock_inbox,
                                                   mock_ilog, tmp_path):
        mems = [{"id": "m1", "category": "experience", "content": "x", "tags": ["a"], "relevance": 0.9}]
        sched = _garden_scheduler(mock_cfg, store, mock_brain, mock_registry, mock_prompt_builder,
                                  mock_inbox, mock_ilog, tmp_path,
                                  palaces=[_palace("p0", ["a"]), _palace("p1", ["b"])], mems=mems,
                                  every_seconds=1)
        captured = []

        async def fake_spawn(mode, ctx, goal_override=None):
            captured.append(goal_override)

        sched._spawn = fake_spawn
        await sched._spawn_garden(0.0)
        await sched._spawn_garden(0.0)
        assert "p0" in captured[0] and "p1" in captured[1]

    async def test_skips_when_no_palaces(self, mock_cfg, store, mock_brain, mock_registry,
                                         mock_prompt_builder, mock_inbox, mock_ilog, tmp_path):
        sched = _garden_scheduler(mock_cfg, store, mock_brain, mock_registry, mock_prompt_builder,
                                  mock_inbox, mock_ilog, tmp_path, palaces=[], mems=[])
        called = []
        sched._spawn = lambda *a, **k: called.append(1)
        await sched._spawn_garden(0.0)
        assert called == []

    async def test_spawns_even_with_no_tagged_memories(self, mock_cfg, store, mock_brain,
                                                       mock_registry, mock_prompt_builder, mock_inbox,
                                                       mock_ilog, tmp_path):
        sched = _garden_scheduler(mock_cfg, store, mock_brain, mock_registry, mock_prompt_builder,
                                  mock_inbox, mock_ilog, tmp_path,
                                  palaces=[_palace("p0", ["a"])], mems=[], every_seconds=1)
        called = []

        async def fake_spawn(mode, ctx, goal_override=None):
            called.append(1)

        sched._spawn = fake_spawn
        await sched._spawn_garden(0.0)
        assert called == [1]

    async def test_garden_context_includes_card_and_memories(self, mock_cfg, store, mock_brain,
                                                             mock_registry, mock_prompt_builder, mock_inbox,
                                                             mock_ilog, tmp_path):
        mems = [{"id": "mem_xyz", "category": "experience", "content": "登录复现要点", "tags": ["a"], "relevance": 0.9}]
        sched = _garden_scheduler(mock_cfg, store, mock_brain, mock_registry, mock_prompt_builder,
                                  mock_inbox, mock_ilog, tmp_path,
                                  palaces=[_palace("p0", ["a"])], mems=mems, every_seconds=1)
        captured = {}

        async def fake_spawn(mode, ctx, goal_override=None):
            captured["ctx"] = ctx

        sched._spawn = fake_spawn
        await sched._spawn_garden(0.0)
        texts = [m.content for m in captured["ctx"]]
        assert any("[宫殿:p0]" in t for t in texts)
        assert any("mem_xyz" in t for t in texts)

    async def test_garden_triggers_by_time(self, mock_cfg, store, mock_brain, mock_registry,
                                           mock_prompt_builder, mock_inbox, mock_ilog, tmp_path, messages):
        mems = [{"id": "m1", "category": "experience", "content": "x", "tags": ["a"], "relevance": 0.9}]
        sched = _garden_scheduler(mock_cfg, store, mock_brain, mock_registry, mock_prompt_builder,
                                  mock_inbox, mock_ilog, tmp_path,
                                  palaces=[_palace("p0", ["a"])], mems=mems, every_seconds=1)
        spawned = []

        async def fake_spawn(mode, ctx, goal_override=None):
            spawned.append(mode.name)

        sched._spawn = fake_spawn
        await sched.notify_cycle_complete(1, messages)
        assert "garden" in spawned


def _done_palace_bubble(store, palaces):
    b = store.create("g", [], max_cycles=1)
    assert isinstance(b, Bubble)
    b.status = "done"
    b.palaces = list(palaces)
    return b


class TestGardenDebt:
    async def test_usage_debt_triggers_and_resets(self, mock_cfg, store, mock_brain,
                                                  mock_registry, mock_prompt_builder, mock_inbox,
                                                  mock_ilog, tmp_path, messages):
        mems = [{"id": "m1", "category": "experience", "content": "x", "tags": ["a"], "relevance": 0.9}]
        sched = _garden_scheduler(mock_cfg, store, mock_brain, mock_registry, mock_prompt_builder,
                                  mock_inbox, mock_ilog, tmp_path,
                                  palaces=[_palace("p0", ["a"])], mems=mems, use_threshold=2)
        spawned = []

        async def fake_spawn(mode, ctx, goal_override=None):
            spawned.append(goal_override)

        sched._spawn = fake_spawn
        _done_palace_bubble(store, ["p0"])
        _done_palace_bubble(store, ["p0"])
        await sched.notify_cycle_complete(1, messages)

        assert spawned and "p0" in spawned[0]
        assert sched._palace_use_counts["p0"] == 0

    async def test_debt_below_threshold_does_not_trigger(self, mock_cfg, store, mock_brain,
                                                         mock_registry, mock_prompt_builder, mock_inbox,
                                                         mock_ilog, tmp_path, messages):
        mems = [{"id": "m1", "category": "experience", "content": "x", "tags": ["a"], "relevance": 0.9}]
        sched = _garden_scheduler(mock_cfg, store, mock_brain, mock_registry, mock_prompt_builder,
                                  mock_inbox, mock_ilog, tmp_path,
                                  palaces=[_palace("p0", ["a"])], mems=mems, use_threshold=3)
        spawned = []
        sched._spawn = lambda *a, **k: spawned.append(1)
        _done_palace_bubble(store, ["p0"])
        await sched.notify_cycle_complete(1, messages)
        assert spawned == []

    async def test_hottest_palace_gardened_first(self, mock_cfg, store, mock_brain,
                                                 mock_registry, mock_prompt_builder, mock_inbox,
                                                 mock_ilog, tmp_path):
        mems = [{"id": "m1", "category": "experience", "content": "x", "tags": ["a"], "relevance": 0.9}]
        sched = _garden_scheduler(mock_cfg, store, mock_brain, mock_registry, mock_prompt_builder,
                                  mock_inbox, mock_ilog, tmp_path,
                                  palaces=[_palace("p0", ["a"]), _palace("p1", ["b"])], mems=mems,
                                  use_threshold=1)
        sched._palace_use_counts = {"p0": 1, "p1": 5}
        captured = []

        async def fake_spawn(mode, ctx, goal_override=None):
            captured.append(goal_override)

        sched._spawn = fake_spawn
        await sched._spawn_garden(100.0)
        assert "p1" in captured[0]

    async def test_min_interval_blocks_regarden(self, mock_cfg, store, mock_brain,
                                                mock_registry, mock_prompt_builder, mock_inbox,
                                                mock_ilog, tmp_path):
        mems = [{"id": "m1", "category": "experience", "content": "x", "tags": ["a"], "relevance": 0.9}]
        sched = _garden_scheduler(mock_cfg, store, mock_brain, mock_registry, mock_prompt_builder,
                                  mock_inbox, mock_ilog, tmp_path,
                                  palaces=[_palace("p0", ["a"])], mems=mems,
                                  use_threshold=1, min_interval_seconds=1000)
        sched._palace_use_counts = {"p0": 5}
        captured = []

        async def fake_spawn(mode, ctx, goal_override=None):
            captured.append(goal_override)

        sched._spawn = fake_spawn
        r1 = await sched._spawn_garden(100.0)
        assert r1 is True and len(captured) == 1
        sched._palace_use_counts["p0"] = 5
        r2 = await sched._spawn_garden(200.0)
        assert r2 is False and len(captured) == 1

    async def test_each_bubble_counted_once(self, mock_cfg, store, mock_brain, mock_registry,
                                            mock_prompt_builder, mock_inbox, mock_ilog, tmp_path):
        sched = _garden_scheduler(mock_cfg, store, mock_brain, mock_registry, mock_prompt_builder,
                                  mock_inbox, mock_ilog, tmp_path,
                                  palaces=[_palace("p0", ["a"])], mems=[])
        _done_palace_bubble(store, ["p0"])
        sched._tally_palace_usage()
        sched._tally_palace_usage()
        assert sched._palace_use_counts["p0"] == 1

    async def test_use_count_persists(self, mock_cfg, store, mock_brain, mock_registry,
                                      mock_prompt_builder, mock_inbox, mock_ilog, tmp_path):
        state = tmp_path / "subc.json"
        s1 = _garden_scheduler(mock_cfg, store, mock_brain, mock_registry, mock_prompt_builder,
                               mock_inbox, mock_ilog, tmp_path, palaces=[_palace("p0", ["a"])], mems=[])
        s1._state_path = state
        s1._palace_use_counts = {"p0": 4}
        s1.save_state()
        s2 = _garden_scheduler(mock_cfg, store, mock_brain, mock_registry, mock_prompt_builder,
                               mock_inbox, mock_ilog, tmp_path, palaces=[_palace("p0", ["a"])], mems=[])
        s2._state_path = state
        s2._load_state()
        assert s2._palace_use_counts == {"p0": 4}


class TestGardenStaleness:
    async def test_stale_floor_is_per_palace(self, mock_cfg, store, mock_brain,
                                             mock_registry, mock_prompt_builder, mock_inbox,
                                             mock_ilog, tmp_path):
        sched = _garden_scheduler(mock_cfg, store, mock_brain, mock_registry, mock_prompt_builder,
                                  mock_inbox, mock_ilog, tmp_path,
                                  palaces=[_palace("p0", ["a"])], mems=[], every_seconds=100)
        assert sched._has_stale_palace(1000.0) is True
        sched._palace_last_garden_time = {"p0": 1000.0}
        assert sched._has_stale_palace(1050.0) is False
        assert sched._has_stale_palace(1200.0) is True

    async def test_stale_disabled_when_seconds_zero(self, mock_cfg, store, mock_brain,
                                                    mock_registry, mock_prompt_builder, mock_inbox,
                                                    mock_ilog, tmp_path):
        sched = _garden_scheduler(mock_cfg, store, mock_brain, mock_registry, mock_prompt_builder,
                                  mock_inbox, mock_ilog, tmp_path,
                                  palaces=[_palace("p0", ["a"])], mems=[], every_seconds=0)
        assert sched._has_stale_palace(1e9) is False

    async def test_last_garden_time_persists(self, mock_cfg, store, mock_brain,
                                             mock_registry, mock_prompt_builder, mock_inbox,
                                             mock_ilog, tmp_path):
        state = tmp_path / "subc.json"
        s1 = _garden_scheduler(mock_cfg, store, mock_brain, mock_registry, mock_prompt_builder,
                               mock_inbox, mock_ilog, tmp_path,
                               palaces=[_palace("p0", ["a"])], mems=[], every_seconds=100)
        s1._state_path = state
        import time as _t
        s1._palace_last_garden_time = {"p0": _t.monotonic()}
        s1.save_state()
        s2 = _garden_scheduler(mock_cfg, store, mock_brain, mock_registry, mock_prompt_builder,
                               mock_inbox, mock_ilog, tmp_path,
                               palaces=[_palace("p0", ["a"])], mems=[], every_seconds=100)
        s2._state_path = state
        s2._load_state()
        assert "p0" in s2._palace_last_garden_time
        assert s2._has_stale_palace(_t.monotonic()) is False
