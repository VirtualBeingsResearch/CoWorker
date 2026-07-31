"""The single Coworker-owned route policy exposed through E2EE Relay."""

from __future__ import annotations

import re

_REGISTRATION_PATH = re.compile(r"^/api/communicate/register/[^/%\\]+$")
_SSE_PATH = re.compile(r"^/sse/[^/%\\]+$")
_UPDATE_MANIFEST = re.compile(
    r"^/api/desktop-updates/(?:darwin|linux|windows)/[A-Za-z0-9._-]+/[A-Za-z0-9._+-]+$"
)
_UPDATE_ASSET = re.compile(
    r"^/api/desktop-updates/assets/[A-Za-z0-9._+-]+/[A-Za-z0-9._+-]+$"
)


def relay_route_allowed(method: str, path: str) -> bool:
    """Return whether an already-decrypted virtual request may enter ASGI."""

    method = method.upper()
    if method == "GET" and path == "/status":
        return True
    if path == "/messages" and method == "POST":
        return True
    if path == "/api/communicate/register" and method in {"GET", "POST"}:
        return True
    if _REGISTRATION_PATH.fullmatch(path) and method == "DELETE":
        return True
    if _SSE_PATH.fullmatch(path) and method == "GET":
        return True
    if method == "GET" and (
        _UPDATE_MANIFEST.fullmatch(path) or _UPDATE_ASSET.fullmatch(path)
    ):
        return all(segment not in {".", ".."} for segment in path.split("/"))
    return False
