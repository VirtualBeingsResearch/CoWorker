from __future__ import annotations

from datetime import datetime, timedelta

from coworker.agent.event_collector import RuntimeEventCollector


class _FakeLogStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int | float]] = []
        now = datetime.now()
        self.entries = [
            {
                "type": "message_in",
                "seq": 0,
                "ts": (now - timedelta(days=3)).isoformat(),
                "participant_id": "alice",
                "source": "rest",
                "content": "old alice",
            },
            {"type": "system_prompt", "seq": 1, "ts": now.isoformat(), "content": "noise"},
            {
                "type": "message_in",
                "seq": 2,
                "ts": (now - timedelta(minutes=5)).isoformat(),
                "participant_id": "alice",
                "source": "rest",
                "content": "hello alice",
            },
            {
                "type": "message_in",
                "seq": 3,
                "ts": (now + timedelta(days=1)).isoformat(),
                "participant_id": "alice",
                "source": "rest",
                "content": "future alice",
            },
        ]

    def read_recent_days(self, days: int | float):
        raise AssertionError("event replay should use tail when tail_lines is set")

    def read_tail(self, max_lines: int):
        self.calls.append(("read_tail", max_lines))
        return self.entries[-max_lines:], True

    def read_all(self):
        raise AssertionError("event replay should not read all history when tail_lines is set")


def test_recent_uses_tail_window_and_filters_days():
    store = _FakeLogStore()
    collector = RuntimeEventCollector(store, redact=lambda s: s.replace("alice", "Alice"))  # type: ignore[arg-type]

    events = collector.recent(10, days=1, tail_lines=100)

    assert store.calls == [("read_tail", 100)]
    assert events == [
        {
            "seq": 2,
            "ts": store.entries[2]["ts"],
            "type": "message_in",
            "participant_id": "Alice",
            "source": "rest",
            "content": "hello Alice",
        }
    ]


def test_recent_zero_limit_skips_store_read():
    store = _FakeLogStore()
    collector = RuntimeEventCollector(store, redact=lambda s: s)  # type: ignore[arg-type]

    assert collector.recent(0, days=3, tail_lines=100) == []
    assert store.calls == []


def test_on_entry_keeps_thinking_flag():
    store = _FakeLogStore()
    collector = RuntimeEventCollector(store, redact=lambda s: s)  # type: ignore[arg-type]

    q = collector.register()
    collector.on_entry({
        "type": "thinking_start",
        "seq": 7,
        "ts": "2026-06-16T12:00:00",
        "cycle": 3,
        "thinking": False,
    })
    event = q.get_nowait()

    assert event == {
        "seq": 7,
        "ts": "2026-06-16T12:00:00",
        "type": "thinking_start",
        "cycle": 3,
        "thinking": False,
    }


def test_on_entry_keeps_thinking_effort():
    store = _FakeLogStore()
    collector = RuntimeEventCollector(store, redact=lambda s: s)  # type: ignore[arg-type]

    q = collector.register()
    collector.on_entry({
        "type": "thinking_start",
        "seq": 7,
        "ts": "2026-06-16T12:00:00",
        "cycle": 3,
        "thinking": True,
        "thinking_effort": "high",
    })
    event = q.get_nowait()

    assert event == {
        "seq": 7,
        "ts": "2026-06-16T12:00:00",
        "type": "thinking_start",
        "cycle": 3,
        "thinking": True,
        "thinking_effort": "high",
    }


def test_query_memory_tool_call_exposes_query_summary_args():
    store = _FakeLogStore()
    collector = RuntimeEventCollector(store, redact=lambda s: s.replace("alice", "Alice"))  # type: ignore[arg-type]
    q = collector.register()

    collector.on_entry({
        "type": "tool_call",
        "seq": 8,
        "ts": "2026-06-16T12:00:00",
        "id": "tc1",
        "name": "query_memory",
        "arguments": {"query": "alice 的偏好", "start": "2026-06-16T09:00:00", "end": "2026-06-16T10:00:00"},
    })
    event = q.get_nowait()

    assert event["arguments"] == {
        "query": "Alice 的偏好",
        "start": "2026-06-16T09:00:00",
        "end": "2026-06-16T10:00:00",
    }
