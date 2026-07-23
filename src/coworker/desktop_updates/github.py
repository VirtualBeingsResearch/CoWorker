from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

import httpx
from loguru import logger
from pydantic import TypeAdapter, ValidationError

from .assets import (
    CANONICAL_ASSET_SPECS,
    CHECKSUM_FILENAME,
    AssetValidationError,
    canonical_asset_names,
    parse_sha256sums,
)
from .errors import (
    DownloadInterruptedError,
    GitHubAPIError,
    RateLimitError,
    UnsafeSourceURLError,
)
from .models import (
    DownloadProgress,
    GitHubReleasePayload,
    GitHubSourceConfig,
    RateLimitInfo,
    ReleasePage,
    SourceAsset,
    SourceRelease,
    SyncSourceSummary,
)
from .semver import SemVer, SemVerError
from .store import ReleaseStaging

ProgressCallback = Callable[[DownloadProgress], Awaitable[None]]
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_RELEASE_LIST_ADAPTER = TypeAdapter(list[GitHubReleasePayload])
_USER_AGENT = "coworker-desktop-update-sync"
_CHECKSUM_MAX_BYTES = 256 * 1024
_SIGNATURE_MAX_BYTES = 64 * 1024
_RELEASE_LIST_MAX_BYTES = 8 * 1024 * 1024
_ERROR_RESPONSE_MAX_BYTES = 64 * 1024
_DIGEST_RE = re.compile(r"^sha256:([0-9a-fA-F]{64})$")


def _normalize_digest(value: str | None) -> str | None:
    """Return a usable SHA-256 digest from GitHub's ``sha256:<hex>`` form."""
    if value is None:
        return None
    match = _DIGEST_RE.fullmatch(value.strip())
    return match.group(1).lower() if match else None


def validate_source_url(value: str | httpx.URL) -> httpx.URL:
    """Validate a source URL; hostname resolution is delegated to HTTPX."""
    try:
        url = value if isinstance(value, httpx.URL) else httpx.URL(value)
    except Exception as error:
        raise UnsafeSourceURLError(f"invalid source URL: {value!r}") from error
    if url.scheme not in {"http", "https"}:
        raise UnsafeSourceURLError(f"unsupported source URL scheme: {url.scheme!r}")
    if url.userinfo:
        raise UnsafeSourceURLError("source URLs must not contain credentials")
    if not url.host:
        raise UnsafeSourceURLError("source URL must contain a hostname")
    return url


class GitHubReleaseSource:
    """Fetch and verify canonical releases from a GitHub-compatible API."""

    def __init__(
        self,
        config: GitHubSourceConfig,
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
        self._api_origin = _origin(httpx.URL(config.api_base_url))

    @property
    def source_summary(self) -> SyncSourceSummary:
        return SyncSourceSummary(
            source_id=self.config.source_id,
            name=self.config.source_name,
            provider="github",
            endpoint=self.config.api_base_url,
            target=self.config.repository,
            options={
                "include_drafts": self.config.include_drafts,
                "include_prereleases": self.config.include_prereleases,
            },
        )

    async def __aenter__(self) -> GitHubReleaseSource:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def releases_url(self) -> str:
        return f"{self._repository_api_url()}/releases?per_page=20"

    def asset_url(self, asset_id: str | int) -> str:
        return f"{self._repository_api_url()}/releases/assets/{quote(str(asset_id), safe='')}"

    def _repository_api_url(self) -> str:
        owner, repository = self.config.repository.split("/", 1)
        path = f"repos/{quote(owner, safe='')}/{quote(repository, safe='')}"
        return f"{self.config.api_base_url}/{path}"

    async def fetch_releases(self, etag: str | None = None) -> ReleasePage:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if etag:
            headers["If-None-Match"] = etag
        response = await self._request("GET", self.releases_url, headers=headers)
        rate_limit = _rate_limit_info(response.headers)
        response_etag = response.headers.get("etag") or etag
        if response.status_code == 304:
            return ReleasePage(etag=response_etag, not_modified=True, rate_limit=rate_limit)
        self._raise_for_status(response, rate_limit)
        try:
            payload = _RELEASE_LIST_ADAPTER.validate_json(response.content)
        except ValidationError as error:
            raise GitHubAPIError("GitHub release list response is invalid") from error

        releases: list[SourceRelease] = []
        skipped: list[str] = []
        for item in payload[:20]:
            candidate, reason = self._filter_release(item)
            if candidate is None:
                skipped.append(reason)
                logger.debug(f"Skipped GitHub desktop release: {reason}")
            else:
                releases.append(candidate)
        return ReleasePage(
            releases=tuple(releases),
            etag=response_etag,
            rate_limit=rate_limit,
            skipped=tuple(skipped),
        )

    list_releases = fetch_releases

    def _filter_release(self, payload: GitHubReleasePayload) -> tuple[SourceRelease | None, str]:
        label = payload.tag_name or "<missing tag>"
        if payload.draft and not self.config.include_drafts:
            return None, f"{label}: draft releases are disabled"
        if payload.prerelease and not self.config.include_prereleases:
            return None, f"{label}: prereleases are disabled"
        try:
            version = SemVer.parse(payload.tag_name)
        except SemVerError as error:
            return None, f"{label}: {error}"
        if version.build:
            return None, f"{label}: canonical desktop assets do not support build metadata"
        if payload.prerelease != version.is_prerelease:
            return None, f"{label}: prerelease flag does not match the semantic version"
        assets: dict[str, SourceAsset] = {}
        for asset in payload.assets:
            # Releases are allowed to contain only the platforms that were
            # built for that version. Ignore unrelated release assets, while
            # retaining the canonical-name validation for assets we import.
            if asset.name not in canonical_asset_names(version):
                continue
            if asset.name in assets:
                return None, f"{label}: duplicate asset name {asset.name!r}"
            if asset.size <= 0:
                return None, f"{label}: asset {asset.name!r} is empty"
            digest = _normalize_digest(asset.digest)
            if asset.digest and digest is None:
                return None, f"{label}: invalid SHA-256 digest for {asset.name!r}"
            assets[asset.name] = SourceAsset(
                asset_id=str(asset.asset_id),
                name=asset.name,
                size=asset.size,
                digest=digest,
            )
        if not assets or set(assets) <= {CHECKSUM_FILENAME}:
            return None, f"{label}: no recognized desktop assets"
        return (
            SourceRelease(
                release_id=str(payload.release_id),
                version=version,
                tag_name=payload.tag_name,
                name=payload.name or "",
                notes=payload.body or "",
                draft=payload.draft,
                prerelease=payload.prerelease,
                published_at=payload.published_at or payload.created_at,
                html_url=payload.html_url,
                assets=assets,
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
        if not release.assets:
            raise AssetValidationError("release does not contain recognized desktop assets")
        total_declared = sum(asset.size for asset in release.assets.values())
        if total_declared > self.config.max_run_bytes:
            raise AssetValidationError(
                f"release exceeds configured run limit: {total_declared} bytes"
            )
        oversized = [
            asset.name
            for asset in release.assets.values()
            if asset.size > self.config.max_asset_bytes
        ]
        if oversized:
            raise AssetValidationError(f"asset exceeds configured size limit: {oversized[0]}")

        checksum_asset = release.assets.get(CHECKSUM_FILENAME)
        if checksum_asset is not None and checksum_asset.size > _CHECKSUM_MAX_BYTES:
            raise AssetValidationError("SHA256SUMS.txt exceeds the checksum size limit")
        oversized_signature = next(
            (
                asset.name
                for asset in release.assets.values()
                if asset.name.endswith(".sig") and asset.size > _SIGNATURE_MAX_BYTES
            ),
            None,
        )
        if oversized_signature is not None:
            raise AssetValidationError(
                f"signature exceeds the signature size limit: {oversized_signature}"
            )

        downloaded = 0
        checksums: dict[str, str] = {}
        sizes: dict[str, int] = {}
        manifest_checksums: dict[str, str] = {}
        if checksum_asset is not None:
            await _report(progress, release, CHECKSUM_FILENAME, downloaded, total_declared)
            checksum_digest, checksum_size = await self._download_asset(
                checksum_asset,
                staging.asset_path(CHECKSUM_FILENAME),
                run_bytes_before=downloaded,
                max_bytes=_CHECKSUM_MAX_BYTES,
                expected_sha256=checksum_asset.digest,
                stop_event=stop_event,
            )
            downloaded += checksum_size
            sizes[CHECKSUM_FILENAME] = checksum_size
            checksums[CHECKSUM_FILENAME] = checksum_digest
            await _report(progress, release, CHECKSUM_FILENAME, downloaded, total_declared)
            try:
                checksum_content = staging.asset_path(CHECKSUM_FILENAME).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as error:
                raise AssetValidationError("SHA256SUMS.txt is not valid UTF-8") from error
            manifest_checksums = parse_sha256sums(
                checksum_content,
                release.version,
                set(release.assets) - {CHECKSUM_FILENAME},
            )

        for filename in sorted(set(release.assets) - {CHECKSUM_FILENAME}):
            if stop_event is not None and stop_event.is_set():
                raise DownloadInterruptedError("desktop release download was interrupted")
            asset = release.assets[filename]
            await _report(progress, release, filename, downloaded, total_declared)
            digest, size = await self._download_asset(
                asset,
                staging.asset_path(filename),
                expected_sha256=manifest_checksums.get(filename) or release.assets[filename].digest,
                run_bytes_before=downloaded,
                max_bytes=_SIGNATURE_MAX_BYTES if filename.endswith(".sig") else None,
                stop_event=stop_event,
            )
            downloaded += size
            checksums[filename] = digest
            sizes[filename] = size
            await _report(progress, release, filename, downloaded, total_declared)

        fingerprint = release_fingerprint(self.config.repository, release, checksums)
        return self._release_metadata(
            release,
            staging.assets_path,
            checksums,
            sizes,
            fingerprint=fingerprint,
            run_id=run_id,
        )

    async def _download_asset(
        self,
        asset: SourceAsset,
        destination: Path,
        *,
        run_bytes_before: int,
        expected_sha256: str | None = None,
        max_bytes: int | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> tuple[str, int]:
        response = await self._open_stream(
            self.asset_url(asset.asset_id),
            headers={"Accept": "application/octet-stream"},
        )
        temporary = destination.with_name(f".{destination.name}.download-{os.urandom(8).hex()}")
        digest = hashlib.sha256()
        size = 0
        asset_limit = min(self.config.max_asset_bytes, max_bytes or self.config.max_asset_bytes)
        try:
            if not 200 <= response.status_code < 300:
                content = await _read_response_limited(
                    response,
                    _ERROR_RESPONSE_MAX_BYTES,
                    truncate=True,
                )
                self._raise_for_status(
                    _buffered_response(response, content),
                    _rate_limit_info(response.headers),
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("xb") as stream:
                async for chunk in response.aiter_bytes():
                    if stop_event is not None and stop_event.is_set():
                        raise DownloadInterruptedError("desktop release download was interrupted")
                    size += len(chunk)
                    if size > asset_limit:
                        raise AssetValidationError(
                            f"asset exceeds size limit while downloading: {asset.name}"
                        )
                    if run_bytes_before + size > self.config.max_run_bytes:
                        raise AssetValidationError("release exceeds configured run limit while downloading")
                    digest.update(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if size == 0:
                raise AssetValidationError(f"release asset is empty: {asset.name}")
            if size != asset.size:
                raise AssetValidationError(
                    f"release asset size mismatch for {asset.name}: expected {asset.size}, got {size}"
                )
            actual_digest = digest.hexdigest()
            if expected_sha256 is not None and actual_digest != expected_sha256.lower():
                raise AssetValidationError(f"SHA-256 mismatch for {asset.name}")
            os.replace(temporary, destination)
            return actual_digest, size
        finally:
            await response.aclose()
            temporary.unlink(missing_ok=True)

    def _release_metadata(
        self,
        release: SourceRelease,
        assets_root: Path,
        checksums: Mapping[str, str],
        sizes: Mapping[str, int],
        *,
        fingerprint: str,
        run_id: str,
    ) -> dict[str, Any]:
        synced_at = datetime.now(UTC).astimezone().isoformat()
        platforms: dict[str, dict[str, Any]] = {}
        installers: dict[str, dict[str, Any]] = {}
        for spec in CANONICAL_ASSET_SPECS:
            filename = spec.filename(release.version)
            if filename not in sizes:
                continue
            asset = {
                "file": filename,
                "kind": spec.kind,
                "size": sizes[filename],
                "sha256": checksums[filename],
                "uploaded_at": synced_at,
            }
            signature_filename = spec.signature_filename(release.version)
            if signature_filename is not None and signature_filename in sizes:
                try:
                    signature = (assets_root / signature_filename).read_text(encoding="utf-8").strip()
                except (OSError, UnicodeDecodeError) as error:
                    raise AssetValidationError(
                        f"signature file is not valid UTF-8: {signature_filename}"
                    ) from error
                if not signature:
                    raise AssetValidationError(f"signature file is empty: {signature_filename}")
                asset["signature"] = signature
                asset["signature_file"] = signature_filename
            destination = platforms if spec.kind == "updater" else installers
            destination[spec.platform] = asset
        pub_date = release.published_at.isoformat() if release.published_at else synced_at
        return {
            "version": str(release.version),
            "notes": release.notes,
            "pub_date": pub_date,
            "published": False,
            "platforms": platforms,
            "installers": installers,
            "source": {
                "type": "github_release",
                "api_base_url": self.config.api_base_url,
                "repository": self.config.repository,
                "release_id": release.release_id,
                "tag": release.tag_name,
                "html_url": release.html_url,
                "draft": release.draft,
                "prerelease": release.prerelease,
                "fingerprint": fingerprint,
                "run_id": run_id,
                "synced_at": synced_at,
            },
        }

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        response = await self._open_stream(url, method=method, headers=headers)
        try:
            successful = 200 <= response.status_code < 300 or response.status_code == 304
            content = await _read_response_limited(
                response,
                _RELEASE_LIST_MAX_BYTES if successful else _ERROR_RESPONSE_MAX_BYTES,
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
            if self.config.token and _origin(validated) == self._api_origin:
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
                raise GitHubAPIError("redirect response is missing a Location header")
            if redirect_count >= self.config.max_redirects:
                raise GitHubAPIError("too many redirects from release source")
            next_url = httpx.URL(urljoin(str(validated), location))
            if validated.scheme == "https" and next_url.scheme == "http":
                raise GitHubAPIError("HTTPS release source redirected to insecure HTTP")
            current_url = str(next_url)
        raise GitHubAPIError("too many redirects from release source")

    @staticmethod
    def _raise_for_status(response: httpx.Response, rate_limit: RateLimitInfo) -> None:
        if 200 <= response.status_code < 300 or response.status_code == 304:
            return
        message = _response_error_message(response)
        if response.status_code == 429 or (
            response.status_code == 403
            and (rate_limit.remaining == 0 or rate_limit.retry_after_seconds is not None)
        ):
            raise RateLimitError(message, rate_limit, status_code=response.status_code)
        raise GitHubAPIError(message, status_code=response.status_code)


GitHubCompatibleSource = GitHubReleaseSource
GithubReleaseSource = GitHubReleaseSource


async def _read_response_limited(
    response: httpx.Response,
    max_bytes: int,
    *,
    truncate: bool,
) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        remaining = max_bytes - size
        if len(chunk) > remaining:
            if truncate:
                if remaining > 0:
                    chunks.append(chunk[:remaining])
                return b"".join(chunks)
            raise GitHubAPIError(f"GitHub response exceeds {max_bytes} bytes")
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def _buffered_response(response: httpx.Response, content: bytes) -> httpx.Response:
    headers = httpx.Headers(response.headers)
    for name in ("content-encoding", "content-length", "transfer-encoding"):
        headers.pop(name, None)
    return httpx.Response(
        response.status_code,
        headers=headers,
        content=content,
        request=response.request,
    )


def release_fingerprint(
    repository: str,
    release: SourceRelease,
    checksums: Mapping[str, str],
) -> str:
    assets = [
        {
            "asset_id": asset.asset_id,
            "name": asset.name,
            "size": asset.size,
            "sha256": checksums[asset.name],
        }
        for asset in sorted(release.assets.values(), key=lambda item: item.name)
    ]
    payload = {
        "repository": repository,
        "release_id": release.release_id,
        "tag": release.tag_name,
        "assets": assets,
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


def _origin(url: httpx.URL) -> tuple[str, str, int]:
    default_port = 443 if url.scheme == "https" else 80
    return (url.scheme.lower(), (url.host or "").lower(), url.port or default_port)


def _rate_limit_info(headers: httpx.Headers) -> RateLimitInfo:
    limit = _optional_int(headers.get("x-ratelimit-limit"))
    remaining = _optional_int(headers.get("x-ratelimit-remaining"))
    reset_timestamp = _optional_int(headers.get("x-ratelimit-reset"))
    reset_at = (
        datetime.fromtimestamp(reset_timestamp, tz=UTC) if reset_timestamp is not None else None
    )
    retry_after = _retry_after_seconds(headers.get("retry-after"))
    return RateLimitInfo(
        limit=limit,
        remaining=remaining,
        reset_at=reset_at,
        retry_after_seconds=retry_after,
    )


def _optional_int(value: str | None) -> int | None:
    if value is None:
        return None
    with suppress(ValueError):
        return int(value)
    return None


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    with suppress(ValueError):
        return max(0.0, float(value))
    with suppress(ValueError):
        parsed = datetime.strptime(value, "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=UTC)
        return max(0.0, (parsed - datetime.now(UTC)).total_seconds())
    return None


def _response_error_message(response: httpx.Response) -> str:
    detail = ""
    with suppress(json.JSONDecodeError, UnicodeDecodeError):
        payload = json.loads(response.content.decode())
        if isinstance(payload, dict):
            detail = str(payload.get("message") or "")
    suffix = f": {detail}" if detail else ""
    return f"release source returned HTTP {response.status_code}{suffix}"
