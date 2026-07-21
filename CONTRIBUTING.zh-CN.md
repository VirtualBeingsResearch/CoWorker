# 参与 Coworker 贡献

中文 · [English](CONTRIBUTING.md)

感谢参与贡献。请让改动保持聚焦，说明用户可见的结果，并添加一个在没有该改动时会失败的最小测试。

安全问题请按 [安全策略](SECURITY.zh-CN.md) 私下报告，不要提交公开 issue。

## 开发环境

要求：

- Python 3.13+
- [uv](https://docs.astral.sh/uv/)
- 修改 Web 或桌面端时需要 Node.js 20+
- 修改 Bridge 或 Tauri 时需要稳定版 Rust

安装 Python workspace 和开发依赖：

```bash
uv sync --dev
```

如果要修改浏览器工具或运行浏览器集成测试，需要安装一次 Chromium：

```bash
uv run playwright install chromium
```

Debian 或 Ubuntu 缺少所需系统库时，使用 `uv run playwright install --with-deps chromium`。

只安装本次改动所需的前端依赖：

```bash
npm ci --prefix web
npm ci --prefix apps/explore-lab/frontend
npm ci --prefix apps/coworker-desktop/desktop
```

不要提交 `.env`、`providers.json`、凭据、日志、导出的配置或运行时数据目录中的文件。可共享配置应使用仓库内的 `*.example` 文件。

## 检查

运行与改动文件相关的检查。Pull request 会在 CI 中运行全部检查。

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

# Web 应用
npm --prefix web run build
git status --short -- src/coworker/web
npm --prefix apps/explore-lab/frontend run build
npm --prefix apps/coworker-desktop/desktop test
npm --prefix apps/coworker-desktop/desktop run build
```

## Pull request

- 每个 pull request 只包含一个逻辑改动。
- 行为变更需要新增或更新测试。
- 命令、配置或公开行为发生变化时，更新 README 或示例。
- `docs/` 中的成对文档使用 `<name>.md` 表示中文、`<name>.en.md` 表示英文。修改时同时更新两个版本，并保持命令、配置名和产品术语一致。
- 用户可见的变更需要在 `CHANGELOG.md` 中添加简短记录。
- 明确说明迁移、兼容性破坏、安全影响以及未能运行的检查。

提交贡献即表示你同意按仓库的 MIT License 授权该贡献。
