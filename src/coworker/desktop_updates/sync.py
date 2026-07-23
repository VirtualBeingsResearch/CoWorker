from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from loguru import logger

from .errors import DownloadInterruptedError, RateLimitError
from .models import (
    DownloadProgress,
    RateLimitInfo,
    ReleasePage,
    SourceRelease,
    SyncOutcome,
    SyncResult,
    SyncSourceSummary,
    SyncStatus,
)
from .semver import SemVer, SemVerError
from .store import DesktopReleaseStore, ReleaseExistsError, ReleaseStaging

ProgressCallback = Callable[[DownloadProgress], Awaitable[None]]
Readiness = Literal["disabled", "unconfigured", "ready", "reconfiguring", "config_error"]


@dataclass(frozen=True)
class SyncRuntimeSpec:
    source: ReleaseSource | None
    source_summary: SyncSourceSummary | None
    runtime_key: tuple[object, ...] | None
    token: str = ""
    interval_seconds: float = 15 * 60
    enabled: bool = True
    ready: bool = True
    readiness: Readiness = "ready"


class ReleaseSource(Protocol):
    @property
    def source_summary(self) -> SyncSourceSummary: ...

    def fetch_releases(self, etag: str | None = None) -> Awaitable[ReleasePage]: ...

    def download_release(
        self,
        release: SourceRelease,
        staging: ReleaseStaging,
        *,
        run_id: str,
        stop_event: asyncio.Event | None = None,
        progress: ProgressCallback | None = None,
    ) -> Awaitable[dict[str, Any]]: ...


class SyncService:
    """Coalesced manual/periodic importer for one highest eligible draft."""

    def __init__(
        self,
        store: DesktopReleaseStore,
        source: ReleaseSource | None = None,
        *,
        interval_seconds: float = 15 * 60,
        enabled: bool = True,
        ready: bool | None = None,
        readiness: Readiness | None = None,
        source_summary: SyncSourceSummary | None = None,
        runtime_key: tuple[object, ...] | None = None,
        token: str = "",
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.store = store
        self.source = source
        self.source_summary = source_summary or _source_summary(source)
        self.interval_seconds = interval_seconds
        self.enabled = enabled and source is not None
        self.ready = (source is not None) if ready is None else ready
        self.readiness: Readiness = readiness or ("ready" if self.enabled and self.ready else "disabled")
        self._runtime_key = runtime_key
        self._token = token
        self._generation = 0
        self._state_lock = asyncio.Lock()
        self._progress_lock = asyncio.Lock()
        self._current_task: asyncio.Task[SyncResult] | None = None
        self._current_run_id: str | None = None
        self._scheduler_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._closed = False
        self._initialized = False
        self._active_status: SyncStatus | None = None

    @property
    def running(self) -> bool:
        return self._current_task is not None and not self._current_task.done()

    async def start(self, *, run_immediately: bool = False) -> None:
        await self._ensure_initialized()
        async with self._state_lock:
            if self._closed:
                raise RuntimeError("sync service has been stopped")
            if not self.enabled or not self.ready or self.source is None:
                return
            if self._scheduler_task is not None and not self._scheduler_task.done():
                return
            self._stop_event.clear()
            self._scheduler_task = asyncio.create_task(
                self._scheduler(run_immediately=run_immediately),
                name="desktop-update-sync-scheduler",
            )

    async def request_sync(self, trigger: str = "manual") -> dict[str, str | bool]:
        request, _ = await self._request_task(trigger)
        return request

    async def _request_task(
        self,
        trigger: str,
    ) -> tuple[dict[str, str | bool], asyncio.Task[SyncResult]]:
        if trigger not in {"manual", "scheduled"}:
            raise ValueError("trigger must be 'manual' or 'scheduled'")
        await self._ensure_initialized()
        async with self._state_lock:
            if self._closed:
                raise RuntimeError("sync service has been stopped")
            if not self.enabled:
                raise RuntimeError("sync service is disabled")
            if not self.ready or self.source is None:
                raise RuntimeError("sync source is not configured")
            if self._current_task is not None and not self._current_task.done():
                assert self._current_run_id is not None
                return (
                    {"run_id": self._current_run_id, "coalesced": True},
                    self._current_task,
                )
            run_id = uuid.uuid4().hex
            requested_at = _now()
            status = await self.store.read_sync_state()
            queued = status.model_copy(
                update={
                    "enabled": self.enabled,
                    "ready": self.ready,
                    "readiness": self.readiness,
                    "source": self.source_summary,
                    "outcome": "running",
                    "run_id": run_id,
                    "trigger": trigger,
                    "phase": "queued",
                    "version": None,
                    "asset": None,
                    "bytes_downloaded": 0,
                    "bytes_total": 0,
                    "requested_at": requested_at,
                    "started_at": None,
                    "finished_at": None,
                    "next_run_at": None,
                    "last_error": "",
                }
            )
            await self.store.write_sync_state(queued)
            task = asyncio.create_task(
                self._run_sync(trigger=trigger, run_id=run_id),
                name=f"desktop-update-sync-{run_id}",
            )
            self._current_task = task
            self._current_run_id = run_id
            return {"run_id": run_id, "coalesced": False}, task

    trigger = request_sync

    async def sync_now(self) -> SyncResult:
        _, task = await self._request_task("manual")
        return await asyncio.shield(task)

    async def reconfigure(self, spec: SyncRuntimeSpec) -> dict[str, bool | str | None]:
        if spec.interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        await self._ensure_initialized()
        old_source: ReleaseSource | None = None
        unused_source: ReleaseSource | None = None
        current: asyncio.Task[SyncResult] | None = None
        scheduler: asyncio.Task[None] | None = None
        async with self._state_lock:
            if self._closed:
                raise RuntimeError("sync service has been stopped")
            old_runtime_key = self._runtime_key
            old_token = self._token
            runtime_changed = old_runtime_key != spec.runtime_key or old_token != spec.token
            interval_changed = self.interval_seconds != spec.interval_seconds
            source_changed = self.source is not spec.source and runtime_changed
            interrupted = False
            self.interval_seconds = spec.interval_seconds
            self.enabled = spec.enabled and spec.source is not None
            self.ready = spec.ready
            self.readiness = spec.readiness
            self.source_summary = spec.source_summary
            if runtime_changed:
                self._generation += 1
                self._runtime_key = spec.runtime_key
                self._token = spec.token
                old_source = self.source
                self.source = spec.source
                self._stop_event.set()
                current = self._current_task
                self._current_task = None
                self._current_run_id = None
                self._active_status = None
                if current is not None and not current.done():
                    current.cancel()
                    interrupted = True
                scheduler = self._scheduler_task
                self._scheduler_task = None
            elif self.source_summary != spec.source_summary:
                # Display-only changes (for example source rename) keep the client and ETag.
                unused_source = spec.source
                self.source_summary = spec.source_summary
            else:
                unused_source = spec.source
            if interval_changed and not runtime_changed:
                self._stop_event.set()
                scheduler = self._scheduler_task
                self._scheduler_task = None

        if scheduler is not None:
            scheduler.cancel()
            await _await_cancelled(scheduler)
        if current is not None and not current.done():
            await _await_cancelled(current)
        if source_changed and old_source is not None and old_source is not spec.source:
            close = getattr(old_source, "aclose", None)
            if close is not None:
                await close()
        if unused_source is not None and unused_source is not self.source:
            close = getattr(unused_source, "aclose", None)
            if close is not None:
                await close()

        status = await self.store.read_sync_state()
        updates: dict[str, object] = {
            "enabled": self.enabled,
            "ready": self.ready,
            "readiness": self.readiness,
            "source": self.source_summary,
            "next_run_at": None,
        }
        if runtime_changed:
            updates["etag"] = None
            if status.outcome == "running":
                updates.update(
                    outcome="interrupted",
                    phase="interrupted",
                    finished_at=_now(),
                    last_error="synchronization reconfigured",
                )
        await self.store.write_sync_state(status.model_copy(update=updates))
        self._stop_event = asyncio.Event()
        if self.enabled and self.ready and self.source is not None:
            await self.start(run_immediately=False)
        return {
            "runtime_changed": runtime_changed,
            "interval_changed": interval_changed,
            "interrupted_run": interrupted,
            "readiness": self.readiness,
        }

    async def stop(self) -> None:
        await self._ensure_initialized()
        async with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._stop_event.set()
            scheduler = self._scheduler_task
            current = self._current_task
            self._scheduler_task = None
        if scheduler is not None:
            scheduler.cancel()
            await _await_cancelled(scheduler)
        if current is not None and not current.done():
            current.cancel()
            await _await_cancelled(current)
        status = await self.store.read_sync_state()
        updates: dict[str, object] = {"next_run_at": None}
        if status.outcome == "running":
            updates.update(
                outcome="interrupted",
                phase="interrupted",
                finished_at=_now(),
                last_error="synchronization stopped",
            )
        await self.store.write_sync_state(status.model_copy(update=updates))
        close = getattr(self.source, "aclose", None)
        if close is not None:
            await close()

    async def status(self) -> SyncStatus:
        await self._ensure_initialized()
        return await self.store.read_sync_state()

    async def _ensure_initialized(self) -> None:
        async with self._state_lock:
            if self._initialized:
                return
            await self.store.cleanup_staging()
            status = await self.store.read_sync_state()
            source_summary = self.source_summary
            updates: dict[str, object] = {
                "enabled": self.enabled,
                "ready": self.ready,
                "readiness": self.readiness,
                "source": source_summary,
            }
            if status.source != source_summary:
                updates["etag"] = None
            if status.outcome == "running":
                updates.update(
                    outcome="interrupted",
                    phase="interrupted",
                    finished_at=_now(),
                    last_error="previous synchronization was interrupted",
                    next_run_at=None,
                )
            await self.store.write_sync_state(status.model_copy(update=updates))
            self._initialized = True

    async def _scheduler(self, *, run_immediately: bool) -> None:
        immediate = run_immediately
        while not self._stop_event.is_set():
            try:
                if immediate:
                    immediate = False
                else:
                    status = await self.store.read_sync_state()
                    now = _now()
                    next_run = status.next_run_at
                    if next_run is None or next_run <= now:
                        next_run = now + timedelta(seconds=self.interval_seconds)
                        await self.store.write_sync_state(
                            status.model_copy(update={"next_run_at": next_run})
                        )
                    delay = max(0.0, (next_run - now).total_seconds())
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                    except TimeoutError:
                        pass
                if self._stop_event.is_set():
                    break
                _, task = await self._request_task("scheduled")
                await asyncio.shield(task)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Desktop release sync scheduler iteration failed")
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=min(5.0, self.interval_seconds),
                    )
                except TimeoutError:
                    pass

    async def _run_sync(self, *, trigger: str, run_id: str) -> SyncResult:
        previous = await self.store.read_sync_state()
        source = self.source
        if source is None:
            raise RuntimeError("sync source is not configured")
        source_summary = self.source_summary or _source_summary(source)
        etag = previous.etag if previous.source == source_summary else None
        started_at = _now()
        running = previous.model_copy(
            update={
                "enabled": self.enabled,
                "ready": self.ready,
                "readiness": self.readiness,
                "source": source_summary,
                "outcome": "running",
                "run_id": run_id,
                "trigger": trigger,
                "phase": "checking",
                "version": None,
                "asset": None,
                "bytes_downloaded": 0,
                "bytes_total": 0,
                "requested_at": started_at,
                "started_at": started_at,
                "finished_at": None,
                "next_run_at": None,
                "last_error": "",
                "checked_releases": 0,
                "imported_versions": [],
                "skipped_releases": [],
            }
        )
        self._active_status = running
        await self.store.write_sync_state(running)
        skipped: list[str] = []
        checked = 0
        try:
            page = await source.fetch_releases(etag)
            skipped.extend(page.skipped)
            if page.not_modified:
                return await self._finish(
                    running,
                    run_id=run_id,
                    outcome="not_modified",
                    etag=page.etag,
                    skipped=skipped,
                    rate_limit=page.rate_limit,
                )
            checked = len(page.releases)
            if not page.releases:
                return await self._finish(
                    running,
                    run_id=run_id,
                    outcome="no_updates",
                    etag=page.etag,
                    checked=checked,
                    skipped=skipped,
                    rate_limit=page.rate_limit,
                )

            candidate = max(page.releases, key=lambda item: item.version)
            version = str(candidate.version)
            skipped.extend(
                f"{release.version}: lower-priority eligible release"
                for release in page.releases
                if release is not candidate
            )
            running = running.model_copy(update={"phase": "selecting", "version": version})
            self._active_status = running
            await self.store.write_sync_state(running)
            existing, latest = await self.store.read_release_and_latest(version)

            if existing is None and latest is not None:
                latest_value = str(latest.get("version") or "")
                try:
                    latest_version = SemVer.parse(latest_value)
                except SemVerError as error:
                    raise RuntimeError(f"latest release has an invalid version: {latest_value!r}") from error
                if candidate.version <= latest_version:
                    skipped.append(
                        f"{version}: candidate is not newer than latest {latest_version}"
                    )
                    return await self._finish(
                        running,
                        run_id=run_id,
                        outcome="no_updates",
                        etag=page.etag,
                        checked=checked,
                        skipped=skipped,
                        rate_limit=page.rate_limit,
                    )

            async with self.store.staging(version) as staging:
                metadata = await source.download_release(
                    candidate,
                    staging,
                    run_id=run_id,
                    stop_event=self._stop_event,
                    progress=self._record_progress,
                )
                fingerprint = _metadata_fingerprint(metadata)
                if existing is not None:
                    return await self._finish_existing(
                        running,
                        run_id=run_id,
                        version=version,
                        existing=existing,
                        fingerprint=fingerprint,
                        etag=page.etag,
                        checked=checked,
                        skipped=skipped,
                        rate_limit=page.rate_limit,
                    )
                await self._set_phase("committing")
                try:
                    await staging.commit(metadata)
                except ReleaseExistsError:
                    raced = await self.store.read_release(version)
                    return await self._finish_existing(
                        running,
                        run_id=run_id,
                        version=version,
                        existing=raced,
                        fingerprint=fingerprint,
                        etag=page.etag,
                        checked=checked,
                        skipped=skipped,
                        rate_limit=page.rate_limit,
                    )

            return await self._finish(
                running,
                run_id=run_id,
                outcome="succeeded",
                etag=page.etag,
                checked=checked,
                imported=[version],
                skipped=skipped,
                rate_limit=page.rate_limit,
            )
        except (asyncio.CancelledError, DownloadInterruptedError):
            return await self._finish(
                running,
                run_id=run_id,
                outcome="interrupted",
                checked=checked,
                skipped=skipped,
                error="synchronization interrupted",
            )
        except RateLimitError as error:
            next_run = _rate_limit_next_run(error.rate_limit)
            return await self._finish(
                running,
                run_id=run_id,
                outcome="rate_limited",
                checked=checked,
                skipped=skipped,
                error=str(error),
                rate_limit=error.rate_limit,
                next_run_at=next_run,
            )
        except Exception as error:
            logger.exception(f"Desktop release synchronization failed: {error}")
            return await self._finish(
                running,
                run_id=run_id,
                outcome="failed",
                checked=checked,
                skipped=skipped,
                error=str(error) or error.__class__.__name__,
            )
        finally:
            self._active_status = None

    async def _finish_existing(
        self,
        running: SyncStatus,
        *,
        run_id: str,
        version: str,
        existing: Mapping[str, Any],
        fingerprint: str,
        etag: str | None,
        checked: int,
        skipped: list[str],
        rate_limit: RateLimitInfo,
    ) -> SyncResult:
        existing_fingerprint = _metadata_fingerprint(existing)
        outcome: SyncOutcome
        if existing_fingerprint and existing_fingerprint == fingerprint:
            skipped.append(f"{version}: identical source release already exists")
            outcome = "no_updates"
            error = ""
        else:
            skipped.append(f"{version}: local release conflicts with source fingerprint")
            outcome = "conflict"
            error = "local release conflicts with source fingerprint"
        return await self._finish(
            running,
            run_id=run_id,
            outcome=outcome,
            etag=etag,
            checked=checked,
            skipped=skipped,
            error=error,
            rate_limit=rate_limit,
        )

    async def _record_progress(self, progress: DownloadProgress) -> None:
        async with self._progress_lock:
            status = self._active_status
            if status is None:
                return
            status = status.model_copy(
                update={
                    "phase": progress.phase,
                    "version": progress.version,
                    "asset": progress.asset,
                    "bytes_downloaded": progress.bytes_downloaded,
                    "bytes_total": progress.bytes_total,
                }
            )
            self._active_status = status
            await self.store.write_sync_state(status)

    async def _set_phase(self, phase: str) -> None:
        status = self._active_status
        if status is None:
            return
        status = status.model_copy(update={"phase": phase, "asset": None})
        self._active_status = status
        await self.store.write_sync_state(status)

    async def _finish(
        self,
        running: SyncStatus,
        *,
        run_id: str,
        outcome: SyncOutcome,
        etag: str | None = None,
        checked: int = 0,
        imported: list[str] | None = None,
        skipped: list[str] | None = None,
        error: str = "",
        rate_limit: RateLimitInfo | None = None,
        next_run_at: datetime | None = None,
    ) -> SyncResult:
        finished_at = _now()
        if self._current_run_id != run_id:
            return SyncResult(
                run_id=run_id,
                outcome=outcome,
                checked_releases=checked,
                imported_versions=tuple(imported or ()),
                skipped_releases=tuple(skipped or ()),
                error=error,
            )
        base = self._active_status or running
        updates: dict[str, object] = {
            "outcome": outcome,
            "run_id": run_id,
            "phase": outcome,
            "asset": None,
            "finished_at": finished_at,
            "next_run_at": next_run_at,
            "last_error": error,
            "checked_releases": checked,
            "imported_versions": imported or [],
            "skipped_releases": skipped or [],
        }
        if etag is not None:
            updates["etag"] = etag
        if rate_limit is not None:
            updates["rate_limit"] = rate_limit
        if outcome in {"succeeded", "not_modified", "no_updates"}:
            updates["last_success_at"] = finished_at
        status = base.model_copy(update=updates)
        await self.store.write_sync_state(status)
        return SyncResult(
            run_id=run_id,
            outcome=outcome,
            checked_releases=checked,
            imported_versions=tuple(imported or ()),
            skipped_releases=tuple(skipped or ()),
            error=error,
        )


def _metadata_fingerprint(metadata: Mapping[str, Any]) -> str:
    source = metadata.get("source")
    if not isinstance(source, Mapping):
        return ""
    fingerprint = source.get("fingerprint")
    return fingerprint if isinstance(fingerprint, str) else ""


def _source_summary(source: object) -> SyncSourceSummary | None:
    summary = getattr(source, "source_summary", None)
    if isinstance(summary, SyncSourceSummary):
        return summary
    if isinstance(summary, Mapping):
        return SyncSourceSummary.model_validate(summary)
    return None


def _rate_limit_next_run(rate_limit: RateLimitInfo) -> datetime:
    now = _now()
    candidates = [now + timedelta(seconds=rate_limit.retry_after_seconds or 0)]
    if rate_limit.reset_at is not None:
        candidates.append(rate_limit.reset_at.astimezone())
    return max(candidates)


async def _await_cancelled(task: asyncio.Task[Any]) -> None:
    try:
        await task
    except asyncio.CancelledError:
        pass


def _now() -> datetime:
    return datetime.now(UTC).astimezone()
