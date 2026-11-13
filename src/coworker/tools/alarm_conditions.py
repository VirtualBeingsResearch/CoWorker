from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
import socket
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import SplitResult, urljoin, urlsplit

import httpx

ConditionKind = Literal["file_changed", "http_changed", "task_state"]

_MAX_REDIRECTS = 3
_DNS_CACHE_SECONDS = 300.0
_USER_AGENT = "CoworkerConditionAlarm/1.0"
_MAX_URL_CHARS = 2048


class TaskLike(Protocol):
    id: str
    status: str
    updated_at: str


class TaskStoreLike(Protocol):
    def get(self, task_id: str) -> TaskLike | None: ...

    def list(self) -> Sequence[TaskLike]: ...


@dataclass(frozen=True)
class AlarmCondition:
    kind: ConditionKind
    target: str = ""
    pattern: str = ""
    status: str = ""
    stale_seconds: int = 0


@dataclass(frozen=True)
class CheckResult:
    state: str
    stat_key: str = ""
    degraded: bool = False


def _local_datetime(value: datetime) -> datetime:
    return value.astimezone()


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _parse_iso(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _local_datetime(parsed)


def _task_is_stale(task: TaskLike, seconds: int, now: datetime) -> bool:
    if task.status not in {"pending", "in_progress"}:
        return False
    updated = _parse_iso(task.updated_at)
    if updated is None:
        return False
    return (now - updated).total_seconds() >= seconds


def _resolve_public_host(hostname: str, port: int) -> None:
    infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    if not infos:
        raise ValueError("the host did not resolve")
    for info in infos:
        address = info[4][0]
        if not ipaddress.ip_address(address).is_global:
            raise ValueError("private or reserved addresses are not allowed")


def _validate_http_shape(url: str) -> SplitResult:
    parsed = urlsplit((url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("only http/https URLs with a host are allowed")
    if parsed.username or parsed.password:
        raise ValueError("credentials in the URL are not allowed")
    if len(url) > _MAX_URL_CHARS:
        raise ValueError("the URL is too long")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("private or reserved addresses are not allowed")
    return parsed


def validate_public_url(url: str) -> None:
    """Validate URL shape and require every resolved address to be public."""
    parsed = _validate_http_shape(url)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("the URL has no host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    _resolve_public_host(hostname, port)


class ConditionChecker:
    """Read-only checks for the three condition alarm kinds."""

    def __init__(
        self,
        *,
        task_store: TaskStoreLike | None = None,
        allowed_roots: list[Path] | None = None,
        http_max_bytes: int = 256 * 1024,
        check_timeout_seconds: float = 20.0,
        file_max_bytes: int = 8 * 1024 * 1024,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._task_store = task_store
        roots = allowed_roots or [Path.cwd()]
        self._allowed_roots = [root.expanduser().resolve() for root in roots]
        self._http_max_bytes = max(1024, http_max_bytes)
        self._check_timeout_seconds = max(1.0, check_timeout_seconds)
        self._file_max_bytes = max(1024, file_max_bytes)
        self._http_transport = http_transport
        self._http_client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._dns_cache: dict[str, float] = {}

    async def close(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    def validate_target(self, condition: AlarmCondition) -> None:
        if condition.kind == "file_changed":
            self._resolve_file_target(condition.target)
        elif condition.kind == "http_changed":
            _validate_http_shape(condition.target)
        elif condition.kind == "task_state":
            if self._task_store is None:
                raise ValueError("task state condition requires a task store")
        else:
            raise ValueError(f"unknown condition kind: {condition.kind}")

    async def check(
        self,
        condition: AlarmCondition,
        *,
        previous_state: str = "",
        previous_stat: str = "",
    ) -> CheckResult:
        if condition.kind == "file_changed":
            return await asyncio.to_thread(
                self._check_file, condition, previous_state, previous_stat
            )
        if condition.kind == "http_changed":
            return await self._check_http(condition)
        if condition.kind == "task_state":
            return await asyncio.to_thread(self._check_task, condition)
        raise ValueError(f"unknown condition kind: {condition.kind}")

    def _resolve_file_target(self, target: str) -> Path:
        text = (target or "").strip()
        if not text:
            raise ValueError("file condition requires a target path")
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        resolved = path.resolve()
        if not any(
            resolved == root or root in resolved.parents for root in self._allowed_roots
        ):
            raise ValueError("path is outside the allowed roots")
        return resolved

    def _check_file(
        self,
        condition: AlarmCondition,
        previous_state: str,
        previous_stat: str,
    ) -> CheckResult:
        path = self._resolve_file_target(condition.target)
        stat = path.stat()
        stat_key = f"{stat.st_mtime_ns}:{stat.st_size}"
        if stat.st_size > self._file_max_bytes:
            return CheckResult(f"stat:{stat_key}", stat_key, True)
        if previous_stat == stat_key and previous_state.startswith("sha256:"):
            return CheckResult(previous_state, stat_key, False)
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return CheckResult(f"sha256:{digest.hexdigest()}", stat_key, False)

    async def _client(self) -> httpx.AsyncClient:
        async with self._client_lock:
            if self._http_client is None:
                self._http_client = httpx.AsyncClient(
                    follow_redirects=False,
                    timeout=self._check_timeout_seconds,
                    transport=self._http_transport,
                )
            return self._http_client

    async def _check_http(self, condition: AlarmCondition) -> CheckResult:
        if not condition.target:
            raise ValueError("http condition requires a target URL")
        client = await self._client()
        url = condition.target
        for _ in range(_MAX_REDIRECTS + 1):
            await self._validate_public_url(url)
            async with client.stream(
                "GET", url, headers={"User-Agent": _USER_AGENT}
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("redirect without a location")
                    url = urljoin(url, location)
                    continue
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self._http_max_bytes:
                        raise ValueError("response exceeds the byte limit")
                text = bytes(body).decode("utf-8", errors="replace")
                if condition.pattern:
                    match = await asyncio.to_thread(
                        re.search, condition.pattern, text, re.S
                    )
                    if match is None:
                        raise ValueError("pattern was not found")
                    text = match.group(1) if match.groups() else match.group(0)
                normalized = " ".join(text.split())
                return CheckResult(
                    f"sha256:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"
                )
        raise ValueError("too many redirects")

    async def _validate_public_url(self, url: str) -> None:
        parsed = _validate_http_shape(url)
        hostname = parsed.hostname
        if not hostname:
            raise ValueError("the URL has no host")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        key = f"{hostname}:{port}"
        now = time.monotonic()
        if self._dns_cache.get(key, 0) > now:
            return
        await asyncio.to_thread(validate_public_url, url)
        self._dns_cache[key] = now + _DNS_CACHE_SECONDS

    def _check_task(self, condition: AlarmCondition) -> CheckResult:
        if self._task_store is None:
            raise ValueError("task state condition requires a task store")
        tasks = list(self._task_store.list())
        if condition.target:
            task = self._task_store.get(condition.target)
            if task is None:
                return CheckResult("missing")
            tasks = [task]

        parts: list[str] = []
        if condition.status:
            matching = sorted(task.id for task in tasks if task.status == condition.status)
            parts.append("status:" + ",".join(matching))
        if condition.stale_seconds > 0:
            now = _local_now()
            stale = sorted(
                task.id for task in tasks if _task_is_stale(task, condition.stale_seconds, now)
            )
            parts.append("stale:" + ",".join(stale))
        if not parts:
            raise ValueError("task condition requires status or stale_seconds")
        return CheckResult("|".join(parts))
