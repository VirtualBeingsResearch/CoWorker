from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from scripts.prepare_desktop_release import ReleaseAssetError, prepare_release_assets


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _artifact_tree(root: Path) -> dict[str, bytes]:
    files = {
        "coworker-desktop-windows/target/nsis/CoWorker Desktop_0.3.2_x64-setup.exe": b"exe",
        "coworker-desktop-windows/target/nsis/CoWorker Desktop_0.3.2_x64-setup.exe.sig": b"exe-signature",
        "coworker-desktop-macos-arm64/target/dmg/CoWorker Desktop_0.3.2_aarch64.dmg": b"arm-dmg",
        "coworker-desktop-macos-arm64/target/macos/CoWorker Desktop.app.tar.gz": b"arm-updater",
        "coworker-desktop-macos-arm64/target/macos/CoWorker Desktop.app.tar.gz.sig": b"arm-signature",
        "coworker-desktop-macos-x86_64/target/dmg/CoWorker Desktop_0.3.2_x64.dmg": b"x64-dmg",
        "coworker-desktop-macos-x86_64/target/macos/CoWorker Desktop.app.tar.gz": b"x64-updater",
        "coworker-desktop-macos-x86_64/target/macos/CoWorker Desktop.app.tar.gz.sig": b"x64-signature",
        "coworker-desktop-linux/target/appimage/CoWorker Desktop_0.3.2_amd64.AppImage": b"appimage",
        "coworker-desktop-linux/target/appimage/CoWorker Desktop_0.3.2_amd64.AppImage.sig": b"appimage-signature",
        "coworker-desktop-linux/target/deb/CoWorker Desktop_0.3.2_amd64.deb": b"deb",
    }
    for relative, content in files.items():
        _write(root / relative, content)
    return files


def test_prepare_release_assets_normalizes_names_and_writes_checksums(tmp_path) -> None:
    artifacts_dir = tmp_path / "artifacts"
    source_files = _artifact_tree(artifacts_dir)
    output_dir = tmp_path / "release"

    staged = prepare_release_assets("0.3.2", artifacts_dir, output_dir)

    expected_names = {
        "CoWorker.Desktop_0.3.2_x64-setup.exe",
        "CoWorker.Desktop_0.3.2_x64-setup.exe.sig",
        "CoWorker.Desktop_0.3.2_aarch64.dmg",
        "CoWorker.Desktop_0.3.2_aarch64.app.tar.gz",
        "CoWorker.Desktop_0.3.2_aarch64.app.tar.gz.sig",
        "CoWorker.Desktop_0.3.2_x64.dmg",
        "CoWorker.Desktop_0.3.2_x64.app.tar.gz",
        "CoWorker.Desktop_0.3.2_x64.app.tar.gz.sig",
        "CoWorker.Desktop_0.3.2_amd64.AppImage",
        "CoWorker.Desktop_0.3.2_amd64.AppImage.sig",
        "CoWorker.Desktop_0.3.2_amd64.deb",
        "SHA256SUMS.txt",
    }
    assert {path.name for path in staged} == expected_names
    assert len(source_files) == 11

    checksum_lines = (output_dir / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()
    checksum_names = [line.split("  ", 1)[1] for line in checksum_lines]
    assert checksum_names == sorted(expected_names - {"SHA256SUMS.txt"})
    for line in checksum_lines:
        digest, name = line.split("  ", 1)
        assert digest == hashlib.sha256((output_dir / name).read_bytes()).hexdigest()


def test_prepare_release_assets_rejects_a_missing_signature(tmp_path) -> None:
    artifacts_dir = tmp_path / "artifacts"
    _artifact_tree(artifacts_dir)
    missing = next((artifacts_dir / "coworker-desktop-linux").rglob("*.AppImage.sig"))
    missing.unlink()

    with pytest.raises(ReleaseAssetError, match="expected exactly one .AppImage.sig"):
        prepare_release_assets("0.3.2", artifacts_dir, tmp_path / "release")


def test_prepare_release_assets_rejects_duplicate_candidates(tmp_path) -> None:
    artifacts_dir = tmp_path / "artifacts"
    _artifact_tree(artifacts_dir)
    _write(
        artifacts_dir / "coworker-desktop-windows/alternate/duplicate.exe",
        b"duplicate",
    )

    with pytest.raises(ReleaseAssetError, match="expected exactly one .exe file"):
        prepare_release_assets("0.3.2", artifacts_dir, tmp_path / "release")


def test_prepare_release_assets_rejects_an_empty_asset(tmp_path) -> None:
    artifacts_dir = tmp_path / "artifacts"
    _artifact_tree(artifacts_dir)
    empty_asset = next((artifacts_dir / "coworker-desktop-linux").rglob("*.deb"))
    empty_asset.write_bytes(b"")

    with pytest.raises(ReleaseAssetError, match="release asset is empty"):
        prepare_release_assets("0.3.2", artifacts_dir, tmp_path / "release")


@pytest.mark.parametrize("version", ["v0.3.2", "0.3.2+build.1", "0.3"])
def test_prepare_release_assets_rejects_noncanonical_versions(tmp_path, version: str) -> None:
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()

    with pytest.raises(ReleaseAssetError, match="version must match"):
        prepare_release_assets(version, artifacts_dir, tmp_path / "release")
