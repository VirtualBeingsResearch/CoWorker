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
            await sleep_until(start, 2 * 3600, clock)
            assert real_monotonic() - real0 < 2
            delta = (datetime.now().astimezone() - start).total_seconds()
            assert abs(delta - 2 * 3600) < 5
        finally:
            clock.restore()


@pytest.mark.asyncio
async def test_timeline_jump_does_not_wake_every_short_sleep() -> None:
    """A day-long at: gap must not run a model cycle at every 60s sleep boundary."""
    start = datetime(2026, 1, 5, 9, 17).astimezone()
    with install_clock(start) as clock:
        clock.install(asyncio.get_running_loop())
        try:
            wakes = 0

            async def nap() -> None:
                nonlocal wakes
                while True:
                    await asyncio.sleep(60)
                    wakes += 1

            task = asyncio.create_task(nap())
            try:
                await sleep_until(start, 3600, clock)
                await asyncio.sleep(0)
            finally:
                task.cancel()
            assert wakes <= 2
            delta = (datetime.now().astimezone() - start).total_seconds()
            assert abs(delta - 3600) < 5
        finally:
            clock.restore()


async def _serve_http(delay: float) -> tuple[asyncio.AbstractServer, str]:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read(1024)
            await asyncio.sleep(delay)
            body = b"ok"
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n" + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    sockets = server.sockets
    assert sockets
    port = int(sockets[0].getsockname()[1])
    return server, f"http://127.0.0.1:{port}/"


@pytest.mark.asyncio
@pytest.mark.parametrize("modname", ["httpx", "httpx2"])
async def test_async_http_client_blocks_jumps(modname: str) -> None:
    """Model calls use httpx2; jumping while they wait expires the SDK timeout."""
    module = pytest.importorskip(modname)
    start = datetime(2026, 1, 5, 9, 17).astimezone()
    server, url = await _serve_http(0.3)
    try:
        with install_clock(start) as clock:
            clock.install(asyncio.get_running_loop())
            try:
                async with module.AsyncClient(timeout=10.0) as client:
                    mono0 = time.monotonic()
                    pending = asyncio.create_task(asyncio.sleep(3600))
                    try:
                        response = await client.get(url)
                    finally:
                        pending.cancel()
                    assert response.status_code == 200
                    assert response.text == "ok"
                    assert time.monotonic() - mono0 < 5
            finally:
                clock.restore()
    finally:
        server.close()
        await server.wait_closed()
