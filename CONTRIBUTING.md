# Contributing to Coworker

[中文](CONTRIBUTING.zh-CN.md) · English

Thanks for contributing. Keep changes focused, explain the user-visible outcome, and add the
smallest test that would fail without the change.

For security issues, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.

## Development setup

Requirements:

- Python 3.13+
- [uv](https://docs.astral.sh/uv/)
- Node.js 20+ for web or desktop changes
- Stable Rust for bridge or Tauri changes

Install the Python workspace and development dependencies:

```bash
uv sync --dev
```

Install Chromium once if you work on the browser tool or run browser integration tests:

```bash
uv run playwright install chromium
```

On Debian or Ubuntu, use `uv run playwright install --with-deps chromium` when the required system
libraries are not already installed.

Install only the frontend dependencies needed for your change:

```bash
npm ci --prefix web
npm ci --prefix apps/explore-lab/frontend
npm ci --prefix apps/coworker-desktop/desktop
```

Never commit `.env`, `providers.json`, credentials, logs, exported configuration, or files under
runtime data directories. Use the checked-in `*.example` files for shareable configuration.

## Checks

Run the checks relevant to the files you changed. Pull requests run all of these in CI.

```bash
# Python
uv run --frozen python scripts/check_version.py
uv run --frozen ruff check src tests scripts apps/explore-lab/backend
uv run --frozen mypy src
uv run --frozen pytest
uv run --project apps/explore-lab/backend --frozen pytest apps/explore-lab/backend/tests

# Rust
cargo fmt --all -- --check
cargo test --workspace --locked

# Web applications
npm --prefix web run build
git status --short -- src/coworker/web
npm --prefix apps/explore-lab/frontend run build
npm --prefix apps/coworker-desktop/desktop test
npm --prefix apps/coworker-desktop/desktop run build
```

## Pull requests

- Keep one logical change per pull request.
- Add or update tests for behavior changes.
- Update README or examples when commands, configuration, or public behavior change.
- Paired pages under `docs/` use `<name>.md` for Chinese and `<name>.en.md` for English. Update
  both versions together, keeping commands, configuration names, and product terms consistent.
- Add a concise entry to `CHANGELOG.md` for user-visible changes.
- Call out migrations, compatibility breaks, security implications, and checks you could not run.

By contributing, you agree that your contribution is licensed under the repository's MIT License.
