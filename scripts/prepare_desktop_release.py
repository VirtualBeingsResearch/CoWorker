from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

VERSION_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$"
)
PRODUCT_NAME = "CoWorker.Desktop"


class ReleaseAssetError(ValueError):
    pass


@dataclass(frozen=True)
class AssetSpec:
    artifact: str
    suffix: str
    output_suffix: str


ASSET_SPECS = (
    AssetSpec("coworker-desktop-windows", ".exe", "_x64-setup.exe"),
    AssetSpec("coworker-desktop-windows", ".exe.sig", "_x64-setup.exe.sig"),
    AssetSpec("coworker-desktop-macos-arm64", ".dmg", "_aarch64.dmg"),
    AssetSpec("coworker-desktop-macos-arm64", ".tar.gz", "_aarch64.app.tar.gz"),
    AssetSpec("coworker-desktop-macos-arm64", ".tar.gz.sig", "_aarch64.app.tar.gz.sig"),
    AssetSpec("coworker-desktop-macos-x86_64", ".dmg", "_x64.dmg"),
    AssetSpec("coworker-desktop-macos-x86_64", ".tar.gz", "_x64.app.tar.gz"),
    AssetSpec("coworker-desktop-macos-x86_64", ".tar.gz.sig", "_x64.app.tar.gz.sig"),
    AssetSpec("coworker-desktop-linux", ".AppImage", "_amd64.AppImage"),
    AssetSpec("coworker-desktop-linux", ".AppImage.sig", "_amd64.AppImage.sig"),
    AssetSpec("coworker-desktop-linux", ".deb", "_amd64.deb"),
)


def _matches_suffix(path: Path, suffix: str) -> bool:
    name = path.name
    return name.endswith(suffix) and not name.endswith(f"{suffix}.sig")


def _find_asset(artifacts_dir: Path, spec: AssetSpec) -> Path:
    artifact_dir = artifacts_dir / spec.artifact
    if not artifact_dir.is_dir():
        raise ReleaseAssetError(f"missing artifact directory: {spec.artifact}")
    matches = sorted(
        path
        for path in artifact_dir.rglob("*")
        if path.is_file() and not path.is_symlink() and _matches_suffix(path, spec.suffix)
    )
    if len(matches) != 1:
        paths = ", ".join(str(path.relative_to(artifacts_dir)) for path in matches) or "none"
        raise ReleaseAssetError(
            f"expected exactly one {spec.suffix} file in {spec.artifact}, found: {paths}"
        )
    if matches[0].stat().st_size == 0:
        raise ReleaseAssetError(
            f"release asset is empty: {matches[0].relative_to(artifacts_dir)}"
        )
    return matches[0]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_release_assets(version: str, artifacts_dir: Path, output_dir: Path) -> list[Path]:
    if not VERSION_RE.fullmatch(version):
        raise ReleaseAssetError(
            "version must match X.Y.Z or X.Y.Z-prerelease without build metadata"
        )
    if not artifacts_dir.is_dir():
        raise ReleaseAssetError(f"artifacts directory does not exist: {artifacts_dir}")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ReleaseAssetError(f"output directory must be empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    staged: list[Path] = []
    for spec in ASSET_SPECS:
        source = _find_asset(artifacts_dir, spec)
        destination = output_dir / f"{PRODUCT_NAME}_{version}{spec.output_suffix}"
        shutil.copyfile(source, destination)
        staged.append(destination)

    checksum_path = output_dir / "SHA256SUMS.txt"
    checksum_lines = [f"{_sha256(path)}  {path.name}" for path in sorted(staged)]
    checksum_path.write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")
    staged.append(checksum_path)
    return staged


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare canonical CoWorker Desktop release assets")
    parser.add_argument("--version", required=True)
    parser.add_argument("--artifacts-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        staged = prepare_release_assets(args.version, args.artifacts_dir, args.output_dir)
    except ReleaseAssetError as error:
        print(error, file=sys.stderr)
        return 1
    for path in staged:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
