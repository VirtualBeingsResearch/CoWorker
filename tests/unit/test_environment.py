"""Tests for the environment sensing channel.

Covers:
- SourceScheduleState serialization round-trip
- EnvironmentLoader frontmatter parsing + error handling
- SourceExecutor inline mode (signal emission + dedup + error isolation)
- EnvironmentRuntime multi-trigger scheduling (every_seconds, cold_floor, manual)
- EnvironmentChannel (send returns error, agent_instructions, list_connections)
- Protocol JSON-RPC encode/decode
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from textwrap import dedent

import pytest

from coworker.channels.environment.channel import EnvironmentChannel
from coworker.channels.environment.executor import SourceExecutor
from coworker.channels.environment.loader import EnvironmentLoader
from coworker.channels.environment.protocol import (
    decode_line,
    encode_request,
    encode_response,
)
from coworker.channels.environment.runtime import EnvironmentRuntime, _cron_due, _cron_field_matches
from coworker.channels.environment.state import SourceStateStore
from coworker.channels.environment.types import (
    EnvironmentSourceDef,
    SourceScheduleState,
)

# ---------------------------------------------------------------------------
# SourceScheduleState
# ---------------------------------------------------------------------------


class TestSourceScheduleState:
    def test_round_trip(self) -> None:
        state = SourceScheduleState(
            last_run_at=datetime(2026, 8, 11, 10, 0, 0),
            run_count=5,
            success_count=4,
            cursor="2026-08-11T09:00:00Z",
            known_fingerprints={"fp1", "fp2"},
        )
        data = state.to_dict()
        restored = SourceScheduleState.from_dict(data)
        assert restored.run_count == 5
        assert restored.success_count == 4
        assert restored.cursor == "2026-08-11T09:00:00Z"
        assert restored.known_fingerprints == {"fp1", "fp2"}

    def test_empty_from_dict(self) -> None:
        state = SourceScheduleState.from_dict(None)
        assert state.run_count == 0
        assert state.cursor is None

    def test_is_enabled_follows_definition_by_default(self) -> None:
        definition = EnvironmentSourceDef(name="test", enabled=True)
        state = SourceScheduleState()
        assert state.is_enabled(definition) is True

    def test_is_enabled_override_takes_precedence(self) -> None:
        definition = EnvironmentSourceDef(name="test", enabled=True)
        state = SourceScheduleState(enabled_override=False)
        assert state.is_enabled(definition) is False


# ---------------------------------------------------------------------------
# EnvironmentLoader
# ---------------------------------------------------------------------------


def _write_source(base: Path, name: str, frontmatter: str, script: str = "") -> None:
    """Write a SOURCE.md (and optional script) into base/name/."""
    source_dir = base / name
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "SOURCE.md").write_text(f"---\n{frontmatter}\n---\n", encoding="utf-8")
    if script:
        (source_dir / "source.py").write_text(script, encoding="utf-8")


class TestEnvironmentLoader:
    def test_parses_valid_source(self, tmp_path: Path) -> None:
        _write_source(
            tmp_path,
            "test-source",
            dedent("""\
                name: test-source
                description: A test source
                mode: inline
                every_seconds: 60
                params:
                  url: https://example.com
                protected: true
            """),
        )
        loader = EnvironmentLoader(str(tmp_path))
        loader.load_all()
        sources = loader.list_all()
        assert len(sources) == 1
        src = sources[0]
        assert src.name == "test-source"
        assert src.mode == "inline"
        assert src.every_seconds == 60
        assert src.params == {"url": "https://example.com"}
        assert src.protected is True

    def test_invalid_mode_falls_back(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        _write_source(
            tmp_path,
            "bad-mode",
            "name: bad-mode\nmode: invalid_mode\n",
        )
        loader = EnvironmentLoader(str(tmp_path))
        loader.load_all()
        src = loader.get("bad-mode")
        assert src is not None
        assert src.mode == "inline"

    def test_missing_name_is_skipped(self, tmp_path: Path) -> None:
        _write_source(tmp_path, "no-name", "description: missing name field\n")
        loader = EnvironmentLoader(str(tmp_path))
        loader.load_all()
        assert loader.list_all() == []

    def test_duplicate_name_warns(self, tmp_path: Path) -> None:
        _write_source(tmp_path, "dup1", "name: same-name\n")
        _write_source(tmp_path, "dup2", "name: same-name\n")
        loader = EnvironmentLoader(str(tmp_path))
        loader.load_all()
        warnings = loader.consume_load_warnings()
        assert any("same-name" in w for w in warnings)

    def test_nonexistent_directory_is_empty(self, tmp_path: Path) -> None:
        loader = EnvironmentLoader(str(tmp_path / "does-not-exist"))
        loader.load_all()
        assert loader.list_all() == []

    def test_reload_picks_up_new_sources(self, tmp_path: Path) -> None:
        loader = EnvironmentLoader(str(tmp_path))
        loader.load_all()
        assert loader.list_all() == []
        _write_source(tmp_path, "new", "name: new\nevery_seconds: 10\n")
        loader.load_all()
        assert loader.get("new") is not None


# ---------------------------------------------------------------------------
# SourceStateStore
# ---------------------------------------------------------------------------


class TestSourceStateStore:
    async def test_persist_and_restore(self, tmp_path: Path) -> None:
        store = SourceStateStore(tmp_path / "state.json")
        await store.load()
        state = store.get("my-source")
        state.cursor = "abc123"
        state.known_fingerprints.add("fp1")
        await store.save()

        # New store reads from the same file.
        store2 = SourceStateStore(tmp_path / "state.json")
        await store2.load()
        restored = store2.get("my-source")
        assert restored.cursor == "abc123"
        assert "fp1" in restored.known_fingerprints


# ---------------------------------------------------------------------------
# SourceExecutor (inline)
# ---------------------------------------------------------------------------


class TestSourceExecutor:
    async def test_inline_emits_signals(self, tmp_path: Path) -> None:
        script = dedent("""\
            async def poll(ctx):
                ctx.emit_signal(
                    title="Hello",
                    content="World",
                    fingerprint="fp-1",
                )
        """)
        _write_source(tmp_path, "hello", "name: hello\n", script)
        loader = EnvironmentLoader(str(tmp_path))
        loader.load_all()
        definition = loader.get("hello")
        assert definition is not None

        executor = SourceExecutor()
        state = SourceScheduleState()
        signals = await executor.run(definition, state)
        assert len(signals) == 1
        assert signals[0].title == "Hello"
        assert state.run_count == 1
        assert state.success_count == 1
        assert "fp-1" in state.known_fingerprints

    async def test_inline_dedup(self, tmp_path: Path) -> None:
        script = dedent("""\
            async def poll(ctx):
                ctx.emit_signal(title="A", content="x", fingerprint="dup")
        """)
        _write_source(tmp_path, "dedup", "name: dedup\n", script)
        loader = EnvironmentLoader(str(tmp_path))
        loader.load_all()
        definition = loader.get("dedup")
        assert definition is not None

        executor = SourceExecutor()
        state = SourceScheduleState()
        # First poll emits.
        signals1 = await executor.run(definition, state)
        assert len(signals1) == 1
        # Second poll: same fingerprint is deduplicated.
        signals2 = await executor.run(definition, state)
        assert len(signals2) == 0

    async def test_inline_error_is_isolated(self, tmp_path: Path) -> None:
        script = dedent("""\
            async def poll(ctx):
                raise RuntimeError("boom")
        """)
        _write_source(tmp_path, "broken", "name: broken\n", script)
        loader = EnvironmentLoader(str(tmp_path))
        loader.load_all()
        definition = loader.get("broken")
        assert definition is not None

        executor = SourceExecutor()
        state = SourceScheduleState()
        signals = await executor.run(definition, state)
        assert signals == []
        assert state.run_count == 1
        assert state.success_count == 0
        assert "boom" in state.last_error

    async def test_inline_missing_poll_function(self, tmp_path: Path) -> None:
        _write_source(tmp_path, "nopoll", "name: nopoll\n", "x = 1\n")
        loader = EnvironmentLoader(str(tmp_path))
        loader.load_all()
        definition = loader.get("nopoll")
        assert definition is not None

        executor = SourceExecutor()
        state = SourceScheduleState()
        signals = await executor.run(definition, state)
        assert signals == []
        assert "poll" in state.last_error.lower()

    async def test_sync_poll_function_works(self, tmp_path: Path) -> None:
        script = dedent("""\
            def poll(ctx):
                ctx.emit_signal(title="sync", content="ok", fingerprint="s1")
        """)
        _write_source(tmp_path, "sync", "name: sync\n", script)
        loader = EnvironmentLoader(str(tmp_path))
        loader.load_all()
        definition = loader.get("sync")
        assert definition is not None

        executor = SourceExecutor()
        state = SourceScheduleState()
        signals = await executor.run(definition, state)
        assert len(signals) == 1


# ---------------------------------------------------------------------------
# EnvironmentRuntime scheduling
# ---------------------------------------------------------------------------


class TestRuntimeScheduling:
    def _make_def(self, **kwargs: object) -> EnvironmentSourceDef:
        defaults: dict[str, object] = {"name": "test"}
        defaults.update(kwargs)
        return EnvironmentSourceDef(**defaults)  # type: ignore[arg-type]

    def test_manual_never_due(self) -> None:
        runtime = EnvironmentRuntime.__new__(EnvironmentRuntime)
        runtime._started_at = 0.0
        definition = self._make_def(schedule_trigger="manual")
        state = SourceScheduleState()
        assert runtime._is_due(definition, state, 100.0, datetime.now()) is False

    def test_cold_floor_due_after_delay(self) -> None:
        runtime = EnvironmentRuntime.__new__(EnvironmentRuntime)
        runtime._started_at = 0.0
        runtime._cycle_count = 0
        runtime._tool_call_count = 0
        definition = self._make_def(schedule_trigger="cold_floor", cold_floor_seconds=10)
        state = SourceScheduleState()
        # Before the cold_floor window: not due.
        assert runtime._is_due(definition, state, 5.0, datetime.now()) is False
        # After: due.
        assert runtime._is_due(definition, state, 15.0, datetime.now()) is True
        # After already run once: not due again.
        state.last_run_at = datetime.now()
        assert runtime._is_due(definition, state, 20.0, datetime.now()) is False

    def test_every_seconds_due_after_elapsed(self) -> None:
        runtime = EnvironmentRuntime.__new__(EnvironmentRuntime)
        runtime._started_at = 0.0
        runtime._cycle_count = 0
        runtime._tool_call_count = 0
        definition = self._make_def(every_seconds=60)
        state = SourceScheduleState()
        # Never run → due.
        assert runtime._is_due(definition, state, 0.0, datetime.now()) is True
        # Ran 10 seconds ago, interval 60 → not due.
        state.last_run_at = datetime.now() - timedelta(seconds=10)
        assert runtime._is_due(definition, state, 0.0, datetime.now()) is False
        # Ran 120 seconds ago → due.
        state.last_run_at = datetime.now() - timedelta(seconds=120)
        assert runtime._is_due(definition, state, 0.0, datetime.now()) is True

    def test_min_interval_protects(self) -> None:
        runtime = EnvironmentRuntime.__new__(EnvironmentRuntime)
        runtime._started_at = 0.0
        runtime._cycle_count = 0
        runtime._tool_call_count = 0
        definition = self._make_def(every_seconds=1, min_interval_seconds=300)
        state = SourceScheduleState(last_run_at=datetime.now() - timedelta(seconds=10))
        # every_seconds says due, but min_interval says no.
        assert runtime._is_due(definition, state, 0.0, datetime.now()) is False


# ---------------------------------------------------------------------------
# Cron helpers
# ---------------------------------------------------------------------------


class TestCronHelpers:
    def test_field_star_matches(self) -> None:
        assert _cron_field_matches("*", 5, 0, 59) is True

    def test_field_exact_value(self) -> None:
        assert _cron_field_matches("30", 30, 0, 59) is True
        assert _cron_field_matches("30", 15, 0, 59) is False

    def test_field_step(self) -> None:
        assert _cron_field_matches("*/15", 0, 0, 59) is True
        assert _cron_field_matches("*/15", 15, 0, 59) is True
        assert _cron_field_matches("*/15", 16, 0, 59) is False

    def test_field_range(self) -> None:
        assert _cron_field_matches("9-17", 12, 0, 23) is True
        assert _cron_field_matches("9-17", 18, 0, 23) is False

    def test_field_comma_list(self) -> None:
        assert _cron_field_matches("0,15,30,45", 15, 0, 59) is True
        assert _cron_field_matches("0,15,30,45", 7, 0, 59) is False

    def test_cron_due_invalid_expression(self) -> None:
        assert _cron_due("not a cron", datetime.now(), None) is False

    def test_cron_due_wrong_field_count(self) -> None:
        assert _cron_due("* * * *", datetime.now(), None) is False


# ---------------------------------------------------------------------------
# EnvironmentChannel
# ---------------------------------------------------------------------------


class TestEnvironmentChannel:
    async def test_send_returns_error(self, tmp_path: Path) -> None:
        loader = EnvironmentLoader(str(tmp_path / "nonexistent"))
        loader.load_all()
        channel = EnvironmentChannel(loader=loader)
        from coworker.core.types import CommunicateRequest

        result = await channel.send(CommunicateRequest(participant_id="env:test"))
        assert result.is_error is True

    def test_list_connections_empty(self, tmp_path: Path) -> None:
        loader = EnvironmentLoader(str(tmp_path / "nonexistent"))
        loader.load_all()
        channel = EnvironmentChannel(loader=loader)
        assert channel.list_connections() == []

    def test_name_and_prefix(self, tmp_path: Path) -> None:
        loader = EnvironmentLoader(str(tmp_path))
        channel = EnvironmentChannel(loader=loader)
        assert channel.name == "environment"
        assert channel.participant_prefix == "env:"


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class TestProtocol:
    def test_encode_decode_request(self) -> None:
        line = encode_request(request_id=1, method="emit_signal", params={"title": "x"})
        msg = decode_line(line)
        assert msg["method"] == "emit_signal"
        assert msg["id"] == 1

    def test_encode_response_result(self) -> None:
        line = encode_response(request_id=2, result={"ok": True})
        msg = decode_line(line)
        assert msg["result"] == {"ok": True}

    def test_encode_response_error(self) -> None:
        line = encode_response(
            request_id=3, error={"code": -32601, "message": "not found"}
        )
        msg = decode_line(line)
        assert msg["error"]["code"] == -32601

    def test_decode_invalid_json(self) -> None:
        from coworker.channels.environment.protocol import ProtocolError

        with pytest.raises(ProtocolError):
            decode_line("not json at all")
