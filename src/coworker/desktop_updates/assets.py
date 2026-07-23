from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .semver import SemVer

PRODUCT_NAME = "CoWorker.Desktop"
CHECKSUM_FILENAME = "SHA256SUMS.txt"
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class AssetValidationError(ValueError):
    """Raised when a release does not match the canonical desktop asset set."""


@dataclass(frozen=True, slots=True)
class CanonicalAssetSpec:
    suffix: str
    platform: str
    kind: str
    signature_suffix: str | None = None

    def filename(self, version: SemVer | str) -> str:
        normalized = str(version if isinstance(version, SemVer) else SemVer.parse(version))
        return f"{PRODUCT_NAME}_{normalized}{self.suffix}"

    def signature_filename(self, version: SemVer | str) -> str | None:
        if self.signature_suffix is None:
            return None
        normalized = str(version if isinstance(version, SemVer) else SemVer.parse(version))
        return f"{PRODUCT_NAME}_{normalized}{self.signature_suffix}"


CANONICAL_ASSET_SPECS = (
    CanonicalAssetSpec(
        "_x64-setup.exe",
        "windows-x86_64",
        "updater",
        "_x64-setup.exe.sig",
    ),
    CanonicalAssetSpec("_aarch64.dmg", "darwin-aarch64", "installer"),
    CanonicalAssetSpec(
        "_aarch64.app.tar.gz",
        "darwin-aarch64",
        "updater",
        "_aarch64.app.tar.gz.sig",
    ),
    CanonicalAssetSpec("_x64.dmg", "darwin-x86_64", "installer"),
    CanonicalAssetSpec(
        "_x64.app.tar.gz",
        "darwin-x86_64",
        "updater",
        "_x64.app.tar.gz.sig",
    ),
    CanonicalAssetSpec(
        "_amd64.AppImage",
        "linux-x86_64",
        "updater",
        "_amd64.AppImage.sig",
    ),
    CanonicalAssetSpec("_amd64.deb", "linux-x86_64", "installer"),
)


def canonical_asset_names(version: SemVer | str) -> frozenset[str]:
    names = {CHECKSUM_FILENAME}
    for spec in CANONICAL_ASSET_SPECS:
        names.add(spec.filename(version))
        signature = spec.signature_filename(version)
        if signature is not None:
            names.add(signature)
    return frozenset(names)


def validate_canonical_asset_names(
    version: SemVer | str,
    names: Mapping[str, object] | set[str] | frozenset[str],
) -> None:
    actual = set(names)
    expected = set(canonical_asset_names(version))
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing or unexpected or len(actual) != 12:
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected: {', '.join(unexpected)}")
        if not details:
            details.append(f"expected 12 unique assets, found {len(actual)}")
        raise AssetValidationError("canonical desktop asset set mismatch (" + "; ".join(details) + ")")


def parse_sha256sums(
    content: str,
    version: SemVer | str,
    names: set[str] | frozenset[str] | None = None,
) -> dict[str, str]:
    expected = set(names) if names is not None else set(canonical_asset_names(version)) - {CHECKSUM_FILENAME}
    checksums: dict[str, str] = {}
    if not content.endswith("\n"):
        raise AssetValidationError("SHA256SUMS.txt must end with a newline")
    for line_number, line in enumerate(content.splitlines(), start=1):
        parts = line.split("  ", 1)
        if len(parts) != 2:
            raise AssetValidationError(
                f"invalid SHA256SUMS.txt line {line_number}: expected two-space separator"
            )
        digest, filename = parts
        if not _SHA256_RE.fullmatch(digest):
            raise AssetValidationError(f"invalid SHA-256 digest on line {line_number}")
        if filename != Path(filename).name or "/" in filename or "\\" in filename:
            raise AssetValidationError(f"unsafe checksum filename on line {line_number}")
        if filename in checksums:
            raise AssetValidationError(f"duplicate checksum entry: {filename}")
        checksums[filename] = digest.lower()
    if set(checksums) != expected:
        missing = sorted(expected - set(checksums))
        unexpected = sorted(set(checksums) - expected)
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected: {', '.join(unexpected)}")
        raise AssetValidationError("checksum manifest mismatch (" + "; ".join(details) + ")")
    return checksums
