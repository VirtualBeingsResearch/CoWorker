"""Opt-in live contract test for the Go Relay and Python built-in client.

Set COWORKER_RELAY_TEST_URL and COWORKER_RELAY_PAIRING_CODE to run it. Normal
unit/CI runs skip the test because it needs a separately running TLS Relay.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from coworker.core.config import AdminConfig, APIConfig, Config
from coworker.relay import RelayClient


@pytest.mark.asyncio
async def test_live_go_relay_python_client_contract(tmp_path: Path):
    relay_url = os.environ.get("COWORKER_RELAY_TEST_URL", "")
    pairing_code = os.environ.get("COWORKER_RELAY_PAIRING_CODE", "")
    if not relay_url or not pairing_code:
        pytest.skip("live Relay variables are not configured")

    observed: dict[str, object] = {}

    async def app(scope, receive, send):
        observed["scope"] = scope
        await receive()
        body = json.dumps(
            {
                "ok": True,
                "via_relay": scope["state"]["coworker_relay"]["authenticated_tunnel"],
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})

    config = Config(
        api=APIConfig(communication_token="desktop-e2e-secret"),
        admin=AdminConfig(config_file=str(tmp_path / "admin.json")),
    )
    client = RelayClient(app, config)
    try:
        enrollment = await client.enroll(relay_url, pairing_code)
        for _ in range(100):
            snapshot = client.snapshot()
            if snapshot["status"] == "connected" and snapshot["verifier_synced"]:
                break
            await __import__("asyncio").sleep(0.05)
        assert client.snapshot()["status"] == "connected"
        assert client.snapshot()["verifier_synced"] is True
        async with httpx.AsyncClient(timeout=10) as http:
            response = await http.get(
                f"{enrollment['public_base_url']}/status",
                headers={
                    "Authorization": "Bearer desktop-e2e-secret",
                    "X-Coworker-Relay": "forged",
                },
            )
        assert response.status_code == 200
        assert response.json() == {"ok": True, "via_relay": True}
        headers = observed["scope"]["headers"]
        normalized = [(name.lower(), value) for name, value in headers]
        boundary = observed["scope"]["state"]["coworker_relay"]["relay_header_start"]
        assert (b"x-coworker-relay", b"forged") in normalized[:boundary]
        assert (b"x-coworker-relay", b"v1") in normalized[boundary:]
    finally:
        await client.stop()
