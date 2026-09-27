"""Child-side driver: deliver ``at:`` steps and restarts on the virtual clock."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from evals.clock import VirtualClock, real_monotonic
from evals.trace import activity

SCRIPT_PATH = Path(".evals/script.json")
PARENT_READY = Path(".evals/parent_ready")
INSTANCE_STATUS = Path("data/memory/instance_status.json")


def _running_agent() -> Any:
    from coworker.api import routes

    return getattr(routes, "_agent", None)


def _stop_agent() -> None:
    agent = _running_agent()
    if agent is not None:
        agent.stop()


async def sleep_until(origin: datetime, offset_seconds: float) -> None:
    """Sleep until virtual ``origin + offset``. Idle time is jumped by the clock."""
    remaining = offset_seconds - (datetime.now().astimezone() - origin).total_seconds()
    if remaining > 0:
        await asyncio.sleep(remaining)


async def _wait_held(
    clock: VirtualClock, predicate: Any, timeout: float, what: str
) -> None:
    with clock.hold():
        deadline = real_monotonic() + timeout
        while real_monotonic() < deadline:
            if await predicate():
                return
            await asyncio.sleep(0.2)
    raise TimeoutError(f"timed out waiting for {what}")


async def _wait_file(clock: VirtualClock, path: Path, timeout: float = 180.0) -> None:
    async def ready() -> bool:
        return path.is_file()

    await _wait_held(clock, ready, timeout, str(path))


async def _wait_instance(
    clock: VirtualClock, previous: str | None, timeout: float = 180.0
) -> str:
    async def changed() -> bool:
        if not INSTANCE_STATUS.is_file():
            return False
        text = INSTANCE_STATUS.read_text(encoding="utf-8")
        if previous is not None and text == previous:
            return False
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return False
        return not data.get("setup_mode")

    await _wait_held(clock, changed, timeout, "instance status")
    return INSTANCE_STATUS.read_text(encoding="utf-8")


async def _listen_sse(
    client: httpx.AsyncClient, url: str, connected: asyncio.Event
) -> None:
    try:
        async with client.stream("GET", url, timeout=None) as response:
            async for line in response.aiter_lines():
                if line.startswith(":"):
                    connected.set()
    except httpx.HTTPError:
        return


async def _open_streams(
    client: httpx.AsyncClient, base: str, participants: list[str]
) -> list[asyncio.Task[None]]:
    tasks: list[asyncio.Task[None]] = []
    for participant in participants:
        connected = asyncio.Event()
        task = asyncio.create_task(
            _listen_sse(client, f"{base}/sse/{participant}", connected),
            name=f"evals-sse-{participant}",
        )
        tasks.append(task)
        await asyncio.wait_for(connected.wait(), timeout=15)
    return tasks


async def _close_streams(tasks: list[asyncio.Task[None]]) -> None:
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def _wait_settled(clock: VirtualClock, delivered: int, timeout: float = 180.0) -> None:
    """Wait in real time (no jumps) until each delivered message has been handled."""

    async def quiet() -> bool:
        state = activity(Path("."))
        if state.messages_in < delivered:
            return False
        return not state.busy

    await _wait_held(clock, quiet, timeout, "settle after delivery")


async def _deliver(
    client: httpx.AsyncClient, base: str, participant: str, message: str
) -> None:
    response = await client.post(
        f"{base}/messages", json={"sender_id": participant, "content": message}
    )
    response.raise_for_status()


async def run_timeline(clock: VirtualClock) -> None:
    if not SCRIPT_PATH.is_file():
        raise FileNotFoundError(f"timeline script missing: {SCRIPT_PATH}")
    script = json.loads(SCRIPT_PATH.read_text(encoding="utf-8"))
    origin = datetime.fromisoformat(str(script["origin"]))
    if origin.tzinfo is None:
        origin = origin.astimezone()
    horizon = float(script["horizon_seconds"])
    participants = list(
        dict.fromkeys(
            str(step["participant"])
            for step in script["steps"]
            if step.get("participant")
        )
    )
    port = os.environ["API__PORT"]
    token = os.environ["API__COMMUNICATION_TOKEN"]
    base = f"http://127.0.0.1:{port}"
    headers = {"Authorization": f"Bearer {token}"}
    await _wait_file(clock, PARENT_READY)
    streams: list[asyncio.Task[None]] = []
    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        try:
            previous = await _wait_instance(clock, previous=None)
            streams = await _open_streams(client, base, participants)
            delivered = 0
            for step in script["steps"]:
                await sleep_until(origin, float(step["at_seconds"]))
                if step.get("action") == "restart":
                    agent = _running_agent()
                    if agent is None:
                        raise RuntimeError("restart requested before the agent was ready")
                    snapshot = previous
                    agent.request_restart(reason="evals-timeline")
                    previous = await _wait_instance(clock, previous=snapshot)
                    await _close_streams(streams)
                    streams = await _open_streams(client, base, participants)
                    continue
                await _deliver(client, base, str(step["participant"]), str(step["say"]))
                delivered += 1
                await _wait_settled(clock, delivered)
            await sleep_until(origin, horizon)
        finally:
            await _close_streams(streams)


async def hosted_main(clock: VirtualClock) -> None:
    """Run Coworker and the timeline on one loop; restart by calling ``_main`` again."""
    from coworker.api.app import clear_shutdown
    from coworker.application import _main

    timeline_task = asyncio.create_task(run_timeline(clock), name="evals-timeline")
    main_task: asyncio.Task[bool] | None = None
    try:
        while True:
            clear_shutdown()
            main_task = asyncio.create_task(_main(), name="coworker-main")
            done, _pending = await asyncio.wait(
                {main_task, timeline_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if timeline_task in done:
                _stop_agent()
                if main_task is not None and not main_task.done():
                    await main_task
                await timeline_task
                return
            restart = bool(main_task.result())
            if timeline_task.done():
                await timeline_task
                return
            if restart:
                continue
            raise RuntimeError("Coworker stopped before the timeline finished")
    except BaseException:
        _stop_agent()
        if main_task is not None and not main_task.done():
            try:
                await asyncio.wait_for(main_task, timeout=15)
            except (TimeoutError, asyncio.CancelledError, Exception):
                main_task.cancel()
        if not timeline_task.done():
            timeline_task.cancel()
            try:
                await timeline_task
            except (asyncio.CancelledError, Exception):
                pass
        raise
