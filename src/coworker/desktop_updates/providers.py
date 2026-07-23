from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import httpx

from coworker.core.config import (
    CoworkerDesktopUpdateSource,
    DesktopUpdatesConfig,
    DesktopUpdateSourceSpec,
    GitHubDesktopUpdateSource,
)

from .coworker import CoworkerReleaseSource
from .github import GitHubReleaseSource
from .models import CoworkerSourceConfig, GitHubSourceConfig, SyncSourceSummary
from .sync import ReleaseSource, SyncRuntimeSpec

ProviderType = Literal["github", "coworker"]


@dataclass(frozen=True)
class ProviderMetadata:
    type: ProviderType
    label: str
    token_secret_field: str = "token"


PROVIDERS: dict[str, ProviderMetadata] = {
    "github": ProviderMetadata(type="github", label="GitHub Releases"),
    "coworker": ProviderMetadata(type="coworker", label="Coworker Feed"),
}


def provider_metadata() -> list[dict[str, str]]:
    return [
        {"type": provider.type, "label": provider.label, "token_secret_field": provider.token_secret_field}
        for provider in PROVIDERS.values()
    ]


def source_summary(source: DesktopUpdateSourceSpec) -> SyncSourceSummary:
    if isinstance(source, GitHubDesktopUpdateSource):
        return SyncSourceSummary(
            source_id=str(source.id),
            name=source.name,
            provider="github",
            endpoint=source.api_base_url,
            target=source.repository,
            options={
                "include_drafts": source.include_drafts,
                "include_prereleases": source.include_prereleases,
            },
        )
    if isinstance(source, CoworkerDesktopUpdateSource):
        return SyncSourceSummary(
            source_id=str(source.id),
            name=source.name,
            provider="coworker",
            endpoint=source.base_url,
            target=source.base_url,
            options={
                "include_prereleases": source.include_prereleases,
            },
        )
    raise ValueError(f"unsupported desktop update source type: {source!r}")


def is_source_configured(source: DesktopUpdateSourceSpec) -> bool:
    if isinstance(source, GitHubDesktopUpdateSource):
        return bool(source.repository and source.api_base_url)
    if isinstance(source, CoworkerDesktopUpdateSource):
        return bool(source.base_url)
    return False


def runtime_key(source: DesktopUpdateSourceSpec, config: DesktopUpdatesConfig) -> tuple[object, ...]:
    common = (
        str(source.id),
        source.type,
        source.include_prereleases,
        config.sync_max_asset_bytes,
        config.sync_max_run_bytes,
    )
    if isinstance(source, GitHubDesktopUpdateSource):
        return common + (source.api_base_url, source.repository, source.include_drafts)
    if isinstance(source, CoworkerDesktopUpdateSource):
        return common + (source.base_url,)
    raise ValueError(f"unsupported desktop update source type: {source!r}")


def build_runtime_spec(
    config: DesktopUpdatesConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SyncRuntimeSpec:
    active = config.active_source()
    if active is None:
        return SyncRuntimeSpec(
            source=None,
            source_summary=None,
            runtime_key=None,
            token="",
            interval_seconds=config.sync_interval_seconds,
            enabled=False,
            ready=False,
            readiness="disabled",
        )
    summary = source_summary(active)
    if not is_source_configured(active):
        return SyncRuntimeSpec(
            source=None,
            source_summary=summary,
            runtime_key=runtime_key(active, config),
            token=active.token,
            interval_seconds=config.sync_interval_seconds,
            enabled=False,
            ready=False,
            readiness="unconfigured",
        )
    return SyncRuntimeSpec(
        source=build_release_source(active, config, transport=transport),
        source_summary=summary,
        runtime_key=runtime_key(active, config),
        token=active.token,
        interval_seconds=config.sync_interval_seconds,
        enabled=True,
        ready=True,
        readiness="ready",
    )


def build_release_source(
    source: DesktopUpdateSourceSpec,
    config: DesktopUpdatesConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ReleaseSource:
    if isinstance(source, GitHubDesktopUpdateSource):
        return GitHubReleaseSource(
            GitHubSourceConfig(
                repository=source.repository,
                api_base_url=source.api_base_url,
                source_id=str(source.id),
                source_name=source.name,
                token=source.token,
                include_drafts=source.include_drafts,
                include_prereleases=source.include_prereleases,
                max_asset_bytes=config.sync_max_asset_bytes,
                max_run_bytes=config.sync_max_run_bytes,
            ),
            transport=transport,
        )
    if isinstance(source, CoworkerDesktopUpdateSource):
        return CoworkerReleaseSource(
            CoworkerSourceConfig(
                base_url=source.base_url,
                source_id=str(source.id),
                source_name=source.name,
                token=source.token,
                include_prereleases=source.include_prereleases,
                max_asset_bytes=config.sync_max_asset_bytes,
                max_run_bytes=config.sync_max_run_bytes,
            ),
            transport=transport,
        )
    raise ValueError(f"unsupported desktop update source type: {source!r}")
