from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from coworker.agent.bubble import BubbleStore
from coworker.agent.concurrency_hint import ConcurrencyHintTracker
from coworker.agent.loop import AgentLoop
from coworker.core.config import AgentConfig
from coworker.core.types import IncomingEvent
from coworker.i18n import locale_context
from coworker.memory.short_term import ShortTermMemory


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _event(
    participant: str,
    conversation: str | None = None,
    source: str = "telegram",
) -> IncomingEvent:
    return IncomingEvent(
        participant_id=participant,
        content="hello",
        conversation_id=conversation,
        source=source,
    )


def _bound_store(participant: str, conversation: str, max_concurrent: int = 5) -> BubbleStore:
    store = BubbleStore(max_concurrent=max_concurrent)
    bubble = store.create("goal", [], max_cycles=5)
    assert not isinstance(bubble, str)
    bubble.participant_id = participant
    bubble.conversation_id = conversation
    return store


def test_single_conversation_repeated_messages_do_not_hint():
    clock = _FakeClock()
    tracker = ConcurrencyHintTracker(clock=clock)

    assert tracker.observe([_event("alice", "c1")]) is None
    assert tracker.observe([_event("alice", "c1"), _event("alice", "c1")]) is None


def test_distinct_conversations_crossing_threshold_hint_with_count():
    clock = _FakeClock()
    tracker = ConcurrencyHintTracker(clock=clock)

    assert tracker.observe([_event("alice", "c1")]) is None
    hint = tracker.observe([_event("bob", "c2")])
    assert hint is not None
    assert hint.count == 2


def test_same_participant_different_conversations_count_separately():
    clock = _FakeClock()
    tracker = ConcurrencyHintTracker(clock=clock)

    assert tracker.observe([_event("alice", "c1")]) is None
    hint = tracker.observe([_event("alice", "c2")])
    assert hint is not None
    assert hint.count == 2


def test_internal_sources_are_not_counted():
    clock = _FakeClock()
    tracker = ConcurrencyHintTracker(clock=clock)

    batch = [
        _event("system", source="system"),
        _event("clock", source="alarm"),
        _event("watcher", source="file"),
        _event("parent", source="bubble"),
    ]
    assert tracker.observe(batch) is None

    hint = tracker.observe([_event("alice"), _event("bob")])
    assert hint is not None
    assert hint.count == 2


def test_window_expiry_drops_old_conversations():
    clock = _FakeClock()
    tracker = ConcurrencyHintTracker(clock=clock)

    assert tracker.observe([_event("alice")]) is None
    clock.advance(100)
    assert tracker.observe([_event("bob")]) is not None

    clock.advance(250)
    assert tracker.observe([_event("carol")]) is None


def test_sustained_activity_does_not_repeat_hint():
    clock = _FakeClock()
    tracker = ConcurrencyHintTracker(clock=clock)

    assert tracker.observe([_event("alice"), _event("bob")]) is not None
    assert tracker.observe([_event("alice"), _event("bob"), _event("carol")]) is None


def test_cooldown_blocks_rising_edge_within_interval():
    clock = _FakeClock()
    tracker = ConcurrencyHintTracker(clock=clock)

    assert tracker.observe([_event("alice"), _event("bob")]) is not None

    clock.advance(200)
    assert tracker.observe([_event("carol")]) is None
    clock.advance(10)
    assert tracker.observe([_event("dave")]) is None

    clock.advance(490)
    assert tracker.observe([_event("eve")]) is None
    clock.advance(10)
    hint = tracker.observe([_event("frank")])
    assert hint is not None
    assert hint.count == 2


def test_bubble_bound_conversations_are_excluded():
    clock = _FakeClock()
    store = _bound_store("alice", "c1")
    tracker = ConcurrencyHintTracker(clock=clock)

    assert tracker.observe([_event("alice", "c1"), _event("bob", "c2")], store) is None

    bubble = store.list_active()[0]
    store.mark_done(bubble)
    hint = tracker.observe([_event("alice", "c1")], store)
    assert hint is not None
    assert hint.count == 2


def test_full_bubble_store_defers_hint_until_capacity_frees():
    clock = _FakeClock()
    store = _bound_store("zoe", "c0", max_concurrent=1)
    tracker = ConcurrencyHintTracker(clock=clock)

    assert tracker.observe([_event("alice"), _event("bob")], store) is None

    store.mark_done(store.list_active()[0])
    hint = tracker.observe([_event("alice")], store)
    assert hint is not None
    assert hint.count == 2


def test_loop_injects_hint_message_into_short_term_context():
    mem = ShortTermMemory()
    store = BubbleStore()
    clock = _FakeClock()
    loop = AgentLoop.__new__(AgentLoop)
    loop._short_term = mem
    loop._concurrency_hints = ConcurrencyHintTracker(clock=clock)
    loop._bubble_store = store

    with locale_context("en"):
        loop._maybe_inject_concurrency_hint([_event("alice"), _event("bob")])

    hints = [m for m in mem.primary if m.source == "concurrency_hint"]
    assert len(hints) == 1
    assert "2 conversations" in hints[0].content
    assert "5" in hints[0].content

    before = len(mem.primary)
    loop._maybe_inject_concurrency_hint([_event("alice")])
    assert len(mem.primary) == before


_AGENT__HINT_VARS = (
    "AGENT__CONCURRENCY_HINT_WINDOW_SECONDS",
    "AGENT__CONCURRENCY_HINT_THRESHOLD",
    "AGENT__CONCURRENCY_HINT_COOLDOWN_SECONDS",
)


def test_concurrency_hint_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _AGENT__HINT_VARS:
        monkeypatch.delenv(var, raising=False)
    config = AgentConfig(_env_file=None)

    assert config.concurrency_hint_window_seconds == 180.0
    assert config.concurrency_hint_threshold == 2
    assert config.concurrency_hint_cooldown_seconds == 600.0


def test_concurrency_hint_config_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT__CONCURRENCY_HINT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("AGENT__CONCURRENCY_HINT_THRESHOLD", "3")
    monkeypatch.setenv("AGENT__CONCURRENCY_HINT_COOLDOWN_SECONDS", "120")
    config = AgentConfig(_env_file=None)

    assert config.concurrency_hint_window_seconds == 60.0
    assert config.concurrency_hint_threshold == 3
    assert config.concurrency_hint_cooldown_seconds == 120.0


def test_concurrency_hint_threshold_rejects_values_below_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT__CONCURRENCY_HINT_THRESHOLD", "1")
    with pytest.raises(ValidationError):
        AgentConfig(_env_file=None)


def test_agent_loop_wires_concurrency_hint_config() -> None:
    config = SimpleNamespace(
        agent=SimpleNamespace(
            concurrency_hint_window_seconds=60.0,
            concurrency_hint_threshold=3,
            concurrency_hint_cooldown_seconds=120.0,
        )
    )

    loop = AgentLoop(
        brain=MagicMock(),
        short_term=MagicMock(),
        long_term=MagicMock(),
        tool_registry=MagicMock(),
        identity=MagicMock(),
        prompt_builder=MagicMock(),
        inbox_watcher=MagicMock(),
        config=config,
    )

    assert loop._concurrency_hints._window_seconds == 60.0
    assert loop._concurrency_hints._threshold == 3
    assert loop._concurrency_hints._cooldown_seconds == 120.0


def test_configure_updates_parameters_for_hot_reload():
    clock = _FakeClock()
    tracker = ConcurrencyHintTracker(clock=clock)
    tracker.configure(window_seconds=60.0, threshold=3, cooldown_seconds=120.0)
    assert tracker._window_seconds == 60.0
    assert tracker._threshold == 3
    assert tracker._cooldown_seconds == 120.0

    tracker.observe([_event("alice"), _event("bob")])
    assert tracker.observe([]) is None
    hint = tracker.observe([_event("carol")])
    assert hint is not None
    assert hint.count == 3
