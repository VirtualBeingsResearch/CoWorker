from __future__ import annotations

import re
from contextlib import suppress
from datetime import datetime
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .semver import SemVer, SemVerError

_REPOSITORY_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})$"
)


class GitHubSourceConfig(BaseModel):
    """Configuration for a GitHub Releases-compatible API endpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str
    api_base_url: str = "https://api.github.com"
    source_id: str = "github"
    source_name: str = "GitHub Releases"
    token: str = ""
    include_drafts: bool = False
    include_prereleases: bool = False
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    max_asset_bytes: int = Field(default=2 * 1024 * 1024 * 1024, gt=0)
    max_run_bytes: int = Field(default=8 * 1024 * 1024 * 1024, gt=0)
    max_redirects: int = Field(default=5, ge=0, le=20)

    @field_validator("repository")
    @classmethod
    def _validate_repository(cls, value: str) -> str:
        value = value.strip()
        if not _REPOSITORY_RE.fullmatch(value):
            raise ValueError("repository must be a safe owner/name pair")
        owner, repository = value.split("/", 1)
        if owner in {".", ".."} or repository in {".", ".."}:
            raise ValueError("repository contains an unsafe path segment")
        return value

    @field_validator("api_base_url")
    @classmethod
    def _normalize_api_base_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("api_base_url must not be empty")
        try:
            url = httpx.URL(value)
        except Exception as error:
            raise ValueError("api_base_url is not a valid URL") from error
        if url.scheme not in {"http", "https"} or not url.host:
            raise ValueError("api_base_url must be an absolute HTTP(S) URL")
        if url.userinfo:
            raise ValueError("api_base_url must not contain credentials")
        if url.query or url.fragment:
            raise ValueError("api_base_url must not contain a query or fragment")
        return str(url).rstrip("/")

    @model_validator(mode="after")
    def _validate_run_limit(self) -> GitHubSourceConfig:
        if self.max_run_bytes < self.max_asset_bytes:
            raise ValueError("max_run_bytes must be greater than or equal to max_asset_bytes")
        return self


class CoworkerSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str
    source_id: str
    source_name: str
    token: str = ""
    include_prereleases: bool = False
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    max_asset_bytes: int = Field(default=2 * 1024 * 1024 * 1024, gt=0)
    max_run_bytes: int = Field(default=8 * 1024 * 1024 * 1024, gt=0)
    max_redirects: int = Field(default=5, ge=0, le=20)

    @field_validator("base_url")
    @classmethod
    def _normalize_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            raise ValueError("base_url must not be empty")
        try:
            url = httpx.URL(value)
        except Exception as error:
            raise ValueError("base_url is not a valid URL") from error
        if url.scheme not in {"http", "https"} or not url.host:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if url.userinfo:
            raise ValueError("base_url must not contain credentials")
        if url.query or url.fragment:
            raise ValueError("base_url must not contain a query or fragment")
        return str(url).rstrip("/")

    @model_validator(mode="after")
    def _validate_run_limit(self) -> CoworkerSourceConfig:
        if self.max_run_bytes < self.max_asset_bytes:
            raise ValueError("max_run_bytes must be greater than or equal to max_asset_bytes")
        return self


class GitHubAssetPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    asset_id: int = Field(alias="id", gt=0)
    name: str
    size: int = Field(ge=0)
    # GitHub's Releases API may expose a digest without publishing a
    # SHA256SUMS.txt asset.  Keep the provider value so the downloader can
    # verify the bytes before committing them.
    digest: str | None = None


class GitHubReleasePayload(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    release_id: int = Field(alias="id", gt=0)
    tag_name: str
    name: str | None = None
    body: str | None = None
    draft: bool = False
    prerelease: bool = False
    published_at: datetime | None = None
    created_at: datetime | None = None
    html_url: str = ""
    assets: list[GitHubAssetPayload] = Field(default_factory=list)


class SourceAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    source_id: str = Field(alias="asset_id", min_length=1)
    name: str
    size: int = Field(ge=0)
    digest: str | None = None

    @field_validator("source_id", mode="before")
    @classmethod
    def _stringify_source_id(cls, value: object) -> str:
        return str(value)

    @property
    def asset_id(self) -> str | int:
        with suppress(ValueError):
            return int(self.source_id)
        return self.source_id


class SourceRelease(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        arbitrary_types_allowed=True,
        populate_by_name=True,
    )

    source_id: str = Field(alias="release_id", min_length=1)
    version: SemVer
    tag_name: str
    name: str = ""
    notes: str = ""
    draft: bool = False
    prerelease: bool = False
    published_at: datetime | None = None
    html_url: str = ""
    assets: dict[str, SourceAsset]

    @field_validator("source_id", mode="before")
    @classmethod
    def _stringify_source_id(cls, value: object) -> str:
        return str(value)

    @property
    def release_id(self) -> str | int:
        with suppress(ValueError):
            return int(self.source_id)
        return self.source_id

    @field_validator("version", mode="before")
    @classmethod
    def _parse_version(cls, value: object) -> SemVer:
        if isinstance(value, SemVer):
            return value
        if isinstance(value, str):
            try:
                return SemVer.parse(value)
            except SemVerError as error:
                raise ValueError(str(error)) from error
        raise ValueError("version must be a semantic version")


class RateLimitInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    limit: int | None = None
    remaining: int | None = None
    reset_at: datetime | None = None
    retry_after_seconds: float | None = None


class ReleasePage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    releases: tuple[SourceRelease, ...] = ()
    etag: str | None = None
    not_modified: bool = False
    rate_limit: RateLimitInfo = Field(default_factory=RateLimitInfo)
    skipped: tuple[str, ...] = ()


class SyncSourceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    name: str
    provider: str
    endpoint: str
    target: str = ""
    options: dict[str, bool | int | str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_github_summary(cls, value: object) -> object:
        if not isinstance(value, dict) or "api_base_url" not in value:
            return value
        options: dict[str, bool | int | str] = {
            "include_drafts": bool(value.get("include_drafts", False)),
            "include_prereleases": bool(value.get("include_prereleases", False)),
        }
        return {
            "source_id": str(value.get("source_id") or "github"),
            "name": str(value.get("name") or "GitHub Releases"),
            "provider": str(value.get("provider") or "github"),
            "endpoint": str(value.get("api_base_url") or value.get("endpoint") or ""),
            "target": str(value.get("repository") or value.get("target") or ""),
            "options": options,
        }

    @property
    def api_base_url(self) -> str:
        return self.endpoint

    @property
    def repository(self) -> str:
        return self.target

    @property
    def include_drafts(self) -> bool:
        return bool(self.options.get("include_drafts", False))

    @property
    def include_prereleases(self) -> bool:
        return bool(self.options.get("include_prereleases", False))


SyncOutcome = Literal[
    "idle",
    "running",
    "succeeded",
    "not_modified",
    "no_updates",
    "conflict",
    "rate_limited",
    "failed",
    "interrupted",
]


class SyncStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    ready: bool = True
    readiness: Literal["disabled", "unconfigured", "ready", "reconfiguring", "config_error"] = "ready"
    source: SyncSourceSummary | None = None
    outcome: SyncOutcome = "idle"
    etag: str | None = None
    run_id: str | None = None
    trigger: Literal["manual", "scheduled"] | None = None
    phase: str = "idle"
    version: str | None = None
    asset: str | None = None
    bytes_downloaded: int = 0
    bytes_total: int = 0
    requested_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    next_run_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str = ""
    checked_releases: int = 0
    imported_versions: list[str] = Field(default_factory=list)
    skipped_releases: list[str] = Field(default_factory=list)
    rate_limit: RateLimitInfo = Field(default_factory=RateLimitInfo)


class SyncResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    outcome: SyncOutcome
    checked_releases: int = 0
    imported_versions: tuple[str, ...] = ()
    skipped_releases: tuple[str, ...] = ()
    error: str = ""


class SyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    coalesced: bool


class DownloadProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    phase: str
    version: str
    asset: str | None = None
    bytes_downloaded: int = 0
    bytes_total: int = 0
