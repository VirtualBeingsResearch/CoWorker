"""Child-process entry: optional virtual clock, then the real Coworker startup."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from coworker.application import run_sync as coworker_run_sync


def run_sync() -> None:
    start = os.environ.get("EVALS__CLOCK_START", "").strip()
    if not start:
        coworker_run_sync()
        return
    from evals.clock import install_clock

    with install_clock(datetime.fromisoformat(start)) as clock:
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
            audit = os.environ.get("EVALS__CLOCK_AUDIT", "")
            if audit:
                Path(audit).write_text(
                    json.dumps(
                        [jump.__dict__ for jump in clock.jumps],
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
