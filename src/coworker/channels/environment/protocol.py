"""JSON-RPC 2.0 protocol for subprocess environment sources.

A subprocess source communicates with the host over stdin/stdout using a
minimal request/response protocol:

    child stdout → host : ``{"jsonrpc":"2.0","id":1,"method":"emit_signal","params":{...}}``
    host stdout  → child : ``{"jsonrpc":"2.0","id":1,"result":{...}}``

This is a strict subset of JSON-RPC 2.0 (only requests with ``id`` and
responses — no notifications, no batched calls) designed for line-by-line
pumping.  Any language that can read stdin and write stdout can implement a
source.

The host side (:class:`RpcHost`) reads child stdout line by line, dispatches
each request to a handler, and writes the response back to the child's stdin.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

# A handler receives the request ``params`` and returns a JSON-serializable
# result, or raises to produce an error response.
RpcHandler = Callable[[dict[str, Any]], Awaitable[Any]]


class ProtocolError(Exception):
    """Raised when the wire protocol is violated."""


def encode_request(
    *,
    request_id: int,
    method: str,
    params: dict[str, Any] | None = None,
) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
    )


def encode_response(
    *,
    request_id: int,
    result: Any = None,
    error: dict[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    return json.dumps(payload)


def decode_line(line: str) -> dict[str, Any]:
    line = line.strip()
    if not line:
        raise ProtocolError("empty line")
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("message must be a JSON object")
    return obj


class RpcHost:
    """Host-side dispatcher for a single subprocess source session.

    Owns one child process's stdin/stdout pair and pumps JSON-RPC messages
    between the child and a set of registered handlers.
    """

    def __init__(self, handlers: dict[str, RpcHandler]) -> None:
        self._handlers = handlers

    async def serve(
        self,
        stdin: asyncio.StreamWriter,
        stdout: asyncio.StreamReader,
    ) -> None:
        """Read requests from ``stdout`` and write responses to ``stdin``.

        Runs until the child closes its stdout (EOF) or a protocol error
        occurs.
        """
        while True:
            try:
                raw = await stdout.readline()
            except Exception as exc:
                logger.debug(f"Environment source RPC: stdout read ended: {exc}")
                return
            if not raw:
                return
            try:
                line = raw.decode(errors="replace")
                msg = decode_line(line)
            except ProtocolError as exc:
                logger.warning(f"Environment source RPC: {exc}; line={raw!r}")
                continue
            await self._dispatch(msg, stdin)

    async def _dispatch(
        self,
        msg: dict[str, Any],
        stdin: asyncio.StreamWriter,
    ) -> None:
        request_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        if not isinstance(request_id, int):
            # Without an id we cannot route a response; log and skip.
            logger.warning(f"Environment source RPC: missing id in {msg}")
            return
        if not isinstance(method, str):
            await self._write(
                stdin,
                encode_response(
                    request_id=request_id,
                    error={"code": -32600, "message": "missing method"},
                ),
            )
            return
        handler = self._handlers.get(method)
        if handler is None:
            await self._write(
                stdin,
                encode_response(
                    request_id=request_id,
                    error={"code": -32601, "message": f"unknown method: {method}"},
                ),
            )
            return
        try:
            result = await handler(params)
        except Exception as exc:
            logger.exception(f"Environment source RPC handler {method} failed: {exc}")
            await self._write(
                stdin,
                encode_response(
                    request_id=request_id,
                    error={
                        "code": -32000,
                        "message": str(exc) or type(exc).__name__,
                    },
                ),
            )
            return
        await self._write(stdin, encode_response(request_id=request_id, result=result))

    async def _write(self, stdin: asyncio.StreamWriter, line: str) -> None:
        data = (line + "\n").encode("utf-8")
        stdin.write(data)
        try:
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
