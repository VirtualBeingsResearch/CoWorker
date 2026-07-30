# Platform Support and Component Compatibility

[中文](platform-support.md) · English

[← Back to First Run](README.en.md)

This page describes platforms covered by current source and build workflows. It does not imply
that every platform has an official prebuilt package. Use `VERSION`, manifests, and release notes
as the version authority.

## Runtime requirements

| Component | Minimum development requirement | Platform notes |
|---|---|---|
| Coworker Python service | Python 3.13+ and uv | macOS Apple Silicon, Windows, Linux; use Dev Container or Docker on Intel macOS |
| browser tool | Playwright Chromium | Debian/Ubuntu may require system libraries through `--with-deps` |
| Coworker Desktop | Node.js 20+ and stable Rust to build | Windows NSIS, macOS dmg, Linux AppImage/deb targets |
| Explore Lab | Python workspace and Node.js | Local development tool, binding `127.0.0.1:8100` by default |
| Relay | Go 1.26.5+ to build, or Docker | v1 is single-node; do not share a bbolt volume |

Desktop does not bundle the Python service, Codex CLI, or Claude Code CLI. Each is health-checked
independently. Missing Codex or Claude does not block the local user or other available actors.

## CPU and models

The repository uses the PyTorch CPU index on every platform by default. NVIDIA CUDA 13.0 on
Windows/Linux requires switching the `torch` source as described in `pyproject.toml`, then
regenerating the lock and syncing dependencies. Do not copy a virtual environment or local wheel
cache between architectures.

The Docker offline image preloads an embedding model; runtime configuration must match the cache.
Conversation models normally use external Providers and do not become offline because the image is.

## Desktop artifacts

| System | Typical installer | Automatic-update artifact |
|---|---|---|
| Windows | NSIS installer | Tauri updater plus signature |
| macOS Apple Silicon | `.dmg` | `.app.tar.gz` plus `.sig` |
| macOS Intel | x86_64 `.dmg` | x86_64 `.app.tar.gz` plus `.sig` |
| Linux | AppImage / deb | Matching updater plus signature |

Each platform normally builds on a matching runner. Desktop must reject a missing or invalid
signature and retain the installed version.

## Protocol compatibility

- Desktop registration and message envelopes currently use protocol version `1`.
- Relay v1 requires compatible Coworker, Desktop, and Relay protocol implementations.
- API v0.x responses may add fields; clients should ignore unknown fields.
- `/api/admin/*` is an implementation contract for the matching Web console, not a stable SDK.
- Older releases do not receive security fixes by default; see the [Security Policy](../../SECURITY.en.md).

Read [Upgrading and Migration](../operations/upgrading.en.md) and release notes before combining versions.

[← Back to project home](../../README.en.md)
