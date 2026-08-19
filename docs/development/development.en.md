# Development Guide

[中文](development.md) · English

[← Back to Development and Collaboration](README.en.md)

Read the [contributing guide](../../CONTRIBUTING.en.md) before submitting code. Report security issues privately according to the [security policy](../../SECURITY.en.md).

```bash
# Install development dependencies
uv sync --dev
# When using the default mem0 memory backend, also install its optional deps: uv sync --dev --extra mem0

# Install Chromium for the browser tool (once)
uv run playwright install chromium

# Lint
uv run ruff check src tests

# Type-check
uv run mypy src

# Run unit tests
uv run pytest
```

The web frontend requires Node.js 22.12+ (Vite requirement); desktop tests require Node.js
^24.15 or ≥26 because of jsdom. Repository CI and the Dev Container use Node.js 24.
The administration interface build output is written to `src/coworker/web/`, which is shipped as static assets in the Python package:

```bash
npm ci --prefix web
npm --prefix web run build
git status --short -- src/coworker/web
```

On Debian or Ubuntu, use `uv run playwright install --with-deps chromium` if the required browser system libraries are missing.

### Dev Container

The current PyTorch release no longer provides a `macosx_x86_64` wheel for Intel macOS. The
checked-in [`.devcontainer`](../../.devcontainer/devcontainer.json) configuration runs the
development environment in Linux, so an Intel Mac uses PyTorch's `linux/x86_64` CPU wheel.
Apple Silicon uses native `linux/arm64` without forced x86 emulation.

Install Docker Desktop (or a compatible container runtime) and the VS Code Dev Containers
extension, then run **Dev Containers: Reopen in Container** from the repository. The first build:

- installs Python 3.14, uv, Node.js 24, and FFmpeg;
- installs the locked Python development dependencies and Linux CPU build of PyTorch;
- installs Playwright Chromium and its Linux system libraries;
- the Dev Container configures no port forwarding by default; to open it from the host browser, forward `8000` (CoWorker API) manually in the VS Code Ports view — VS Code may also auto-forward ports it detects listening in the container.

The source checkout remains bind-mounted from the host, while the container's Python environment
lives at `/opt/venv`. After creation, run the `uv run ...`, `npm ...`, and test commands from this
guide directly. Run **Dev Containers: Rebuild Container** after dependency or lockfile changes to
refresh the cached layers.

The Dev Container is a Linux environment. It supports Python and web development,
but cannot build or validate macOS-specific Tauri `.app`/`.dmg` artifacts, signing, or
notarization. Continue to perform those tasks on macOS or a matching CI runner.

### Develop with the offline image

To run and debug only the Coworker service, reuse the published strict-offline image and mount the
current checkout directly at `/app`:

```bash
COWORKER_IMAGE=ghcr.io/virtualbeingsresearch/coworker:offline \
docker compose up --pull always --no-build
```

`/app` is both the source directory Python actually loads and the Agent workspace, so the host,
Agent, and running process see the same Git checkout. The image supplies the Linux Python
environment, Chromium, FFmpeg, a preloaded embedding model, and lightweight command-line tools
including `vim-tiny`, `nano`, `less`, `jq`, and `ripgrep`. Restart the container after source
changes. If `pyproject.toml` or `uv.lock` changes, rebuild the dependency environment with
`docker compose up --build`.

To clean runtime caches and data, see:

```bash
uv run python scripts/cleanup.py
```

[← Back to project home](../../README.en.md)
