from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from .assets import AssetValidationError
from .errors import DownloadInterruptedError, SourceAPIError
from .github import (
    _buffered_response,
    _origin,
    _read_response_limited,
    _response_error_message,
    validate_source_url,
)
from .models import (
    CoworkerSourceConfig,
    DownloadProgress,
    ReleasePage,
    SourceRelease,
    SyncSourceSummary,
)
from .semver import SemVer, SemVerError
from .store import ReleaseStaging, validate_filename

ProgressCallback = Callable[[DownloadProgress], Awaitable[None]]
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_USER_AGENT = "coworker-desktop-update-sync"
_FEED_LIST_MAX_BYTES = 8 * 1024 * 1024
_MANIFEST_MAX_BYTES = 2 * 1024 * 1024
_ERROR_RESPONSE_MAX_BYTES = 64 * 1024


class CoworkerFeedAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: str
    kind: str
    file: str
    size: int = Field(gt=0)
    sha256: str
    signature: str = ""
    download_url: str

    @field_validator("file")
    @classmethod
    def _validate_file(cls, value: str) -> str:
        return validate_filename(value)

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, value: str) -> str:
        if value not in {"updater", "installer"}:
            raise ValueError("asset kind must be updater or installer")
        return value

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        value = value.lower()
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("sha256 must be a hex SHA-256 digest")
        return value


class CoworkerFeedRelease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version: str
    notes: str = ""
    pub_date: datetime | None = None
    prerelease: bool = False
    manifest_url: str


class CoworkerFeedList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    releases: list[CoworkerFeedRelease] = Field(default_factory=list)


class CoworkerFeedManifestRelease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version: str
    notes: str = ""
    pub_date: datetime | None = None
    prerelease: bool = False
    revision: str
    assets: list[CoworkerFeedAsset] = Field(default_factory=list)


class CoworkerFeedManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    release: CoworkerFeedManifestRelease


_LIST_ADAPTER = TypeAdapter(CoworkerFeedList)
_MANIFEST_ADAPTER = TypeAdapter(CoworkerFeedManifest)


class CoworkerReleaseSource:
    """Fetch published desktop releases from another Coworker instance's feed."""

    def __init__(
        self,
        config: CoworkerSourceConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("pass either client or transport, not both")
        self.config = config
        self._client = client or httpx.AsyncClient(
            transport=transport,
            timeout=config.request_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )
        self._owns_client = client is None
        self._base_origin = _origin(httpx.URL(config.base_url))

    @property
    def source_summary(self) -> SyncSourceSummary:
        return SyncSourceSummary(
            source_id=self.config.source_id,
            name=self.config.source_name,
            provider="coworker",
            endpoint=self.config.base_url,
            target=self.config.base_url,
            options={
                "include_prereleases": self.config.include_prereleases,
            },
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def releases_url(self) -> str:
        return urljoin(self.config.base_url + "/", "api/desktop-updates/feed/v1/releases?limit=20")

    async def fetch_releases(self, etag: str | None = None) -> ReleasePage:
        headers: dict[str, str] = {"Accept": "application/json"}
        if etag:
            headers["If-None-Match"] = etag
        response = await self._request("GET", self.releases_url, headers=headers, max_bytes=_FEED_LIST_MAX_BYTES)
        response_etag = response.headers.get("etag") or etag
        if response.status_code == 304:
            return ReleasePage(etag=response_etag, not_modified=True)
        self._raise_for_status(response)
        try:
            payload = _LIST_ADAPTER.validate_json(response.content)
        except ValidationError as error:
            raise SourceAPIError("Coworker release feed response is invalid") from error
        if payload.schema_version != 1:
            raise SourceAPIError("unsupported Coworker feed schema version")

        releases: list[SourceRelease] = []
        skipped: list[str] = []
        for item in payload.releases[:20]:
            candidate, reason = self._filter_release(item)
            if candidate is None:
                skipped.append(reason)
            else:
                releases.append(candidate)
        return ReleasePage(releases=tuple(releases), etag=response_etag, skipped=tuple(skipped))

    list_releases = fetch_releases

    def _filter_release(self, payload: CoworkerFeedRelease) -> tuple[SourceRelease | None, str]:
        label = payload.version or payload.id or "<missing version>"
        try:
            version = SemVer.parse(payload.version)
        except SemVerError as error:
            return None, f"{label}: {error}"
        if payload.prerelease and not self.config.include_prereleases:
            return None, f"{label}: prereleases are disabled"
        if payload.prerelease != version.is_prerelease:
            return None, f"{label}: prerelease flag does not match the semantic version"
        manifest_url = self._resolve_url(payload.manifest_url)
        return (
            SourceRelease(
                release_id=payload.id,
                version=version,
                tag_name=payload.version,
                name=payload.version,
                notes=payload.notes,
                prerelease=payload.prerelease,
                published_at=payload.pub_date,
                html_url=manifest_url,
                assets={},
            ),
            "",
        )

    async def download_release(
        self,
        release: SourceRelease,
        staging: ReleaseStaging,
        *,
        run_id: str,
        stop_event: asyncio.Event | None = None,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        manifest = await self._fetch_manifest(release.html_url)
        item = manifest.release
        if item.version != str(release.version):
            raise AssetValidationError("Coworker manifest version does not match feed list")
        total_declared = sum(asset.size for asset in item.assets)
        if total_declared > self.config.max_run_bytes:
            raise AssetValidationError(f"release exceeds configured run limit: {total_declared} bytes")
        seen: set[tuple[str, str]] = set()
        downloaded = 0
        platforms: dict[str, dict[str, Any]] = {}
        installers: dict[str, dict[str, Any]] = {}
        fingerprint_assets: list[dict[str, Any]] = []
        for asset in item.assets:
            if stop_event is not None and stop_event.is_set():
                raise DownloadInterruptedError("desktop release download was interrupted")
            key = (asset.kind, asset.platform)
            if key in seen:
                raise AssetValidationError(f"duplicate Coworker feed asset for {asset.platform}/{asset.kind}")
            seen.add(key)
            if asset.kind == "updater" and not asset.signature.strip():
                raise AssetValidationError(f"updater signature is missing for {asset.platform}")
            if asset.size > self.config.max_asset_bytes:
                raise AssetValidationError(f"asset exceeds configured size limit: {asset.file}")
            await _report(progress, release, asset.file, downloaded, total_declared)
            digest, size = await self._download_asset(
                self._resolve_url(asset.download_url),
                staging.asset_path(asset.file),
                name=asset.file,
                declared_size=asset.size,
                expected_sha256=asset.sha256,
                run_bytes_before=downloaded,
                stop_event=stop_event,
            )
            downloaded += size
            stored = {
                "file": asset.file,
                "kind": asset.kind,
                "size": size,
                "sha256": digest,
                "uploaded_at": datetime.now().astimezone().isoformat(),
            }
            if asset.signature:
                stored["signature"] = asset.signature.strip()
            (platforms if asset.kind == "updater" else installers)[asset.platform] = stored
            fingerprint_assets.append(
                {
                    "platform": asset.platform,
                    "kind": asset.kind,
                    "file": asset.file,
                    "size": size,
                    "sha256": digest,
                }
            )
            await _report(progress, release, asset.file, downloaded, total_declared)
        if not platforms:
            raise AssetValidationError("Coworker manifest does not contain updater assets")
        synced_at = datetime.now().astimezone().isoformat()
        pub_date = item.pub_date.isoformat() if item.pub_date else synced_at
        fingerprint = coworker_release_fingerprint(self.config.base_url, item.revision, fingerprint_assets)
        return {
            "version": str(release.version),
            "notes": item.notes,
            "pub_date": pub_date,
            "published": False,
            "platforms": platforms,
            "installers": installers,
            "source": {
                "type": "coworker_release",
                "source_id": self.config.source_id,
                "base_url": self.config.base_url,
                "release_id": item.id,
                "revision": item.revision,
                "prerelease": item.prerelease,
                "fingerprint": fingerprint,
                "run_id": run_id,
                "synced_at": synced_at,
            },
        }

    async def _fetch_manifest(self, url: str) -> CoworkerFeedManifest:
        response = await self._request("GET", url, headers={"Accept": "application/json"}, max_bytes=_MANIFEST_MAX_BYTES)
        self._raise_for_status(response)
        try:
            payload = _MANIFEST_ADAPTER.validate_json(response.content)
        except ValidationError as error:
            raise SourceAPIError("Coworker release manifest is invalid") from error
        if payload.schema_version != 1:
            raise SourceAPIError("unsupported Coworker manifest schema version")
        return payload

    async def _download_asset(
        self,
        url: str,
        destination: Path,
        *,
        name: str,
        declared_size: int,
        expected_sha256: str,
        run_bytes_before: int,
        stop_event: asyncio.Event | None,
    ) -> tuple[str, int]:
        response = await self._open_stream(url, headers={"Accept": "application/octet-stream"})
        temporary = destination.with_name(f".{destination.name}.download-{os.urandom(8).hex()}")
        digest = hashlib.sha256()
        size = 0
        try:
            if not 200 <= response.status_code < 300:
                content = await _read_response_limited(response, _ERROR_RESPONSE_MAX_BYTES, truncate=True)
                self._raise_for_status(_buffered_response(response, content))
            destination.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("xb") as stream:
                async for chunk in response.aiter_bytes():
                    if stop_event is not None and stop_event.is_set():
                        raise DownloadInterruptedError("desktop release download was interrupted")
                    size += len(chunk)
                    if size > self.config.max_asset_bytes:
                        raise AssetValidationError(f"asset exceeds size limit while downloading: {name}")
                    if run_bytes_before + size > self.config.max_run_bytes:
                        raise AssetValidationError("release exceeds configured run limit while downloading")
                    digest.update(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if size != declared_size:
                raise AssetValidationError(f"release asset size mismatch for {name}: expected {declared_size}, got {size}")
            actual = digest.hexdigest()
            if actual != expected_sha256.lower():
                raise AssetValidationError(f"SHA-256 mismatch for {name}")
            os.replace(temporary, destination)
            return actual, size
        finally:
            await response.aclose()
            temporary.unlink(missing_ok=True)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        max_bytes: int,
    ) -> httpx.Response:
        response = await self._open_stream(url, method=method, headers=headers)
        try:
            successful = 200 <= response.status_code < 300 or response.status_code == 304
            content = await _read_response_limited(
                response,
                max_bytes if successful else _ERROR_RESPONSE_MAX_BYTES,
                truncate=not successful,
            )
            return _buffered_response(response, content)
        finally:
            await response.aclose()

    async def _open_stream(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        current_url = url
        for redirect_count in range(self.config.max_redirects + 1):
            validated = validate_source_url(current_url)
            request_headers = {"User-Agent": _USER_AGENT, **dict(headers or {})}
            if self.config.token and _origin(validated) == self._base_origin:
                request_headers["Authorization"] = f"Bearer {self.config.token}"
            else:
                request_headers.pop("Authorization", None)
            request_headers["Connection"] = "close"
            request = self._client.build_request(method, validated, headers=request_headers)
            response = await self._client.send(request, stream=True, follow_redirects=False)
            if response.status_code not in _REDIRECT_STATUSES:
                return response
            location = response.headers.get("location")
            await response.aclose()
            if not location:
                raise SourceAPIError("redirect response is missing a Location header")
            if redirect_count >= self.config.max_redirects:
                raise SourceAPIError("too many redirects from release source")
            next_url = httpx.URL(urljoin(str(validated), location))
            if validated.scheme == "https" and next_url.scheme == "http":
                raise SourceAPIError("HTTPS release source redirected to insecure HTTP")
            current_url = str(next_url)
        raise SourceAPIError("too many redirects from release source")

    def _resolve_url(self, value: str) -> str:
        return urljoin(self.config.base_url + "/", value)

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if 200 <= response.status_code < 300 or response.status_code == 304:
            return
        raise SourceAPIError(_response_error_message(response), status_code=response.status_code)


def coworker_release_fingerprint(
    base_url: str,
    revision: str,
    assets: Sequence[Mapping[str, Any]],
) -> str:
    payload = {
        "base_url": base_url,
        "revision": revision,
        "assets": sorted(assets, key=lambda item: (str(item.get("platform")), str(item.get("kind")))),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


async def _report(
    callback: ProgressCallback | None,
    release: SourceRelease,
    asset: str,
    downloaded: int,
    total: int,
) -> None:
    if callback is not None:
        await callback(
            DownloadProgress(
                phase="downloading",
                version=str(release.version),
                asset=asset,
                bytes_downloaded=downloaded,
                bytes_total=total,
            )
        )
