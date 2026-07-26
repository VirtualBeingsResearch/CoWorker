from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from functools import cmp_to_key
from typing import TypedDict

from coworker.core.types import CommunicateRegistration

from .semver import SemVer, SemVerError

_DESKTOP_KIND = "coworker-desktop"


class DesktopVersionCount(TypedDict):
    version: str | None
    desktops: int
    active_desktops: int
    outdated: bool | None


class DesktopVersionStatistics(TypedDict):
    latest_version: str | None
    total_desktops: int
    active_desktops: int
    outdated_desktops: int
    unknown_version_desktops: int
    versions: list[DesktopVersionCount]


class _DesktopInstallation(TypedDict):
    version: str | None
    active: bool


def build_desktop_version_statistics(
    registrations: Iterable[CommunicateRegistration],
    live_participant_ids: set[str],
    latest_version: str | None,
) -> DesktopVersionStatistics:
    installations = _desktop_installations(registrations, live_participant_ids)
    version_counts = _version_counts(installations, latest_version)
    return {
        "latest_version": latest_version,
        "total_desktops": len(installations),
        "active_desktops": sum(item["active"] for item in installations),
        "outdated_desktops": sum(
            item["desktops"] for item in version_counts if item["outdated"] is True
        ),
        "unknown_version_desktops": sum(
            item["desktops"] for item in version_counts if item["version"] is None
        ),
        "versions": version_counts,
    }


def _desktop_installations(
    registrations: Iterable[CommunicateRegistration],
    live_participant_ids: set[str],
) -> list[_DesktopInstallation]:
    registrations_by_desktop: dict[str, list[CommunicateRegistration]] = defaultdict(list)
    for registration in registrations:
        if registration.kind != _DESKTOP_KIND:
            continue
        desktop_id = _desktop_id(registration)
        if desktop_id:
            registrations_by_desktop[desktop_id].append(registration)

    installations: list[_DesktopInstallation] = []
    for desktop_registrations in registrations_by_desktop.values():
        active_registrations = [
            registration
            for registration in desktop_registrations
            if registration.participant_id in live_participant_ids
        ]
        representative = max(
            active_registrations or desktop_registrations,
            key=lambda registration: registration.last_registered_at,
        )
        version = str(representative.metadata.get("desktop_version") or "").strip()
        installations.append(
            {
                "version": version or None,
                "active": bool(active_registrations),
            }
        )
    return installations


def _desktop_id(registration: CommunicateRegistration) -> str:
    metadata_id = str(registration.metadata.get("desktop_id") or "").strip()
    if metadata_id:
        return metadata_id
    return registration.client_id.partition(":")[0].strip()


def _version_counts(
    installations: list[_DesktopInstallation],
    latest_version: str | None,
) -> list[DesktopVersionCount]:
    counts: dict[str | None, DesktopVersionCount] = {}
    for installation in installations:
        version = installation["version"]
        count = counts.setdefault(
            version,
            {
                "version": version,
                "desktops": 0,
                "active_desktops": 0,
                "outdated": _is_outdated(version, latest_version),
            },
        )
        count["desktops"] += 1
        count["active_desktops"] += int(installation["active"])
    return sorted(
        counts.values(),
        key=cmp_to_key(_compare_version_counts),
    )


def _compare_version_counts(
    left: DesktopVersionCount,
    right: DesktopVersionCount,
) -> int:
    return _compare_versions(left["version"], right["version"])


def _is_outdated(version: str | None, latest_version: str | None) -> bool | None:
    if version is None or latest_version is None:
        return None
    try:
        return SemVer.parse(version) < SemVer.parse(latest_version)
    except SemVerError:
        return None


def _compare_versions(left: str | None, right: str | None) -> int:
    if left is None:
        return 0 if right is None else 1
    if right is None:
        return -1
    try:
        left_semver = SemVer.parse(left)
    except SemVerError:
        left_semver = None
    try:
        right_semver = SemVer.parse(right)
    except SemVerError:
        right_semver = None
    if left_semver is not None and right_semver is not None:
        return (right_semver > left_semver) - (right_semver < left_semver)
    if left_semver is not None:
        return -1
    if right_semver is not None:
        return 1
    return (right > left) - (right < left)
