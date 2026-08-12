from __future__ import annotations

from typing import Any


def desktop_update_asset_base_url(
    request_base_url: str,
    request_state: dict[str, Any] | None,
) -> str:
    """Resolve the public base URL for a Desktop updater asset response."""

    relay = request_state.get("coworker_relay") if isinstance(request_state, dict) else None
    relay_base = (
        relay.get("public_base_url")
        if isinstance(relay, dict) and relay.get("authenticated_tunnel") is True
        else None
    )
    if isinstance(relay_base, str) and relay_base:
        return relay_base.rstrip("/")
    return request_base_url.rstrip("/")
