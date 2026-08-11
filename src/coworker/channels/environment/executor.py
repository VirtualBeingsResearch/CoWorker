"""Execute environment source ``poll`` calls in one of two modes.

**inline** — the source is Python and runs in-process.  We ``exec`` the script
with a :class:`~coworker.channels.environment.api.SourceContext` injected into
its namespace; the script defines ``poll(ctx)`` (sync or async).  This is the
highest-fidelity mode: sources get real host objects with no serialization.

**subprocess** — the source runs in a child process and talks to the host over
stdin/stdout using the JSON-RPC protocol (see :mod:`.protocol`).  Any language
that can do line-oriented stdin/stdout works here.

Both modes share the same lifecycle: the executor invokes the source, collects
emitted signals via a callback, enforces a timeout, and isolates failures so
one broken source never crashes the scheduler.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil
from loguru import logger

from .api import SourceContext
from .protocol import RpcHost
from .types import EnvironmentSignal, EnvironmentSourceDef, SourceScheduleState

if TYPE_CHECKING:
    import httpx

_Collect = Any  # callable(EnvironmentSignal) -> None


class SourceError(Exception):
    """Raised when a source script fails to load or execute."""


class SourceExecutor:
    """Loads and runs individual environment sources."""

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        default_logger: Any = None,
    ) -> None:
        self._http = http_client
        self._logger = default_logger or logger

    async def run(
        self,
        definition: EnvironmentSourceDef,
        state: SourceScheduleState,
    ) -> list[EnvironmentSignal]:
        """Run one poll for ``definition``.

        Returns the list of signals produced (after dedup).  Updates ``state``
        in place (fingerprints, cursor, timestamps).  Never raises — exceptions
        are captured into ``state.last_error``.
        """
        started = datetime.now()
        state.last_run_at = started
        state.run_count += 1
        signals: list[EnvironmentSignal] = []

        def _collect(signal: EnvironmentSignal) -> None:
            signals.append(signal)

        try:
            if definition.mode == "subprocess":
                await self._run_subprocess(definition, state, _collect)
            else:
                await self._run_inline(definition, state, _collect)
            state.last_error = ""
            state.last_success_at = datetime.now()
            state.success_count += 1
            return signals
        except TimeoutError:
            state.last_error = f"timed out after {definition.timeout_seconds}s"
            self._logger.warning(
                f"Environment source {definition.name}: {state.last_error}"
            )
            return []
        except Exception as exc:
            state.last_error = str(exc) or type(exc).__name__
            self._logger.exception(
                f"Environment source {definition.name} poll failed: {exc}"
            )
            return []

    # ------------------------------------------------------------------ inline

    async def _run_inline(
        self,
        definition: EnvironmentSourceDef,
        state: SourceScheduleState,
        collect: _Collect,
    ) -> None:
        script_path = Path(definition.source_dir) / definition.script
        if not script_path.is_file():
            raise SourceError(
                f"script not found: {script_path} (source_dir={definition.source_dir})"
            )
        code = script_path.read_text(encoding="utf-8")
        ctx = SourceContext(
            source_id=definition.name,
            config=definition.params,
            state=state,
            http=self._http,
            emit=collect,
            logger=self._logger.bind(source=definition.name),
        )
        namespace: dict[str, Any] = {
            "__name__": f"env_source_{definition.name}",
            "__file__": str(script_path),
            "ctx": ctx,
        }
        compiled = compile(code, str(script_path), "exec")
        # Run module-level code so poll() gets defined.  exec in the main
        # process; the timeout is enforced around the poll call below.
        exec(compiled, namespace)  # noqa: S102 — intentional inline execution
        poll_fn = namespace.get("poll")
        if not callable(poll_fn):
            raise SourceError(
                f"{definition.name}: script must define a callable poll(ctx)"
            )
        result = poll_fn(ctx)
        if asyncio.iscoroutine(result):
            await asyncio.wait_for(result, timeout=definition.timeout_seconds)

    # ------------------------------------------------------------------ subprocess

    async def _run_subprocess(
        self,
        definition: EnvironmentSourceDef,
        state: SourceScheduleState,
        collect: _Collect,
    ) -> None:
        script_path = Path(definition.source_dir) / definition.script
        if not script_path.is_file():
            raise SourceError(f"script not found: {script_path}")

        handlers = self._build_handlers(definition, state, collect)
        host = RpcHost(handlers)

        env = {**os.environ, "COWORKER_SOURCE_ID": definition.name}
        # Pass params as a JSON env var so the child can pick them up without
        # an extra round-trip.
        import json

        env["COWORKER_SOURCE_CONFIG"] = json.dumps(definition.params)

        proc = await asyncio.create_subprocess_exec(
            sys.executable if definition.language == "python" else "/bin/sh",
            *self._child_argv(definition, script_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(script_path.parent),
            env=env,
        )
        try:
            assert proc.stdin is not None
            assert proc.stdout is not None
            assert proc.stderr is not None

            async def _read_stderr() -> None:
                async for line in proc.stderr:  # type: ignore[union-attr]
                    text = line.decode(errors="replace").rstrip()
                    if text:
                        self._logger.debug(
                            f"Environment source {definition.name} stderr: {text}"
                        )

            stderr_task = asyncio.create_task(_read_stderr())
            try:
                await asyncio.wait_for(
                    host.serve(proc.stdin, proc.stdout),
                    timeout=definition.timeout_seconds,
                )
            finally:
                stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stderr_task
        finally:
            if proc.returncode is None:
                _kill_tree(proc.pid)
                with contextlib.suppress(ProcessLookupError):
                    await proc.wait()

    def _child_argv(self, definition: EnvironmentSourceDef, script_path: Path) -> list[str]:
        if definition.language == "python":
            return [str(script_path)]
        # /bin/sh script.sh
        return [str(script_path)]

    def _build_handlers(self, definition, state, collect):
        """Build the RPC handler table for subprocess mode."""

        def emit_signal(params: dict) -> dict:
            fingerprint = str(params.get("fingerprint") or "")
            if not fingerprint:
                raise ValueError("emit_signal requires a non-empty fingerprint")
            if fingerprint in state.known_fingerprints:
                return {"deduplicated": True}
            signal = EnvironmentSignal(
                source_id=definition.name,
                title=str(params.get("title") or ""),
                content=str(params.get("content") or ""),
                fingerprint=fingerprint,
                url=params.get("url"),
                severity=str(params.get("severity") or "info"),  # type: ignore[arg-type]
            )
            state.known_fingerprints.add(fingerprint)
            collect(signal)
            return {"deduplicated": False}

        async def http_get(params: dict) -> dict:
            url = str(params.get("url") or "")
            if not url:
                raise ValueError("http_get requires a url")
            client = self._http
            if client is None:
                import httpx

                client = httpx.AsyncClient(timeout=30.0)
            headers = params.get("headers") or {}
            response = await client.get(url, headers=headers)
            return {
                "status": response.status_code,
                "body": response.text,
                "headers": dict(response.headers),
            }

        def get_cursor(params: dict) -> dict:
            return {"cursor": state.cursor}

        def set_cursor(params: dict) -> dict:
            state.cursor = str(params.get("cursor") or "")
            return {"ok": True}

        def is_known(params: dict) -> dict:
            return {"known": str(params.get("fingerprint") or "") in state.known_fingerprints}

        def get_config(params: dict) -> dict:
            return {"config": dict(definition.params)}

        return {
            "emit_signal": emit_signal,
            "http_get": http_get,
            "get_cursor": get_cursor,
            "set_cursor": set_cursor,
            "is_known": is_known,
            "get_config": get_config,
        }


def _kill_tree(pid: int) -> None:
    """Kill a process and all its descendants (mirrors code_tools)."""
    with contextlib.suppress(psutil.NoSuchProcess, ProcessLookupError):
        parent = psutil.Process(pid)
        for child in parent.children(recursive=True):
            with contextlib.suppress(psutil.NoSuchProcess):
                child.kill()
        parent.kill()
