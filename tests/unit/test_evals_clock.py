from __future__ import annotations

import asyncio
import socket
import sys
import threading
import time
from datetime import datetime, timedelta

import pytest
from evals.clock import install_clock, real_monotonic
from evals.timeline import sleep_until

from coworker.agent.inbox_watcher import InboxWatcher
from coworker.tools.alarm_tools import AlarmManager
from coworker.tools.system_tools import SleepTool

_REAL = time.monotonic


def _slow_tcp(delay: float) -> int:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def serve() -> None:
        conn, _ = server.accept()
        time.sleep(delay)
        conn.sendall(b"ok")
        conn.close()
        server.close()

    threading.Thread(target=serve, daemon=True).start()
    return int(server.getsockname()[1])


@pytest.mark.asyncio
async def test_idle_wait_jumps_and_busy_work_does_not(tmp_path) -> None:
    start = datetime(2026, 1, 5, 9, 17).astimezone()
    with install_clock(start) as clock:
        clock.install(asyncio.get_running_loop())
        try:
            real0, mono0 = _REAL(), time.monotonic()
            try:
                await asyncio.wait_for(asyncio.Event().wait(), timeout=6 * 3600)
            except TimeoutError:
                pass
            assert _REAL() - real0 < 2
            assert time.monotonic() - mono0 >= 6 * 3600 - 1

            inbox = InboxWatcher(inbox_dir=str(tmp_path / "inbox"))
            alarms = AlarmManager(inbox)
            sleep_tool = SleepTool(inbox_watcher=inbox)
            wall0 = datetime.now().astimezone()
            await alarms.set("evals", wall0 + timedelta(hours=2), "water")
            await sleep_tool.execute(seconds=3 * 3600)
            events = await inbox.get_pending()
            assert events and events[0].source == "alarm"
            assert abs((datetime.now().astimezone() - wall0).total_seconds() - 2 * 3600) < 30

            mono0 = time.monotonic()
            await asyncio.to_thread(time.sleep, 0.2)
            assert time.monotonic() - mono0 < 5

            mono0 = time.monotonic()
            clock.http += 1
            pending = asyncio.create_task(asyncio.sleep(3600))
            await asyncio.sleep(0.2)
            assert time.monotonic() - mono0 < 5
            clock.http -= 1
            pending.cancel()
            port = _slow_tcp(0.2)
            mono0 = time.monotonic()
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            clock.http += 1
            try:
                await reader.read(2)
            finally:
                clock.http -= 1
                writer.close()
            assert time.monotonic() - mono0 < 5

            mono0 = time.monotonic()
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", "import time; time.sleep(0.2)"
            )
            await proc.wait()
            assert time.monotonic() - mono0 < 5

            with clock.hold():
                mono0 = time.monotonic()
                pending = asyncio.create_task(asyncio.sleep(3600))
                await asyncio.sleep(0.2)
                assert time.monotonic() - mono0 < 5
                pending.cancel()
        finally:
            clock.restore()


@pytest.mark.asyncio
async def test_sleep_until_jumps_idle_hours() -> None:
    start = datetime(2026, 1, 5, 9, 17).astimezone()
    with install_clock(start) as clock:
        clock.install(asyncio.get_running_loop())
        try:
            real0 = real_monotonic()
            await sleep_until(start, 2 * 3600)
            assert real_monotonic() - real0 < 2
            delta = (datetime.now().astimezone() - start).total_seconds()
            assert abs(delta - 2 * 3600) < 5
        finally:
            clock.restore()
