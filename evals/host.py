"""Child-process entry: optional virtual clock, timeline driver, then Coworker."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from coworker.application import run_sync as coworker_run_sync


def _write_audit(clock: object) -> None:
    audit = os.environ.get("EVALS__CLOCK_AUDIT", "")
    if not audit:
        return
    jumps = getattr(clock, "jumps", [])
    Path(audit).write_text(
        json.dumps([jump.__dict__ for jump in jumps], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _run_timeline(start: str) -> None:
    from evals.clock import install_clock
    from evals.timeline import hosted_main

    origin = datetime.fromisoformat(start)
    with install_clock(origin) as clock:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            clock.install(loop)
            loop.run_until_complete(hosted_main(clock))
        finally:
            clock.restore()
            with suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            asyncio.set_event_loop(None)
            _write_audit(clock)


def _run_clocked_coworker(start: str) -> None:
    from evals.clock import install_clock

    origin = datetime.fromisoformat(start)
    with install_clock(origin) as clock:
        original = asyncio.new_event_loop

        def factory() -> asyncio.AbstractEventLoop:
            loop = original()
            clock.install(loop)
            return loop

        setattr(asyncio, "new_event_loop", factory)
        try:
            coworker_run_sync()
        finally:
            setattr(asyncio, "new_event_loop", original)
            clock.restore()
            _write_audit(clock)


def run_sync() -> None:
    start = os.environ.get("EVALS__CLOCK_START", "").strip()
    if os.environ.get("EVALS__TIMELINE", "").strip():
        if not start:
            raise RuntimeError("EVALS__TIMELINE requires EVALS__CLOCK_START")
        _run_timeline(start)
        return
    if not start:
        coworker_run_sync()
        return
    _run_clocked_coworker(start)
