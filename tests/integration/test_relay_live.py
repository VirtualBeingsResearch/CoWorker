"""Opt-in live contract test for the Go Relay and Python built-in client.

Set COWORKER_RELAY_TEST_URL and COWORKER_RELAY_PAIRING_CODE to run it. Normal
unit/CI runs skip the test because it needs a separately running Relay.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from coworker.core.config import AdminConfig, APIConfig, Config
from coworker.relay import RelayClient
from coworker.relay.crypto import generate_communication_token


@pytest.mark.asyncio
async def test_live_go_relay_python_client_contract(tmp_path: Path):
    relay_url = os.environ.get("COWORKER_RELAY_TEST_URL", "")
    pairing_code = os.environ.get("COWORKER_RELAY_PAIRING_CODE", "")
    if not relay_url or not pairing_code:
        pytest.skip("live Relay variables are not configured")

    async def app(scope, receive, send):
        raise AssertionError("the plaintext public facade must never reach ASGI")

    config = Config(
        api=APIConfig(communication_token=generate_communication_token()),
        admin=AdminConfig(config_file=str(tmp_path / "admin.json")),
    )
    client = RelayClient(app, config)
    try:
        enrollment = await client.enroll(relay_url, pairing_code)
        for _ in range(100):
            snapshot = client.snapshot()
            if snapshot["status"] == "connected" and snapshot["auth_key_synced"]:
                break
            await __import__("asyncio").sleep(0.05)
        assert client.snapshot()["status"] == "connected"
        assert client.snapshot()["auth_key_synced"] is True
        async with httpx.AsyncClient(timeout=10) as http:
            response = await http.get(f"{enrollment['public_base_url']}/status")
        assert response.status_code == 404

        previous_token = config.api.communication_token
        result = await client.rotate_token()
        assert result["desktop_reconfiguration_required"] is True
        assert config.api.communication_token != previous_token
        for _ in range(100):
            if (
                client.snapshot()["status"] == "connected"
                and client.snapshot()["auth_key_synced"]
            ):
                break
            await __import__("asyncio").sleep(0.05)
        assert client.snapshot()["status"] == "connected"
        assert client.snapshot()["auth_key_synced"] is True
    finally:
        await client.stop()
