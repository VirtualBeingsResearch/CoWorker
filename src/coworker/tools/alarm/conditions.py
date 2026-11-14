from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
import socket
import stat as stat_module
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import SplitResult, urljoin, urlsplit

import httpx

from coworker.i18n import tr

ConditionKind = Literal["file_changed", "http_changed", "task_state"]

_MAX_REDIRECTS = 3
_DNS_CACHE_SECONDS = 300.0
_USER_AGENT = "CoworkerConditionAlarm/1.0"
_MAX_URL_CHARS = 2048
MISSING_STATE = "missing"
MISSING_MATCH_STATE = "nomatch"
_ACTIVE_TASK_STATUSES = frozenset({"pending", "in_progress"})


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


def _error(key: str, **values: object) -> ValueError:
    return ValueError(tr(f"tool_result.alarm.{key}", **values))


def local_datetime(value: datetime) -> datetime:
    return value.astimezone()


def local_now() -> datetime:
    return datetime.now().astimezone()


def parse_optional_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return local_datetime(datetime.fromisoformat(value))
    except ValueError:
        return None


def _parse_iso(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return local_datetime(parsed)


def _task_is_stale(task: TaskLike, seconds: int, now: datetime) -> bool:
    if task.status not in _ACTIVE_TASK_STATUSES:
        return False
    updated = _parse_iso(task.updated_at)
    if updated is None:
        return False
    return (now - updated).total_seconds() >= seconds


def _resolve_public_host(hostname: str, port: int) -> None:
    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise _error("condition_error_host_unresolved") from error
    if not infos:
        raise _error("condition_error_host_unresolved")
    for info in infos:
        address = info[4][0]
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as error:
            raise _error("condition_error_private_address") from error
        if not parsed.is_global:
            raise _error("condition_error_private_address")


def _validate_http_shape(url: str) -> SplitResult:
    text = (url or "").strip()
    try:
        parsed = urlsplit(text)
    except ValueError as error:
        raise _error("condition_error_url_invalid") from error
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise _error("condition_error_url_scheme")
    if parsed.username or parsed.password:
        raise _error("condition_error_url_credentials")
    if len(text) > _MAX_URL_CHARS:
        raise _error("condition_error_url_too_long")
    try:
        parsed.port
    except ValueError as error:
        raise _error("condition_error_url_port") from error
    host = parsed.hostname
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise _error("condition_error_private_address")
    return parsed


def validate_public_url(url: str) -> None:
    """Validate URL shape and require every resolved address to be public."""
    parsed = _validate_http_shape(url)
    hostname = parsed.hostname
    if not hostname:
        raise _error("condition_error_url_scheme")
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
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
            if condition.pattern or condition.status or condition.stale_seconds:
                raise _error("condition_error_file_fields")
            resolved = self._resolve_file_target(condition.target)
            if resolved.is_dir():
                raise _error("condition_error_file_is_directory")
        elif condition.kind == "http_changed":
            if condition.status or condition.stale_seconds:
                raise _error("condition_error_http_fields")
            _validate_http_shape(condition.target)
            if condition.pattern:
                try:
                    re.compile(condition.pattern)
                except re.error as error:
                    raise _error("condition_error_pattern_invalid") from error
        elif condition.kind == "task_state":
            if condition.pattern:
                raise _error("condition_error_task_pattern")
            if self._task_store is None:
                raise _error("condition_error_task_store")
            if not condition.status and condition.stale_seconds <= 0:
                raise _error("condition_error_task_requires_status")
        else:
            raise _error("condition_error_unknown_kind", kind=condition.kind)

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
        raise _error("condition_error_unknown_kind", kind=condition.kind)

    def _resolve_file_target(self, target: str) -> Path:
        text = (target or "").strip()
        if not text:
            raise _error("condition_error_file_target_required")
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        resolved = path.resolve()
        if not any(
            resolved == root or root in resolved.parents for root in self._allowed_roots
        ):
            raise _error("condition_error_outside_roots")
        return resolved

    def _check_file(
        self,
        condition: AlarmCondition,
        previous_state: str,
        previous_stat: str,
    ) -> CheckResult:
        path = self._resolve_file_target(condition.target)
        try:
            stat_result = path.stat()
        except (FileNotFoundError, NotADirectoryError):
            # A watched file may not exist yet; its appearance is the change.
            return CheckResult(MISSING_STATE)
        if stat_module.S_ISDIR(stat_result.st_mode):
            raise _error("condition_error_file_is_directory")
        stat_key = f"{stat_result.st_mtime_ns}:{stat_result.st_size}"
        if stat_result.st_size > self._file_max_bytes:
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
            raise _error("condition_error_http_target_required")
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
                        raise _error("condition_error_redirect_location")
                    url = urljoin(url, location)
                    continue
                if not response.is_success:
                    raise _error(
                        "condition_error_http_status", status=response.status_code
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self._http_max_bytes:
                        raise _error("condition_error_response_too_large")
                text = bytes(body).decode("utf-8", errors="replace")
                if condition.pattern:
                    match = await asyncio.to_thread(
                        re.search, condition.pattern, text, re.S
                    )
                    if match is None:
                        # A disappearing match is a page change, not a check error.
                        return CheckResult(MISSING_MATCH_STATE)
                    text = match.group(1) if match.groups() else match.group(0)
                normalized = " ".join(text.split())
                return CheckResult(
                    f"sha256:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"
                )
        raise _error("condition_error_too_many_redirects")

    async def _validate_public_url(self, url: str) -> None:
        parsed = _validate_http_shape(url)
        hostname = parsed.hostname
        if not hostname:
            raise _error("condition_error_url_scheme")
        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme == "https" else 80
        key = f"{hostname}:{port}"
        now = time.monotonic()
        if self._dns_cache.get(key, 0) > now:
            return
        await asyncio.to_thread(validate_public_url, url)
        self._dns_cache[key] = now + _DNS_CACHE_SECONDS

    def _check_task(self, condition: AlarmCondition) -> CheckResult:
        if self._task_store is None:
            raise _error("condition_error_task_store")
        tasks = list(self._task_store.list())
        if condition.target:
            task = self._task_store.get(condition.target)
            if task is None:
                return CheckResult(MISSING_STATE)
            tasks = [task]

        parts: list[str] = []
        if condition.status:
            matching = sorted(task.id for task in tasks if task.status == condition.status)
            parts.append("status:" + ",".join(matching))
        if condition.stale_seconds > 0:
            now = local_now()
            stale = sorted(
                task.id for task in tasks if _task_is_stale(task, condition.stale_seconds, now)
            )
            parts.append("stale:" + ",".join(stale))
        if not parts:
            raise _error("condition_error_task_requires_status")
        return CheckResult("|".join(parts))
