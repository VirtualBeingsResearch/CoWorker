"""Fallback and recovery behavior under real provider transport faults.

The primary provider is the production ``OpenAIProvider`` talking over a real
loopback TCP socket to :mod:`tests.support.faulty_http_server`, so every case
exercises the same client stack (httpx + openai SDK) production uses. SDK-level
retries are disabled so the Brain's own retry policy is what gets measured.

Faults are grouped by the layer that produces them:

* connect stage: closed port (refused or connect timeout), DNS failure, TLS
  handshake failure;
* transfer stage: RST mid-request, silent black hole (read timeout), non-HTTP
  bytes, body shorter than ``Content-Length``;
* HTTP stage: 429/5xx retryable statuses, 4xx client errors, and a 200 whose
  body is not the JSON the API contract promises.

For each fault the Brain must retry the candidate, hand over to the fallback,
keep serving from the fallback while the probe hits the same fault, and switch
back only once the transport actually recovers.

The client stack surfaces transfer faults as ``ProviderError``; a 200 body that
violates the API contract can surface as ``json.JSONDecodeError`` instead. The
Brain only special-cases its own configuration and interruption exceptions, so
both classes must count as a failed candidate.
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any
from unittest.mock import AsyncMock

import httpx
import openai
import pytest

from coworker.brain.base import BaseLLMProvider
from coworker.brain.brain import Brain
from coworker.brain.openai_provider import OpenAIProvider
from coworker.core.types import LLMResponse
from tests.support.faulty_http_server import FaultyHttpServer, closed_port

PRIMARY_MODEL = "gpt-4o"
BACKUP_MODEL = "backup-model"
API_KEY = "test-key"
BACKUP_TEXT = "backup response"


class _BackupProvider(BaseLLMProvider):
    provider_type = ""
    provider_name = "backup"
    default_model = BACKUP_MODEL

    def __init__(self) -> None:
        super().__init__("backup")
        self._current_model = BACKUP_MODEL
        self.default_model = BACKUP_MODEL

    async def complete(self, *args: Any, **kwargs: Any) -> LLMResponse:
        return LLMResponse(
            content=BACKUP_TEXT,
            tool_calls=[],
            stop_reason="end_turn",
            model=BACKUP_MODEL,
            usage={"input_tokens": 1, "output_tokens": 1},
        )

    def set_model(self, model_id: str) -> None:
        self._current_model = model_id

    def list_models(self) -> list[str]:
        return [BACKUP_MODEL]

    def supports_tool_use(self, model_id: str) -> bool:
        return True

    def supports_vision(self, model_id: str) -> bool:
        return False


@contextmanager
def _no_backoff() -> Iterator[None]:
    """Skip the 1s/2s retry sleeps; real connect/read timeouts still apply."""

    original = asyncio.sleep
    asyncio.sleep = AsyncMock()  # type: ignore[assignment]
    try:
        yield
    finally:
        asyncio.sleep = original  # type: ignore[assignment]


async def _make_primary(
    port: int,
    *,
    https: bool = False,
    timeout: float = 0.4,
    base_url: str | None = None,
) -> OpenAIProvider:
    url = base_url or f"{'https' if https else 'http'}://127.0.0.1:{port}/v1"
    provider = OpenAIProvider(API_KEY, base_url=url, name="primary")
    previous = provider._client  # type: ignore[attr-defined]
    provider._client = openai.AsyncOpenAI(  # type: ignore[attr-defined]
        api_key=API_KEY,
        base_url=url,
        max_retries=0,  # keep the SDK from masking the Brain's retry policy
        timeout=httpx.Timeout(timeout, connect=timeout),
    )
    await previous.close()
    return provider


@asynccontextmanager
async def _primary(
    port: int,
    *,
    https: bool = False,
    timeout: float = 0.4,
    base_url: str | None = None,
) -> AsyncIterator[OpenAIProvider]:
    provider = await _make_primary(
        port, https=https, timeout=timeout, base_url=base_url
    )
    try:
        yield provider
    finally:
        await provider._client.close()  # type: ignore[attr-defined]


def _brain(primary: OpenAIProvider) -> Brain:
    brain = Brain(
        "primary",
        PRIMARY_MODEL,
        fallbacks=["backup"],
        fallback_recovery_seconds=0,
        message_time_prefix=False,
    )
    brain.register_provider(primary)
    brain.register_provider(_BackupProvider())
    return brain


def _count_calls(provider: OpenAIProvider) -> list[int]:
    calls: list[int] = []
    original = provider.complete

    async def counted(*args: Any, **kwargs: Any) -> LLMResponse:
        calls.append(1)
        return await original(*args, **kwargs)

    provider.complete = counted  # type: ignore[method-assign]
    return calls


async def _think(brain: Brain) -> LLMResponse:
    return await asyncio.wait_for(brain.think([], "", []), timeout=10)


@pytest.fixture
async def server() -> AsyncIterator[FaultyHttpServer]:
    fault_server = FaultyHttpServer()
    await fault_server.start()
    try:
        yield fault_server
    finally:
        await fault_server.close()


@pytest.mark.asyncio
async def test_healthy_peer_serves_primary_without_degrading(
    server: FaultyHttpServer,
) -> None:
    async with _primary(server.port) as primary:
        calls = _count_calls(primary)
        brain = _brain(primary)

        response = await _think(brain)

    assert response.content == "primary response"
    assert len(calls) == 1
    assert server.requests == 1
    assert brain.current_provider_name == "primary"
    assert brain.degraded is False
    assert brain.consume_switch_cause() is None


@pytest.mark.asyncio
async def test_transport_fault_falls_back_after_three_attempts(
    server: FaultyHttpServer,
) -> None:
    server.mode = "reset"
    async with _primary(server.port) as primary:
        calls = _count_calls(primary)
        brain = _brain(primary)

        with _no_backoff():
            response = await _think(brain)

    assert response.content == BACKUP_TEXT
    assert len(calls) == 3
    assert server.requests == 3
    assert brain.current_provider_name == "backup"
    assert brain.degraded is True
    assert brain.recovery_target == ("primary", PRIMARY_MODEL)
    assert brain.consume_switch_cause() == "fallback"


@pytest.mark.asyncio
async def test_blackholed_request_times_out_and_falls_back(
    server: FaultyHttpServer,
) -> None:
    server.mode = "stall"
    async with _primary(server.port, timeout=0.25) as primary:
        calls = _count_calls(primary)
        brain = _brain(primary)

        started = time.monotonic()
        with _no_backoff():
            response = await _think(brain)
        elapsed = time.monotonic() - started

    assert response.content == BACKUP_TEXT
    assert len(calls) == 3
    assert server.requests == 3
    # 3 read timeouts, not a hung request: the Brain must not wait forever.
    assert elapsed < 3
    assert brain.degraded is True
    assert brain.consume_switch_cause() == "fallback"


@pytest.mark.asyncio
async def test_tls_handshake_failure_falls_back(server: FaultyHttpServer) -> None:
    async with _primary(server.port, https=True) as primary:
        calls = _count_calls(primary)
        brain = _brain(primary)

        with _no_backoff():
            response = await _think(brain)

    assert response.content == BACKUP_TEXT
    assert len(calls) == 3
    assert server.connections == 3
    assert server.requests == 0  # the TLS ClientHello never became an HTTP request
    assert brain.degraded is True


@pytest.mark.asyncio
async def test_dns_resolution_failure_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_lookup(*args: Any, **kwargs: Any) -> Any:
        raise socket.gaierror(11001, "getaddrinfo failed")

    monkeypatch.setattr(socket, "getaddrinfo", fail_lookup)
    async with _primary(
        0, base_url="http://provider.invalid/v1", timeout=0.3
    ) as primary:
        calls = _count_calls(primary)
        brain = _brain(primary)

        with _no_backoff():
            response = await _think(brain)

    assert response.content == BACKUP_TEXT
    assert len(calls) == 3
    assert brain.degraded is True
    assert brain.consume_switch_cause() == "fallback"


@pytest.mark.asyncio
async def test_closed_port_falls_back() -> None:
    # A loopback port with no listener: connection refused, or a connect timeout
    # when the local stack retries the SYN instead of resetting it.
    async with _primary(closed_port(), timeout=0.3) as primary:
        calls = _count_calls(primary)
        brain = _brain(primary)

        with _no_backoff():
            response = await _think(brain)

    assert response.content == BACKUP_TEXT
    assert len(calls) == 3
    assert brain.degraded is True
    assert brain.consume_switch_cause() == "fallback"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    ["garbage", "truncated", "chunked_corrupt", "bad_body", "error_payload"],
)
async def test_malformed_http_response_falls_back(
    server: FaultyHttpServer, mode: str
) -> None:
    server.mode = mode
    async with _primary(server.port) as primary:
        brain = _brain(primary)

        with _no_backoff():
            response = await _think(brain)

    assert response.content == BACKUP_TEXT
    assert server.requests == 3
    assert brain.degraded is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_retryable_http_status_falls_back(
    server: FaultyHttpServer, status: int
) -> None:
    server.mode = f"status:{status}"
    async with _primary(server.port) as primary:
        calls = _count_calls(primary)
        brain = _brain(primary)

        with _no_backoff():
            response = await _think(brain)

    assert response.content == BACKUP_TEXT
    assert len(calls) == 3
    assert brain.current_provider_name == "backup"
    assert brain.degraded is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_client_error_status_falls_back(
    server: FaultyHttpServer, status: int
) -> None:
    # The current policy retries any non-configuration failure; the assertion
    # deliberately leaves the exact attempt count free so a future fail-fast
    # policy for terminal 4xx responses does not have to rewrite this test.
    server.mode = f"status:{status}"
    async with _primary(server.port) as primary:
        calls = _count_calls(primary)
        brain = _brain(primary)

        with _no_backoff():
            response = await _think(brain)

    assert response.content == BACKUP_TEXT
    assert len(calls) >= 1
    assert brain.degraded is True
    assert brain.consume_switch_cause() == "fallback"


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["reset", "garbage", "truncated", "status:503"])
async def test_probe_failure_keeps_fallback_and_backs_off(
    server: FaultyHttpServer, fault: str
) -> None:
    server.mode = fault
    async with _primary(server.port) as primary:
        calls = _count_calls(primary)
        brain = _brain(primary)

        with _no_backoff():
            await _think(brain)
        assert brain.consume_switch_cause() == "fallback"

        served = server.requests
        started = time.monotonic()
        with _no_backoff():
            response = await _think(brain)
        elapsed = time.monotonic() - started

    assert response.content == BACKUP_TEXT
    # The probe is one attempt, not another three-try candidate.
    assert server.requests - served == 1
    assert len(calls) == 4
    assert brain.degraded is True
    assert brain.recovery_target == ("primary", PRIMARY_MODEL)
    assert brain._recovery_attempts == 1
    assert brain._recovery_not_before - started >= 59
    # A failed probe is not a new switch: no duplicate fallback notice.
    assert brain.consume_switch_cause() is None
    assert elapsed < 2


@pytest.mark.asyncio
async def test_recovery_probes_once_and_switches_back(
    server: FaultyHttpServer,
) -> None:
    server.mode = "status:503"
    async with _primary(server.port) as primary:
        calls = _count_calls(primary)
        brain = _brain(primary)
        listener = AsyncMock()
        brain.add_model_switch_listener(listener)

        with _no_backoff():
            await _think(brain)
        assert brain.consume_switch_cause() == "fallback"
        listener.reset_mock()

        server.mode = "ok"
        served = server.requests
        with _no_backoff():
            response = await _think(brain)

    assert response.content == "primary response"
    assert server.requests - served == 1
    assert len(calls) == 4
    assert brain.current_provider_name == "primary"
    assert brain.current_model == PRIMARY_MODEL
    assert brain.degraded is False
    assert brain.recovery_target is None
    assert brain.consume_switch_cause() == "recovery"
    listener.assert_awaited_once_with("primary", PRIMARY_MODEL)


@pytest.mark.asyncio
async def test_recovery_works_after_read_timeouts(server: FaultyHttpServer) -> None:
    server.mode = "stall"
    async with _primary(server.port, timeout=0.25) as primary:
        brain = _brain(primary)

        with _no_backoff():
            await _think(brain)
        assert brain.degraded is True
        assert brain.consume_switch_cause() == "fallback"

        server.mode = "ok"
        with _no_backoff():
            response = await _think(brain)

    assert response.content == "primary response"
    assert brain.current_provider_name == "primary"
    assert brain.degraded is False
    assert brain.consume_switch_cause() == "recovery"
