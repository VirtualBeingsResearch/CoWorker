# Development Guide

[中文](development.md) · English

[← Back to Development and Collaboration](README.en.md)

Read the [contributing guide](../../CONTRIBUTING.md) before submitting code. Report security issues privately according to the [security policy](../../SECURITY.md).

```bash
# Install development dependencies
uv sync --dev

# Install Chromium for the browser tool (once)
uv run playwright install chromium

# Lint
uv run ruff check src tests

# Type-check
uv run mypy src

# Run unit tests
uv run pytest
```

Changes to the administration interface also require Node.js 20+. The build output is written to `src/coworker/web/`, which is shipped as static assets in the Python package:

```bash
npm ci --prefix web
npm --prefix web run build
git status --short -- src/coworker/web
```

On Debian or Ubuntu, use `uv run playwright install --with-deps chromium` if the required browser system libraries are missing.

### Explore Lab

The Explore Lab backend can serve the frontend build directly. Normal use requires starting only the backend after building the UI:

```bash
# 1. Install dependencies and build the frontend assets
npm ci --prefix apps/explore-lab/frontend
npm --prefix apps/explore-lab/frontend run build

# 2. Start the backend and serve the UI
uv run --project apps/explore-lab/backend python -m explore_lab
# Equivalent: uv run --project apps/explore-lab/backend explore-lab

# 3. Open
# http://127.0.0.1:8100/
```

The default build directory is `apps/explore-lab/frontend/dist`. To use another directory:

```bash
uv run --project apps/explore-lab/backend python -m explore_lab --ui-dir path/to/dist
```

To clean runtime caches and data, see:

```bash
uv run python scripts/cleanup.py
```

[← Back to project home](../../README.en.md)
