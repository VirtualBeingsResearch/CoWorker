"""Run one sample: start a real Coworker, deliver the script, wait, and stop it.

Participants talk to her the way the web chat does: each keeps an SSE stream open
(``GET /sse/{id}``) and sends through ``POST /messages``. Every sample gets its own
communication token, so nothing else on the machine can reach the instance.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from evals.scenario import SETTLE, Scenario
from evals.trace import activity
from evals.workspace import ModelTarget, child_env, free_port, prepare

STARTUP_TIMEOUT_SECONDS = 180.0
POLL_SECONDS = 0.5
# Each failed call already includes Coworker's own retries and fallbacks, so a few in
# a row mean the model is unreachable (exhausted quota, bad key), not a passing blip.
PROVIDER_FAILURE_LIMIT = 3
_PROVIDER_ERROR_MARKERS = ("exhausted", "Unexpected error in cycle", "LLM call")
HOST_COMMAND = ("-c", "from coworker.application import run_sync; run_sync()")


class SampleAborted(Exception):
    def __init__(self, status: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class SampleOutcome:
    status: str
    detail: str
    started_at: datetime
    real_seconds: float
    workspace: Path


class _Sample:
    def __init__(
        self, scenario: Scenario, locale: str, target: ModelTarget, sample_dir: Path
    ) -> None:
        self.scenario = scenario
        self.locale = locale
        self.target = target
        self.sample_dir = sample_dir
        self.workspace = sample_dir / "workspace"
        self.port = free_port()
        self.token = secrets.token_urlsafe(24)
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.proc: asyncio.subprocess.Process | None = None
        self.deadline = 0.0
        self.delivered = 0
        self.streams: list[asyncio.Task[None]] = []

    async def run(self) -> SampleOutcome:
        started_at = datetime.now().astimezone()
        started = time.monotonic()
        self.deadline = started + self.scenario.guard.max_seconds
        prepare(self.workspace, self.scenario, self.locale, self.target)
        env = child_env(self.scenario, self.locale, self.target, self.port, self.token)
        status, detail = "completed", ""
        with (self.sample_dir / "coworker.log").open("wb") as log:
            self.proc = await asyncio.create_subprocess_exec(
                sys.executable,
                *HOST_COMMAND,
                cwd=self.workspace,
                env=env,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                async with httpx.AsyncClient(timeout=10.0, headers=self.headers) as client:
                    await self._wait_ready(client)
                    await self._open_streams(client)
                    for step in self.scenario.script:
                        if step.after == SETTLE:
                            await self._wait_settled(client)
                        else:
                            await self._sleep(float(step.after))
                        await self._deliver(
                            client, step.participant, self.scenario.message_for(step, self.locale)
                        )
                    await self._wait_settled(client)
            except SampleAborted as abort:
                status, detail = abort.status, abort.detail
            finally:
                await self._close_streams()
                await self._stop()
        return SampleOutcome(
            status=status,
            detail=detail,
            started_at=started_at,
            real_seconds=round(time.monotonic() - started, 2),
            workspace=self.workspace,
        )

    def _check_guards(self) -> None:
        if self.proc is not None and self.proc.returncode is not None:
            raise SampleAborted("crashed", f"Coworker exited with code {self.proc.returncode}")
        if time.monotonic() > self.deadline:
            raise SampleAborted("timeout", f"exceeded {self.scenario.guard.max_seconds}s")
        state = activity(self.workspace)
        if state.llm_calls > self.scenario.guard.max_llm_calls:
            raise SampleAborted(
                "guard_exceeded",
                f"{state.llm_calls} LLM calls exceeded "
                f"max_llm_calls={self.scenario.guard.max_llm_calls}",
            )
        if state.failed_calls >= PROVIDER_FAILURE_LIMIT:
            raise SampleAborted(
                "provider_error",
                f"{state.failed_calls} model calls in a row failed; "
                f"last error: {self._last_provider_error()}",
            )

    def _last_provider_error(self) -> str:
        log = self.sample_dir / "coworker.log"
        try:
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return "unknown"
        for line in reversed(lines):
            if any(marker in line for marker in _PROVIDER_ERROR_MARKERS):
                return line.split(" | ", 2)[-1].strip()[:300]
        return "unknown"

    async def _status(self, client: httpx.AsyncClient) -> dict[str, Any] | None:
        try:
            response = await client.get(f"{self.base_url}/status")
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        data = response.json()
        return data if isinstance(data, dict) else None

    async def _wait_ready(self, client: httpx.AsyncClient) -> None:
        ready_by = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < ready_by:
            self._check_guards()
            status = await self._status(client)
            if status is not None:
                if status.get("setup_mode"):
                    raise SampleAborted(
                        "setup_mode",
                        "no usable LLM provider; check the API key and --provider/--model",
                    )
                return
            await asyncio.sleep(POLL_SECONDS)
        raise SampleAborted("startup_timeout", f"not ready after {STARTUP_TIMEOUT_SECONDS}s")

    async def _open_streams(self, client: httpx.AsyncClient) -> None:
        participants = dict.fromkeys(step.participant for step in self.scenario.script)
        record = self.sample_dir / "sse.jsonl"
        for participant in participants:
            connected = asyncio.Event()
            self.streams.append(
                asyncio.create_task(self._listen(client, participant, record, connected))
            )
            try:
                await asyncio.wait_for(connected.wait(), timeout=15)
            except TimeoutError as error:
                raise SampleAborted("stream_failed", f"SSE for {participant} did not open") from error

    async def _listen(
        self,
        client: httpx.AsyncClient,
        participant: str,
        record: Path,
        connected: asyncio.Event,
    ) -> None:
        url = f"{self.base_url}/sse/{participant}"
        data: list[str] = []
        with contextlib.suppress(httpx.HTTPError):
            async with client.stream("GET", url, timeout=None) as response:
                async for line in response.aiter_lines():
                    if line.startswith(":"):
                        connected.set()
                    elif line.startswith("data: "):
                        data.append(line[6:])
                    elif not line and data:
                        event = {
                            "ts": datetime.now().isoformat(),
                            "participant": participant,
                            "data": "\n".join(data),
                        }
                        with record.open("a", encoding="utf-8") as out:
                            out.write(json.dumps(event, ensure_ascii=False) + "\n")
                        data = []

    async def _close_streams(self) -> None:
        for task in self.streams:
            task.cancel()
        for task in self.streams:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.streams.clear()

    async def _wait_settled(self, client: httpx.AsyncClient) -> None:
        """Wait until every delivered message is seen and all lines of thought rest quietly."""
        settle = self.scenario.guard.settle_seconds
        quiet_since = time.monotonic()
        last: Any = None
        while True:
            self._check_guards()
            await asyncio.sleep(POLL_SECONDS)
            state = activity(self.workspace)
            snapshot = (state.llm_calls, state.messages_in, state.busy, state.main_resting)
            if snapshot != last or state.busy or state.messages_in < self.delivered:
                last, quiet_since = snapshot, time.monotonic()
                continue
            if not state.main_resting:
                status = await self._status(client)
                if status is None or not status.get("is_sleeping"):
                    quiet_since = time.monotonic()
                    continue
            if time.monotonic() - quiet_since >= settle:
                return

    async def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self._check_guards()
            await asyncio.sleep(min(POLL_SECONDS, max(0.0, end - time.monotonic())))

    async def _deliver(self, client: httpx.AsyncClient, participant: str, message: str) -> None:
        response = await client.post(
            f"{self.base_url}/messages", json={"sender_id": participant, "content": message}
        )
        if response.status_code != 200:
            raise SampleAborted(
                "delivery_failed", f"POST /messages → {response.status_code}: {response.text}"
            )
        self.delivered += 1

    async def _stop(self) -> None:
        if self.proc is None or self.proc.returncode is not None:
            return
        self.proc.terminate()
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=15)
        except TimeoutError:
            self.proc.kill()
            await self.proc.wait()


async def run_sample(
    scenario: Scenario, locale: str, target: ModelTarget, sample_dir: Path
) -> SampleOutcome:
    return await _Sample(scenario, locale, target, sample_dir).run()
