from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import SyncStatus
from .semver import SemVer, SemVerError

RELEASE_FILENAME = "release.json"
LATEST_FILENAME = "latest.json"
SYNC_STATE_FILENAME = "state.json"
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9_.+@()-]+$")
_SANITIZE_FILENAME_RE = re.compile(r"[^A-Za-z0-9_.+@()-]+")
_PLATFORM_RE = re.compile(r"^(windows|linux|darwin)-(x86_64|i686|aarch64|armv7)$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_HASH_CHUNK_SIZE = 1024 * 1024
_ARCH_FILENAME_ALIASES = {
    "x86_64": ("x86_64", "x64", "amd64", "intel"),
    "aarch64": ("aarch64", "arm64", "apple-silicon", "applesilicon"),
    "i686": ("i686", "x86"),
    "armv7": ("armv7", "armhf"),
}

_LOCKS: dict[str, asyncio.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class DesktopReleaseStoreError(RuntimeError):
    """Base error for desktop release storage operations."""


class ReleaseNotFoundError(DesktopReleaseStoreError):
    pass


class ReleaseExistsError(DesktopReleaseStoreError):
    pass


class InvalidReleaseDataError(DesktopReleaseStoreError):
    pass


class UnsafePathError(DesktopReleaseStoreError, ValueError):
    pass


def _shared_lock(root: Path) -> asyncio.Lock:
    key = os.path.normcase(str(root.resolve()))
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _LOCKS[key] = lock
        return lock


def normalize_version(value: str | SemVer) -> str:
    try:
        return str(value if isinstance(value, SemVer) else SemVer.parse(value))
    except SemVerError as error:
        raise UnsafePathError(str(error)) from error


def validate_filename(filename: str) -> str:
    if not isinstance(filename, str) or not filename:
        raise UnsafePathError("asset filename must not be empty")
    if filename != Path(filename).name or "/" in filename or "\\" in filename:
        raise UnsafePathError("asset filename must be a single path component")
    if filename in {".", ".."} or filename.strip(" .") != filename:
        raise UnsafePathError("asset filename has unsafe leading or trailing characters")
    if not _SAFE_FILENAME_RE.fullmatch(filename):
        raise UnsafePathError("asset filename contains unsupported characters")
    return filename


def sanitize_filename(filename: str) -> str:
    leaf = filename.replace("\\", "/").split("/")[-1].strip(" .")
    safe = _SANITIZE_FILENAME_RE.sub("-", leaf).strip(" .-")
    if not safe:
        raise UnsafePathError("asset filename must not be empty")
    return validate_filename(safe)


def _filename_mentions_arch(filename: str, platform: str) -> bool:
    _, arch = platform.split("-", 1)
    compact = re.sub(r"[^a-z0-9]+", "", filename.lower())
    return any(
        re.sub(r"[^a-z0-9]+", "", alias.lower()) in compact
        for alias in _ARCH_FILENAME_ALIASES.get(arch, (arch,))
    )


def stored_asset_filename(filename: str, platform: str, kind: str) -> str:
    if not _PLATFORM_RE.fullmatch(platform):
        raise InvalidReleaseDataError(f"invalid desktop platform: {platform}")
    if kind not in {"updater", "installer"}:
        raise InvalidReleaseDataError(f"invalid desktop asset kind: {kind}")
    safe = sanitize_filename(filename)
    if kind == "updater" and platform.startswith("darwin-") and not _filename_mentions_arch(
        safe, platform
    ):
        return sanitize_filename(f"{platform}-{safe}")
    return safe


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _json_bytes(data: Mapping[str, Any]) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ReleaseNotFoundError(f"file does not exist: {path}") from error
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidReleaseDataError(f"invalid JSON file {path}: {error}") from error
    if not isinstance(data, dict):
        raise InvalidReleaseDataError(f"JSON root must be an object: {path}")
    return data


def _now() -> str:
    return datetime.now(UTC).astimezone().isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DesktopReleaseStore:
    """Atomic filesystem boundary for releases, publishing, and sync state."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.releases_root = self.root / "releases"
        self.staging_root = self.root / ".staging"
        self.sync_root = self.root / "sync"
        self.lock = _shared_lock(self.root)

    @property
    def latest_path(self) -> Path:
        return self.root / LATEST_FILENAME

    @property
    def sync_state_path(self) -> Path:
        return self.sync_root / SYNC_STATE_FILENAME

    def release_dir(self, version: str | SemVer) -> Path:
        base = self.releases_root.resolve()
        destination = self.releases_root / normalize_version(version)
        if destination.resolve().parent != base:
            raise UnsafePathError("release path escapes the releases directory")
        return destination

    def release_path(self, version: str | SemVer) -> Path:
        return self.release_dir(version) / RELEASE_FILENAME

    def assets_dir(self, version: str | SemVer) -> Path:
        release = self.release_dir(version)
        destination = release / "assets"
        if destination.resolve().parent != release.resolve():
            raise UnsafePathError("assets path escapes the release directory")
        return destination

    def asset_path(self, version: str | SemVer, filename: str) -> Path:
        safe = validate_filename(filename)
        root = self.assets_dir(version).resolve()
        destination = (root / safe).resolve()
        if destination.parent != root:
            raise UnsafePathError("asset path escapes the release assets directory")
        return destination

    async def write_asset(
        self,
        version: str | SemVer,
        filename: str,
        content: bytes,
    ) -> Path:
        if not content:
            raise InvalidReleaseDataError("release asset must not be empty")
        destination = self.asset_path(version, filename)
        async with self.lock:
            _atomic_write(destination, content)
        return destination

    async def upload_asset(
        self,
        version: str | SemVer,
        *,
        platform: str,
        signature: str,
        kind: str,
        filename: str,
        content: bytes,
    ) -> dict[str, Any]:
        normalized = normalize_version(version)
        stored_filename = stored_asset_filename(filename, platform, kind)
        signature = signature.strip()
        if kind == "updater" and not signature:
            raise InvalidReleaseDataError("updater assets require a signature")
        if not content:
            raise InvalidReleaseDataError("release asset must not be empty")
        async with self.lock:
            release = _read_json(self.release_path(normalized))
            destination = self.asset_path(normalized, stored_filename)
            _atomic_write(destination, content)
            asset = {
                "file": stored_filename,
                "signature": signature,
                "kind": kind,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "uploaded_at": _now(),
            }
            collection_name = "platforms" if kind == "updater" else "installers"
            collection = release.setdefault(collection_name, {})
            if not isinstance(collection, dict):
                raise InvalidReleaseDataError(f"{collection_name} must be an object")
            collection[platform] = asset
            release["updated_at"] = _now()
            _atomic_write(self.release_path(normalized), _json_bytes(release))
            return release

    update_asset = upload_asset

    async def has_release(self, version: str | SemVer) -> bool:
        async with self.lock:
            return self.release_path(version).is_file()

    async def read_release(self, version: str | SemVer) -> dict[str, Any]:
        async with self.lock:
            return _read_json(self.release_path(version))

    async def read_release_or_none(self, version: str | SemVer) -> dict[str, Any] | None:
        async with self.lock:
            path = self.release_path(version)
            return _read_json(path) if path.is_file() else None

    async def read_release_and_latest(
        self,
        version: str | SemVer,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        async with self.lock:
            release_path = self.release_path(version)
            release = _read_json(release_path) if release_path.is_file() else None
            latest = _read_json(self.latest_path) if self.latest_path.is_file() else None
            return release, latest

    async def write_release(self, version: str | SemVer, data: Mapping[str, Any]) -> None:
        normalized = normalize_version(version)
        payload = dict(data)
        payload["version"] = normalized
        async with self.lock:
            _atomic_write(self.release_path(normalized), _json_bytes(payload))

    async def create_release(
        self,
        version: str | SemVer,
        *,
        notes: str = "",
        pub_date: str = "",
    ) -> dict[str, Any]:
        normalized = normalize_version(version)
        now = _now()
        release = {
            "version": normalized,
            "notes": notes,
            "pub_date": pub_date,
            "published": False,
            "created_at": now,
            "updated_at": now,
            "platforms": {},
            "installers": {},
        }
        async with self.lock:
            path = self.release_path(normalized)
            if path.exists():
                raise ReleaseExistsError(f"release already exists: {normalized}")
            _atomic_write(path, _json_bytes(release))
        return release

    async def list_releases(self) -> list[dict[str, Any]]:
        async with self.lock:
            if not self.releases_root.is_dir():
                return []
            releases: list[tuple[SemVer, dict[str, Any]]] = []
            for path in self.releases_root.glob(f"*/{RELEASE_FILENAME}"):
                try:
                    version = SemVer.parse(path.parent.name)
                    releases.append((version, _read_json(path)))
                except (SemVerError, DesktopReleaseStoreError):
                    continue
            releases.sort(key=lambda item: item[0], reverse=True)
            return [release for _, release in releases]

    async def read_latest(self) -> dict[str, Any] | None:
        async with self.lock:
            return _read_json(self.latest_path) if self.latest_path.is_file() else None

    async def write_latest(self, data: Mapping[str, Any]) -> None:
        async with self.lock:
            _atomic_write(self.latest_path, _json_bytes(data))

    async def asset_sha256(self, version: str | SemVer, filename: str) -> str:
        normalized = normalize_version(version)
        safe = validate_filename(filename)
        async with self.lock:
            release = _read_json(self.release_path(normalized))
            for collection_name in ("platforms", "installers"):
                collection = release.get(collection_name) or {}
                if not isinstance(collection, Mapping):
                    continue
                for asset in collection.values():
                    if isinstance(asset, Mapping) and asset.get("file") == safe:
                        existing = str(asset.get("sha256") or "").lower()
                        if _SHA256_RE.fullmatch(existing):
                            return existing
            path = self.asset_path(normalized, safe)
            before = path.stat()
        digest = await asyncio.to_thread(_sha256_file, path)
        async with self.lock:
            release = _read_json(self.release_path(normalized))
            path = self.asset_path(normalized, safe)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                return digest
            changed = False
            for collection_name in ("platforms", "installers"):
                collection = release.get(collection_name) or {}
                if not isinstance(collection, dict):
                    continue
                for asset in collection.values():
                    if isinstance(asset, dict) and asset.get("file") == safe:
                        asset["sha256"] = digest
                        changed = True
            if changed:
                release["updated_at"] = _now()
                _atomic_write(self.release_path(normalized), _json_bytes(release))
            return digest

    async def publish_release(
        self,
        version: str | SemVer,
        platforms: Sequence[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        normalized = normalize_version(version)
        async with self.lock:
            release = _read_json(self.release_path(normalized))
            platform_map = release.get("platforms") or {}
            if not isinstance(platform_map, dict):
                raise InvalidReleaseDataError("platforms must be an object")
            selected = list(platforms) if platforms is not None else sorted(platform_map)
            if not selected:
                raise InvalidReleaseDataError("release has no updater platforms")
            publish_platforms = list(selected)
            if platforms is not None and self.latest_path.is_file():
                current_latest = _read_json(self.latest_path)
                if str(current_latest.get("version") or "") == normalized:
                    current_platforms = current_latest.get("platforms") or {}
                    if isinstance(current_platforms, dict):
                        selected_set = set(selected)
                        publish_platforms = sorted(
                            platform
                            for platform in current_platforms
                            if platform not in selected_set and platform in platform_map
                        ) + publish_platforms
            latest_platforms: dict[str, dict[str, str]] = {}
            for platform in publish_platforms:
                if not _PLATFORM_RE.fullmatch(platform):
                    raise InvalidReleaseDataError(f"invalid desktop platform: {platform}")
                asset = platform_map.get(platform)
                if not isinstance(asset, dict):
                    raise InvalidReleaseDataError(f"updater asset is missing for {platform}")
                filename = str(asset.get("file") or "")
                signature = str(asset.get("signature") or "").strip()
                if not filename or not self.asset_path(normalized, filename).is_file():
                    raise InvalidReleaseDataError(f"updater file is missing for {platform}")
                if not signature:
                    raise InvalidReleaseDataError(f"updater signature is missing for {platform}")
                latest_platforms[platform] = {"file": filename, "signature": signature}
            now = _now()
            latest = {
                "version": normalized,
                "notes": release.get("notes", ""),
                "pub_date": release.get("pub_date") or now,
                "platforms": latest_platforms,
            }
            release["published"] = True
            release["updated_at"] = now
            _atomic_write(self.release_path(normalized), _json_bytes(release))
            _atomic_write(self.latest_path, _json_bytes(latest))
            return {"release": release, "latest": latest}

    rollback_release = publish_release

    async def read_sync_state(self) -> SyncStatus:
        async with self.lock:
            if not self.sync_state_path.exists():
                return SyncStatus()
            data = _read_json(self.sync_state_path)
        try:
            return SyncStatus.model_validate(data)
        except ValidationError as error:
            raise InvalidReleaseDataError(f"invalid sync state: {error}") from error

    read_sync_status = read_sync_state

    async def write_sync_state(self, status: SyncStatus | Mapping[str, Any]) -> SyncStatus:
        try:
            value = status if isinstance(status, SyncStatus) else SyncStatus.model_validate(status)
        except ValidationError as error:
            raise InvalidReleaseDataError(f"invalid sync state: {error}") from error
        async with self.lock:
            _atomic_write(self.sync_state_path, _json_bytes(value.model_dump(mode="json")))
        return value

    write_sync_status = write_sync_state

    async def cleanup_staging(self) -> int:
        removed = 0
        async with self.lock:
            if not self.staging_root.is_dir():
                return 0
            for path in self.staging_root.iterdir():
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
                removed += 1
        return removed

    def staging(self, version: str | SemVer) -> ReleaseStaging:
        return ReleaseStaging(self, normalize_version(version))

    stage_release = staging

    async def _commit_staging(
        self,
        staging: ReleaseStaging,
        release: Mapping[str, Any],
    ) -> dict[str, Any]:
        if staging.committed:
            raise DesktopReleaseStoreError("staging area was already committed")
        normalized = staging.version
        payload = dict(release)
        payload["version"] = normalized
        payload["published"] = False
        payload.setdefault("platforms", {})
        payload.setdefault("installers", {})
        now = _now()
        payload.setdefault("created_at", now)
        payload["updated_at"] = now
        self._validate_release_asset_references(staging.path, payload)
        _atomic_write(staging.path / RELEASE_FILENAME, _json_bytes(payload))
        async with self.lock:
            destination = self.release_dir(normalized)
            if destination.exists():
                raise ReleaseExistsError(f"release already exists: {normalized}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging.path, destination)
            staging.committed = True
        return payload

    @staticmethod
    def _validate_release_asset_references(
        staging_root: Path,
        release: Mapping[str, Any],
    ) -> None:
        assets_root = staging_root / "assets"
        for collection_name in ("platforms", "installers"):
            collection = release.get(collection_name, {})
            if not isinstance(collection, Mapping):
                raise InvalidReleaseDataError(f"{collection_name} must be an object")
            for platform, asset in collection.items():
                if not isinstance(platform, str) or not isinstance(asset, Mapping):
                    raise InvalidReleaseDataError(f"invalid {collection_name} entry")
                filename = asset.get("file")
                if not isinstance(filename, str):
                    raise InvalidReleaseDataError(f"asset file is missing for {platform}")
                safe = validate_filename(filename)
                if not (assets_root / safe).is_file():
                    raise InvalidReleaseDataError(f"staged asset is missing: {safe}")


class ReleaseStaging:
    """A same-filesystem staging directory committed by one atomic rename."""

    def __init__(self, store: DesktopReleaseStore, version: str) -> None:
        self.store = store
        self.version = version
        self.path = store.staging_root / f"{version}-{uuid.uuid4().hex}"
        self.assets_path = self.path / "assets"
        self.committed = False
        self._entered = False

    async def __aenter__(self) -> ReleaseStaging:
        if self._entered:
            raise DesktopReleaseStoreError("staging area cannot be entered twice")
        self.assets_path.mkdir(parents=True, exist_ok=False)
        self._entered = True
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if not self.committed:
            shutil.rmtree(self.path, ignore_errors=True)

    def asset_path(self, filename: str) -> Path:
        if not self._entered:
            raise DesktopReleaseStoreError("staging area has not been entered")
        safe = validate_filename(filename)
        destination = (self.assets_path / safe).resolve()
        if destination.parent != self.assets_path.resolve():
            raise UnsafePathError("staged asset path escapes the assets directory")
        return destination

    async def write_asset(self, filename: str, content: bytes) -> Path:
        destination = self.asset_path(filename)
        await asyncio.to_thread(_atomic_write, destination, content)
        return destination

    async def commit(self, release: Mapping[str, Any]) -> dict[str, Any]:
        if not self._entered:
            raise DesktopReleaseStoreError("staging area has not been entered")
        return await self.store._commit_staging(self, release)
