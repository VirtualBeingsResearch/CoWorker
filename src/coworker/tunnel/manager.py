"""Own the lifecycle of one Cloudflare Tunnel child process.

The manager keeps the cloudflared binary out of the request path: it validates
that the binary can run, spawns it with ``--no-autoupdate``, waits for a public
URL, probes that URL, and restarts the process with exponential backoff when it
dies or stops answering. ``mode=named`` uses a fixed public hostname; ``quick``
mints an ephemeral ``*.trycloudflare.com`` hostname on every start.

The token is passed through ``TUNNEL_TOKEN`` and never appears in argv, so a
local user cannot read it from the process list.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import httpx
import psutil
from loguru import logger

from coworker.core.config import Config
from coworker.i18n import tr

TunnelMode = Literal["quick", "named"]
TunnelPhase = Literal["stopped", "starting", "running", "failed"]

QUICK_URL_PATTERN = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")
CONNECTED_LOG_PATTERN = re.compile(
    r"Registered tunnel connection|registered with protocol", re.IGNORECASE
)
URL_TIMEOUT_SECONDS = 30.0
READY_TIMEOUT_SECONDS = 30.0
PROBE_TIMEOUT_SECONDS = 10.0
HEALTH_INTERVAL_SECONDS = 60.0
HEALTH_FAILURE_LIMIT = 2
RESTART_BASE_SECONDS = 5.0
RESTART_MAX_SECONDS = 60.0
BINARY_PROBE_TIMEOUT_SECONDS = 10.0
TERMINATE_TIMEOUT_SECONDS = 5.0
RECORD_FILENAME = "cloudflared-child.json"
LOG_TAIL_LINES = 40


class TunnelError(RuntimeError):
    """A tunnel operation failed with a user-facing explanation."""


@dataclass(frozen=True)
class TunnelTarget:
    mode: TunnelMode
    public_url: str = ""
    token: str = ""
    allowed_emails: tuple[str, ...] = ()


@dataclass
class _BinaryInfo:
    found: bool = False
    path: str = ""
    version: str = ""


class TunnelManager:
    """Start, stop, and supervise one cloudflared process."""

    def __init__(self, config: Config, *, port: int | None = None) -> None:
        self._config = config
        self._port = port or config.api.port
        self._phase: TunnelPhase = "stopped"
        self._url = ""
        self._error = ""
        self._started_at = ""
        self._binary = _BinaryInfo()
        self._binary_checked = False
        self._health: dict[str, Any] = {
            "last_probe_at": "",
            "failures": 0,
            "latency_ms": 0,
        }
        self._target: TunnelTarget | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._runner: asyncio.Task[None] | None = None
        self._reader: asyncio.Task[None] | None = None
        self._health_task: asyncio.Task[None] | None = None
        self._restart_task: asyncio.Task[None] | None = None
        self._generation = 0
        self._stopping = False
        self._attempts = 0
        self._log_tail: list[str] = []
        self._minted_url = ""
        self._url_event = asyncio.Event()
        self._connected_event = asyncio.Event()
        self._record_path = (
            Path(config.agent.logs_dir).parent / "run" / RECORD_FILENAME
        )

    # ------------------------------------------------------------------ status

    @property
    def phase(self) -> TunnelPhase:
        return self._phase

    def snapshot(self) -> dict[str, Any]:
        target = self._target
        settings = self._config.tunnel
        mode = target.mode if target is not None else settings.mode
        public_url = (
            target.public_url if target is not None else settings.public_url
        )
        token_configured = bool(
            (target.token if target is not None else settings.token).strip()
        )
        return {
            "mode": mode,
            "configured": mode != "off",
            "phase": self._phase,
            "url": self._url,
            "public_url": public_url,
            "token_configured": token_configured,
            "quick_allowed_emails": list(settings.quick_allowed_emails),
            "protocol": settings.protocol,
            "error": self._error,
            "started_at": self._started_at,
            "binary": {
                "found": self._binary.found,
                "checked": self._binary_checked,
                "path": self._binary.path,
                "version": self._binary.version,
            },
            "health": dict(self._health),
        }

    def configured_target(self) -> TunnelTarget | None:
        settings = self._config.tunnel
        if settings.mode == "named":
            if not settings.token.strip() or not settings.public_url:
                return None
            return TunnelTarget(
                mode="named",
                public_url=settings.public_url,
                token=settings.token.strip(),
            )
        if settings.mode == "quick":
            return TunnelTarget(
                mode="quick",
                allowed_emails=tuple(settings.quick_allowed_emails),
            )
        return None

    # ---------------------------------------------------------------- lifecycle

    async def start_if_configured(self) -> None:
        target = self.configured_target()
        if target is None:
            return
        try:
            await self.start(target)
        except TunnelError as error:
            logger.warning(f"Cloudflare Tunnel not started: {error}")

    async def start(self, target: TunnelTarget) -> dict[str, Any]:
        if self._target == target and self._phase in {"starting", "running"}:
            return self.snapshot()
        await self._stop()
        self._target = target
        self._stopping = False
        self._generation += 1
        self._attempts = 0
        self._error = ""
        self._minted_url = ""
        self._url_event.clear()
        self._connected_event.clear()
        generation = self._generation
        self._runner = asyncio.create_task(
            self._run(generation), name="cloudflare-tunnel"
        )
        return self.snapshot()

    async def connect(
        self,
        *,
        mode: TunnelMode,
        token: str = "",
        public_url: str = "",
        allowed_emails: list[str] | tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Validate, then start a target supplied by the admin API."""

        emails = tuple(str(item).strip() for item in allowed_emails if str(item).strip())
        settings = self._config.tunnel
        if mode == "named":
            resolved_token = token.strip() or settings.token.strip()
            resolved_public_url = (
                public_url.strip().rstrip("/") or settings.public_url.rstrip("/")
            )
            if not resolved_token:
                raise TunnelError(tr("tunnel.named_token_required"))
            if not resolved_public_url:
                raise TunnelError(tr("tunnel.named_public_url_required"))
            target = TunnelTarget(
                mode="named", public_url=resolved_public_url, token=resolved_token
            )
        elif mode == "quick":
            target = TunnelTarget(mode="quick", allowed_emails=emails)
        else:
            raise TunnelError(tr("tunnel.invalid_mode", mode=mode))
        await self._ensure_binary()
        return await self.start(target)

    async def reconnect(self) -> dict[str, Any]:
        target = self._target or self.configured_target()
        if target is None:
            raise TunnelError(tr("tunnel.not_configured"))
        await self._stop()
        return await self.start(target)

    async def stop(self) -> None:
        await self._stop()

    async def test(self) -> dict[str, Any]:
        url = (
            self._url
            or (self._target.public_url if self._target is not None else "")
            or self._config.tunnel.public_url
        )
        if not url:
            raise TunnelError(tr("tunnel.no_url"))
        reachable, status, latency = await self._probe(url)
        return {
            "url": url,
            "reachable": reachable,
            "status": status,
            "latency_ms": latency,
        }

    # ----------------------------------------------------------------- internals

    async def _run(self, generation: int) -> None:
        target = self._target
        if target is None:
            return
        self._set_phase("starting")
        try:
            await self._ensure_binary()
            await self._reap_orphan()
            if generation != self._generation or self._stopping:
                return
            await self._spawn(target)
            if target.mode == "quick":
                url = await self._await_minted_url()
            else:
                url = target.public_url
            self._url = url
            ready = await self._await_ready(url)
            if not ready:
                raise TunnelError(tr("tunnel.url_timeout", url=url))
            self._started_at = datetime.now().isoformat(timespec="seconds")
            self._error = ""
            self._attempts = 0
            self._health = {
                "last_probe_at": datetime.now().isoformat(timespec="seconds"),
                "failures": 0,
                "latency_ms": self._health.get("latency_ms", 0),
            }
            self._set_phase("running")
            self._health_task = asyncio.create_task(
                self._health_loop(url, generation), name="cloudflare-tunnel-health"
            )
            process = self._process
            if process is not None:
                await process.wait()
            if not self._stopping and generation == self._generation:
                raise TunnelError(self._failure_detail(tr("tunnel.process_exited")))
        except asyncio.CancelledError:
            raise
        except TunnelError as error:
            await self._fail(str(error), generation)
        except Exception as error:  # pragma: no cover - defensive
            await self._fail(self._failure_detail(str(error)), generation)

    async def _stop(self) -> None:
        self._stopping = True
        self._generation += 1
        self._stop_health()
        self._cancel(self._restart_task)
        self._restart_task = None
        runner = self._runner
        self._runner = None
        if runner is not None and runner is not asyncio.current_task():
            runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runner
        await self._terminate_process()
        self._clear_record()
        self._target = None
        self._url = ""
        self._error = ""
        self._started_at = ""
        self._attempts = 0
        self._health = {"last_probe_at": "", "failures": 0, "latency_ms": 0}
        self._set_phase("stopped")

    async def _fail(self, message: str, generation: int) -> None:
        if self._stopping or generation != self._generation or self._phase == "failed":
            return
        self._error = message
        self._url = ""
        self._health["failures"] = 0
        self._stop_health()
        await self._terminate_process()
        self._set_phase("failed")
        self._attempts += 1
        delay = min(
            RESTART_BASE_SECONDS * 2 ** max(0, self._attempts - 1), RESTART_MAX_SECONDS
        )
        logger.warning(
            f"Cloudflare Tunnel failed (attempt {self._attempts}); retrying in {delay:.0f}s: {message}"
        )
        self._restart_task = asyncio.create_task(
            self._restart_after(delay, generation), name="cloudflare-tunnel-restart"
        )

    async def _restart_after(self, delay: float, generation: int) -> None:
        await asyncio.sleep(delay)
        if self._stopping or generation != self._generation:
            return
        self._runner = asyncio.create_task(
            self._run(generation), name="cloudflare-tunnel"
        )

    # ------------------------------------------------------------------- binary

    async def refresh_binary(self, *, force: bool = False) -> _BinaryInfo:
        """Detect the configured or PATH-provided cloudflared binary.

        Cheap to call from status endpoints: a cached working path short-circuits
        before running ``--version`` again, while a missing binary is re-resolved
        on every call so installing it while the process runs is picked up.
        """

        configured = self._config.tunnel.binary.strip()
        if configured:
            candidate = Path(configured).expanduser()
            path = str(candidate) if candidate.is_file() else ""
            missing_hint = str(candidate)
        else:
            path = shutil.which("cloudflared") or ""
            missing_hint = "cloudflared"
        if not path:
            self._binary = _BinaryInfo(found=False, path=missing_hint)
            self._binary_checked = True
            return self._binary
        if (
            not force
            and self._binary_checked
            and self._binary.found
            and self._binary.path == path
        ):
            return self._binary
        version = await self._probe_binary_version(path)
        self._binary = _BinaryInfo(found=version is not None, path=path, version=version or "")
        self._binary_checked = True
        return self._binary

    async def _ensure_binary(self) -> None:
        info = await self.refresh_binary(force=True)
        if info.found:
            return
        if info.path and info.path != "cloudflared" and Path(info.path).exists():
            raise TunnelError(tr("tunnel.binary_not_executable", path=info.path))
        raise TunnelError(tr("tunnel.binary_missing", path=info.path or "cloudflared"))

    async def _probe_binary_version(self, path: str) -> str | None:
        try:
            process = await asyncio.create_subprocess_exec(
                path,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            return None
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=BINARY_PROBE_TIMEOUT_SECONDS
            )
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            return None
        if process.returncode != 0:
            return None
        text = (stdout or stderr).decode("utf-8", "replace").strip()
        return text.splitlines()[0] if text else "unknown"

    # ------------------------------------------------------------------- spawn

    async def _spawn(self, target: TunnelTarget) -> None:
        args = [
            self._binary.path,
            "tunnel",
            "--no-autoupdate",
            "--protocol",
            self._config.tunnel.protocol,
        ]
        env = os.environ.copy()
        if target.mode == "quick":
            args += ["--url", f"http://127.0.0.1:{self._port}"]
            for email in target.allowed_emails:
                args += ["--allowed-mail", email]
        else:
            args.append("run")
            env["TUNNEL_TOKEN"] = target.token
        options: dict[str, Any] = {}
        if os.name == "nt":
            options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._log_tail.clear()
        self._minted_url = ""
        self._url_event.clear()
        self._connected_event.clear()
        self._process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            **options,
        )
        self._record_child(self._process, args)
        self._reader = asyncio.create_task(
            self._read_logs(self._process, target), name="cloudflare-tunnel-log"
        )

    async def _read_logs(
        self, process: asyncio.subprocess.Process, target: TunnelTarget
    ) -> None:
        stream = process.stderr
        if stream is None:
            return
        try:
            while True:
                raw = await stream.readline()
                if not raw:
                    return
                text = self._redact(raw.decode("utf-8", "replace").strip(), target)
                if not text:
                    continue
                self._log_tail.append(text)
                del self._log_tail[:-LOG_TAIL_LINES]
                if target.mode == "quick" and not self._minted_url:
                    match = QUICK_URL_PATTERN.search(text)
                    if match:
                        self._minted_url = match.group(0)
                        self._url_event.set()
                if not self._connected_event.is_set() and CONNECTED_LOG_PATTERN.search(text):
                    self._connected_event.set()
                logger.debug(f"cloudflared: {text}")
        except (asyncio.CancelledError, OSError):
            return

    async def _await_minted_url(self) -> str:
        try:
            await asyncio.wait_for(self._url_event.wait(), timeout=URL_TIMEOUT_SECONDS)
        except TimeoutError:
            raise TunnelError(self._failure_detail(tr("tunnel.url_timeout_short"))) from None
        if not self._minted_url:
            raise TunnelError(self._failure_detail(tr("tunnel.url_timeout_short")))
        return self._minted_url

    async def _await_ready(self, url: str) -> bool:
        """Wait until cloudflared registers with the edge or the URL answers.

        The connector's own registration log is the fastest truthful signal: the
        public hostname may take longer to propagate than the connector takes to
        come up, and a local probe can be blocked by egress filtering even when
        the tunnel works for remote clients.
        """

        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._connected_event.is_set():
                return True
            reachable, _status, latency = await self._probe(url)
            self._health["latency_ms"] = latency
            if reachable:
                return True
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._connected_event.wait(), timeout=1.0)
            if self._connected_event.is_set():
                return True
        return self._connected_event.is_set()

    async def _probe(self, url: str) -> tuple[bool, int, int]:
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(
                timeout=PROBE_TIMEOUT_SECONDS,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.get(url)
            status = response.status_code
            return status < 500, status, int((time.monotonic() - started) * 1000)
        except Exception:
            return False, 0, int((time.monotonic() - started) * 1000)

    # ------------------------------------------------------------------- health

    async def _health_loop(self, url: str, generation: int) -> None:
        while not self._stopping and self._url == url:
            await asyncio.sleep(HEALTH_INTERVAL_SECONDS)
            if self._stopping or self._url != url or generation != self._generation:
                return
            reachable, _status, latency = await self._probe(url)
            if self._stopping or self._url != url or generation != self._generation:
                return
            self._health["last_probe_at"] = datetime.now().isoformat(timespec="seconds")
            self._health["latency_ms"] = latency
            if reachable:
                self._health["failures"] = 0
                continue
            self._health["failures"] = int(self._health.get("failures", 0)) + 1
            if int(self._health["failures"]) >= HEALTH_FAILURE_LIMIT:
                await self._fail(
                    self._failure_detail(tr("tunnel.unhealthy", url=url)), generation
                )
                return

    def _stop_health(self) -> None:
        task = self._health_task
        self._health_task = None
        if task is not None and task is not asyncio.current_task():
            self._cancel(task)

    # ------------------------------------------------------------------ process

    async def _terminate_process(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(
                    process.wait(), timeout=TERMINATE_TIMEOUT_SECONDS
                )
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(Exception):
                    await process.wait()
        reader = self._reader
        self._reader = None
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader

    # ------------------------------------------------------------------- orphan

    def _record_child(self, process: asyncio.subprocess.Process, args: list[str]) -> None:
        with contextlib.suppress(OSError, psutil.Error):
            create_time = psutil.Process(process.pid).create_time()
            self._record_path.parent.mkdir(parents=True, exist_ok=True)
            self._record_path.write_text(
                json.dumps(
                    {
                        "pid": process.pid,
                        "create_time": create_time,
                        "argv": args,
                    }
                ),
                encoding="utf-8",
            )
            if os.name != "nt":
                self._record_path.chmod(0o600)

    def _clear_record(self) -> None:
        with contextlib.suppress(OSError):
            self._record_path.unlink()

    async def _reap_orphan(self) -> None:
        try:
            record = json.loads(self._record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        pid = record.get("pid")
        create_time = record.get("create_time")
        argv = record.get("argv")
        if not isinstance(pid, int) or not isinstance(create_time, (int, float)):
            self._clear_record()
            return
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            self._clear_record()
            return
        try:
            process = psutil.Process(pid)
            if abs(process.create_time() - float(create_time)) > 2.0:
                return
            if list(process.cmdline()) != argv:
                return
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, TERMINATE_TIMEOUT_SECONDS)
            except psutil.TimeoutExpired:
                process.kill()
            logger.warning(f"Reaped leftover cloudflared process (pid={pid})")
        except psutil.Error:
            pass
        finally:
            self._clear_record()

    # -------------------------------------------------------------------- misc

    def _set_phase(self, phase: TunnelPhase) -> None:
        self._phase = phase

    def _failure_detail(self, base: str) -> str:
        tail = self._log_tail[-1] if self._log_tail else ""
        if "flag provided but not defined" in tail:
            return tr("tunnel.binary_outdated")
        if tail:
            return f"{base} · {tail}"
        return base

    def _redact(self, text: str, target: TunnelTarget) -> str:
        if target.token and target.token in text:
            return text.replace(target.token, "***")
        return text

    @staticmethod
    def _cancel(task: asyncio.Task[Any] | None) -> None:
        if task is not None and not task.done():
            task.cancel()
