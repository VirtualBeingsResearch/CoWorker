from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, unquote

import httpx
import pytest
from pydantic import ValidationError

import coworker.desktop_updates.store as desktop_store_module
from coworker.core.config import DesktopUpdatesConfig
from coworker.desktop_updates import (
    CHECKSUM_FILENAME,
    AssetValidationError,
    DesktopReleaseStore,
    DownloadProgress,
    GitHubAPIError,
    GitHubReleaseSource,
    GitHubSourceConfig,
    RateLimitError,
    RateLimitInfo,
    ReleasePage,
    SemVer,
    SemVerError,
    SourceAsset,
    SourceRelease,
    SyncService,
    SyncSourceSummary,
    SyncStatus,
    UnsafePathError,
    canonical_asset_names,
)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("1.0.0-alpha", "1.0.0-alpha.1"),
        ("1.0.0-alpha.1", "1.0.0-alpha.beta"),
        ("1.0.0-alpha.beta", "1.0.0-beta"),
        ("1.0.0-beta", "1.0.0-beta.2"),
        ("1.0.0-beta.2", "1.0.0-beta.11"),
        ("1.0.0-beta.11", "1.0.0-rc.1"),
        ("1.0.0-rc.1", "1.0.0"),
    ],
)
def test_semver_uses_strict_semver_precedence(left: str, right: str) -> None:
    assert SemVer.parse(left) < SemVer.parse(right)


def test_semver_accepts_v_and_ignores_build_for_precedence() -> None:
    assert str(SemVer.parse("v1.2.3-alpha.1+build.9")) == "1.2.3-alpha.1+build.9"
    assert SemVer.parse("1.2.3+one") == SemVer.parse("1.2.3+two")


@pytest.mark.parametrize(
    "value",
    ["1.2", "01.2.3", "1.02.3", "1.2.03", "1.0.0-01", "V1.2.3", " 1.2.3", "1.2.3+"],
)
def test_semver_rejects_non_strict_values(value: str) -> None:
    with pytest.raises(SemVerError):
        SemVer.parse(value)


def test_github_config_defaults_to_stable_published_releases_and_safe_repo() -> None:
    config = GitHubSourceConfig(repository="acme/desktop")
    assert config.include_drafts is False
    assert config.include_prereleases is False
    assert config.max_run_bytes >= config.max_asset_bytes

    for repository in ("owner", "../repo", "owner/repo/extra", "owner/re po", "-owner/repo"):
        with pytest.raises(ValidationError):
            GitHubSourceConfig(repository=repository)


@pytest.mark.parametrize(
    "repository",
    ("../repo", "./repo", "-owner/repo", "owner/-repo", "owner/repo/extra"),
)
def test_runtime_config_rejects_repositories_rejected_by_source_config(repository: str) -> None:
    with pytest.raises(ValidationError):
        DesktopUpdatesConfig(
            sync_sources=[
                {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "name": "bad github",
                    "type": "github",
                    "repository": repository,
                }
            ]
        )


async def test_store_shares_lock_and_atomically_commits_an_unpublished_draft(tmp_path) -> None:
    first = DesktopReleaseStore(tmp_path / "updates")
    second = DesktopReleaseStore(tmp_path / "updates")
    assert first.lock is second.lock

    async with first.staging("v1.2.3") as staging:
        await staging.write_asset("desktop.bin", b"payload")
        release = await staging.commit(
            {
                "version": "wrong",
                "published": True,
                "platforms": {
                    "linux-x86_64": {
                        "file": "desktop.bin",
                        "signature": "signature",
                    }
                },
                "installers": {},
            }
        )

    assert release["version"] == "1.2.3"
    assert release["published"] is False
    assert (first.assets_dir("1.2.3") / "desktop.bin").read_bytes() == b"payload"
    assert await second.read_latest() is None
    assert first.sync_state_path == first.root / "sync" / "state.json"
    with pytest.raises(UnsafePathError):
        first.asset_path("1.2.3", "../secret")


async def test_store_upload_and_publish_are_single_lock_domain_operations(tmp_path) -> None:
    store = DesktopReleaseStore(tmp_path / "updates")
    await store.create_release("1.2.3", notes="notes")
    uploaded = await store.upload_asset(
        "1.2.3",
        platform="windows-x86_64",
        signature="signed",
        kind="updater",
        filename="desktop.exe",
        content=b"binary",
    )
    assert uploaded["platforms"]["windows-x86_64"]["file"] == "desktop.exe"

    result = await store.publish_release("1.2.3")
    assert result["latest"]["platforms"]["windows-x86_64"] == {
        "file": "desktop.exe",
        "signature": "signed",
    }
    assert result["release"]["published"] is True
    assert (await store.read_latest())["version"] == "1.2.3"


async def test_publish_makes_release_durable_before_exposing_latest(
    tmp_path,
    monkeypatch,
) -> None:
    store = DesktopReleaseStore(tmp_path / "updates")
    await store.create_release("1.2.3")
    await store.upload_asset(
        "1.2.3",
        platform="windows-x86_64",
        signature="signed",
        kind="updater",
        filename="desktop.exe",
        content=b"binary",
    )
    real_atomic_write = desktop_store_module._atomic_write
    writes: list[object] = []

    def fail_latest(path, data: bytes) -> None:
        writes.append(path)
        if path == store.latest_path:
            raise OSError("simulated latest write failure")
        real_atomic_write(path, data)

    monkeypatch.setattr(desktop_store_module, "_atomic_write", fail_latest)
    with pytest.raises(OSError, match="simulated latest write failure"):
        await store.publish_release("1.2.3")

    assert writes == [store.release_path("1.2.3"), store.latest_path]
    assert (await store.read_release("1.2.3"))["published"] is True
    assert await store.read_latest() is None


def _canonical_contents(version: str) -> dict[str, bytes]:
    contents: dict[str, bytes] = {}
    for name in canonical_asset_names(version) - {CHECKSUM_FILENAME}:
        contents[name] = f"content:{name}".encode()
    for name in tuple(contents):
        if name.endswith(".sig"):
            contents[name] = f"signature:{name}".encode()
    checksum_lines = [
        f"{hashlib.sha256(contents[name]).hexdigest()}  {name}"
        for name in sorted(contents)
    ]
    contents[CHECKSUM_FILENAME] = ("\n".join(checksum_lines) + "\n").encode()
    return contents


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.yielded = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk



def _github_transport(
    contents: dict[str, bytes],
    requests: list[httpx.Request],
    *,
    draft: bool = False,
) -> httpx.MockTransport:
    sorted_names = sorted(contents)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["user-agent"] == "coworker-desktop-update-sync"
        if request.url.path == "/api/v3/repos/acme/desktop/releases":
            assets = [
                {
                    "id": index,
                    "name": name,
                    "size": len(contents[name]),
                    "url": "http://127.0.0.1/untrusted",
                    "browser_download_url": "http://127.0.0.1/also-untrusted",
                }
                for index, name in enumerate(sorted_names, start=1)
            ]
            return httpx.Response(
                200,
                headers={
                    "ETag": '"release-etag"',
                    "X-RateLimit-Limit": "60",
                    "X-RateLimit-Remaining": "59",
                },
                json=[
                    {
                        "id": 10,
                        "tag_name": "v1.2.3",
                        "name": "CoWorker 1.2.3",
                        "body": "release notes",
                        "draft": draft,
                        "prerelease": False,
                        "created_at": "2026-07-22T10:00:00Z",
                        "html_url": "https://example.test/releases/10",
                        "assets": assets,
                    }
                ],
            )
        if request.headers["host"] == "api.example.test" and "/releases/assets/" in request.url.path:
            assert request.url.path.startswith(
                "/api/v3/repos/acme/desktop/releases/assets/"
            )
            assert request.headers["authorization"] == "Bearer secret"
            index = int(request.url.path.rsplit("/", 1)[1])
            name = sorted_names[index - 1]
            return httpx.Response(
                302,
                headers={
                    "Location": "https://downloads.example.test/files/" + quote(name, safe="")
                },
            )
        if request.headers["host"] == "downloads.example.test":
            assert "authorization" not in request.headers
            name = unquote(request.url.path.removeprefix("/files/"))
            return httpx.Response(200, content=contents[name])
        raise AssertionError(f"unexpected request: {request.url}")

    return httpx.MockTransport(handler)


async def test_github_source_uses_asset_ids_and_writes_fingerprinted_metadata(tmp_path) -> None:
    version = "1.2.3"
    contents = _canonical_contents(version)
    requests: list[httpx.Request] = []
    source = GitHubReleaseSource(
        GitHubSourceConfig(
            repository="acme/desktop",
            api_base_url="https://api.example.test/api/v3",
            token="secret",
        ),
        transport=_github_transport(contents, requests),
    )
    page = await source.fetch_releases()
    release = page.releases[0]
    assert release.release_id == 10
    assert {asset.asset_id for asset in release.assets.values()} == set(range(1, 13))

    progress: list[DownloadProgress] = []
    store = DesktopReleaseStore(tmp_path / "updates")
    async with store.staging(version) as staging:
        metadata = await source.download_release(
            release,
            staging,
            run_id="run-123",
            progress=lambda item: _append_progress(progress, item),
        )
        await staging.commit(metadata)
    await source.aclose()

    imported = await store.read_release(version)
    source_metadata = imported["source"]
    assert source_metadata["type"] == "github_release"
    assert source_metadata["api_base_url"] == "https://api.example.test/api/v3"
    assert source_metadata["repository"] == "acme/desktop"
    assert source_metadata["release_id"] == 10
    assert source_metadata["tag"] == "v1.2.3"
    assert source_metadata["html_url"] == "https://example.test/releases/10"
    assert source_metadata["run_id"] == "run-123"
    assert len(source_metadata["fingerprint"]) == 64
    assert progress[-1].bytes_downloaded == sum(len(value) for value in contents.values())
    assert len(
        [request for request in requests if request.headers["host"] == "downloads.example.test"]
    ) == 12
    assert not any("127.0.0.1" in str(request.url) for request in requests)
    assert await store.read_latest() is None


async def test_github_source_accepts_partial_assets_and_api_digests(tmp_path) -> None:
    version = "1.2.3"
    names = [
        "CoWorker.Desktop_1.2.3_amd64.deb",
        "CoWorker.Desktop_1.2.3_amd64.AppImage",
    ]
    contents = {name: f"content:{name}".encode() for name in names}
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v3/repos/acme/desktop/releases":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 10,
                        "tag_name": "v1.2.3",
                        "assets": [
                            {
                                "id": index,
                                "name": name,
                                "size": len(contents[name]),
                                "digest": "sha256:" + hashlib.sha256(contents[name]).hexdigest(),
                            }
                            for index, name in enumerate(names, start=1)
                        ],
                    }
                ],
            )
        if "/releases/assets/" in request.url.path:
            index = int(request.url.path.rsplit("/", 1)[1])
            return httpx.Response(
                302,
                headers={"Location": "https://downloads.example.test/files/" + quote(names[index - 1], safe="")},
            )
        if request.url.path.startswith("/files/"):
            return httpx.Response(200, content=contents[unquote(request.url.path.removeprefix("/files/"))])
        raise AssertionError(f"unexpected request: {request.url}")

    source = GitHubReleaseSource(
        GitHubSourceConfig(
            repository="acme/desktop",
            api_base_url="https://api.example.test/api/v3",
        ),
        transport=httpx.MockTransport(handler),
    )
    page = await source.fetch_releases()
    assert set(page.releases[0].assets) == set(names)
    store = DesktopReleaseStore(tmp_path / "updates")
    async with store.staging(version) as staging:
        metadata = await source.download_release(page.releases[0], staging, run_id="partial")
        await staging.commit(metadata)
    await source.aclose()

    imported = await store.read_release(version)
    assert set(imported["installers"]) == {"linux-x86_64"}
    assert set(imported["platforms"]) == {"linux-x86_64"}
    assert "signature" not in imported["platforms"]["linux-x86_64"]
    assert len([request for request in requests if request.url.path.startswith("/files/")]) == 2


async def _append_progress(target: list[DownloadProgress], item: DownloadProgress) -> None:
    target.append(item)


async def test_github_source_checks_declared_run_size_before_asset_download(tmp_path) -> None:
    contents = _canonical_contents("1.2.3")
    total = sum(len(value) for value in contents.values())
    requests: list[httpx.Request] = []
    source = GitHubReleaseSource(
        GitHubSourceConfig(
            repository="acme/desktop",
            api_base_url="https://api.example.test/api/v3",
            token="secret",
            max_asset_bytes=max(len(value) for value in contents.values()),
            max_run_bytes=total - 1,
        ),
        transport=_github_transport(contents, requests),
    )
    page = await source.fetch_releases()
    store = DesktopReleaseStore(tmp_path / "updates")
    async with store.staging("1.2.3") as staging:
        with pytest.raises(AssetValidationError, match="run limit"):
            await source.download_release(page.releases[0], staging, run_id="run-limit")
    await source.aclose()
    assert not any("/releases/assets/" in request.url.path for request in requests)


@pytest.mark.parametrize(
    ("oversized_name", "oversized_content", "error"),
    [
        (CHECKSUM_FILENAME, b"x" * (256 * 1024 + 1), "checksum size limit"),
        ("CoWorker.Desktop_1.2.3_x64-setup.exe.sig", b"x" * (64 * 1024 + 1), "signature size limit"),
    ],
    ids=("checksum", "signature"),
)
async def test_github_source_rejects_oversized_text_assets_before_download(
    tmp_path,
    oversized_name: str,
    oversized_content: bytes,
    error: str,
) -> None:
    contents = _canonical_contents("1.2.3")
    contents[oversized_name] = oversized_content
    requests: list[httpx.Request] = []
    source = GitHubReleaseSource(
        GitHubSourceConfig(
            repository="acme/desktop",
            api_base_url="https://api.example.test/api/v3",
            token="secret",
        ),
        transport=_github_transport(contents, requests),
    )
    page = await source.fetch_releases()
    store = DesktopReleaseStore(tmp_path / "updates")
    async with store.staging("1.2.3") as staging:
        with pytest.raises(AssetValidationError, match=error):
            await source.download_release(page.releases[0], staging, run_id="text-limit")
    await source.aclose()
    assert not any("/releases/assets/" in request.url.path for request in requests)


async def test_github_source_defaults_filter_drafts_and_handles_304() -> None:
    contents = _canonical_contents("1.2.3")
    requests: list[httpx.Request] = []
    source = GitHubReleaseSource(
        GitHubSourceConfig(
            repository="acme/desktop",
            api_base_url="https://api.example.test/api/v3",
        ),
        transport=_github_transport(contents, requests, draft=True),
    )
    page = await source.fetch_releases()
    await source.aclose()
    assert page.releases == ()
    assert "draft releases are disabled" in page.skipped[0]

    def not_modified(request: httpx.Request) -> httpx.Response:
        assert request.headers["if-none-match"] == '"old"'
        return httpx.Response(304, headers={"ETag": '"old"'})

    source = GitHubReleaseSource(
        GitHubSourceConfig(repository="acme/desktop"),
        transport=httpx.MockTransport(not_modified),
    )
    assert (await source.fetch_releases('"old"')).not_modified is True
    await source.aclose()


async def test_github_source_bounds_release_list_response() -> None:
    stream = _ChunkStream([b"[" + b" " * (8 * 1024 * 1024)])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    source = GitHubReleaseSource(
        GitHubSourceConfig(repository="acme/desktop"),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(GitHubAPIError, match="response exceeds"):
        await source.fetch_releases()
    await source.aclose()
    assert stream.yielded == 1


async def test_github_source_truncates_asset_error_response(tmp_path) -> None:
    stream = _ChunkStream([b"x" * (32 * 1024) for _ in range(4)])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, stream=stream)

    source = GitHubReleaseSource(
        GitHubSourceConfig(repository="acme/desktop"),
        transport=httpx.MockTransport(handler),
    )
    store = DesktopReleaseStore(tmp_path / "updates")
    async with store.staging("1.2.3") as staging:
        with pytest.raises(GitHubAPIError, match="HTTP 500"):
            await source._download_asset(
                SourceAsset(asset_id=1, name="asset.bin", size=1),
                staging.asset_path("asset.bin"),
                run_bytes_before=0,
            )
    await source.aclose()
    assert stream.yielded == 3


async def test_github_source_keeps_hostname_for_httpx_connection() -> None:
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.host)
        assert request.url.host == "api.example.test"
        assert request.headers["host"] == "api.example.test"
        return httpx.Response(200, json=[])

    source = GitHubReleaseSource(
        GitHubSourceConfig(
            repository="acme/desktop",
            api_base_url="https://api.example.test",
        ),
        transport=httpx.MockTransport(handler),
    )
    page = await source.fetch_releases()
    await source.aclose()

    assert page.releases == ()
    assert attempts == ["api.example.test"]

def _source_release(version: str, release_id: int) -> SourceRelease:
    return SourceRelease(
        release_id=release_id,
        version=version,
        tag_name=f"v{version}",
        assets={},
    )


class _FakeSource:
    source_summary = SyncSourceSummary(
        api_base_url="https://api.example.test/api/v3",
        repository="acme/desktop",
    )

    def __init__(
        self,
        releases: list[SourceRelease] | None = None,
        *,
        fingerprint: str = "source-fingerprint",
    ) -> None:
        self.releases = releases or []
        self.fingerprint = fingerprint
        self.fetch_calls = 0
        self.downloaded: list[str] = []

    async def fetch_releases(self, etag: str | None = None) -> ReleasePage:
        self.fetch_calls += 1
        return ReleasePage(releases=tuple(self.releases), etag='"etag"')

    async def download_release(
        self,
        release: SourceRelease,
        staging,
        *,
        run_id: str,
        stop_event=None,
        progress=None,
    ) -> dict[str, Any]:
        version = str(release.version)
        self.downloaded.append(version)
        await staging.write_asset("desktop.bin", b"binary")
        if progress is not None:
            await progress(
                DownloadProgress(
                    phase="downloading",
                    version=version,
                    asset="desktop.bin",
                    bytes_downloaded=6,
                    bytes_total=6,
                )
            )
        return {
            "version": version,
            "published": False,
            "platforms": {
                "linux-x86_64": {
                    "file": "desktop.bin",
                    "signature": "signed",
                }
            },
            "installers": {},
            "source": {
                "type": "github_release",
                "fingerprint": self.fingerprint,
                "run_id": run_id,
            },
        }


class _EtagSource(_FakeSource):
    def __init__(self, source_summary: SyncSourceSummary) -> None:
        super().__init__()
        self.source_summary = source_summary
        self.etags: list[str | None] = []

    async def fetch_releases(self, etag: str | None = None) -> ReleasePage:
        self.etags.append(etag)
        return ReleasePage(not_modified=True, etag='"current"')


async def _commit_existing(
    store: DesktopReleaseStore,
    version: str,
    fingerprint: str | None,
) -> None:
    async with store.staging(version) as staging:
        await staging.write_asset("desktop.bin", b"binary")
        source = {"fingerprint": fingerprint} if fingerprint is not None else None
        release: dict[str, Any] = {
            "platforms": {
                "linux-x86_64": {
                    "file": "desktop.bin",
                    "signature": "signed",
                }
            },
            "installers": {},
        }
        if source is not None:
            release["source"] = source
        await staging.commit(release)


@pytest.mark.parametrize(("include_drafts", "expected_etag"), [(False, '"old"'), (True, None)])
async def test_sync_discards_etag_when_source_filter_scope_changes(
    tmp_path,
    include_drafts: bool,
    expected_etag: str | None,
) -> None:
    store = DesktopReleaseStore(tmp_path / "updates")
    previous_source = SyncSourceSummary(
        api_base_url="https://api.example.test/api/v3",
        repository="acme/desktop",
        include_drafts=False,
    )
    await store.write_sync_state(SyncStatus(source=previous_source, etag='"old"'))
    source = _EtagSource(
        SyncSourceSummary(
            api_base_url="https://api.example.test/api/v3",
            repository="acme/desktop",
            include_drafts=include_drafts,
        )
    )
    service = SyncService(store, source)

    result = await service.sync_now()

    assert result.outcome == "not_modified"
    assert source.etags == [expected_etag]
    await service.stop()


async def test_sync_imports_only_highest_candidate_and_persists_progress(tmp_path) -> None:
    store = DesktopReleaseStore(tmp_path / "updates")
    source = _FakeSource([_source_release("1.1.0", 1), _source_release("2.0.0", 2)])
    service = SyncService(store, source)

    result = await service.sync_now()

    assert result.outcome == "succeeded"
    assert result.imported_versions == ("2.0.0",)
    assert source.downloaded == ["2.0.0"]
    imported = await store.read_release("2.0.0")
    assert imported["source"]["run_id"] == result.run_id
    status = await service.status()
    assert status.run_id == result.run_id
    assert status.version == "2.0.0"
    assert status.bytes_downloaded == status.bytes_total == 6
    assert status.source.repository == "acme/desktop"
    assert await store.read_latest() is None
    await service.stop()


async def test_sync_requires_candidate_to_be_newer_than_latest(tmp_path) -> None:
    store = DesktopReleaseStore(tmp_path / "updates")
    await store.write_latest({"version": "2.0.0", "platforms": {}})
    source = _FakeSource([_source_release("1.9.0", 1)])
    service = SyncService(store, source)

    result = await service.sync_now()

    assert result.outcome == "no_updates"
    assert source.downloaded == []
    await service.stop()


@pytest.mark.parametrize(
    ("existing_fingerprint", "expected"),
    [("source-fingerprint", "no_updates"), ("different", "conflict"), (None, "conflict")],
)
async def test_sync_compares_existing_release_fingerprint(
    tmp_path,
    existing_fingerprint: str | None,
    expected: str,
) -> None:
    store = DesktopReleaseStore(tmp_path / "updates")
    await _commit_existing(store, "1.2.3", existing_fingerprint)
    source = _FakeSource([_source_release("1.2.3", 10)])
    service = SyncService(store, source)

    result = await service.sync_now()

    assert result.outcome == expected
    assert source.downloaded == ["1.2.3"]
    await service.stop()


class _CoalescingSource(_FakeSource):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def fetch_releases(self, etag: str | None = None) -> ReleasePage:
        self.fetch_calls += 1
        self.started.set()
        await self.release.wait()
        return ReleasePage(etag='"new"', not_modified=True)


async def test_request_sync_is_nonblocking_and_coalesces_run_id(tmp_path) -> None:
    store = DesktopReleaseStore(tmp_path / "updates")
    source = _CoalescingSource()
    service = SyncService(store, source)

    first = await service.request_sync("manual")
    queued = await store.read_sync_state()
    assert queued.run_id == first["run_id"]
    assert queued.phase in {"queued", "checking"}
    await source.started.wait()
    second = await service.request_sync("manual")
    waiter = asyncio.create_task(service.sync_now())
    assert first["coalesced"] is False
    assert second == {"run_id": first["run_id"], "coalesced": True}
    source.release.set()

    result = await waiter
    assert result.run_id == first["run_id"]
    assert source.fetch_calls == 1
    await service.stop()


class _BlockingSource(_FakeSource):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def fetch_releases(self, etag: str | None = None) -> ReleasePage:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def test_sync_start_recovers_interrupted_state_and_stale_staging(tmp_path) -> None:
    store = DesktopReleaseStore(tmp_path / "updates")
    await store.write_sync_state(SyncStatus(outcome="running", run_id="old", phase="downloading"))
    stale = store.staging_root / "stale-run"
    stale.mkdir(parents=True)
    (stale / "partial").write_bytes(b"partial")
    service = SyncService(store, _FakeSource())

    status = await service.status()

    assert status.outcome == "interrupted"
    assert status.phase == "interrupted"
    assert not stale.exists()
    await service.stop()


async def test_sync_records_interrupted_on_stop(tmp_path) -> None:
    store = DesktopReleaseStore(tmp_path / "updates")
    source = _BlockingSource()
    service = SyncService(store, source)
    trigger = asyncio.create_task(service.sync_now())
    await source.started.wait()

    await service.stop()
    result = await trigger

    assert result.outcome == "interrupted"
    assert (await store.read_sync_state()).outcome == "interrupted"


class _FlakyScheduledSource(_FakeSource):
    def __init__(self) -> None:
        super().__init__()
        self.second_call = asyncio.Event()

    async def fetch_releases(self, etag: str | None = None) -> ReleasePage:
        self.fetch_calls += 1
        if self.fetch_calls == 1:
            raise ValueError("one bad scheduler iteration")
        self.second_call.set()
        return ReleasePage(not_modified=True, etag='"recovered"')


async def test_scheduler_continues_after_one_failed_iteration(tmp_path) -> None:
    source = _FlakyScheduledSource()
    service = SyncService(
        DesktopReleaseStore(tmp_path / "updates"),
        source,
        interval_seconds=0.01,
    )

    await service.start(run_immediately=True)
    await asyncio.wait_for(source.second_call.wait(), timeout=1)

    assert source.fetch_calls >= 2
    await service.stop()


class _RateLimitedSource(_FakeSource):
    async def fetch_releases(self, etag: str | None = None) -> ReleasePage:
        raise RateLimitError(
            "rate limited",
            RateLimitInfo(remaining=0, retry_after_seconds=60),
            status_code=429,
        )


async def test_sync_rate_limit_sets_outcome_and_delayed_next_run(tmp_path) -> None:
    store = DesktopReleaseStore(tmp_path / "updates")
    service = SyncService(store, _RateLimitedSource())
    before = datetime.now(UTC).astimezone()

    result = await service.sync_now()
    status = await service.status()

    assert result.outcome == "rate_limited"
    assert status.outcome == "rate_limited"
    assert status.next_run_at is not None
    assert (status.next_run_at - before).total_seconds() >= 55
    await service.stop()
